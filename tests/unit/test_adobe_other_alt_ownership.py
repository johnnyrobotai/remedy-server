"""Regression coverage for Acrobat's ``Other elements alternate text`` rule.

The Pressbooks source associates the same image MCID with both a mixed-content
paragraph and a leaf Figure.  Its ParentTree chooses the paragraph, so Acrobat
sees non-figure image content and fails the rule even though the Figure has
meaningful alternate text.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from project_remedy.pdf_checker import PDFAccessibilityChecker, walk_structure_tree
from project_remedy.pdf_fixer import (
    _get_node_mcids,
    fix_image_struct_elems_retag,
    fix_xobject_bearing_text_elements,
    fix_page_retag,
)


def _write_duplicate_image_owner_pdf(path: Path) -> None:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    image = pdf.make_stream(b"\x00")
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page["/Resources"] = Dictionary(XObject=Dictionary(Im0=image))
    page["/Contents"] = pdf.make_stream(
        b"/P <</MCID 0>> BDC q 40 0 0 40 10 10 cm /Im0 Do Q EMC\n"
        b"/P <</MCID 1>> BDC BT ET EMC\n"
    )
    page["/StructParents"] = 0

    paragraph = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/P"),
            Pg=page.obj,
            K=Array(
                [
                    Dictionary(Type=Name("/MCR"), Pg=page.obj, MCID=0),
                    Dictionary(Type=Name("/MCR"), Pg=page.obj, MCID=1),
                ]
            ),
        )
    )
    figure = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/Figure"),
            Pg=page.obj,
            K=0,
            Alt=String("A student interviews a research participant."),
        )
    )
    document = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), K=Array([paragraph, figure]))
    )
    paragraph["/P"] = document
    figure["/P"] = document

    parent_array = pdf.make_indirect(Array([paragraph, paragraph]))
    parent_tree = pdf.make_indirect(Dictionary(Nums=Array([0, parent_array])))
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


def _write_cross_page_parenttree_mismatch_pdf(path: Path) -> None:
    """Image Figure is correct, but its ParentTree entry points to a later P."""
    pdf = pikepdf.Pdf.new()
    image_page = pdf.add_blank_page(page_size=(200, 200))
    text_page = pdf.add_blank_page(page_size=(200, 200))

    image = pdf.make_stream(b"\x00")
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    image_page["/Resources"] = Dictionary(XObject=Dictionary(Im0=image))
    image_page["/Contents"] = pdf.make_stream(
        b"/Figure <</MCID 0>> BDC q 40 0 0 40 10 10 cm /Im0 Do Q EMC\n"
    )
    text_page["/Contents"] = pdf.make_stream(b"/P <</MCID 0>> BDC BT ET EMC\n")
    image_page["/StructParents"] = 0
    text_page["/StructParents"] = 1

    paragraph = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/P"),
            Pg=image_page.obj,
            K=Dictionary(Type=Name("/MCR"), Pg=text_page.obj, MCID=0),
        )
    )
    figure = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/Figure"),
            Pg=image_page.obj,
            K=0,
            Alt=String("A student interviews a research participant."),
        )
    )
    document = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), K=Array([figure, paragraph]))
    )
    figure["/P"] = document
    paragraph["/P"] = document

    parent_tree = pdf.make_indirect(
        Dictionary(
            Nums=Array(
                [
                    0,
                    pdf.make_indirect(Array([paragraph])),
                    1,
                    pdf.make_indirect(Array([paragraph])),
                ]
            )
        )
    )
    pdf.Root["/StructTreeRoot"] = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructTreeRoot"),
            K=Array([document]),
            ParentTree=parent_tree,
            ParentTreeNextKey=2,
        )
    )
    pdf.Root["/MarkInfo"] = Dictionary(Marked=True)
    pdf.save(path)
    pdf.close()


def _write_heading_nested_figure_pdf(path: Path) -> None:
    """Cover image Figure is incorrectly nested inside a heading."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))

    image = pdf.make_stream(b"\x00")
    image["/Type"] = Name("/XObject")
    image["/Subtype"] = Name("/Image")
    image["/Width"] = 1
    image["/Height"] = 1
    image["/ColorSpace"] = Name("/DeviceGray")
    image["/BitsPerComponent"] = 8
    page["/Resources"] = Dictionary(XObject=Dictionary(Im0=image))
    page["/Contents"] = pdf.make_stream(
        b"/H1 <</MCID 0>> BDC BT ET EMC\n"
        b"/Span <</MCID 6>> BDC q 10 0 0 10 10 10 cm /Im0 Do Q EMC\n"
    )
    page["/StructParents"] = 0

    figure = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/Figure"),
            Pg=page.obj,
            K=6,
            Alt=String("Copyright symbol."),
        )
    )
    heading = pdf.make_indirect(
        Dictionary(
            Type=Name("/StructElem"),
            S=Name("/H1"),
            Pg=page.obj,
            K=Array([0, figure]),
        )
    )
    document = pdf.make_indirect(
        Dictionary(Type=Name("/StructElem"), S=Name("/Document"), K=Array([heading]))
    )
    heading["/P"] = document
    figure["/P"] = heading

    null = pikepdf.Object.parse(b"null")
    parent_array = pdf.make_indirect(Array([heading, null, null, null, null, null, figure]))
    parent_tree = pdf.make_indirect(Dictionary(Nums=Array([0, parent_array])))
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


def _alt_elements_result(path: Path):
    report = PDFAccessibilityChecker(path).run_all()
    return next(result for result in report.results if result.rule_id == "alt-elements")


