"""Shared PDF semantics for checker and fixer logic."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pikepdf

MULTIMEDIA_ANNOT_TYPES = frozenset({"/RichMedia", "/Screen", "/Movie", "/Sound"})


def resolve_pdf_object(obj):
    """Best-effort resolve for pikepdf objects."""
    if isinstance(obj, pikepdf.Object) and obj.is_indirect:
        try:
            return obj.resolve()
        except Exception:
            return obj
    return obj


def _page_objgen_index_map(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    """Return a cached page objgen -> page index map for the document."""
    cached = getattr(pdf, "_page_objgen_index_map", None)
    if isinstance(cached, dict):
        return cached

    mapping: dict[tuple[int, int], int] = {}
    for idx, page in enumerate(pdf.pages):
        try:
            mapping[page.obj.objgen] = idx
        except Exception:
            continue

    pdf._page_objgen_index_map = mapping
    return mapping


def _node_page_cache(pdf: pikepdf.Pdf) -> dict[tuple[str, object], int | None]:
    """Return a cached structure-node -> page index map for the document."""
    cached = getattr(pdf, "_node_page_cache", None)
    if isinstance(cached, dict):
        return cached
    cache: dict[tuple[str, object], int | None] = {}
    pdf._node_page_cache = cache
    return cache


def _node_page_cache_key(node: pikepdf.Dictionary) -> tuple[str, object]:
    """Return a stable cache key for a structure node."""
    resolved = resolve_pdf_object(node)
    try:
        objgen = resolved.objgen
    except Exception:
        objgen = None
    if objgen is not None and objgen != (0, 0):
        return ("objgen", objgen)
    return ("id", id(resolved))


def iter_resolved_kids(node: pikepdf.Dictionary) -> Iterator[object]:
    """Yield a node's resolved /K children without materializing large arrays."""
    kids = node.get("/K")
    if kids is None:
        return
    if isinstance(kids, pikepdf.Array):
        for idx in range(len(kids)):
            yield resolve_pdf_object(kids[idx])
    else:
        yield resolve_pdf_object(kids)


def get_direct_mcid_refs(
    node: pikepdf.Dictionary,
    pdf: pikepdf.Pdf,
) -> list[tuple[int, int]]:
    """Return ``(page_index, mcid)`` pairs directly owned by *node*.

    A structure element can span pages. Integer ``/K`` entries inherit the
    element's page, while MCR dictionaries can name a different page through
    ``/Pg``. Child structure elements are intentionally excluded: their MCIDs
    belong to the child, not the container.
    """
    default_page = find_node_page(node, pdf)
    refs: list[tuple[int, int]] = []

    for child in iter_resolved_kids(node):
        if isinstance(child, pikepdf.Dictionary):
            if "/S" in child or "/MCID" not in child:
                continue
            page_idx = default_page
            if child.get("/Pg") is not None:
                page_idx = get_page_index_from_ref(pdf, child["/Pg"])
            if page_idx is None:
                continue
            try:
                refs.append((page_idx, int(child["/MCID"])))
            except Exception:
                continue
            continue

        if default_page is None:
            continue
        try:
            refs.append((default_page, int(child)))
        except Exception:
            continue

    return refs


_XOBJECT_DO_RE = re.compile(rb"/([A-Za-z][\w]*)\s+Do\b")


def _read_page_content_bytes(page: pikepdf.Page) -> bytes | None:
    content = page.get("/Contents")
    if content is None:
        return None
    try:
        if isinstance(content, pikepdf.Array):
            chunks = []
            for ref in content:
                obj = ref.get_object() if hasattr(ref, "get_object") else ref
                chunks.append(
                    obj.read_bytes() if hasattr(obj, "read_bytes") else bytes(obj)
                )
            return b"\n".join(chunks)
        else:
            obj = content.get_object() if hasattr(content, "get_object") else content
            return obj.read_bytes() if hasattr(obj, "read_bytes") else bytes(obj)
    except Exception:
        return None


def get_mcid_marked_content_tags(page: pikepdf.Page) -> dict[int, list[str]]:
    """Map MCIDs to the raw marked-content tag names used in page streams."""
    raw = _read_page_content_bytes(page)
    if raw is None:
        return {}
    pattern = re.compile(
        rb"/(?P<tag>[A-Za-z][\w]*)\s*<<[^>]*?/MCID\s+(?P<mcid>\d+)[^>]*?>>\s*BDC"
    )
    result: dict[int, list[str]] = {}
    for match in pattern.finditer(raw):
        mcid = int(match.group("mcid"))
        result.setdefault(mcid, []).append(match.group("tag").decode("latin-1"))
    return result


