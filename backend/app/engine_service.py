"""Bridge between the HTTP job worker and the engine.

One ``run_job(job)`` dispatch entry point. The worker routes each job
to the right engine function based on ``job.kind``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import shutil
from dataclasses import asdict
from pathlib import Path

from project_remedy.compliance_report import generate_document_report
from project_remedy.config import load_config
from project_remedy.database import DatabaseManager
from project_remedy.models import DocumentJob, FileType, JobStatus
from project_remedy.pdf_acceptance import evaluate_pdf_acceptance
from project_remedy.pdf_fixer import fix_and_verify
from project_remedy.rebuild.ast import (
    Conformance as _RbConf,
    Margin as _RbMargin,
    Metadata as _RbMetadata,
    PageSettings as _RbPage,
)
from project_remedy.rebuild.ast_builder import (
    ASTBuildError as _ASTBuildError,
    build as _ast_build,
)
from project_remedy.rebuild.markdown_parser import parse as _md_parse
from project_remedy.rebuild.sidecar import (
    QuestPdfSidecar as _Sidecar,
    SidecarError as _SidecarError,
    SidecarTimeout as _SidecarTimeout,
)
from project_remedy.rebuild.vision_enricher import (
    VisionEnrichmentError as _VisionEnrichmentError,
    enrich as _vision_enrich,
)

from backend.app.config import Settings
from backend.app.jobs import (
    JOB_KIND_CONVERT_HTML_TO_EPUB,
    JOB_KIND_CONVERT_HTML_TO_PDF,
    JOB_KIND_CONVERT_OFFICE_TO_HTML,
    JOB_KIND_CONVERT_PDF_TO_HTML,
    JOB_KIND_REMEDIATE_OFFICE,
    JOB_KIND_REMEDIATE_PDF,
    JOB_KIND_VISION_PLAN_RUN,
    Job,
    JobStore,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File-type detection
# ---------------------------------------------------------------------------


_EXT_TO_FILETYPE: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".doc": FileType.DOC,
    ".pptx": FileType.PPTX,
    ".ppt": FileType.PPT,
    ".xlsx": FileType.XLSX,
    ".xls": FileType.XLS,
}


_MEDIA_TYPES: dict[FileType, str] = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    FileType.DOC: "application/msword",
    FileType.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    FileType.PPT: "application/vnd.ms-powerpoint",
    FileType.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    FileType.XLS: "application/vnd.ms-excel",
}


_OFFICE_TYPES = {
    FileType.DOCX, FileType.DOC,
    FileType.PPTX, FileType.PPT,
    FileType.XLSX, FileType.XLS,
}


def filetype_for_suffix(suffix: str) -> FileType | None:
    return _EXT_TO_FILETYPE.get(suffix.lower())


def media_type_for(ft: FileType) -> str:
    return _MEDIA_TYPES[ft]


def is_office(ft: FileType) -> bool:
    return ft in _OFFICE_TYPES


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def run_job(job: Job, store: JobStore, settings: Settings) -> None:
    """Route *job* to the correct engine handler based on ``job.kind``."""
    kind = job.kind
    if kind == JOB_KIND_REMEDIATE_PDF:
        await _remediate_pdf(job, store, settings)
    elif kind == JOB_KIND_REMEDIATE_OFFICE:
        await _remediate_office(job, store, settings)
    elif kind == JOB_KIND_CONVERT_PDF_TO_HTML:
        await _convert_pdf_to_html(job, store, settings)
    elif kind == JOB_KIND_CONVERT_OFFICE_TO_HTML:
        await _convert_office_to_html(job, store, settings)
    elif kind == JOB_KIND_CONVERT_HTML_TO_PDF:
        await _convert_html_to_pdf(job, store, settings)
    elif kind == JOB_KIND_CONVERT_HTML_TO_EPUB:
        await _convert_html_to_epub(job, store, settings)
    elif kind == JOB_KIND_VISION_PLAN_RUN:
        await _vision_plan_run(job, store, settings)
    else:
        raise ValueError(f"Unknown job kind: {kind!r}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _remediate_pdf(job: Job, store: JobStore, settings: Settings) -> None:
    input_path = Path(job.input_path)
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / "remediated.pdf"
    report_dir = workdir / "report"
    cfg = load_config()

    try:
        remediation_timeout = float(
            os.environ.get("PDF_REMEDIATION_TIMEOUT_SECONDS", "900")
        )
    except ValueError:
        remediation_timeout = 900.0
    try:
        acceptance_timeout = float(
            os.environ.get("PDF_ACCEPTANCE_TIMEOUT_SECONDS", "300")
        )
    except ValueError:
        acceptance_timeout = 300.0

    log.info(
        "Starting remediation for job %s (timeout=%.0fs, acceptance_timeout=%.0fs)",
        job.id,
        remediation_timeout,
        acceptance_timeout,
    )

    await store.update(job.id, stage="remediating", progress=0.15)
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                fix_and_verify,
                input_path,
                output_path,
                config=cfg,
                original_path=input_path,
                conformance_repair=True,
            ),
            timeout=remediation_timeout,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"remediation_timeout: fix_and_verify exceeded {remediation_timeout:.0f}s"
        ) from exc

    await store.update(job.id, stage="evaluating_acceptance", progress=0.65)
    try:
        acceptance = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_pdf_acceptance,
                output_path,
                config=cfg,
                original_path=input_path,
            ),
            timeout=acceptance_timeout,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"acceptance_timeout: evaluate_pdf_acceptance exceeded {acceptance_timeout:.0f}s"
        ) from exc

    # Rebuild-tier escalation: if the deterministic repair didn't produce a
    # passing PDF and the client opted in, run the semantic rebuild tier
    # instead of emitting a likely-broken result. For explicit rebuild opt-in,
    # any veraPDF failure also escalates, including source-font-only failures
    # that the default acceptance gate treats as non-blocking. Default behavior
    # (no flag) is identical to before: we always continue to generate the
    # report and mark the job done, even if acceptance has warning_reasons.
    try:
        meta = _json.loads(job.metadata_json or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    allow_rebuild = bool(meta.get("allow_semantic_rebuild", False))
    quality_requested = bool(meta.get("quality", False))

    verapdf_failed = (
        acceptance.verapdf_result.checked and not acceptance.verapdf_result.passed
    )

    should_rebuild = (
        allow_rebuild
        and cfg.rebuild.enabled
        and (not acceptance.passed or verapdf_failed)
    )

    if should_rebuild:
        try:
            await store.update(
                job.id, stage="rebuilding_from_semantics", progress=0.70,
            )
            await _rebuild_from_semantics(
                input_path, output_path, cfg, job, store, settings,
            )
            return
        except Exception as exc:  # noqa: BLE001
            # Orchestrator sets specific rebuild_* error codes on failure.
            # Any uncaught exception here gets a generic fallback message.
            await store.update(
                job.id, status="failed",
                error=f"rebuild_unexpected: {exc}",
            )
            return

    if quality_requested:
        from backend.app.quality_calibration import assert_quality_calibrated
        from project_remedy.quality_judges.pdf.audit import audit_pdf_quality

        assert_quality_calibrated(settings, "pdf")
        try:
            acceptance.quality_result = await asyncio.wait_for(
                asyncio.to_thread(
                    audit_pdf_quality,
                    output_path,
                    config=cfg,
                ),
                timeout=acceptance_timeout,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"acceptance_timeout: audit_pdf_quality exceeded {acceptance_timeout:.0f}s"
            ) from exc

    await store.update(job.id, stage="generating_report", progress=0.85)
    await asyncio.to_thread(
        generate_document_report,
        original_path=input_path,
        remediated_path=output_path,
        output_dir=report_dir,
        acceptance=acceptance,
    )

    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_path),
        report_path=str(_first_html(report_dir) or ""),
        result_media_type="application/pdf",
    )


async def _remediate_office(job: Job, store: JobStore, settings: Settings) -> None:
    from project_remedy.office_remediator import OfficeRemediator

    input_path = Path(job.input_path)
    ft = filetype_for_suffix(input_path.suffix) or FileType.DOCX
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / f"remediated{input_path.suffix}"

    await store.update(job.id, stage="remediating", progress=0.30)
    remediator = OfficeRemediator()
    await remediator.remediate(input_path, output_path, title=input_path.stem)

    try:
        meta = _json.loads(job.metadata_json or "{}")
    except Exception:  # noqa: BLE001
        meta = {}
    if meta.get("quality"):
        # NOTE: ``ft`` may be a legacy Office type (FileType.DOC/PPT/XLS) when
        # the upload's suffix matched a legacy extension. Neither
        # ``quality_calibration_status`` (via ``DIMENSIONS_BY_FORMAT``) nor
        # ``audit_office_quality`` (via ``_FORMAT_BY_FILE_TYPE``) accepts those
        # values, so the quality opt-in path currently raises on legacy
        # uploads. No suffix-level guard exists here yet.
        from backend.app.quality_calibration import assert_quality_calibrated
        from project_remedy.quality_judges.office.audit import audit_office_quality

        assert_quality_calibrated(settings, ft.value)
        quality_result = await asyncio.to_thread(
            audit_office_quality,
            output_path,
            file_type=ft,
            config=load_config(),
        )
        meta["quality_result"] = asdict(quality_result)
        await store.update(job.id, metadata_json=_json.dumps(meta))

    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_path),
        result_media_type=media_type_for(ft),
    )


async def _doc_to_html(
    job: Job, store: JobStore, settings: Settings, file_type: FileType
) -> None:
    """Shared handler for PDF→HTML and Office→HTML conversion."""
    from project_remedy.converter import HTMLConverter
    from project_remedy.extractor import ContentExtractor
    from project_remedy.ollama_client import OllamaClient

    input_path = Path(job.input_path)
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / "converted.html"
    cfg = load_config()
    db = DatabaseManager()
    ollama = OllamaClient(cfg)
    await ollama.start()
    try:
        await store.update(job.id, stage="extracting", progress=0.2)
        doc_job = DocumentJob(
            link_text=input_path.stem,
            file_type=file_type,
            local_path=str(input_path),
            status=JobStatus.DISCOVERED,
        )
        await db.create_job(doc_job)

        extractor = ContentExtractor(cfg, ollama, db)
        await extractor.extract(doc_job)

        await store.update(job.id, stage="converting", progress=0.65)
        converter = HTMLConverter(cfg, ollama, db, campus=cfg.branding)
        html = await converter.convert(doc_job)

        output_path.write_text(html, encoding="utf-8")

        await store.update(
            job.id,
            status="done",
            stage="complete",
            progress=1.0,
            output_path=str(output_path),
            result_media_type="text/html",
        )
    finally:
        try:
            await ollama.close()
        except Exception:  # noqa: BLE001
            pass  # never mask a real error


async def _convert_pdf_to_html(job: Job, store: JobStore, settings: Settings) -> None:
    await _doc_to_html(job, store, settings, FileType.PDF)


async def _convert_office_to_html(job: Job, store: JobStore, settings: Settings) -> None:
    input_path = Path(job.input_path)
    ft = filetype_for_suffix(input_path.suffix) or FileType.DOCX
    await _doc_to_html(job, store, settings, ft)


async def _vision_plan_run(job: Job, store: JobStore, settings: Settings) -> None:
    """Tier-3 opt-in rescue. Not part of the default /v1/remediate flow.

    Per the repo's AI strategy: the deterministic fix_and_verify + faithful
    rebuild path is the default; vision-planner runs only when explicitly
    invoked via this endpoint.
    """
    import json
    from project_remedy.vision_planner.harness import VisionPlannerHarness
    from project_remedy.vision_planner.pipeline import run_vision_plan

    input_path = Path(job.input_path)
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_pdf = workdir / "vp_output.pdf"
    trace_path = workdir / "trace.json"

    cfg = load_config()

    from project_remedy.ollama_client import OllamaClient
    client = OllamaClient(cfg)

    harness = VisionPlannerHarness()

    await store.update(job.id, stage="vision_planning", progress=0.1)
    await client.start()
    try:
        trace = await run_vision_plan(
            pdf_path=input_path,
            output_path=trace_path,
            harness=harness,
            client=client,
            config=cfg,
            pdf_output_path=output_pdf,
        )
    finally:
        if hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # Persist trace as JSON next to the PDF
    trace_path.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")

    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_pdf) if output_pdf.exists() else str(trace_path),
        report_path=str(trace_path),
        result_media_type="application/pdf" if output_pdf.exists() else "application/json",
        metadata_json=json.dumps({
            "passed": trace.get("passed", False),
            "violations_before": trace.get("violations_before", 0),
            "violations_after": trace.get("violations_after", 0),
        }),
    )


async def _convert_html_to_pdf(job: Job, store: JobStore, settings: Settings) -> None:
    from project_remedy.html_to_pdf import HTMLToPDFConverter

    input_path = Path(job.input_path)
    html_content = input_path.read_text(encoding="utf-8")
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / "converted.pdf"

    await store.update(job.id, stage="rendering", progress=0.3)
    converter = HTMLToPDFConverter(max_concurrent=1)
    await converter.start()
    try:
        await converter.convert(html_content, output_path, title=input_path.stem)
    finally:
        await converter.close()

    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_path),
        result_media_type="application/pdf",
    )


async def _convert_html_to_epub(job: Job, store: JobStore, settings: Settings) -> None:
    """Convert uploaded accessible HTML into an EPUB Accessibility 1.1 package.

    Runs ``HTMLToEPUBConverter`` to produce the .epub, then ``verify_epub``
    to run EPUBCheck + ACE. The verify report is persisted as the job's
    metadata so callers can inspect it via the report endpoint. The job
    is marked ``done`` even when the automated verifiers report
    violations — the verify report itself surfaces them, and per DAISY
    a clean automated report is not by itself a conformance certificate.
    """
    from project_remedy.epub_verifier import verify_epub
    from project_remedy.html_to_epub import HTMLToEPUBConverter

    input_path = Path(job.input_path)
    html_content = input_path.read_text(encoding="utf-8")
    workdir = settings.job_dir / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / "converted.epub"

    await store.update(job.id, stage="rendering", progress=0.3)
    converter = HTMLToEPUBConverter(max_concurrent=1)
    await converter.start()
    try:
        result = await converter.convert(
            html_content, output_path, title=input_path.stem,
        )
    finally:
        await converter.close()

    if not result.success:
        await store.update(
            job.id,
            status="failed",
            stage="failed",
            progress=1.0,
            error=f"epub_conversion_failed: {result.error_message}",
        )
        return

    await store.update(job.id, stage="verifying", progress=0.7)
    report = await verify_epub(output_path)

    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_path),
        result_media_type="application/epub+zip",
        metadata_json=_json.dumps({
            "verify": report.to_dict(),
            "features": result.accessibility_features,
            "chapters": result.chapters,
        }),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_html(directory: Path) -> Path | None:
    if not directory.exists():
        return None
    htmls = sorted(directory.glob("*.html"))
    return htmls[0] if htmls else None


# ---------------------------------------------------------------------------
# Rebuild-tier orchestrator
# ---------------------------------------------------------------------------


async def _rebuild_from_semantics(
    input_path: Path,
    output_path: Path,
    cfg,  # PipelineConfig
    job: Job,
    store: JobStore,
    settings: Settings,
) -> None:
    """Run the full rebuild tier. Writes remediated.pdf on success.

    Orchestrates extractor → (markdown_parser || vision_enricher via gather)
    → ast_builder → sidecar.render → acceptance(rebuild_mode=True) → finalize.
    Specific ``rebuild_*`` error codes on failure. Marks job ``status=done``
    or ``status=failed`` itself — caller should ``return`` after awaiting.
    """
    from project_remedy.extractor import ContentExtractor
    from project_remedy.ollama_client import OllamaClient
    from project_remedy.pdf_vision import create_provider_from_config

    # --- step 1: extract ---
    db = DatabaseManager()
    ollama = OllamaClient(cfg)
    await ollama.start()
    try:
        extractor = ContentExtractor(cfg, ollama, db)
        doc_job = DocumentJob(
            link_text=input_path.stem,
            file_type=FileType.PDF,
            local_path=str(input_path),
            status=JobStatus.DISCOVERED,
        )
        await db.create_job(doc_job)
        try:
            ocr_markdown = await extractor.extract(doc_job)
        except Exception as exc:  # noqa: BLE001
            await store.update(
                job.id, status="failed",
                error=f"rebuild_extractor_failed: {exc}",
            )
            return
    finally:
        try:
            await ollama.close()
        except Exception:  # noqa: BLE001
            pass  # never mask a real error

    if not ocr_markdown or not ocr_markdown.strip():
        await store.update(
            job.id, status="failed",
            error="rebuild_extractor_empty",
        )
        return

    extracted_images = doc_job.get_extracted_images()
    # Image files live at: cfg.output.output_dir / "images" / doc_job.id[:12]
    # (per extractor._extract_pdf). Use that for image_dir in ast_builder.
    image_dir = cfg.output.output_dir / "images" / doc_job.id[:12]

    # --- step 2: compose (parallel fan-in) ---
    provider = create_provider_from_config(cfg)
    if provider is None:
        await store.update(
            job.id, status="failed",
            error="rebuild_vision_provider_unavailable",
        )
        return

    try:
        block_tree, image_semantics = await asyncio.gather(
            asyncio.to_thread(_md_parse, ocr_markdown),
            _vision_enrich(
                extracted_images, provider,
                concurrency=cfg.rebuild.vision_concurrency,
            ),
        )
    except _VisionEnrichmentError as exc:
        await store.update(
            job.id, status="failed",
            error=f"rebuild_vision_total_failure: {exc}",
        )
        return

    metadata_ast = _RbMetadata(title=input_path.stem, language="en-US")
    page_settings = _RbPage(
        size="Letter",
        margin=_RbMargin(top=0.75, right=0.75, bottom=0.75, left=0.75, unit="in"),
    )
    conformance = _RbConf(pdfua="PDFUA_1", pdfa=None)

    try:
        request = _ast_build(
            block_tree, image_semantics, extracted_images,
            metadata_ast, page_settings, conformance,
            image_dir=image_dir,
        )
    except _ASTBuildError as exc:
        await store.update(
            job.id, status="failed",
            error=f"rebuild_ast_invariant_violation: {exc}",
        )
        return

    # --- step 3: render via sidecar ---
    binary = _resolve_sidecar_binary()
    if binary is None:
        await store.update(
            job.id, status="failed",
            error="rebuild_sidecar_not_available",
        )
        return

    sidecar = _Sidecar(binary_path=binary, timeout_s=cfg.rebuild.sidecar_timeout_s)
    try:
        pdf_bytes = await sidecar.render(request)
    except _SidecarTimeout as exc:
        await store.update(
            job.id, status="failed",
            error=f"rebuild_sidecar_timeout: {exc}",
        )
        return
    except _SidecarError as exc:
        await store.update(
            job.id, status="failed",
            error=f"rebuild_sidecar_failed: {exc}",
        )
        return

    # --- step 4: persist to temp path ---
    # Write to a temp path; only rename to output_path after acceptance passes.
    # If rebuild fails acceptance, we don't want to destroy the tier-1 PDF
    # that may already be at output_path.
    rebuilt_path = output_path.with_name("_rebuilt_tmp.pdf")
    rebuilt_path.write_bytes(pdf_bytes)

    # --- step 5: acceptance (rebuild mode) against temp path ---
    try:
        report = await asyncio.to_thread(
            evaluate_pdf_acceptance,
            rebuilt_path,
            config=cfg,
            original_path=input_path,
            rebuild_mode=True,
        )
    except Exception as exc:  # noqa: BLE001
        rebuilt_path.unlink(missing_ok=True)  # cleanup temp on error
        await store.update(
            job.id, status="failed",
            error=f"rebuild_acceptance_error: {exc}",
        )
        return

    # T11's pdf_acceptance uses a return-based pattern. Check report.passed.
    # Fail-closed: default to False when the attribute is missing/unreadable.
    if not getattr(report, "passed", False):
        rebuilt_path.unlink(missing_ok=True)  # cleanup temp on acceptance fail
        reasons = getattr(report, "warning_reasons", []) or []
        reason = reasons[0] if reasons else "unspecified"
        await store.update(
            job.id, status="failed",
            error=f"rebuild_acceptance_failed: {reason}",
        )
        return

    # --- step 6: acceptance passed, atomic rename to output_path ---
    rebuilt_path.replace(output_path)

    # --- step 7: generate conformance report ---
    workdir = settings.job_dir / job.id
    report_dir = workdir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(
            generate_document_report,
            original_path=input_path,
            remediated_path=output_path,
            output_dir=report_dir,
        )
        report_html = _first_html(report_dir)
        report_path_str = str(report_html) if report_html else ""
    except Exception:  # noqa: BLE001
        # Report generation is non-fatal — the remediated PDF is already good.
        # Carry on without a report path.
        report_path_str = ""

    # --- step 8: finalize ---
    try:
        existing_meta = _json.loads(job.metadata_json or "{}")
    except Exception:  # noqa: BLE001
        existing_meta = {}
    existing_meta["tier"] = "rebuilt"
    await store.update(
        job.id,
        status="done",
        stage="complete",
        progress=1.0,
        output_path=str(output_path),
        report_path=report_path_str,
        metadata_json=_json.dumps(existing_meta),
        result_media_type="application/pdf",
    )


def _resolve_sidecar_binary() -> Path | None:
    """Locate the QuestPDF sidecar binary. Env override or build-output walk."""
    import os

    env = os.environ.get("REMEDY_QUESTPDF_BINARY")
    if env:
        p = Path(env)
        return p if p.exists() else None
    # Repo-relative: sidecar/QuestPdfRenderer/bin/Release/net9.0/*/publish/remedy-questpdf
    root = (
        Path(__file__).resolve().parents[2]
        / "sidecar"
        / "QuestPdfRenderer"
        / "bin"
        / "Release"
        / "net9.0"
    )
    if not root.exists():
        return None
    for rid in root.iterdir():
        candidate = rid / "publish" / "remedy-questpdf"
        if candidate.exists():
            return candidate
    return None
