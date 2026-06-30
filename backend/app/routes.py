"""HTTP routes.

Grouped by capability:
- /v1/remediate                 upload (PDF or Office), async remediation
- /v1/convert/*                 format conversions (async)
- /v1/jobs/{id}*                shared job status/result/report/delete
- /v1/pdf/*                     (wired in Phase C–D)
- /v1/office/*                  (wired in Phase E)
- /v1/validate/*                (wired in Phase F)
- /v1/vision-plan/*             (wired in Phase G)
- /healthz                      liveness
- /readyz                       readiness
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter

from backend.app.auth import require_api_key_dependency
from backend.app.config import Settings
from backend.app.engine_service import (
    filetype_for_suffix,
    is_office,
    media_type_for,
)
from backend.app.jobs import (
    JOB_KIND_CONVERT_HTML_TO_EPUB,
    JOB_KIND_CONVERT_HTML_TO_PDF,
    JOB_KIND_CONVERT_OFFICE_TO_HTML,
    JOB_KIND_CONVERT_PDF_TO_HTML,
    JOB_KIND_REMEDIATE_OFFICE,
    JOB_KIND_REMEDIATE_PDF,
    JobStore,
    JobWorker,
    serialize_job,
)
from project_remedy.models import FileType


# Magic-byte prefixes we recognize.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"            # OOXML + other zip-based
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy .doc/.ppt/.xls


# File-type groups for each endpoint's accept list.
_PDF_ONLY = {FileType.PDF}
_OFFICE_ONLY = {FileType.DOCX, FileType.DOC, FileType.PPTX, FileType.PPT, FileType.XLSX, FileType.XLS}
_PDF_OR_OFFICE = _PDF_ONLY | _OFFICE_ONLY


def _validate_magic(contents: bytes, ft: FileType) -> bool:
    if ft == FileType.PDF:
        return contents.startswith(_PDF_MAGIC)
    if ft in (FileType.DOCX, FileType.PPTX, FileType.XLSX):
        return contents.startswith(_ZIP_MAGIC)
    if ft in (FileType.DOC, FileType.PPT, FileType.XLS):
        return contents.startswith(_OLE2_MAGIC)
    return False


async def _stage_upload(
    file: UploadFile,
    settings: Settings,
    allowed: set[FileType],
) -> tuple[Path, FileType, bytes]:
    """Read upload, validate, and stage it under settings.job_dir."""
    suffix = Path(file.filename or "").suffix.lower()
    ft = filetype_for_suffix(suffix)
    if ft is None or ft not in allowed:
        accepted = sorted({f.value for f in allowed})
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type. Accepted: {', '.join(accepted)}",
        )

    max_bytes = settings.max_upload_mb * 1024 * 1024
    contents = await file.read(max_bytes + 1)
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds max upload size ({settings.max_upload_mb} MB).",
        )

    if not _validate_magic(contents, ft):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Uploaded file is not a valid {ft.value.upper()} (magic-byte check failed).",
        )

    settings.job_dir.mkdir(parents=True, exist_ok=True)
    staging = settings.job_dir / f"_staging-{uuid.uuid4().hex}{suffix}"
    staging.write_bytes(contents)
    return staging, ft, contents


async def _finalize_upload_and_enqueue(
    store: JobStore,
    worker: JobWorker,
    settings: Settings,
    staging: Path,
    suffix: str,
    *,
    kind: str,
    result_media_type: str,
    metadata_json: str | None = None,
) -> dict:
    """Create the job record, move staging → final path, enqueue.

    When ``metadata_json`` is provided, it is persisted in the SAME
    ``store.update`` that sets ``input_path`` and ``stage='queued'`` —
    i.e. BEFORE ``worker.enqueue``. This closes a TOCTOU race where the
    worker (on the same event loop) could snapshot the job row with empty
    metadata_json between enqueue and a trailing post-enqueue update.
    """
    job = await store.create(staging, kind=kind, result_media_type=result_media_type)
    final_input = settings.job_dir / job.id / f"input{suffix}"
    final_input.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(final_input)

    update_kwargs: dict = {"input_path": str(final_input), "stage": "queued"}
    if metadata_json is not None:
        update_kwargs["metadata_json"] = metadata_json

    await store.update(job.id, **update_kwargs)
    await worker.enqueue(job.id)
    refreshed = await store.get(job.id)
    return serialize_job(refreshed)


def build_router(
    settings: Settings,
    store: JobStore,
    worker: JobWorker,
    limiter: Limiter,
    upload_rate_limit: str,
) -> APIRouter:
    router = APIRouter()
    require_key = Depends(require_api_key_dependency(settings))
    upload_limit = limiter.limit(upload_rate_limit)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @router.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness check for orchestrators and reverse proxies."""
        checks: dict[str, str] = {}

        try:
            await store.ping()
            checks["job_store"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["job_store"] = f"error:{type(exc).__name__}"

        probe = settings.job_dir / f".readyz-{uuid.uuid4().hex}"
        try:
            settings.job_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            checks["job_dir"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["job_dir"] = f"error:{type(exc).__name__}"
        finally:
            probe.unlink(missing_ok=True)

        checks["worker"] = "ok" if worker.is_running else "not_running"
        ok = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"ok": ok, "checks": checks},
        )

    # ------------------------------------------------------------------
    # /v1/remediate — multi-format upload
    # ------------------------------------------------------------------

    @router.post("/v1/remediate", dependencies=[require_key])
    @upload_limit
    async def remediate(
        request: Request,
        file: UploadFile = File(...),
        allow_semantic_rebuild: bool = Form(False),
        quality: bool = Query(
            False,
            description="Opt in to attaching quality-layer audit results to the remediation report.",
        ),
    ) -> JSONResponse:
        """Upload PDF or Office doc → async remediation job."""
        staging, ft, _ = await _stage_upload(file, settings, _PDF_OR_OFFICE)
        kind = JOB_KIND_REMEDIATE_PDF if ft == FileType.PDF else JOB_KIND_REMEDIATE_OFFICE
        # Persist the rebuild-tier flag in the SAME store.update() that
        # finalizes input_path/stage, BEFORE worker.enqueue. Writing it
        # after enqueue races with the worker's store.get() snapshot on
        # the shared event loop.
        metadata: dict[str, object] = {"allow_semantic_rebuild": allow_semantic_rebuild}
        if quality:
            metadata["quality"] = True
        body = await _finalize_upload_and_enqueue(
            store, worker, settings, staging, Path(file.filename or "").suffix,
            kind=kind, result_media_type=media_type_for(ft),
            metadata_json=json.dumps(metadata),
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    # ------------------------------------------------------------------
    # /v1/convert/* — format conversions
    # ------------------------------------------------------------------

    @router.post("/v1/convert/pdf-to-html", dependencies=[require_key])
    @upload_limit
    async def convert_pdf_to_html(
        request: Request,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        staging, _, _ = await _stage_upload(file, settings, _PDF_ONLY)
        body = await _finalize_upload_and_enqueue(
            store, worker, settings, staging, ".pdf",
            kind=JOB_KIND_CONVERT_PDF_TO_HTML, result_media_type="text/html",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    @router.post("/v1/convert/office-to-html", dependencies=[require_key])
    @upload_limit
    async def convert_office_to_html(
        request: Request,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        staging, ft, _ = await _stage_upload(file, settings, _OFFICE_ONLY)
        body = await _finalize_upload_and_enqueue(
            store, worker, settings, staging, Path(file.filename or "").suffix,
            kind=JOB_KIND_CONVERT_OFFICE_TO_HTML, result_media_type="text/html",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    @router.post("/v1/convert/html-to-pdf", dependencies=[require_key])
    @upload_limit
    async def convert_html_to_pdf(
        request: Request,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Upload an HTML file → async tagged-PDF conversion."""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".html", ".htm"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Only .html / .htm accepted.",
            )
        max_bytes = settings.max_upload_mb * 1024 * 1024
        contents = await file.read(max_bytes + 1)
        if len(contents) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds max upload size ({settings.max_upload_mb} MB).",
            )
        # Light sanity: must contain an "<html" tag.
        if b"<html" not in contents.lower():
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File does not appear to be HTML.",
            )

        settings.job_dir.mkdir(parents=True, exist_ok=True)
        staging = settings.job_dir / f"_staging-{uuid.uuid4().hex}.html"
        staging.write_bytes(contents)
        body = await _finalize_upload_and_enqueue(
            store, worker, settings, staging, ".html",
            kind=JOB_KIND_CONVERT_HTML_TO_PDF, result_media_type="application/pdf",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    @router.post("/v1/convert/html-to-epub", dependencies=[require_key])
    @upload_limit
    async def convert_html_to_epub(
        request: Request,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Upload an accessible HTML file → async EPUB Accessibility 1.1 conversion.

        Returns 202 Accepted with the job descriptor. Poll
        ``/v1/jobs/{job_id}`` for status; fetch the resulting EPUB via
        ``/v1/jobs/{job_id}/result``. The EPUBCheck + ACE verify report
        is attached to the job's ``metadata_json`` field — a clean
        automated report is NOT by itself a conformance certificate per
        DAISY guidance; manual SMART-style review is required.
        """
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".html", ".htm"):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="Only .html / .htm accepted.",
            )
        max_bytes = settings.max_upload_mb * 1024 * 1024
        contents = await file.read(max_bytes + 1)
        if len(contents) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds max upload size ({settings.max_upload_mb} MB).",
            )
        if b"<html" not in contents.lower():
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="File does not appear to be HTML.",
            )

        settings.job_dir.mkdir(parents=True, exist_ok=True)
        staging = settings.job_dir / f"_staging-{uuid.uuid4().hex}.html"
        staging.write_bytes(contents)
        body = await _finalize_upload_and_enqueue(
            store, worker, settings, staging, ".html",
            kind=JOB_KIND_CONVERT_HTML_TO_EPUB,
            result_media_type="application/epub+zip",
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)

    @router.post("/v1/convert/extract-markdown", dependencies=[require_key])
    @upload_limit
    async def convert_extract_markdown(
        request: Request,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Synchronous extract of markdown from an uploaded PDF/Office doc.

        Returns the markdown inline rather than creating a job — extraction
        is usually fast and callers typically need it immediately.
        """
        from project_remedy.config import load_config
        from project_remedy.database import DatabaseManager
        from project_remedy.extractor import ContentExtractor
        from project_remedy.models import DocumentJob, JobStatus
        from project_remedy.ollama_client import OllamaClient

        staging, ft, _ = await _stage_upload(file, settings, _PDF_OR_OFFICE)
        try:
            cfg = load_config()
            db = DatabaseManager()
            ollama = OllamaClient(cfg)
            doc_job = DocumentJob(
                link_text=Path(file.filename or "").stem,
                file_type=ft,
                local_path=str(staging),
                status=JobStatus.DISCOVERED,
            )
            await db.create_job(doc_job)
            extractor = ContentExtractor(cfg, ollama, db)
            markdown = await extractor.extract(doc_job)
            return JSONResponse({
                "file_type": ft.value,
                "markdown": markdown,
                "characters": len(markdown),
            })
        finally:
            staging.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # /v1/jobs/{id} — shared status / result / report / delete
    # ------------------------------------------------------------------

    @router.get("/v1/jobs/{job_id}", dependencies=[require_key])
    async def get_job(job_id: str) -> dict:
        job = await store.get(job_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        return serialize_job(job)

    @router.get("/v1/jobs/{job_id}/result", dependencies=[require_key])
    async def get_result(job_id: str):
        job = await store.get(job_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if job.status != "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Job status is '{job.status}'; result not available.",
            )
        path = Path(job.output_path)
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Result file missing on server.",
            )
        ext_for_media = {
            "application/pdf": ".pdf",
            "text/html": ".html",
        }.get(job.result_media_type, path.suffix or "")
        return FileResponse(
            path,
            media_type=job.result_media_type,
            filename=f"{job_id}{ext_for_media}",
        )

    @router.get("/v1/jobs/{job_id}/report", dependencies=[require_key])
    async def get_report(job_id: str):
        job = await store.get(job_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        if job.status != "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Job status is '{job.status}'; report not available.",
            )
        if not job.report_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No report was generated for this job kind.",
            )
        path = Path(job.report_path)
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Report file missing on server.",
            )
        return FileResponse(path, media_type="text/html", filename=f"{job_id}_acr.html")

    @router.delete("/v1/jobs/{job_id}", dependencies=[require_key])
    async def delete_job(job_id: str) -> dict:
        job = await store.get(job_id)
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
        await store.delete(job_id)
        workdir = settings.job_dir / job_id
        if workdir.exists():
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        return {"deleted": job_id}

    return router
