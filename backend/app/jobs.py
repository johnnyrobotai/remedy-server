"""In-process job store + async worker.

Jobs are persisted to a tiny SQLite table so state survives restart.
The worker task consumes them from an asyncio.Queue and calls the
engine wrapper. Files for each job live under ``settings.job_dir/{id}/``.

Each job has a ``kind`` discriminator so a single ``/v1/jobs/{id}``
endpoint works for remediation, conversion, office remediation, etc.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable


log = logging.getLogger("project_remedy.backend.jobs")


JobRunner = Callable[["Job"], Awaitable[None]]

# Terminal statuses eligible for retention-based pruning.
TERMINAL_STATUSES = frozenset({"done", "failed"})


# Known job kinds. The worker dispatches on this value.
JOB_KIND_REMEDIATE_PDF = "remediate_pdf"
JOB_KIND_REMEDIATE_OFFICE = "remediate_office"
JOB_KIND_CONVERT_PDF_TO_HTML = "convert_pdf_to_html"
JOB_KIND_CONVERT_OFFICE_TO_HTML = "convert_office_to_html"
JOB_KIND_CONVERT_HTML_TO_PDF = "convert_html_to_pdf"
JOB_KIND_CONVERT_HTML_TO_EPUB = "convert_html_to_epub"
JOB_KIND_VISION_PLAN_RUN = "vision_plan_run"


@dataclass
class Job:
    id: str
    kind: str                 # see JOB_KIND_* constants
    status: str               # queued | running | done | failed
    stage: str                # free-text current step
    progress: float           # 0.0 – 1.0
    input_path: str
    output_path: str
    report_path: str
    error: str
    created_at: str
    updated_at: str
    result_media_type: str = "application/pdf"
    metadata_json: str = "{}"

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    """SQLite-backed job registry. Safe for single-process concurrent access."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'remediate_pdf',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0.0,
                    input_path TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    report_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    result_media_type TEXT NOT NULL DEFAULT 'application/pdf',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Forward-compat: add columns if upgrading from an older DB.
            for col, ddl in (
                ("kind", "ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'remediate_pdf'"),
                ("result_media_type", "ALTER TABLE jobs ADD COLUMN result_media_type TEXT NOT NULL DEFAULT 'application/pdf'"),
                ("metadata_json", "ALTER TABLE jobs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"),
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists

    async def create(
        self,
        input_path: Path,
        *,
        kind: str = JOB_KIND_REMEDIATE_PDF,
        result_media_type: str = "application/pdf",
    ) -> Job:
        now = datetime.now(timezone.utc).isoformat()
        job = Job(
            id=uuid.uuid4().hex,
            kind=kind,
            status="queued",
            stage="queued",
            progress=0.0,
            input_path=str(input_path),
            output_path="",
            report_path="",
            error="",
            created_at=now,
            updated_at=now,
            result_media_type=result_media_type,
        )
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO jobs(id,kind,status,stage,progress,input_path,output_path,report_path,error,result_media_type,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (job.id, job.kind, job.status, job.stage, job.progress,
                     job.input_path, job.output_path, job.report_path,
                     job.error, job.result_media_type, job.metadata_json,
                     job.created_at, job.updated_at),
                )
        return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    async def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        input_path: str | None = None,
        output_path: str | None = None,
        report_path: str | None = None,
        error: str | None = None,
        result_media_type: str | None = None,
        metadata_json: str | None = None,
    ) -> Job | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row:
                    return None
                updates: dict = {}
                if status is not None:
                    updates["status"] = status
                if stage is not None:
                    updates["stage"] = stage
                if progress is not None:
                    updates["progress"] = progress
                if input_path is not None:
                    updates["input_path"] = input_path
                if output_path is not None:
                    updates["output_path"] = output_path
                if report_path is not None:
                    updates["report_path"] = report_path
                if error is not None:
                    updates["error"] = error
                if result_media_type is not None:
                    updates["result_media_type"] = result_media_type
                if metadata_json is not None:
                    updates["metadata_json"] = metadata_json
                updates["updated_at"] = datetime.now(timezone.utc).isoformat()
                if updates:
                    cols = ", ".join(f"{k}=?" for k in updates)
                    params = list(updates.values()) + [job_id]
                    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", params)
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    async def delete(self, job_id: str) -> bool:
        async with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                return cur.rowcount > 0

    async def list_older_than(self, cutoff: datetime) -> list[Job]:
        async with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE created_at < ?",
                    (cutoff.isoformat(),),
                ).fetchall()
        return [_row_to_job(r) for r in rows]

    async def list_by_statuses(self, statuses: Iterable[str]) -> list[Job]:
        values = tuple(statuses)
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        async with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE status IN ({placeholders})",
                    values,
                ).fetchall()
        return [_row_to_job(r) for r in rows]

    async def fail_running_jobs(self, error: str) -> int:
        """Mark jobs left running by a prior process as failed."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """UPDATE jobs
                       SET status='failed', stage='interrupted', error=?, updated_at=?
                       WHERE status='running'""",
                    (error, now),
                )
                return cur.rowcount

    async def ping(self) -> None:
        """Raise if SQLite cannot be opened and queried."""
        async with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()


def _row_to_job(row) -> Job:
    keys = set(row.keys())
    return Job(
        id=row["id"],
        kind=row["kind"] if "kind" in keys else JOB_KIND_REMEDIATE_PDF,
        status=row["status"],
        stage=row["stage"],
        progress=row["progress"],
        input_path=row["input_path"],
        output_path=row["output_path"],
        report_path=row["report_path"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result_media_type=row["result_media_type"] if "result_media_type" in keys else "application/pdf",
        metadata_json=row["metadata_json"] if "metadata_json" in keys else "{}",
    )


class JobWorker:
    """Consumes a queue of job IDs and runs them in-process."""

    def __init__(self, store: JobStore, runner: JobRunner, *, concurrency: int = 1) -> None:
        self.store = store
        self.runner = runner
        self.concurrency = max(1, int(concurrency))
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    @property
    def is_running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    def start(self) -> None:
        if self.is_running:
            return
        self._tasks = [
            asyncio.create_task(self._loop(i), name=f"job-worker-{i + 1}")
            for i in range(self.concurrency)
        ]

    async def stop(self) -> None:
        tasks = list(self._tasks)
        self._tasks = []
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def _loop(self, _worker_index: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = await self.store.get(job_id)
                if not job:
                    continue
                await self.store.update(job_id, status="running", stage="starting", progress=0.05)
                await self.runner(job)
            except Exception as exc:  # noqa: BLE001
                await self.store.update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.queue.task_done()


def serialize_job(job: Job) -> dict:
    """Return a JSON-safe dict; hide internal paths."""
    d = job.to_dict()
    d.pop("input_path", None)
    d.pop("output_path", None)
    d.pop("report_path", None)
    return d


async def prune_expired_jobs(
    store: JobStore,
    job_dir: Path,
    retention_hours: int,
) -> int:
    """Delete jobs older than ``retention_hours`` with status in (done, failed).

    Returns the number of jobs pruned. Best-effort per-job: if removing
    one job's directory fails, log and continue with the next. The outer
    loop is wrapped so a single bad job cannot crash the caller.
    """
    if retention_hours <= 0:
        # Retention disabled; skip.
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    pruned = 0
    try:
        candidates = await store.list_older_than(cutoff)
    except Exception:  # noqa: BLE001
        log.exception("prune_expired_jobs: failed to list candidates")
        return 0

    for job in candidates:
        if job.status not in TERMINAL_STATUSES:
            continue
        try:
            await store.delete(job.id)
            shutil.rmtree(job_dir / job.id, ignore_errors=True)
            pruned += 1
        except Exception:  # noqa: BLE001
            log.exception("prune_expired_jobs: failed to prune job %s", job.id)
            continue

    if pruned:
        log.info("prune_expired_jobs: pruned %d expired job(s)", pruned)
    return pruned
