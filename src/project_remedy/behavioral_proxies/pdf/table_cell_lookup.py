"""PDF table-cell lookup behavioral proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from project_remedy.behavioral_proxies.shared.base import (
    BehavioralTestResult,
    require_unit_interval,
)
from project_remedy.behavioral_proxies.shared.llm_answering import (
    BehavioralAnswerer,
    score_answer_retention,
)
from project_remedy.behavioral_proxies.shared.question_generator import GeneratedQuestion
from project_remedy.tag_tree_reader import TagTreeReport, read_tag_tree


def _table_descendants(report: TagTreeReport, table_index: int) -> list:
    table = report.nodes[table_index]
    descendants = []
    for node in report.nodes[table_index + 1 :]:
        if node.depth <= table.depth:
            break
        descendants.append(node)
    return descendants


def _cell_text(descendants: list, start: int) -> str:
    """Aggregate a cell's own text plus the text of its descendant nodes.

    Cell content is frequently wrapped in child ``Span``/``P`` elements — this
    is valid PDF/UA structure and is exactly what a screen reader speaks when it
    lands on the cell. The tag-tree reader only attaches marked-content text to
    the node that directly owns the MCID, so a ``TH``/``TD`` whose text lives in
    a child ``Span`` reads as empty on the cell itself. Collect text from the
    whole cell subtree so wrapped cells are not mistaken for blank cells.
    """
    cell = descendants[start]
    parts: list[str] = []
    own = " ".join((cell.text or cell.alt_text or "").split())
    if own:
        parts.append(own)
    for node in descendants[start + 1 :]:
        if node.depth <= cell.depth:
            break
        text = " ".join((node.text or node.alt_text or "").split())
        if text:
            parts.append(text)
    return " ".join(parts)


def _iter_table_cells(descendants: list) -> list[tuple[str, str]]:
    """Return ``(tag, aggregated_text)`` for each ``TH``/``TD`` in the subtree."""
    cells: list[tuple[str, str]] = []
    for index, node in enumerate(descendants):
        if node.tag in ("TH", "TD"):
            cells.append((node.tag, _cell_text(descendants, index)))
    return cells


def score_table_cell_lookup_report(
    report: TagTreeReport,
    *,
    threshold: float = 0.95,
    answerer: BehavioralAnswerer | None = None,
    baseline_text: str = "",
    candidate_text: str = "",
) -> BehavioralTestResult:
    """Score whether tables expose enough structure for cell lookup."""
    table_indices = [index for index, node in enumerate(report.nodes) if node.tag == "Table"]
    findings: list[dict[str, Any]] = []
    if not table_indices:
        return BehavioralTestResult(
            test_name="table_cell_lookup",
            dimension="table_structure",
            format="pdf",
            passed=True,
            score=1.0,
            threshold=threshold,
            confidence=1.0,
            metadata={"applicable": False, "table_count": 0},
        )

    lookup_ready = 0
    blank_fillable_grids = 0
    lookup_questions: list[GeneratedQuestion] = []
    serialized_tables: list[str] = []
    for table_number, table_index in enumerate(table_indices, start=1):
        descendants = _table_descendants(report, table_index)
        has_rows = any(node.tag == "TR" for node in descendants)
        cells = _iter_table_cells(descendants)
        header_texts = [text for tag, text in cells if tag == "TH"]
        data_cell_texts = [text for tag, text in cells if tag == "TD"]
        has_headers = bool(header_texts)
        has_non_empty_headers = any(header_texts)
        has_data_cells = bool(data_cell_texts)
        has_non_empty_data_cells = any(data_cell_texts)
        has_cells = has_headers or has_data_cells
        rows = _table_rows(descendants)
        serialized = _serialize_table(rows)
        if serialized:
            serialized_tables.append(serialized)
        lookup_questions.extend(_lookup_questions(rows, table_number=table_number))
        if (
            has_rows
            and has_headers
            and has_non_empty_headers
            and has_data_cells
            and has_non_empty_data_cells
        ):
            lookup_ready += 1
            continue
        # Blank fillable grid: labeled headers and data-cell *nodes* present, but
        # every data cell is empty. This is an unfilled form (rating grid, blank
        # claim form) — structurally navigable, with no values to look up.
        # Remediation cannot invent data the source form lacks (these tables
        # score the same on the input), so it is not-applicable for cell lookup
        # rather than a structural failure. The guard requires the cell nodes to
        # be present, so a table whose cells were *dropped* by remediation
        # (has_data_cells is False) still fails instead of being excused.
        if (
            has_rows
            and has_non_empty_headers
            and has_data_cells
            and not has_non_empty_data_cells
        ):
            blank_fillable_grids += 1
            findings.append(
                {
                    "severity": "info",
                    "issue": "blank_fillable_grid",
                    "table_index": table_number,
                    "has_rows": has_rows,
                    "has_non_empty_headers": has_non_empty_headers,
                    "has_data_cells": has_data_cells,
                }
            )
            continue
        findings.append(
            {
                "severity": "error",
                "issue": "table_not_lookup_ready",
                "table_index": table_number,
                "has_rows": has_rows,
                "has_headers": has_headers,
                "has_non_empty_headers": has_non_empty_headers,
                "has_data_cells": has_data_cells,
                "has_non_empty_data_cells": has_non_empty_data_cells,
                "has_cells": has_cells,
            }
        )

    scorable_tables = len(table_indices) - blank_fillable_grids
    # All tables are blank fillable grids -> structure is sound, nothing to look
    # up. Score as passing structure (consistent with the no-tables branch).
    structural_score = 1.0 if scorable_tables <= 0 else lookup_ready / scorable_tables
    score = structural_score
    metadata: dict[str, Any] = {
        "applicable": True,
        "table_count": len(table_indices),
        "blank_fillable_grids": blank_fillable_grids,
        "scorable_tables": scorable_tables,
        "llm_answering_enabled": answerer is not None,
        "lookup_question_count": len(lookup_questions),
    }
    if answerer is not None and lookup_questions:
        candidate_context = candidate_text or "\n\n".join(serialized_tables)
        retention = score_answer_retention(
            questions=lookup_questions,
            baseline_context=baseline_text or candidate_context,
            candidate_context=candidate_context,
            answerer=answerer,
        )
        score = min(structural_score, retention.retention)
        findings.extend(retention.findings)
        metadata.update(
            {
                "baseline_accuracy": retention.baseline_accuracy,
                "candidate_accuracy": retention.candidate_accuracy,
                "answer_accuracy_retention": retention.retention,
            }
        )
    return BehavioralTestResult(
        test_name="table_cell_lookup",
        dimension="table_structure",
        format="pdf",
        passed=score >= threshold,
        score=round(score, 4),
        threshold=threshold,
        confidence=0.75,
        findings=findings,
        metadata=metadata,
    )


class PDFTableCellLookupTest:
    """Deterministic scaffold for the PRD table cell lookup proxy."""

    test_name = "table_cell_lookup"
    dimension = "table_structure"
    format = "pdf"

    def run(self, artifact_path: Path, **kwargs: Any) -> BehavioralTestResult:
        report = kwargs.get("tag_tree_report") or read_tag_tree(artifact_path)
        threshold = require_unit_interval("threshold", kwargs.get("threshold", 0.95))
        return score_table_cell_lookup_report(
            report,
            threshold=threshold,
            answerer=kwargs.get("answerer"),
            baseline_text=str(kwargs.get("baseline_text") or ""),
            candidate_text=str(kwargs.get("candidate_text") or ""),
        )


def _table_rows(descendants: list) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] | None = None
    for index, node in enumerate(descendants):
        if node.tag == "TR":
            current = []
            rows.append(current)
            continue
        if node.tag not in {"TH", "TD"}:
            continue
        text = _cell_text(descendants, index)
        if current is None:
            current = []
            rows.append(current)
        current.append((node.tag, text))
    return [row for row in rows if row]


def _lookup_questions(
    rows: list[list[tuple[str, str]]],
    *,
    table_number: int,
) -> list[GeneratedQuestion]:
    if len(rows) < 2:
        return []
    headers = [text for tag, text in rows[0] if tag == "TH" and text]
    if not headers:
        return []
    questions: list[GeneratedQuestion] = []
    for row_index, row in enumerate(rows[1:], start=1):
        values = [text for tag, text in row if tag in {"TH", "TD"}]
        if len(values) < 2:
            continue
        row_label = values[0]
        for column_index, expected in enumerate(values[1:], start=1):
            if not expected:
                continue
            header = headers[column_index] if column_index < len(headers) else f"column {column_index + 1}"
            questions.append(
                GeneratedQuestion(
                    question=(
                        f"In table {table_number}, what is the value for row "
                        f"{row_label}, column {header}?"
                    ),
                    expected_answer=expected,
                    source_dimension="table_structure",
                )
            )
    return questions[:5]


def _serialize_table(rows: list[list[tuple[str, str]]]) -> str:
    lines = []
    for row in rows:
        values = [text for _tag, text in row if text]
        if values:
            lines.append(" | ".join(values))
    return "\n".join(lines)
