"""Regression: pruning dead leaves must not delete empty table GRID CELLS.

`_prune_dead_and_empty_nodes` removes any leaf struct node with no live content.
On the LAMC 2008 monthly calendars that is catastrophic, because a calendar is a
39x33 grid in which most cells are legitimately BLANK.

The failure is a cascade, not a single deletion. Every /TD in those files wraps a
/Normal child, so Case 3 (`_has_struct_children`) protects the cell on the first
pass. Then the blank cell's inner /Normal leaf is pruned as dead, the /TD becomes
childless, and the NEXT pass deletes the /TD itself. The pass is multi-pass
(max_passes=10), so the cascade runs to completion within one call.

Two harms:
  1. A table cell is POSITIONAL. In a grid, a blank cell holds a column
     coordinate -- "this Tuesday has no event" is meaning carried by position.
     Delete it and every cell to its right shifts, silently corrupting the
     row/column correspondence a screen reader announces.
  2. `_prune_dead_and_empty_nodes` then calls `fix_table_regularity` to
     "re-enforce" the regularity it just destroyed, which FABRICATES /ColSpan to
     pad the mutilated rows back to width. On source SEP-08 (which has no spans
     at all) one call produced /ColSpan 2 on 28 cells; before the runaway clamp
     in 2d01a8b that padding ratcheted to 7,208,595 in a delivered file.

Measured on the pristine source before this fix:
    BEFORE prune: TD=1287 TR=39  ColSpan: none
    AFTER  prune: TD=1255 TR=39  ColSpan: {2: 28}   (52 nodes removed)

An empty table cell is structurally live even when it is contentless.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name

import project_remedy.pdf_fixer as PF


def _grid_pdf(nrows, ncols, blank_cells):
    """A regular nrows x ncols table. Each /TD wraps a /Normal child.

    blank_cells: set of (row, col) whose /Normal child references a DEAD mcid --
    i.e. a legitimately empty grid cell, exactly the calendar's blank days. Every
    other cell gets a real marked-content sequence with visible text.
    """
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pg = pdf.pages[0].obj
    pdf.Root.MarkInfo = Dictionary(Marked=True)

    content = b""
    mcid = 0
    dead_mcid = 9000  # never emitted into the content stream
    trs = []
    for r in range(nrows):
        cells = []
        for c in range(ncols):
            if (r, c) in blank_cells:
                inner_k = dead_mcid
                dead_mcid += 1
            else:
                content += (
                    b"/P <</MCID %d>> BDC BT /F1 10 Tf 10 700 Td (x) Tj ET EMC\n" % mcid
                )
                inner_k = mcid
                mcid += 1

            inner = pdf.make_indirect(Dictionary(
                Type=Name("/StructElem"), S=Name("/Normal"), Pg=pg, K=inner_k))
            cell = pdf.make_indirect(Dictionary(
                Type=Name("/StructElem"), S=Name("/TD"), Pg=pg, K=Array([inner])))
            inner.P = cell
            cells.append(cell)

        tr = pdf.make_indirect(Dictionary(
            Type=Name("/StructElem"), S=Name("/TR"), Pg=pg, K=Array(cells)))
        for cell in cells:
            cell.P = tr
        trs.append(tr)

    pg.Contents = pdf.make_stream(content)
    tbody = pdf.make_indirect(Dictionary(
        Type=Name("/StructElem"), S=Name("/TBody"), Pg=pg, K=Array(trs)))
    for tr in trs:
        tr.P = tbody
    table = pdf.make_indirect(Dictionary(
        Type=Name("/StructElem"), S=Name("/Table"), Pg=pg, K=Array([tbody])))
    tbody.P = table
    doc = pdf.make_indirect(Dictionary(
        Type=Name("/StructElem"), S=Name("/Document"), K=Array([table])))
    table.P = doc
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        Dictionary(Type=Name("/StructTreeRoot"), K=Array([doc])))
    return pdf


def _count(pdf, stype):
    n = 0
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and str(obj.get("/S")) == stype:
            n += 1
    return n


def _spans(pdf):
    out = []
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and str(obj.get("/S")) in ("/TD", "/TH"):
            v = obj.get("/ColSpan")
            attrs = obj.get("/A")
            if v is None and isinstance(attrs, pikepdf.Dictionary):
                v = attrs.get("/ColSpan")
            if v is not None:
                out.append(int(v))
    return out


def test_prune_preserves_blank_grid_cells():
    """A blank cell holds a column position; pruning it corrupts the grid."""
    # Row 1 is entirely blank -- the calendar week with no events.
    blanks = {(1, 0), (1, 1), (1, 2), (1, 3), (2, 2)}
    pdf = _grid_pdf(3, 4, blanks)
    assert _count(pdf, "/TD") == 12

    PF._prune_dead_and_empty_nodes(pdf)

    assert _count(pdf, "/TD") == 12, (
        "pruning deleted blank grid cells; every cell to their right now shifts "
        "column, corrupting the row/column correspondence"
    )


def test_prune_does_not_fabricate_colspan_on_a_regular_grid():
    """The grid is regular; the repair must have nothing to 'fix'."""
    blanks = {(1, 0), (1, 1), (1, 2), (1, 3), (2, 2)}
    pdf = _grid_pdf(3, 4, blanks)

    PF._prune_dead_and_empty_nodes(pdf)

    assert _spans(pdf) == [], (
        f"prune destroyed the grid then fabricated spans to hide it: {_spans(pdf)}"
    )