def _struct_nodes(pdf: pikepdf.Pdf):
    nodes = {
        str(node.get("/S", "")): node
        for node, _depth, _parent in walk_structure_tree(pdf)
        if str(node.get("/S", "")) in {"/P", "/Figure"}
    }
    return nodes["/P"], nodes["/Figure"]


def test_checker_flags_image_mcid_owned_by_text_element(tmp_path: Path):
    """Removing the mixed-content ownership check must make this test fail."""
    path = tmp_path / "duplicate-owner.pdf"
    _write_duplicate_image_owner_pdf(path)

    result = _alt_elements_result(path)

    assert result.status == "Failed"
    assert result.fixable is True
    assert any("page 1" in detail and "/P" in detail and "MCID 0" in detail for detail in result.details)


def test_fixer_reassigns_duplicate_image_mcid_to_existing_figure(tmp_path: Path):
    """The repair must preserve paragraph text and never hide it behind /Alt."""
    path = tmp_path / "duplicate-owner.pdf"
    _write_duplicate_image_owner_pdf(path)

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        changes = fix_xobject_bearing_text_elements(pdf)
        paragraph, figure = _struct_nodes(pdf)

        assert paragraph.get("/Alt") is None
        assert _get_node_mcids(paragraph) == [1]
        assert _get_node_mcids(figure) == [0]

        parent_array = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
        assert parent_array[0].objgen == figure.objgen
        assert parent_array[1].objgen == paragraph.objgen
        assert changes == [
            "Reassigned 1 image MCID ParentTree entry to an existing Figure",
            "Aligned 1 Figure marked-content tag",
        ]
        pdf.save(path)

    assert _alt_elements_result(path).status == "Passed"

    with pikepdf.open(path) as pdf:
        assert fix_xobject_bearing_text_elements(pdf) == []


def test_checker_flags_figure_when_parenttree_points_to_cross_page_paragraph(
    tmp_path: Path,
):
    path = tmp_path / "cross-page-owner.pdf"
    _write_cross_page_parenttree_mismatch_pdf(path)

    result = _alt_elements_result(path)

    assert result.status == "Failed"
    assert any(
        "page 1" in detail
        and "Figure" in detail
        and "MCID 0" in detail
        and "ParentTree" in detail
        for detail in result.details
    )


def test_fixer_reassigns_cross_page_parenttree_without_mutating_paragraph(
    tmp_path: Path,
):
    path = tmp_path / "cross-page-owner.pdf"
    _write_cross_page_parenttree_mismatch_pdf(path)

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        paragraph, figure = _struct_nodes(pdf)
        paragraph_k_before = repr(paragraph["/K"])

        changes = fix_xobject_bearing_text_elements(pdf)

        assert repr(paragraph["/K"]) == paragraph_k_before
        assert paragraph.get("/Alt") is None
        parent_array = pdf.Root["/StructTreeRoot"]["/ParentTree"]["/Nums"][1]
        assert parent_array[0].objgen == figure.objgen
        assert changes == [
            "Reassigned 1 image MCID ParentTree entry to an existing Figure"
        ]

        fix_page_retag(pdf)
        assert parent_array[0].objgen == figure.objgen


def test_checker_flags_figure_nested_inside_heading(tmp_path: Path):
    path = tmp_path / "heading-figure.pdf"
    _write_heading_nested_figure_pdf(path)

    result = _alt_elements_result(path)

    assert result.status == "Failed"
    assert any(
        "page 1" in detail and "Figure" in detail and "nested inside /H1" in detail
        for detail in result.details
    )
    assert any("MCID 6" in detail and "marked-content tag /Span" in detail for detail in result.details)


def test_fixer_hoists_figure_out_of_heading(tmp_path: Path):
    path = tmp_path / "heading-figure.pdf"
    _write_heading_nested_figure_pdf(path)

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        changes = fix_xobject_bearing_text_elements(pdf)
        nodes = list(walk_structure_tree(pdf))
        heading = next(node for node, _depth, _parent in nodes if str(node.get("/S")) == "/H1")
        figure = next(node for node, _depth, _parent in nodes if str(node.get("/S")) == "/Figure")
        document = next(
            node for node, _depth, _parent in nodes if str(node.get("/S")) == "/Document"
        )

        assert _get_node_mcids(heading) == [0]
        assert figure["/P"].objgen == document.objgen
        assert [child.objgen for child in document["/K"]] == [heading.objgen, figure.objgen]
        content = pdf.pages[0]["/Contents"].read_bytes()
        assert b"/Figure <</MCID 6>> BDC" in content
        assert b"/Span <</MCID 6>> BDC" not in content
        assert changes == [
            "Hoisted 1 Figure out of a heading element",
            "Aligned 1 Figure marked-content tag",
        ]


def test_image_structure_retag_also_aligns_marked_content_tag(tmp_path: Path):
    path = tmp_path / "span-image.pdf"
    _write_heading_nested_figure_pdf(path)

    with pikepdf.open(path) as pdf:
        figure = next(
            node
            for node, _depth, _parent in walk_structure_tree(pdf)
            if str(node.get("/S", "")) == "/Figure"
        )
        figure["/S"] = Name("/Span")

        fix_image_struct_elems_retag(pdf)

        assert str(figure["/S"]) == "/Figure"
        content = pdf.pages[0]["/Contents"].read_bytes()
        assert b"/Figure <</MCID 6>> BDC" in content
        assert b"/Span <</MCID 6>> BDC" not in content
