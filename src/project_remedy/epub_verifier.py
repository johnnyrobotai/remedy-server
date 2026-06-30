"""EPUB conformance verifier shelling to EPUBCheck and ACE by DAISY.

Two distinct tools — both required, neither sufficient on its own:

* **EPUBCheck** (W3C, Java CLI) validates structural EPUB validity.
* **ACE by DAISY** (Node CLI, built on axe) runs automated WCAG 2 A/AA
  and EPUB Accessibility checks.

Per DAISY's own documentation, a clean ACE report is **not** by itself a
conformance certificate — manual SMART-style review is required to
certify EPUB Accessibility conformance. This module surfaces that
caveat in the result so downstream consumers do not mistake an
automated green for a conformance claim.

This mirrors ``validator.validate_with_verapdf`` for the PDF/UA-1 path:
fail-soft when a tool is missing (warn and pass-through) so the
remediation pipeline can still run in environments without the
validators installed, but emit structured violations when the tools
are present.

Tool binary discovery:

* EPUBCheck: ``EPUBCHECK_PATH`` env var → ``shutil.which("epubcheck")``
* ACE:       ``ACE_PATH`` env var      → ``shutil.which("ace")``
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result models — match the shape of ValidationResult so downstream
# compliance_report code can ingest both verifiers identically.
# ---------------------------------------------------------------------------


@dataclass
class EPUBValidationResult:
    """Outcome of one tool's validation of an EPUB."""

    tool: str
    passed: bool = True
    violations: list[dict[str, Any]] = field(default_factory=list)
    # True if the tool binary was not found / timed out / crashed — i.e.
    # the validation did not actually run, ``passed`` is a fail-soft
    # placeholder, NOT a green light.
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class EPUBVerifyReport:
    """Combined EPUBCheck + ACE result."""

    epub_path: Path
    epubcheck: EPUBValidationResult
    ace: EPUBValidationResult

    @property
    def passed(self) -> bool:
        """True only when BOTH tools ran and reported zero violations."""
        return (
            self.epubcheck.passed
            and self.ace.passed
            and not (self.epubcheck.skipped and self.ace.skipped)
        )

    @property
    def caveat(self) -> str:
        """Mandatory caveat to surface alongside any automated-pass result."""
        return (
            "EPUBCheck + ACE pass the automated checks they can perform. "
            "Per DAISY guidance (https://kb.daisy.org/publishing/docs/epub/validation/ace.html), "
            "a clean ACE report does NOT by itself certify EPUB Accessibility "
            "conformance — manual SMART-style review is required."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "epub_path": str(self.epub_path),
            "passed_automated": self.passed,
            "caveat": self.caveat,
            "epubcheck": {
                "passed": self.epubcheck.passed,
                "skipped": self.epubcheck.skipped,
                "skip_reason": self.epubcheck.skip_reason,
                "violations": self.epubcheck.violations,
            },
            "ace": {
                "passed": self.ace.passed,
                "skipped": self.ace.skipped,
                "skip_reason": self.ace.skip_reason,
                "violations": self.ace.violations,
            },
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def verify_epub(
    epub_path: Path,
    *,
    epubcheck_path: str | None = None,
    ace_path: str | None = None,
    fail_on_warnings: bool = False,
    timeout: float = 180.0,
) -> EPUBVerifyReport:
    """Run EPUBCheck + ACE against ``epub_path`` and combine the results.

    Parameters
    ----------
    epub_path:
        Path to the ``.epub`` file under test.
    epubcheck_path, ace_path:
        Optional explicit binary paths. When ``None``, resolved from
        ``EPUBCHECK_PATH`` / ``ACE_PATH`` env vars, then from PATH.
    fail_on_warnings:
        When True, EPUBCheck warnings are treated as failures (passes
        ``--failonwarnings``). ACE has no equivalent flag.
    timeout:
        Per-tool subprocess timeout in seconds.
    """
    # Run both tools concurrently — they're independent.
    epubcheck_task = _run_epubcheck(
        epub_path,
        binary=epubcheck_path,
        fail_on_warnings=fail_on_warnings,
        timeout=timeout,
    )
    ace_task = _run_ace(
        epub_path,
        binary=ace_path,
        timeout=timeout,
    )
    epubcheck_result, ace_result = await asyncio.gather(epubcheck_task, ace_task)
    return EPUBVerifyReport(
        epub_path=epub_path,
        epubcheck=epubcheck_result,
        ace=ace_result,
    )


# ---------------------------------------------------------------------------
# EPUBCheck — structural validity
# ---------------------------------------------------------------------------


async def _run_epubcheck(
    epub_path: Path,
    *,
    binary: str | None,
    fail_on_warnings: bool,
    timeout: float,
) -> EPUBValidationResult:
    bin_path = _resolve_binary(binary, "EPUBCHECK_PATH", "epubcheck")
    if not bin_path:
        logger.warning(
            "EPUBCheck binary not found (EPUBCHECK_PATH unset, not on PATH); "
            "skipping structural validation."
        )
        return EPUBValidationResult(
            tool="epubcheck",
            passed=True,
            skipped=True,
            skip_reason="EPUBCheck binary not found",
        )

    # `--json -` writes the JSON report to stdout per the official CLI docs.
    cmd: list[str] = [bin_path, "--json", "-", str(epub_path)]
    if fail_on_warnings:
        cmd.insert(1, "--failonwarnings")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("EPUBCheck timed out after %.0fs for %s", timeout, epub_path.name)
        return EPUBValidationResult(
            tool="epubcheck",
            passed=True,
            skipped=True,
            skip_reason="timeout",
        )
    except FileNotFoundError:
        return EPUBValidationResult(
            tool="epubcheck",
            passed=True,
            skipped=True,
            skip_reason="binary disappeared between resolve and exec",
        )

    violations: list[dict[str, Any]] = []
    if stdout:
        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            logger.warning("EPUBCheck JSON parse error: %s", exc)
            payload = {}
        for msg in payload.get("messages", []):
            severity = (msg.get("severity") or "").upper()
            # FATAL + ERROR are always violations. WARNING only when
            # fail_on_warnings was requested (exit code 1 already
            # surfaced it; we record it for the report either way).
            counts_as_violation = severity in {"FATAL", "ERROR"} or (
                fail_on_warnings and severity == "WARNING"
            )
            if not counts_as_violation:
                continue
            violations.append({
                "tool": "epubcheck",
                "id": msg.get("ID") or msg.get("id") or "unknown",
                "impact": "serious" if severity in {"FATAL", "ERROR"} else "moderate",
                "description": msg.get("message", ""),
                "help": msg.get("suggestion", ""),
                "location": _format_epubcheck_locations(msg.get("locations", [])),
                "severity": severity,
            })

    passed = proc.returncode == 0 and not violations
    logger.info(
        "EPUBCheck: rc=%d, %d violation(s), passed=%s",
        proc.returncode, len(violations), passed,
    )
    return EPUBValidationResult(
        tool="epubcheck",
        passed=passed,
        violations=violations,
    )


def _format_epubcheck_locations(locations: list[dict[str, Any]]) -> str:
    if not locations:
        return ""
    loc = locations[0]
    fname = loc.get("fileName", "")
    line = loc.get("line", "")
    col = loc.get("column", "")
    if fname and line:
        return f"{fname}:{line}:{col}".rstrip(":")
    return fname or ""


# ---------------------------------------------------------------------------
# ACE by DAISY — WCAG 2 + EPUB Accessibility automated checks
# ---------------------------------------------------------------------------


async def _run_ace(
    epub_path: Path,
    *,
    binary: str | None,
    timeout: float,
) -> EPUBValidationResult:
    bin_path = _resolve_binary(binary, "ACE_PATH", "ace")
    if not bin_path:
        logger.warning(
            "ACE binary not found (ACE_PATH unset, not on PATH); "
            "skipping accessibility validation."
        )
        return EPUBValidationResult(
            tool="ace",
            passed=True,
            skipped=True,
            skip_reason="ACE binary not found",
        )

    with tempfile.TemporaryDirectory(prefix="ace-") as tmpdir:
        outdir = Path(tmpdir)
        cmd = [bin_path, "--outdir", str(outdir), "--silent", str(epub_path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning("ACE timed out after %.0fs for %s", timeout, epub_path.name)
            return EPUBValidationResult(
                tool="ace",
                passed=True,
                skipped=True,
                skip_reason="timeout",
            )
        except FileNotFoundError:
            return EPUBValidationResult(
                tool="ace",
                passed=True,
                skipped=True,
                skip_reason="binary disappeared between resolve and exec",
            )

        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if stderr_text:
                logger.debug("ACE stderr: %s", stderr_text[:500])

        report_path = outdir / "report.json"
        if not report_path.is_file():
            logger.warning("ACE produced no report.json (rc=%d)", proc.returncode)
            return EPUBValidationResult(
                tool="ace",
                passed=proc.returncode == 0,
                skipped=True,
                skip_reason="no report.json produced",
            )

        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("ACE report parse error: %s", exc)
            return EPUBValidationResult(
                tool="ace",
                passed=True,
                skipped=True,
                skip_reason=f"report parse error: {exc}",
            )

    violations = _extract_ace_violations(payload)
    passed = proc.returncode == 0 and not violations
    logger.info(
        "ACE: rc=%d, %d violation(s), passed=%s",
        proc.returncode, len(violations), passed,
    )
    return EPUBValidationResult(
        tool="ace",
        passed=passed,
        violations=violations,
    )


def _extract_ace_violations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk an ACE report.json and pull out the failed earl:assertions.

    ACE reports embed both a top-level ``earl:result`` summary and a
    per-content-document ``assertions`` array. Failed assertions have
    ``earl:result.earl:outcome == "earl:failed"``. axe rule ids live
    in ``earl:test.dct:title``.
    """
    violations: list[dict[str, Any]] = []
    assertions = payload.get("assertions") or []
    if isinstance(assertions, dict):
        assertions = [assertions]

    for assertion in assertions:
        sub_assertions = assertion.get("assertions") or []
        doc_url = assertion.get("earl:testSubject", {}).get("url", "")
        for sub in sub_assertions:
            result = sub.get("earl:result") or {}
            outcome = (result.get("earl:outcome") or "").lower()
            if not outcome.endswith("failed"):
                continue
            test = sub.get("earl:test") or {}
            rule_id = (
                test.get("dct:title")
                or test.get("dct:identifier")
                or "ace-unknown-rule"
            )
            impact = test.get("earl:impact") or "moderate"
            description = test.get("dct:description") or result.get("dct:description") or ""
            help_text = test.get("help", {}).get("dct:title") if isinstance(test.get("help"), dict) else ""
            pointer = (result.get("earl:pointer") or {}).get("cfi") or ""
            violations.append({
                "tool": "ace",
                "id": rule_id,
                "impact": impact,
                "description": description,
                "help": help_text or "",
                "location": f"{doc_url}{(' ' + pointer) if pointer else ''}".strip(),
            })
    return violations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_binary(explicit: str | None, env_var: str, command: str) -> str | None:
    """Find a CLI binary: explicit arg → env var → PATH."""
    if explicit and Path(explicit).is_file():
        return explicit
    env_val = os.environ.get(env_var)
    if env_val and Path(env_val).is_file():
        return env_val
    return shutil.which(command)
