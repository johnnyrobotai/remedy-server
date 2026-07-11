"""Regression tests for descendant-aware table cell text extraction.

The tag-tree reader only attaches marked-content text to the struct node that
directly owns the MCID. Real-world (and Adobe/AI-authored) tables wrap each
cell's text in a child ``Span``/``P`` element, which is valid PDF/UA. The table
cell-lookup proxy must read the whole cell subtree, otherwise every wrapped
table is scored 0.0 ("table_not_lookup_ready") even though a screen reader
speaks the cell content correctly.
"""

from __future__ import annotations

from pathlib import Path

from project_remedy.behavioral_proxies.pdf.table_cell_lookup import (
    score_table_cell_lookup_report,
)
from project_remedy.tag_tree_reader import TagNode, TagTreeReport


def _node(tag: str, depth: int, text: str = "") -> TagNode:
    return TagNode(
        tag=tag,
        depth=depth,
        page=0,
        text=text,
        alt_text="",
        lang="",
        children_count=0,
        has_content=bool(text),
    )


def _report(nodes: list[TagNode]) -> TagTreeReport:
    return TagTreeReport(
        file_path=Path("synthetic.pdf"),
        page_count=1,
        has_structure_tree=True,
        nodes=nodes,
    )


def _wrapped_table() -> TagTreeReport:
    """Table whose cell text lives one level down in a Span (valid PDF/UA)."""
    return _report(
        [
            _node("Table", 1),
            _node("TR", 2),
            _node("TH", 3),
            _node("Span", 4, "Date"),
            _node("TH", 3),
            _node("Span", 4, "Amount"),
            _node("TR", 2),
            _node("TD", 3),
            _node("Span", 4, "2024-01-01"),
            _node("TD", 3),
            _node("Span", 4, "$50.00"),
        ]
    )


def test_wrapped_cells_are_lookup_ready() -> None:
    result = score_table_cell_lookup_report(_wrapped_table())
    assert result.score == 1.0, result.findings
    assert result.passed is True


def test_direct_text_cells_still_pass() -> None:
    """Cells that carry their own text must keep passing (no regression)."""
    report = _report(
        [
            _node("Table", 1),
            _node("TR", 2),
            _node("TH", 3, "Date"),
            _node("TH", 3, "Amount"),
            _node("TR", 2),
            _node("TD", 3, "2024-01-01"),
            _node("TD", 3, "$50.00"),
        ]
    )
    result = score_table_cell_lookup_report(report)
    assert result.score == 1.0
    assert result.passed is True


def test_genuinely_empty_cells_still_fail() -> None:
    """A blank-form table with empty data cells must not be scored as ready."""
    report = _report(
        [
            _node("Table", 1),
            _node("TR", 2),
            _node("TH", 3),
            _node("Span", 4, "Date"),
            _node("TH", 3),
            _node("Span", 4, "Amount"),
            _node("TR", 2),
            _node("TD", 3),  # empty cell, no descendant text
            _node("TD", 3),  # empty cell, no descendant text
        ]
    )
    result = score_table_cell_lookup_report(report)
    assert result.score == 0.0
    assert result.passed is False
    finding = result.findings[0]
    assert finding["issue"] == "table_not_lookup_ready"
    assert finding["has_non_empty_headers"] is True
    assert finding["has_non_empty_data_cells"] is False


# --- Blank fillable grid semantics -----------------------------------------
# An unfilled form grid (labeled headers, data-cell nodes present but empty) is
# structurally navigable: there are no values to look up, so it is
# not-applicable for cell lookup rather than a structural failure. The guard
# requires the data-cell nodes to be present, so a grid whose cells were
# *dropped* by remediation still fails.


def _blank_rating_grid() -> TagTreeReport:
    """Labeled headers, data-cell nodes present but text-empty (unfilled form)."""
    return _report(
        [
            _node("Table", 1),
            _node("TR", 2),
            _node("TH", 3, "Excellent"),
            _node("TH", 3, "Good"),
            _node("TH", 3, "Fair"),
            _node("TR", 2),
            _node("TD", 3, ""),
            _node("TD", 3, ""),
            _node("TD", 3, ""),
        ]
    )


def _headers_only_no_cells() -> TagTreeReport:
    """Labeled headers but the data-cell nodes are gone (dropped by remediation)."""
    return _report(
        [
            _node("Table", 1),
            _node("TR", 2),
            _node("TH", 3, "Excellent"),
            _node("TH", 3, "Good"),
            _node("TH", 3, "Fair"),
        ]
    )


def test_blank_fillable_grid_is_not_a_failure():
    result = score_table_cell_lookup_report(_blank_rating_grid(), threshold=0.75)
    assert result.metadata["blank_fillable_grids"] == 1
    assert result.metadata["scorable_tables"] == 0
    assert result.score == 1.0
    assert result.passed
    assert not any(f["severity"] == "error" for f in result.metadata.get("findings", []))


def test_dropped_data_cells_still_fail():
    # No TD nodes at all -> cells were dropped, not a blank form -> must fail.
    result = score_table_cell_lookup_report(_headers_only_no_cells(), threshold=0.75)
    assert result.metadata["blank_fillable_grids"] == 0
    assert result.score == 0.0
    assert not result.passed


def test_blank_grid_does_not_mask_a_real_failing_table():
    # One blank grid (N/A) + one genuinely broken data table (has values in the
    # source sense but no headers) -> score reflects only the scorable table.
    nodes = _blank_rating_grid().nodes + [
        _node("Table", 1),
        _node("TR", 2),
        _node("TD", 3, "42"),  # data with no header row -> not lookup_ready
    ]
    result = score_table_cell_lookup_report(_report(nodes), threshold=0.75)
    assert result.metadata["blank_fillable_grids"] == 1
    assert result.metadata["scorable_tables"] == 1
    assert result.score == 0.0
    assert not result.passed
