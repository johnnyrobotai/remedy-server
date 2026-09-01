"""Deterministic image semantics when a vision provider is unavailable."""

from __future__ import annotations

from pathlib import Path
import zlib

import pikepdf
from pikepdf import Array, Dictionary, Name, String

import project_remedy.pdf_fixer as fixer
from project_remedy.pdf_checker import walk_structure_tree


def _write_figure_pdf(
    path: Path,
    *,
    page_text: str = "",
    alt_text: str = "",
    image_tag: str = "Figure",
    nested_in_paragraph: bool = False,
    image_size: tuple[int, int] = (32, 32),
) -> None:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(600, 200))

    width, height = image_size
    image = pdf.make_stream(zlib.compress(bytes([0] * width * height)))
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = width
    image["/Height"] = height
    image["/ColorSpace"] = Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    image["/Filter"] = Name("/FlateDecode")

    font = pdf.make_indirect(
        Dictionary(Type=Name("/Font"), Subtype=Name("/Type1"), BaseFont=Name("/Helvetica"))
    )
    page["/Resources"] = Dictionary(
        XObject=Dictionary(Im0=image),
        Font=Dictionary(F1=font),
    )
    text_block = (
        b"/P <</MCID 1>> BDC BT /F1 12 Tf 10 150 Td ("
        + page_text.encode("latin-1")
        + b") Tj ET EMC\n"
        if page_text
        else b""
    )
    page["/Contents"] = pdf.make_stream(
        f"/{image_tag} <</MCID 0>> BDC q 20 0 0 30 10 10 cm /Im0 Do Q EMC\n".encode()
        + text_block
    )
    page["/StructParents"] = 0

    figure_values = {
        "/Type": Name("/StructElem"),
        "/S": Name("/Figure"),
        "/Pg": page.obj,
        "/K": 0,
    }
    if alt_text:
        figure_values["/Alt"] = String(alt_text)
    figure = pdf.make_indirect(Dictionary(figure_values))

    paragraph = None
    if page_text:
        paragraph_k = Array([figure, 1]) if nested_in_paragraph else 1
        paragraph = pdf.make_indirect(
            Dictionary(
                Type=Name("/StructElem"),
                S=Name("/P"),
                Pg=page.obj,
                K=paragraph_k,
            )
        )

    document_kids = [paragraph] if nested_in_paragraph else [figure]
    if paragraph is not None and not nested_in_paragraph:
        document_kids.append(paragraph)
    document = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), K=Array(document_kids))
    )
    figure["/P"] = paragraph if nested_in_paragraph else document
    if paragraph is not None:
        paragraph["/P"] = document

    owners = Array([figure])
    if paragraph is not None:
        owners.append(paragraph)
    parent_tree = pdf.make_indirect(Dictionary(Nums=Array([0, pdf.make_indirect(owners)])))
    pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructTreeRoot"),
            K=Array([document]),
            ParentTree=parent_tree,
            ParentTreeNextKey=1,
        )
    )
    pdf.Root["/MarkInfo"] = Dictionary(Marked=True)
    pdf.save(path)
    pdf.close()


def _figures(pdf: pikepdf.Pdf) -> list[pikepdf.Dictionary]:
    return [
        node
        for node, _depth, _parent in walk_structure_tree(pdf)
        if str(node.get("/S", "")) == "/Figure"
    ]


def _add_second_named_figure(path: Path, alt_text: str) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        existing = page["/Contents"].read_bytes()
        page["/Contents"] = pdf.make_stream(
            existing
            + b"/Figure <</MCID 2>> BDC q 20 0 0 30 60 10 cm /Im0 Do Q EMC\n"
        )
        document = next(
            node
            for node, _depth, _parent in walk_structure_tree(pdf)
            if str(node.get("/S", "")) == "/Document"
        )
        figure = pdf.make_indirect(
            Dictionary(
                Type=Name("/StructElem"),
                S=Name("/Figure"),
                Pg=page.obj,
                K=2,
                Alt=String(alt_text),
                P=document,
            )
        )
        document["/K"] = Array([figure, *list(document["/K"])])
        owners = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
        owners.append(figure)
        pdf.save(path)


def test_redundant_creative_commons_icon_becomes_artifact(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "redundant-cc.pdf"
    _write_figure_pdf(
        path,
        page_text="Licensed under a Creative Commons Attribution-NonCommercial 4.0 license.",
        alt_text="Creative Commons logo",
        image_tag="Span",
    )
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "CC")

    with pikepdf.open(path) as pdf:
        changes = fixer.fix_figures_alt_text(pdf, vision_provider=None)

        assert _figures(pdf) == []
        assert b"/Artifact BMC" in pdf.pages[0]["/Contents"].read_bytes()
        owners = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
        assert owners[0] is None
        assert any("Creative Commons" in change and "Artifactized" in change for change in changes)


def test_creative_commons_icon_without_text_equivalent_stays_figure(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "nonredundant-cc.pdf"
    _write_figure_pdf(path, page_text="Copyright 2020 Example College.", alt_text="Creative Commons logo")
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "CC")

    with pikepdf.open(path) as pdf:
        fixer.fix_figures_alt_text(pdf, vision_provider=None)

        assert len(_figures(pdf)) == 1


