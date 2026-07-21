"""Regression: `_prune_dead_and_empty_nodes` must keep tables regular.

Pruning dead/empty table cells (dangling MCRs) leaves rows with unequal column
counts, which veraPDF flags as 7.2-42/43 ("Table rows shall have the same number
of columns"). This introduced 7.2-42/43 on delivered ISS reports / catalog addenda
whose tables were otherwise fully content-recovered.

The INVARIANT above is still the contract. What changed is how it is satisfied.

Originally the pruner deleted the dead cell and then called `fix_table_regularity`
to rebuild the width it had just destroyed -- which padded the short row with a
FABRICATED /ColSpan. That workaround was the origin of the runaway span: on the
39x33 LAMC calendars it manufactured spans on a table whose source has none, and
before the 2d01a8b clamp it ratcheted to /ColSpan 7,208,595 in a delivered file.

A table cell is POSITIONAL -- a blank or dangling cell still holds a column
coordinate -- so `_TABLE_GRID_TYPES` is now exempt from pruning. The row never
becomes short, so nothing needs re-widening and no span is invented. The end state
is a genuine 2x2 grid instead of one cell claiming to span two columns.

Verified on the real files: source SEP-08/MAY-08 re-remediate to TD=1254 + TH=33
(= the source's 1287 cells exactly), 39 rows, zero /ColSpan, and both PASS
`verapdf -f ua1` with no failed clauses -- including no 7.2-42/43 and nothing
raised by the retained dangling MCR.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Array, Dictionary, Name

import project_remedy.pdf_fixer as PF


# One live cell per drawn MCID; MCID 3 is intentionally absent (dead cell).
CONTENT = (
    b"/TD <</MCID 0>> BDC BT /F1 10 Tf 10 700 Td (a) Tj ET EMC\n"
    b"/TD <</MCID 1>> BDC BT /F1 10 Tf 40 700 Td (b) Tj ET EMC\n"
    b"/TD <</MCID 2>> BDC BT /F1 10 Tf 10 680 Td (c) Tj ET EMC\n"
)


def _table_pdf():
    """1-page PDF: Document -> Table -> [TR(TD0,TD1), TR(TD2,TD3)]; TD3 is dead."""
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pg = pdf.pages[0].obj
    pg.Contents = pdf.make_stream(CONTENT)

    def td(mcid):
        return pdf.make_indirect(Dictionary(
            Type=Name("/StructElem"), S=Name("/TD"), Pg=pg, K=mcid))

    tr1_cells = [td(0), td(1)]
    tr2_cells = [td(2), td(3)]  # td(3) references absent MCID 3 -> dead
    tr1 = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/TR"),
                                       Pg=pg, K=Array(tr1_cells)))
    tr2 = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/TR"),
                                       Pg=pg, K=Array(tr2_cells)))
    for c in tr1_cells: c.P = tr1
    for c in tr2_cells: c.P = tr2
    table = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Table"),
                                         Pg=pg, K=Array([tr1, tr2])))
    tr1.P = table; tr2.P = table
    doc = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Document"),
                                       K=Array([table])))
    table.P = doc
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        Dictionary(Type=Name("/StructTreeRoot"), K=Array([doc])))
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return pdf, table


def _table_cells(table):
    return sum(
        1
        for tr in table.K
        for c in tr.K
        if isinstance(c, pikepdf.Dictionary) and str(c.get("/S")) in ("/TD", "/TH")
    )


def _row_widths(table):
    """Column count per row, counting each cell's /ColSpan."""
    widths = []
    for tr in table.K:
        w = 0
        for c in tr.K:
            if isinstance(c, pikepdf.Dictionary) and str(c.get("/S")) in ("/TD", "/TH"):
                span = c.get("/ColSpan")
                attrs = c.get("/A")
                if span is None and isinstance(attrs, pikepdf.Dictionary):
                    span = attrs.get("/ColSpan")
                w += int(span) if span is not None else 1
        widths.append(w)
    return widths


def test_table_stays_regular_after_pruning():
    """The 7.2-42/43 invariant: every row ends the same width."""
    pdf, table = _table_pdf()
    assert _row_widths(table) == [2, 2]  # sanity: starts as a regular 2x2

    PF._prune_dead_and_empty_nodes(pdf)

    assert len(set(_row_widths(table))) == 1, (
        f"rows ended with unequal column counts (veraPDF 7.2-42/43): "
        f"{_row_widths(table)}"
    )


def test_dead_cell_is_preserved_rather_than_pruned_and_padded():
    """A dangling cell holds a column position; keep it instead of inventing a span.

    The old behaviour deleted this cell and re-widened the row with a fabricated
    /ColSpan. Both halves of that trade are wrong: the grid loses a coordinate and
    the surviving cell lies about its width.
    """
    pdf, table = _table_pdf()

    PF._prune_dead_and_empty_nodes(pdf)

    assert _table_cells(table) == 4, "the positional grid cell was pruned"
    spans = [
        int(c.get("/ColSpan"))
        for tr in table.K
        for c in tr.K
        if isinstance(c, pikepdf.Dictionary) and c.get("/ColSpan") is not None
    ]
    assert spans == [], f"a /ColSpan was fabricated to hide the deletion: {spans}"
