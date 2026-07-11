"""Stability-voting, deterministic-prominence rescue, and the generalized
failure-driven refix loop.

The heading vision adapter is the weakest-trained of the five and acts as both
detector and verifier, so a single pass flags *different* headings run-to-run
(the retag conversion plateaued at ~20%/pass). These tests drive:

  Part A — consensus voting: run the analyzer N times and apply only the
           retag decisions that recur across a majority of runs.
  Part B — a vision-free rescue that assigns heading *level* from deterministic
           visual prominence (font size / weight), which the noisy adapter is
           worst at, gated by the same safe-retag guard.
  Part C — a rule-agnostic refix registry so heading / alt-text / untagged
           handlers all plug into one failure-driven dispatch.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pikepdf
from pikepdf import Array, Dictionary, Name

import project_remedy.pdf_fixer as PF
from project_remedy import heading_feedback as HF
from project_remedy.pdf_vision import HeadingIssue


def _issue(page=1, current_tag="P", correct_tag="H1", text="Annual Program Review",
           element_index=None, severity="error"):
    return HeadingIssue(
        page=page, description="title/section heading is tagged as body text",
        severity=severity, suggestion=f"Retag as {correct_tag}",
        element_index=element_index, current_tag=current_tag,
        correct_tag=correct_tag, text=text,
    )


# --- Part A: stability voting ------------------------------------------------

def test_decision_key_is_stable_across_runs_ignoring_element_index():
    """Two passes flag the same visible heading but the model re-numbers the
    element each time — the voting identity must be the same regardless."""
    a = _issue(element_index=3)
    b = _issue(element_index=7)   # same page/tags/text, different model index
    assert HF.heading_decision_key(a) == HF.heading_decision_key(b)
    assert HF.heading_decision_key(a) is not None


def test_decision_key_none_for_non_actionable_issues():
    assert HF.heading_decision_key(_issue(severity="warning")) is None
    # no target tag derivable
    no_target = HeadingIssue(page=1, description="", severity="error", correct_tag="",
                             suggestion="", current_tag="P", text="x")
    assert HF.heading_decision_key(no_target) is None


def test_consensus_keeps_only_majority_agreed_retags():
    stable = _issue(text="Annual Program Review", element_index=3)
    noise = _issue(text="Fleeting Sidebar Label", correct_tag="H2", element_index=9)
    runs = [
        [stable, noise],       # run 1: both
        [_issue(text="Annual Program Review", element_index=5)],  # run 2: stable only
        [_issue(text="Annual Program Review", element_index=2)],  # run 3: stable only
    ]
    kept = HF.consensus_heading_issues(runs, threshold=2)
    texts = {i.text for i in kept}
    assert "Annual Program Review" in texts, "3/3 agreement must survive"
    assert "Fleeting Sidebar Label" not in texts, "1/3 noise must be dropped"


def test_consensus_counts_each_key_once_per_run():
    """A run that repeats the same decision twice still counts as one vote."""
    dup = _issue(text="Same Title")
    runs = [[dup, _issue(text="Same Title", element_index=2)], [_issue(text="Other")]]
    kept = HF.consensus_heading_issues(runs, threshold=2)
    assert kept == [], "one run agreeing twice is still a single vote < threshold"


def test_consensus_representative_prefers_indexed_richer_issue():
    runs = [
        [_issue(text="T", element_index=None)],
        [_issue(text="T", element_index=4)],
    ]
    kept = HF.consensus_heading_issues(runs, threshold=2)
    assert len(kept) == 1
    assert kept[0].element_index == 4, "representative should keep the located index"


# --- Part A wiring into fix_heading_hierarchy_quality ------------------------

def _doc_two_p(pdf_content):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pg = pdf.pages[0].obj
    pg.Contents = pdf.make_stream(pdf_content)
    elems = [
        pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/P"), Pg=pg, K=0)),
        pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/P"), Pg=pg, K=1)),
    ]
    doc = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Document"),
                                       K=Array(elems)))
    for e in elems:
        e.P = doc
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        Dictionary(Type=Name("/StructTreeRoot"), K=Array([doc])))
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return pdf, elems


_CONTENT = (
    b"/P <</MCID 0>> BDC\n"
    b"BT /F1 24 Tf 72 720 Td (Annual Program Review) Tj ET\nEMC\n"
    b"/P <</MCID 1>> BDC\n"
    b"BT /F1 11 Tf 72 690 Td (Fleeting Sidebar Label) Tj ET\nEMC\n"
)


def test_voting_applies_only_consensus_retag(monkeypatch):
    pdf, elems = _doc_two_p(_CONTENT)
    monkeypatch.setenv("PDF_HEADING_VOTE_ROUNDS", "3")
    monkeypatch.setenv("PDF_HEADING_VOTE_THRESHOLD", "2")
    monkeypatch.setattr(PF, "_extract_mcid_text",
                        lambda page: {0: "Annual Program Review", 1: "Fleeting Sidebar Label"})

    import project_remedy.pdf_vision as PV
    monkeypatch.setattr(
        PV, "VisionAnalyzer",
        lambda provider: SimpleNamespace(analyze_heading_hierarchy=lambda *a, **k: None))

    nodes = PF._page_structure_nodes_for_vision_order(pdf, 0)
    i0 = next(i for i, n in enumerate(nodes) if n.objgen == elems[0].objgen) + 1
    i1 = next(i for i, n in enumerate(nodes) if n.objgen == elems[1].objgen) + 1

    def stable():
        return _issue(text="Annual Program Review", current_tag="P",
                      correct_tag="H1", element_index=i0)

    # node 0 flagged every round (stable); node 1 flagged once (noise).
    runs = [
        [stable(), _issue(text="Fleeting Sidebar Label", current_tag="P",
                          correct_tag="H2", element_index=i1)],
        [stable()],
        [stable()],
    ]
    seq = iter(runs)
    monkeypatch.setattr(PF, "_run_async_callable_blocking",
                        lambda func, *a, **k: SimpleNamespace(heading_issues=next(seq)))

    PF.fix_heading_hierarchy_quality(pdf, vision_provider=object(), force_pages=[0])

    assert PF._get_struct_type(elems[0]) == "H1", "3/3-agreed heading retagged"
    assert PF._get_struct_type(elems[1]) == "P", "1/3 noise heading left untouched"


# --- Part B: deterministic-prominence rescue (vision-free) -------------------

import pytest  # noqa: E402


def _norm(text):
    return PF._normalize_extracted_text(text).lower()


def _prominence_pdf(path):
    """Real PDF (reportlab): a 24pt title, a 16pt subhead, several 11pt body
    lines so body is the modal size. fitz reads real font sizes off this."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(72, 740, "Annual Program Review")
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, 700, "Enrollment Summary")
    c.setFont("Helvetica", 11)
    for i, y in enumerate(range(660, 500, -20)):
        c.drawString(72, y, f"Body sentence number {i} with ordinary running text.")
    c.showPage()
    c.save()