def get_mcid_xobject_names(page: pikepdf.Page) -> dict[int, list[str]]:
    """Map each marked-content MCID to XObjects invoked inside its scope."""
    raw = _read_page_content_bytes(page)
    if raw is None:
        return {}

    mcid_re = re.compile(
        rb"/(?P<tag>[A-Za-z][\w]*)\s*<<[^>]*?/MCID\s+(?P<mcid>\d+)[^>]*?>>\s*BDC"
    )
    emc_re = re.compile(rb"\bEMC\b")
    open_bdc_re = re.compile(rb"\bBDC\b")

    result: dict[int, list[str]] = {}
    stack: list[tuple[int | None, int]] = []
    pos = 0
    while pos < len(raw):
        m_mcid = mcid_re.search(raw, pos)
        m_bdc = open_bdc_re.search(raw, pos)
        m_emc = emc_re.search(raw, pos)
        candidates = [match for match in (m_mcid, m_bdc, m_emc) if match is not None]
        if not candidates:
            break
        nxt = min(candidates, key=lambda match: match.start())
        if nxt is m_mcid:
            stack.append((int(m_mcid.group("mcid")), m_mcid.end()))
            pos = m_mcid.end()
        elif nxt is m_emc:
            scope_mcid, scope_start = stack.pop() if stack else (None, pos)
            if scope_mcid is not None:
                names = [
                    name.decode("latin-1")
                    for name in _XOBJECT_DO_RE.findall(raw, scope_start, m_emc.start())
                ]
                if names:
                    result.setdefault(scope_mcid, []).extend(names)
            pos = m_emc.end()
        else:
            stack.append((None, m_bdc.end()))
            pos = m_bdc.end()

    return result


def get_parent_tree_owner(
    pdf: pikepdf.Pdf,
    page_index: int,
    mcid: int,
) -> pikepdf.Dictionary | None:
    """Return the StructElem that the page ParentTree assigns to *mcid*."""
    if page_index < 0 or page_index >= len(pdf.pages):
        return None
    struct_root = resolve_pdf_object(pdf.Root.get("/StructTreeRoot"))
    if not isinstance(struct_root, pikepdf.Dictionary):
        return None
    parent_tree = resolve_pdf_object(struct_root.get("/ParentTree"))
    if not isinstance(parent_tree, pikepdf.Dictionary):
        return None
    struct_parents = pdf.pages[page_index].get("/StructParents")
    if struct_parents is None:
        return None
    try:
        target = int(struct_parents)
    except Exception:
        return None

    stack = [parent_tree]
    while stack:
        node = resolve_pdf_object(stack.pop())
        if not isinstance(node, pikepdf.Dictionary):
            continue
        nums = node.get("/Nums")
        if isinstance(nums, pikepdf.Array):
            for idx in range(0, len(nums) - 1, 2):
                try:
                    if int(nums[idx]) != target:
                        continue
                except Exception:
                    continue
                owners = resolve_pdf_object(nums[idx + 1])
                if not isinstance(owners, pikepdf.Array) or mcid >= len(owners):
                    return None
                owner = resolve_pdf_object(owners[mcid])
                return owner if isinstance(owner, pikepdf.Dictionary) else None
        kids = node.get("/Kids")
        if isinstance(kids, pikepdf.Array):
            stack.extend(kids)
    return None


def node_has_struct_children(node: pikepdf.Dictionary) -> bool:
    """True when the node has at least one child structure element."""
    kids = node.get("/K")
    if kids is None:
        return False
    items = kids if isinstance(kids, pikepdf.Array) else [kids]
    for item in items:
        child = resolve_pdf_object(item)
        if isinstance(child, pikepdf.Dictionary) and "/S" in child:
            return True
    return False


def node_has_annotation_ref(node: pikepdf.Dictionary) -> bool:
    """True when the node references an actual annotation through OBJR or /Obj.

    PDF/UA-1 §7.18 ``alt-hides-annotation`` flags struct elements that own
    an annotation reference *and* an /Alt that would hide that annotation's
    own contents. The rule must not trip on /OBJR references that point to
    a non-annotation indirect object (e.g. an image XObject used to give a
    /Figure proper content association). Verify the resolved /Obj actually
    is an annotation before reporting True.
    """
    for child in iter_resolved_kids(node):
        if not isinstance(child, pikepdf.Dictionary):
            continue
        obj_type = str(child.get("/Type", ""))
        if obj_type != "/OBJR" and child.get("/Obj") is None:
            continue
        target = child.get("/Obj")
        if target is None:
            # /OBJR without /Obj is malformed; treat as annotation-ish by
            # legacy default so we don't lose existing detections.
            return True
        try:
            resolved = target.get_object() if hasattr(target, "get_object") else target
        except Exception:
            return True
        if not isinstance(resolved, pikepdf.Dictionary):
            continue
        target_type = str(resolved.get("/Type", ""))
        target_subtype = str(resolved.get("/Subtype", ""))
        # XObjects (Image, Form) are not annotations.
        if target_type == "/XObject" or target_subtype in {"/Image", "/Form"}:
            continue
        return True
    return False


