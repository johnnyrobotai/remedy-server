"""Remediate and audit the desktop PDF corpus.

This is a local operations helper, not part of the HTTP API. It runs the same
engine calls used by the API (`fix_and_verify` + `evaluate_pdf_acceptance`) and
writes a JSONL manifest so long corpus runs can be resumed.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pikepdf

from project_remedy.config import load_config
from project_remedy.levels import (
    classify_level,
    is_font_clause_only,
    oversized_reason,
    probe_structure,
    select_shard,
    summarize_levels,
)
from project_remedy.heading_feedback import apply_prominence_heading_rescue
from project_remedy.pdf_acceptance import evaluate_pdf_acceptance
from project_remedy.pdf_fixer import (
    apply_heading_retag_refix,
    fix_and_verify,
    heading_retag_pages_from_failures,
)
from project_remedy.pdf_vision import create_provider_from_config
from project_remedy.tag_tree_reader import Severity


DEFAULT_INPUT_ROOTS = [
    Path.home() / "Desktop" / "Chicano Studies Docs",
    Path.home() / "Desktop" / "sample pdfs",
    Path.home() / "Desktop" / "Syllabus Examples",
]
DEFAULT_OUTPUT_ROOT = Path.home() / "Desktop" / "remediated_pdfs"


@dataclass
class CorpusRecord:
    source: str
    output: str
    status: str
    elapsed_seconds: float
    acceptance_passed: bool
    clean: bool
    checker_failures: list[dict[str, Any]]
    manual_checks: list[dict[str, Any]]
    screen_reader_errors: list[dict[str, Any]]
    verapdf_passed: bool
    verapdf_violations: list[dict[str, Any]]
    non_blocking_verapdf_warnings: int
    visual_diff: dict[str, Any] | None
    warning_reasons: list[str]
    fix_changes: list[str]
    fix_skipped: list[str]
    error: str = ""
    completed_at: str = ""
    # Phase 0 remediation-level fields (defaults keep old manifests readable).
    root: str = ""
    level: str = ""
    level_blocking: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    sub_scores: dict[str, Any] = field(default_factory=dict)


def _source_files(input_roots: list[Path]) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for root in input_roots:
        for path in sorted(root.rglob("*.pdf")):
            files.append((root, path))
    return files


def _output_path(output_root: Path, root: Path, source: Path) -> Path:
    return output_root / root.name / source.relative_to(root)


def _manifest_done(manifest_path: Path) -> set[str]:
    done: set[str] = set()
    for source, record in _manifest_latest(manifest_path).items():
        status = record.get("status")
        # Skip any file already carrying a terminal result on resume. Previously
        # only clean+True files were skipped, so failed / font_clause_residue /
        # skipped_oversized files were fully re-remediated on every relaunch
        # (deterministic re-work that never advanced). "error" stays retryable
        # (may be transient); persistent crashers/hangs are handled by quarantine.
        if status in {"clean", "accepted"} and record.get("clean"):
            done.add(source)
        elif status in {"failed", "font_clause_residue", "skipped_oversized"}:
            done.add(source)
    return done


def _manifest_latest(manifest_path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not manifest_path.exists():
        return latest
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        source = str(record.get("source", ""))
        if source:
            latest[source] = record
    return latest


def _manual_checks(acceptance) -> list[dict[str, Any]]:
    return [
        {
            "rule_id": result.rule_id,
            "description": result.description,
            "details": list(result.details),
        }
        for result in acceptance.checker_report.results
        if result.status == "Manual Check Needed"
    ]


def _checker_failures(acceptance) -> list[dict[str, Any]]:
    return [
        {
            "rule_id": result.rule_id,
            "description": result.description,
            "details": list(result.details),
        }
        for result in acceptance.checker_failures
        if not acceptance._is_source_font_checker_failure(result)
    ]


def _screen_reader_errors(acceptance) -> list[dict[str, Any]]:
    return [
        {
            "rule_id": issue.rule_id,
            "description": issue.description,
            "page": issue.page,
            "element": issue.element,
        }
        for issue in acceptance.tag_tree_result.issues
        if issue.severity == Severity.ERROR
    ]


def _visual_diff(acceptance) -> dict[str, Any] | None:
    result = acceptance.visual_diff_result
    if result is None:
        return None
    return {
        "checked": result.checked,
        "passed": result.passed,
        "total_pages": result.total_pages,
        "differing_pages": [page + 1 for page in result.differing_pages],
        "max_page_diff": result.max_page_diff,
        "error": result.error,
    }


def _is_clean(acceptance) -> bool:
    manual_checks = _manual_checks(acceptance)
    checker_failures = _checker_failures(acceptance)
    screen_reader_errors = _screen_reader_errors(acceptance)
    visual = acceptance.visual_diff_result
    visual_ok = visual is None or not visual.checked or visual.passed
    verapdf_ok = (
        not acceptance.verapdf_result.checked
        or acceptance.verapdf_result.passed
        or len(acceptance.non_blocking_verapdf_warnings)
        == len(acceptance.verapdf_result.violations)
    )
    return (
        acceptance.openable
        and acceptance.passed
        and not checker_failures
        and not manual_checks
        and not screen_reader_errors
        and visual_ok
        and verapdf_ok
    )


def _evaluate(source: Path, output: Path, config) -> tuple[Any, bool]:
    acceptance = evaluate_pdf_acceptance(output, config=config, original_path=source)
    return acceptance, _is_clean(acceptance)


def _classify(path: Path, acceptance):
    """Probe ``path`` and assign an L0–L4 level. Never raises."""
    return classify_level(acceptance, probe_structure(path))


def _quick_metrics(path: Path) -> tuple[int, int]:
    """Cheap (size_bytes, page_count) for the oversized guard.

    Deliberately avoids walking the structure tree or extracting text — those
    are exactly what melt down on pathological PDFs the guard exists to skip.
    """
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    pages = 0
    try:
        with pikepdf.open(path) as pdf:
            pages = len(pdf.pages)
    except Exception:  # noqa: BLE001 - unreadable -> 0 pages, handled downstream
        pages = 0
    return size, pages


class _TimeoutError(Exception):
    pass


@contextmanager
def _time_limit(seconds: int):
    """Best-effort per-file wall-clock cap via SIGALRM (main thread, Unix).

    Note: SIGALRM cannot always preempt a tight loop inside a C extension
    (e.g. MuPDF); the oversized pre-check is the primary guard and this is the
    backstop. seconds<=0 disables the limit.
    """
    if not seconds or seconds <= 0:
        yield
        return

    def _handler(_signum, _frame):
        raise _TimeoutError(f"per-file timeout after {seconds}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _record(
    *,
    source: Path,
    output: Path,
    status: str,
    elapsed_seconds: float,
    acceptance,
    clean: bool,
    fix_changes: list[str],
    fix_skipped: list[str],
    error: str = "",
    root: Path | None = None,
    level_result=None,
) -> CorpusRecord:
    return CorpusRecord(
        source=str(source),
        output=str(output),
        status=status,
        elapsed_seconds=round(elapsed_seconds, 2),
        acceptance_passed=bool(acceptance and acceptance.passed),
        clean=clean,
        checker_failures=[] if acceptance is None else _checker_failures(acceptance),
        manual_checks=[] if acceptance is None else _manual_checks(acceptance),
        screen_reader_errors=[] if acceptance is None else _screen_reader_errors(acceptance),
        verapdf_passed=bool(acceptance and acceptance.verapdf_result.passed),
        verapdf_violations=[] if acceptance is None else acceptance.verapdf_result.violations,
        non_blocking_verapdf_warnings=(
            0 if acceptance is None else len(acceptance.non_blocking_verapdf_warnings)
        ),
        visual_diff=None if acceptance is None else _visual_diff(acceptance),
        warning_reasons=[] if acceptance is None else list(acceptance.warning_reasons),
        fix_changes=fix_changes,
        fix_skipped=fix_skipped,
        error=error,
        completed_at=datetime.now(timezone.utc).isoformat(),
        root="" if root is None else str(root),
        level="" if level_result is None else level_result.level,
        level_blocking=[] if level_result is None else list(level_result.blocking_conditions),
        needs_human=[] if level_result is None else list(level_result.needs_human),
        sub_scores={} if level_result is None else dict(level_result.sub_scores),
    )


def _append_record(manifest_path: Path, record: CorpusRecord) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        fh.flush()


def run(args: argparse.Namespace) -> int:
    input_roots = [Path(root).expanduser() for root in args.input_root]
    output_root = Path(args.output_root).expanduser()
    manifest_path = Path(args.manifest).expanduser()
    config = load_config()
    no_vision = bool(getattr(args, "no_vision", False)) or (
        os.environ.get("PDF_CORPUS_FIX_WITHOUT_VISION", "").lower()
        in {"1", "true", "yes"}
    )
    fix_config = None if no_vision else config
    acceptance_config = None if no_vision else config
    latest_records = _manifest_latest(manifest_path) if args.resume else {}
    done = _manifest_done(manifest_path) if args.resume else set()
    files = _source_files(input_roots)
    # Shard BEFORE limit so each parallel worker takes a disjoint stride of the
    # full corpus (index % shard_count == shard_index).
    shard_count = getattr(args, "shard_count", 1) or 1
    if shard_count > 1:
        files = select_shard(files, shard_index=args.shard_index, shard_count=shard_count)
        print(f"shard {args.shard_index}/{shard_count}: {len(files)} files", flush=True)
    limit = args.limit if args.limit and args.limit > 0 else None
    selected = files[:limit] if limit else files

    classify_only = getattr(args, "classify_only", False)
    summary_rows: list[dict[str, Any]] = []
    oversized_count = 0
    font_residue_count = 0

    failures = 0
    for index, (root, source) in enumerate(selected, 1):
        output = _output_path(output_root, root, source)
        if str(source) in done:
            print(f"[{index}/{len(selected)}] SKIP clean {source.name}", flush=True)
            continue

        # --- Baseline classify-only: evaluate the SOURCE as-is, no remediation.
        if classify_only:
            print(f"[{index}/{len(selected)}] CLASSIFY {source}", flush=True)
            start = time.time()
            acceptance = None
            try:
                # original_path=None: skip page-by-page visual-diff / text
                # similarity (a baseline compares the file to itself — pure
                # waste). Classification only needs checker + tag tree + veraPDF.
                acceptance = evaluate_pdf_acceptance(
                    source,
                    config=acceptance_config,
                    original_path=None,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"    classify error: {exc}", flush=True)
            level_result = _classify(source, acceptance)
            record = _record(
                source=source,
                output=source,
                status="classified",
                elapsed_seconds=time.time() - start,
                acceptance=acceptance,
                clean=False,
                fix_changes=[],
                fix_skipped=[],
                root=root,
                level_result=level_result,
            )
            _append_record(manifest_path, record)
            summary_rows.append(
                {"root": str(root), "level": record.level, "needs_human": record.needs_human}
            )
            print(f"    -> {record.level} blocking={record.level_blocking}", flush=True)
            continue

        print(f"[{index}/{len(selected)}] {source}", flush=True)
        start = time.time()

        # --- Pathological-file guard: route oversized PDFs to a manual queue
        # instead of wedging the pipeline (cheap page/size pre-check).
        if not args.audit_only:
            size_bytes, page_count = _quick_metrics(source)
            reason = oversized_reason(
                file_size_bytes=size_bytes, page_count=page_count,
                max_mb=args.max_mb, max_pages=args.max_pages,
            )
            if reason:
                print(f"    SKIP oversized -> manual queue: {reason}", flush=True)
                record = _record(
                    source=source, output=output, status="skipped_oversized",
                    elapsed_seconds=time.time() - start, acceptance=None,
                    clean=False, fix_changes=[], fix_skipped=[],
                    error=reason, root=root, level_result=None,
                )
                record.needs_human = [f"oversized: {reason} — manual remediation"]
                _append_record(manifest_path, record)
                summary_rows.append(
                    {"root": str(root), "level": "skipped_oversized",
                     "needs_human": record.needs_human}
                )
                oversized_count += 1
                continue

        acceptance = None
        fix_changes: list[str] = []
        fix_skipped: list[str] = []
        try:
          with _time_limit(args.per_file_timeout):
            latest = latest_records.get(str(source), {})
            known_failed = (
                not args.audit_only
                and latest.get("status") in {"failed", "error"}
                and not latest.get("clean")
            )
            if args.audit_only and not output.exists():
                raise FileNotFoundError(f"missing output: {output}")

            if not args.audit_only and (args.force or known_failed or not output.exists()):
                output.parent.mkdir(parents=True, exist_ok=True)
                input_path = source
                print(f"    stage=fix input={input_path.name}", flush=True)
                report = fix_and_verify(
                    input_path,
                    output,
                    config=fix_config,
                    original_path=source,
                    conformance_repair=True,
                )
                fix_changes.extend(report.changes)
                fix_skipped.extend(report.skipped)

            print("    stage=acceptance", flush=True)
            acceptance, clean = _evaluate(source, output, acceptance_config)

            # Failure-driven heading retag: the acceptance checker's vision
            # pass names the exact pages with mis-tagged headings; route them
            # into the targeted retag fixer instead of hoping the generic
            # refix (page-sampled, large-doc-deferred) happens to cover them.
            if not clean and not args.audit_only and acceptance_config is not None:
                retag_pages = heading_retag_pages_from_failures(
                    getattr(acceptance, "checker_failures", None) or []
                )
                if retag_pages:
                    retag_changes = []
                    provider = create_provider_from_config(acceptance_config)
                    if provider is not None:
                        print(
                            f"    stage=heading-retag pages={[p + 1 for p in retag_pages]}",
                            flush=True,
                        )
                        retag_changes.extend(apply_heading_retag_refix(
                            output,
                            vision_provider=provider,
                            checker_failures=acceptance.checker_failures,
                        ))
                    # Vision-free level rescue on the same flagged pages: the
                    # heading adapter is worst at *which* H-level, so assign it
                    # deterministically from font-size prominence (guard-gated,
                    # so table cells / image figures are never promoted). Runs
                    # even when no vision provider is configured.
                    print("    stage=heading-prominence-rescue", flush=True)
                    retag_changes.extend(
                        apply_prominence_heading_rescue(output, retag_pages)
                    )
                    fix_changes.extend(retag_changes)
                    if retag_changes:
                        print("    stage=reacceptance(heading-retag)", flush=True)
                        acceptance, clean = _evaluate(source, output, acceptance_config)

            if not clean and not args.audit_only:
                residue = (
                    acceptance.verapdf_result.violations
                    if acceptance and acceptance.verapdf_result.checked
                    else []
                )
                if is_font_clause_only(residue):
                    # Fix #3: remaining failures are all PDF/UA-1 Fonts-clause
                    # (ISO 14289-1 §7.21.x — font program / descriptor / CIDSet
                    # / ToUnicode). The structural+LLM refix loop can't fix these
                    # and burns ~15 min/file for nothing. The faithful_rebuild
                    # "preserving" mode also preserves the broken fonts (and
                    # introduces new structural violations). Short-circuit:
                    # mark for the manual font-remediation queue and move on.
                    #
                    # TODO(font-rebuild): wire a faithful_rebuild mode that
                    # actually replaces fonts (SimpleFontReplacer per-font swap),
                    # then escalate here instead of routing to manual.
                    rule_ids = [v.get("id", "?") for v in residue]
                    print(f"    SKIP refix -> font-residue manual queue: {rule_ids}", flush=True)
                    fix_changes.append(
                        f"short-circuited refix: font-clause-only residue ({len(residue)} viol)"
                    )
                else:
                    print("    stage=refix", flush=True)
                    report = fix_and_verify(
                        output,
                        output,
                        config=fix_config,
                        original_path=source,
                        conformance_repair=True,
                    )
                    fix_changes.extend(report.changes)
                    fix_skipped.extend(report.skipped)
                    print("    stage=reacceptance", flush=True)
                    acceptance, clean = _evaluate(source, output, acceptance_config)

            # When the failure is font-clause-only, mark it for the manual
            # font-remediation queue (the engine has no path that fixes 7.21.x).
            font_clause = (
                acceptance is not None
                and acceptance.verapdf_result.checked
                and not clean
                and is_font_clause_only(acceptance.verapdf_result.violations)
            )
            if font_clause:
                status = "font_clause_residue"
                font_residue_count += 1
            else:
                status = "clean" if clean else "failed"
            if not clean and not font_clause:
                failures += 1
            record = _record(
                source=source,
                output=output,
                status=status,
                elapsed_seconds=time.time() - start,
                acceptance=acceptance,
                clean=clean,
                fix_changes=fix_changes,
                fix_skipped=fix_skipped,
                root=root,
                level_result=_classify(output, acceptance),
            )
            if font_clause:
                record.needs_human = (record.needs_human or []) + [
                    "font-clause residue (7.21.x): engine cannot remediate; "
                    "needs manual font replacement"
                ]
        except Exception as exc:  # noqa: BLE001 - corpus runner records every failure
            failures += 1
            record = _record(
                source=source,
                output=output,
                status="error",
                elapsed_seconds=time.time() - start,
                acceptance=acceptance,
                clean=False,
                fix_changes=fix_changes,
                fix_skipped=fix_skipped,
                error=str(exc),
                root=root,
                level_result=_classify(output, acceptance),
            )

        _append_record(manifest_path, record)
        summary_rows.append(
            {"root": str(root), "level": record.level, "needs_human": record.needs_human}
        )
        print(
            f"    -> {record.status} level={record.level} clean={record.clean} "
            f"failures={len(record.checker_failures)} "
            f"manual={len(record.manual_checks)} "
            f"sr={len(record.screen_reader_errors)} "
            f"elapsed={record.elapsed_seconds}s",
            flush=True,
        )

    # --- Burndown summary -------------------------------------------------
    vision_enabled = fix_config is not None
    summary = summarize_levels(
        summary_rows,
        vision_enabled=vision_enabled,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    summary_path = manifest_path.with_name(
        manifest_path.stem + "_levels_summary.json"
    )
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(
        f"Levels: {summary['totals']}  needs_human_total={summary['needs_human_total']}"
        f"  oversized_skipped={oversized_count}"
        f"  font_clause_residue={font_residue_count}",
        flush=True,
    )
    if not vision_enabled:
        print(
            "  NOTE: vision disabled — most files cap at L3. "
            "L3 = machine-verified PDF/UA-1, NOT 'ADA compliant' "
            "(that requires L5 human validation).",
            flush=True,
        )
    print(f"  summary -> {summary_path}", flush=True)

    print(f"Done. failures={failures} manifest={manifest_path}", flush=True)
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", action="append", default=[])
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_OUTPUT_ROOT / "corpus_acceptance_manifest.jsonl"),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="Disable vision during both fixing and acceptance. Useful for "
             "deterministic pre-RunPod corpus prep.",
    )
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Baseline mode: classify each SOURCE file's as-is L0–L4 level "
             "without remediating or writing outputs.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--max-pages", type=int, default=200,
        help="Skip PDFs with more than this many pages to the manual queue (0=off).",
    )
    parser.add_argument(
        "--max-mb", type=float, default=30.0,
        help="Skip PDFs larger than this many MB to the manual queue (0=off).",
    )
    parser.add_argument(
        "--per-file-timeout", type=int, default=900,
        help="Best-effort per-file wall-clock cap in seconds (0=disabled).",
    )
    parser.add_argument(
        "--shard-index", type=int, default=0,
        help="This worker's shard number (0-based). Use with --shard-count.",
    )
    parser.add_argument(
        "--shard-count", type=int, default=1,
        help="Total number of parallel shards. Worker takes files where "
             "index %% shard_count == shard_index.",
    )
    args = parser.parse_args()
    if not args.input_root:
        args.input_root = [str(root) for root in DEFAULT_INPUT_ROOTS]
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