def test_deterministic_heading_levels_ranks_by_font_size(tmp_path):
    path = tmp_path / "prom.pdf"
    _prominence_pdf(path)

    levels = HF.deterministic_heading_levels(path, 0)

    assert levels.get(_norm("Annual Program Review")) == 1, "largest = H1"
    assert levels.get(_norm("Enrollment Summary")) == 2, "second-largest = H2"
    assert not any("body sentence" in k for k in levels), "modal body text is not a heading"


def test_prominence_rescue_retags_matching_node_via_guard(tmp_path, monkeypatch):
    pdf, elems = _doc_two_p(_CONTENT)
    path = tmp_path / "doc.pdf"
    pdf.save(path)

    monkeypatch.setattr(PF, "_extract_mcid_text",
                        lambda page: {0: "Annual Program Review", 1: "Fleeting Sidebar Label"})
    # Deterministic measurement is tested separately; here inject a level map.
    monkeypatch.setattr(HF, "deterministic_heading_levels",
                        lambda p, idx: {_norm("Annual Program Review"): 1})

    changes = HF.apply_prominence_heading_rescue(path, [0])

    with pikepdf.open(path) as out:
        types = [PF._get_struct_type(n) for n, _d, _p in PF.walk_structure_tree(out)]
    assert "H1" in types, "prominence-detected heading retagged without any vision call"
    assert types.count("H1") == 1 and "P" in types, "only the detected line promoted"
    assert changes, "rescue reports the retag"


def _doc_p_and_td(pdf_content):
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pg = pdf.pages[0].obj
    pg.Contents = pdf.make_stream(pdf_content)
    p = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/P"), Pg=pg, K=0))
    td = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/TD"), Pg=pg, K=1))
    doc = pdf.make_indirect(Dictionary(Type=Name("/StructElem"), S=Name("/Document"),
                                       K=Array([p, td])))
    p.P = doc
    td.P = doc
    pdf.Root.StructTreeRoot = pdf.make_indirect(
        Dictionary(Type=Name("/StructTreeRoot"), K=Array([doc])))
    pdf.Root.MarkInfo = Dictionary(Marked=True)
    return pdf, p, td