def test_creative_commons_alt_without_ocr_confirmation_stays_figure(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "unconfirmed-cc.pdf"
    _write_figure_pdf(
        path,
        page_text="Licensed under a Creative Commons Attribution 4.0 license.",
        alt_text="Creative Commons logo",
    )
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    with pikepdf.open(path) as pdf:
        fixer.fix_figures_alt_text(pdf, vision_provider=None)

        assert len(_figures(pdf)) == 1


def test_cc_detection_uses_pdf_text_when_mcid_decoder_cannot_read_license(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "cc-font-fallback.pdf"
    _write_figure_pdf(
        path,
        page_text="Licensed under a Creative Commons Attribution-NonCommercial 4.0 license.",
        alt_text="Creative Commons logo",
    )
    monkeypatch.setattr(fixer, "_extract_mcid_text", lambda _page: {})
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "CC")

    with pikepdf.open(path) as pdf:
        fixer.fix_figures_alt_text(pdf, vision_provider=None)

        assert _figures(pdf) == []


def test_captioned_portrait_uses_person_name_without_vision(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "portrait.pdf"
    _write_figure_pdf(
        path,
        page_text="Katie Nelson is an instructor of anthropology.",
        nested_in_paragraph=True,
        image_size=(30, 45),
    )
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    with pikepdf.open(path) as pdf:
        fixer.fix_figures_alt_text(pdf, vision_provider=None)

        assert str(_figures(pdf)[0]["/Alt"]) == "Portrait of Katie Nelson."


def test_portrait_uses_following_biography_text_without_vision(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "portrait-with-sibling-bio.pdf"
    _write_figure_pdf(
        path,
        page_text=(
            "ABOUT THE AUTHORS Katie Nelson is an instructor of anthropology. "
            "Lara Braff is an instructor of anthropology."
        ),
        image_size=(30, 45),
    )
    _add_second_named_figure(path, "Image of Lara Braff")
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    with pikepdf.open(path) as pdf:
        fixer.fix_figures_alt_text(pdf, vision_provider=None)

        alts = [str(figure.get("/Alt", "")) for figure in _figures(pdf)]
        assert "Portrait of Katie Nelson." in alts
        assert "Image of Lara Braff" in alts


def test_uncaptioned_photo_without_vision_requires_manual_review(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "uncaptioned.pdf"
    _write_figure_pdf(path, image_size=(45, 30))
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    with pikepdf.open(path) as pdf:
        changes = fixer.fix_figures_alt_text(pdf, vision_provider=None)
        fixer.fix_alt_text_elements(pdf)

        assert _figures(pdf)[0].get("/Alt") is None
        assert any("manual review" in change.lower() for change in changes)


def test_failed_vision_request_requires_manual_review(
    tmp_path: Path,
    monkeypatch,
):
    class FailingVisionProvider:
        async def analyze_image(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    path = tmp_path / "vision-failed.pdf"
    _write_figure_pdf(path, image_size=(45, 30))
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    with pikepdf.open(path) as pdf:
        changes = fixer.fix_figures_alt_text(
            pdf,
            vision_provider=FailingVisionProvider(),
        )

        assert _figures(pdf)[0].get("/Alt") is None
        assert any("manual review" in change.lower() for change in changes)


def test_fix_report_exposes_manual_alt_review_status(
    tmp_path: Path,
    monkeypatch,
):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    _write_figure_pdf(source, image_size=(45, 30))
    monkeypatch.setattr(fixer, "_ocr_text_from_image", lambda *_args, **_kwargs: "")

    report = fixer.fix_all(source, output, only="alt-figures")

    assert report.needs_manual_review is True
    assert "alt text" in report.manual_review_reason.lower()


def test_final_alt_validation_clears_resolved_manual_review(tmp_path: Path):
    output = tmp_path / "resolved.pdf"
    _write_figure_pdf(output, alt_text="Portrait of Katie Nelson.")
    report = fixer.FixReport(
        input_path=output,
        output_path=output,
        needs_manual_review=True,
        manual_review_reason="Manual review required for 1 figure alt text",
    )

    fixer._reconcile_manual_alt_review(report)

    assert report.needs_manual_review is False
    assert report.manual_review_reason == ""
    assert any("Resolved manual alt-text review" in change for change in report.changes)


def test_final_alt_validation_retains_unresolved_manual_review(tmp_path: Path):
    output = tmp_path / "unresolved.pdf"
    _write_figure_pdf(output)
    report = fixer.FixReport(
        input_path=output,
        output_path=output,
        needs_manual_review=True,
        manual_review_reason="Manual review required for 1 figure alt text",
    )

    fixer._reconcile_manual_alt_review(report)

    assert report.needs_manual_review is True
    assert "alt text" in report.manual_review_reason.lower()


def test_final_alt_validation_does_not_clear_visual_review(tmp_path: Path):
    output = tmp_path / "visual-review.pdf"
    _write_figure_pdf(output, alt_text="Portrait of Katie Nelson.")
    report = fixer.FixReport(
        input_path=output,
        output_path=output,
        needs_manual_review=True,
        manual_review_reason="Visual diff 30.0% exceeds 25% threshold",
    )

    fixer._reconcile_manual_alt_review(report)

    assert report.needs_manual_review is True
    assert report.manual_review_reason.startswith("Visual diff")