def node_has_direct_content(node: pikepdf.Dictionary) -> bool:
    """True if the node has MCR/MCID/OBJR-like direct content children."""
    for child in iter_resolved_kids(node):
        if not isinstance(child, pikepdf.Dictionary):
            return True
        if "/S" not in child:
            return True
    return False


def node_has_content_association(node: pikepdf.Dictionary) -> bool:
    """True when an alt-bearing node is tied to actual rendered content."""
    stype = str(node.get("/S", "")).lstrip("/")
    if stype in {"Table", "Formula"}:
        return node_has_direct_content(node) or node_has_struct_children(node)
    return node_has_direct_content(node) or node_has_annotation_ref(node)


def document_requires_bookmarks(pdf: pikepdf.Pdf) -> bool:
    """Adobe-style rule: bookmarks are required for documents over 20 pages."""
    return len(pdf.pages) > 20


def document_has_bookmarks(pdf: pikepdf.Pdf) -> bool:
    """True when the document has a non-empty /Outlines tree."""
    outlines = pdf.Root.get("/Outlines")
    return bool(outlines and outlines.get("/Count", 0) != 0)


def get_page_index_from_ref(pdf: pikepdf.Pdf, page_ref) -> int | None:
    """Resolve a page reference to a 0-based page index."""
    page_obj = resolve_pdf_object(page_ref)
    try:
        target = page_obj.objgen
    except Exception:
        return None
    return _page_objgen_index_map(pdf).get(target)


def find_node_page(node: pikepdf.Dictionary, pdf: pikepdf.Pdf) -> int | None:
    """Find a structure node's page via /Pg or child MCR references."""
    cache = _node_page_cache(pdf)
    cache_key = _node_page_cache_key(node)
    if cache_key in cache:
        return cache[cache_key]

    pg = node.get("/Pg")
    if pg is not None:
        idx = get_page_index_from_ref(pdf, pg)
        if idx is not None:
            cache[cache_key] = idx
            return idx

    for child in iter_resolved_kids(node):
        if isinstance(child, pikepdf.Dictionary) and "/Pg" in child:
            idx = get_page_index_from_ref(pdf, child["/Pg"])
            if idx is not None:
                cache[cache_key] = idx
                return idx
    cache[cache_key] = None
    return None


def get_rendered_multimedia_names(page: pikepdf.Page) -> set[str]:
    """Return multimedia annotation subtype names present on the page.

    PDF multimedia is represented through rich-media/screen/movie/sound
    annotations, not ordinary /Form XObjects used for layout reuse.
    """
    annots = page.get("/Annots")
    if not annots:
        return set()

    names: set[str] = set()
    for annot_ref in annots:
        annot = resolve_pdf_object(annot_ref)
        if not isinstance(annot, pikepdf.Dictionary):
            continue
        subtype = str(annot.get("/Subtype", ""))
        if subtype in MULTIMEDIA_ANNOT_TYPES:
            names.add(subtype.lstrip("/"))
    return names


def get_rendered_image_names(page: pikepdf.Page) -> list[str]:
    """Return rendered image XObject names in content-stream order."""
    resources = page.get("/Resources")
    if not resources:
        return []

    xobjects = resources.get("/XObject")
    if not xobjects:
        return []

    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:
        return []

    names: list[str] = []
    for operands, operator in instructions:
        if str(operator) != "Do" or not operands:
            continue
        raw_name = str(operands[0]).lstrip("/")
        try:
            xobj_ref = xobjects.get(f"/{raw_name}") or xobjects.get(raw_name)
        except Exception:
            xobj_ref = xobjects.get(raw_name)
        if xobj_ref is None:
            continue
        xobj = resolve_pdf_object(xobj_ref)
        if isinstance(xobj, pikepdf.Stream) and str(xobj.get("/Subtype", "")) == "/Image":
            names.append(raw_name)
    return names