def test_prominence_rescue_never_promotes_a_table_cell(tmp_path, monkeypatch):
    """A large-font TABLE CELL is visually prominent but must never become a
    heading — the safe-retag guard forbids TD -> H*."""
    content = (b"/P <</MCID 0>> BDC\nBT /F1 20 Tf 72 730 Td (Federal Data) Tj ET\nEMC\n"
               b"/TD <</MCID 1>> BDC\nBT /F1 20 Tf 72 700 Td (Big Cell Value) Tj ET\nEMC\n")
    pdf, p, td = _doc_p_and_td(content)
    path = tmp_path / "table.pdf"
    pdf.save(path)

    monkeypatch.setattr(PF, "_extract_mcid_text",
                        lambda page: {0: "Federal Data", 1: "Big Cell Value"})
    monkeypatch.setattr(HF, "deterministic_heading_levels",
                        lambda p, idx: {_norm("Federal Data"): 1, _norm("Big Cell Value"): 1})

    HF.apply_prominence_heading_rescue(path, [0])

    with pikepdf.open(path) as out:
        by_mcid = {}
        for n, _d, _p in PF.walk_structure_tree(out):
            try:
                mcid = int(n.get("/K"))
            except (TypeError, ValueError):
                continue
            by_mcid[mcid] = PF._get_struct_type(n)
    assert by_mcid[0] == "H1", "the paragraph title is promoted"
    assert by_mcid[1] == "TD", "the table cell must stay a TD"


# --- Part C: generalized failure-driven refix registry ----------------------

def _fail(rule_id, details=None):
    return {"rule_id": rule_id, "description": "", "details": details or []}


def test_dispatch_runs_only_handlers_whose_rule_fired(monkeypatch):
    calls = []
    monkeypatch.setattr(PF, "apply_heading_retag_refix",
                        lambda p, **k: calls.append("heading") or ["h"])
    monkeypatch.setattr(PF, "_fix_missing_alt_text",
                        lambda pdf, vp: calls.append("alt") or 0)
    monkeypatch.setattr(PF, "fix_untagged_content",
                        lambda pdf: calls.append("untagged") or [])

    changes = HF.apply_failure_driven_refix(
        Path("/nonexistent.pdf"),
        vision_provider=object(),
        checker_failures=[_fail("headings-nesting",
                                ["Page 1: title (P -> H1) (Retag as H1)"])],
    )

    assert calls == ["heading"], "only the heading handler should fire"
    assert changes == ["h"]


def test_vision_handler_skipped_without_a_provider(monkeypatch):
    called = []
    monkeypatch.setattr(PF, "_fix_missing_alt_text",
                        lambda pdf, vp: called.append("alt") or 1)

    changes = HF.apply_failure_driven_refix(
        Path("/nonexistent.pdf"),
        vision_provider=None,
        checker_failures=[_fail("sr-figure-no-alt")],
    )

    assert called == [], "alt-text refix needs a vision provider — must be skipped"
    assert changes == []


def test_untagged_handler_is_deterministic_no_vision_needed(tmp_path, monkeypatch):
    pdf, _elems = _doc_two_p(_CONTENT)
    path = tmp_path / "u.pdf"
    pdf.save(path)
    monkeypatch.setattr(PF, "fix_untagged_content",
                        lambda pdf: ["Tagged 2 untagged content items"])

    changes = HF.apply_failure_driven_refix(
        path, vision_provider=None,
        checker_failures=[_fail("page-content-tagged")],
    )

    assert changes == ["Tagged 2 untagged content items"], \
        "untagged-content refix runs deterministically even with no vision provider"


def test_dispatch_aggregates_across_multiple_fired_rules(monkeypatch):
    monkeypatch.setattr(PF, "apply_heading_retag_refix", lambda p, **k: ["h"])
    monkeypatch.setattr(PF, "fix_untagged_content", lambda pdf: ["u"])

    changes = HF.apply_failure_driven_refix(
        Path("/nonexistent.pdf"),
        vision_provider=object(),
        checker_failures=[_fail("headings-nesting",
                                ["Page 1: t (P -> H1) (Retag as H1)"]),
                          _fail("page-content-tagged")],
    )

    assert set(changes) == {"h", "u"}, "changes from every fired handler are aggregated"


def test_dispatch_handler_error_is_isolated(monkeypatch):
    """One handler raising must not sink the others."""
    monkeypatch.setattr(PF, "apply_heading_retag_refix",
                        lambda p, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(PF, "fix_untagged_content", lambda pdf: ["u"])

    changes = HF.apply_failure_driven_refix(
        Path("/nonexistent.pdf"),
        vision_provider=object(),
        checker_failures=[_fail("headings-nesting",
                                ["Page 1: t (P -> H1) (Retag as H1)"]),
                          _fail("page-content-tagged")],
    )

    assert changes == ["u"], "a raising handler is isolated; others still apply"
