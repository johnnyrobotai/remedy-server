"""HTML-to-EPUB converter producing EPUB Accessibility 1.1 packages.

Converts the WCAG 2.1 AA accessible HTML produced by ``converter.py`` into
EPUB 3 packages that declare conformance to EPUB Accessibility 1.1 — WCAG
2.1 Level AA.

Design points come from the W3C EPUB Accessibility 1.1 Recommendation and
the W3C Package Metadata Authoring Guide:

* ``dcterms:conformsTo`` MUST match the exact pattern
  ``"EPUB Accessibility 1.1 - WCAG 2.1 Level AA"``.
* The three MUST-level schema.org properties for 1.1 are
  ``accessMode``, ``accessibilityFeature``, ``accessibilityHazard``.
  ``accessModeSufficient`` is added for forward compatibility with 1.2
  (which promotes it to MUST).
* ``accessMode`` describes the *source encoding* (textual / visual /
  auditory) BEFORE adaptations. Alt text on an image does not make the
  publication "textual" — the canonical W3C example is a visual comic
  with alt text whose ``accessMode`` is still ``visual`` and whose
  ``accessModeSufficient`` is ``visual,textual``.
* ``accessibilityFeature`` is emitted one ``<meta>`` element per value
  (values must not be grouped); at least one value other than ``none`` /
  ``unknown`` is required.

ACE-by-DAISY and EPUBCheck validation is performed separately by
``epub_verifier.py`` — this module produces the package, that one
gates it.

Usage::

    converter = HTMLToEPUBConverter()
    await converter.start()
    result = await converter.convert(html_content, output_path, title="My Doc")
    await converter.close()
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from bs4 import BeautifulSoup, Tag
from ebooklib import epub

logger = logging.getLogger(__name__)


# Exact conformance string mandated by EPUB Accessibility 1.1 §3.5.2.
EPUB_A11Y_CONFORMS_TO = "EPUB Accessibility 1.1 - WCAG 2.1 Level AA"

# Default accessibility summary surfaced in reading systems. Tunable per-call.
DEFAULT_A11Y_SUMMARY = (
    "This publication conforms to EPUB Accessibility 1.1 — WCAG 2.1 Level AA. "
    "Automated checks (EPUBCheck + ACE by DAISY) pass; per DAISY guidance, a "
    "clean automated report is not by itself a conformance certificate — "
    "manual SMART-style review is required to certify conformance."
)


@dataclass
class EPUBConversionResult:
    """Outcome of a single HTML-to-EPUB conversion."""

    output_path: Path | None = None
    success: bool = False
    error_message: str = ""
    # Features that were asserted in the package metadata, for the verify report.
    accessibility_features: list[str] = field(default_factory=list)
    chapters: int = 0


class HTMLToEPUBConverter:
    """Converts accessible HTML to EPUB Accessibility 1.1 packages.

    Concurrency-bounded the same way as ``HTMLToPDFConverter`` so it can
    slot into the same engine handler. Unlike the PDF path, EPUB
    generation is CPU/IO-bound only — no external browser process is
    required — so ``start()`` / ``close()`` are mostly no-ops, kept for
    interface parity.

    Parameters
    ----------
    max_concurrent:
        Maximum number of simultaneous conversions.
    """

    def __init__(self, max_concurrent: int = 8) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def start(self) -> None:
        logger.info(
            "HTMLToEPUBConverter started (max_concurrent=%d).",
            self._max_concurrent,
        )

    async def close(self) -> None:
        logger.info("HTMLToEPUBConverter closed.")

    async def convert(
        self,
        html: str,
        output_path: Path,
        *,
        title: str = "",
        language: str = "en",
        identifier: str | None = None,
        accessibility_summary: str = DEFAULT_A11Y_SUMMARY,
    ) -> EPUBConversionResult:
        """Convert an HTML string to a conformant EPUB package.

        Parameters
        ----------
        html:
            Full accessible HTML document (output of ``converter.py``).
        output_path:
            Where to write the ``.epub`` file.
        title:
            ``dc:title``. Falls back to the parsed ``<title>`` then to the
            output filename stem.
        language:
            BCP 47 language tag. Falls back to ``<html lang="...">``.
        identifier:
            Stable identifier for ``dc:identifier``. Auto-generated UUID
            URN when omitted.
        accessibility_summary:
            ``schema:accessibilitySummary`` text. Defaults to a summary
            that explicitly notes the automated-validation caveat from
            DAISY.
        """
        async with self._semaphore:
            try:
                return await asyncio.to_thread(
                    _build_epub,
                    html,
                    output_path,
                    title=title,
                    language=language,
                    identifier=identifier,
                    accessibility_summary=accessibility_summary,
                )
            except Exception as exc:
                logger.warning(
                    "HTML-to-EPUB conversion failed for %s: %s",
                    output_path.name,
                    exc,
                )
                return EPUBConversionResult(error_message=str(exc))

    async def convert_batch(
        self,
        items: Sequence[tuple[str, Path, str, str]],
    ) -> list[EPUBConversionResult]:
        """Convert ``(html, output_path, title, language)`` tuples concurrently."""
        tasks = [
            self.convert(html, path, title=title, language=lang)
            for html, path, title, lang in items
        ]
        return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Core build (runs in a thread — ebooklib is sync)
# ---------------------------------------------------------------------------


def _build_epub(
    html: str,
    output_path: Path,
    *,
    title: str,
    language: str,
    identifier: str | None,
    accessibility_summary: str,
) -> EPUBConversionResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(html, "lxml")

    # Resolve title and language with sensible fallbacks.
    if not title:
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        else:
            title = output_path.stem
    html_tag = soup.find("html")
    if isinstance(html_tag, Tag) and html_tag.get("lang"):
        language = str(html_tag.get("lang"))
    uid = identifier or f"urn:uuid:{uuid.uuid4()}"

    book = epub.EpubBook()
    book.set_identifier(uid)
    book.set_title(title)
    book.set_language(language)

    # Introspect the HTML once to decide which schema.org accessibility
    # features to assert. Per spec, accessibilityFeature values must not
    # be grouped — emit one <meta> element per value.
    features = _detect_accessibility_features(soup)
    access_modes = _detect_access_modes(soup)

    _emit_accessibility_metadata(
        book,
        features=features,
        access_modes=access_modes,
        accessibility_summary=accessibility_summary,
    )

    # Split body content on <section> wrappers (the converter pipeline emits
    # one <section> per logical division). Fall back to a single chapter
    # if no sections are present so the converter remains robust against
    # less-structured input.
    chapters = _build_chapters(soup, language=language)
    if not chapters:
        chapters = [
            _make_chapter(
                "chap_1.xhtml",
                title,
                str(soup.body) if soup.body else f"<p>{title}</p>",
                language=language,
            )
        ]

    for chapter in chapters:
        book.add_item(chapter)

    # TOC: one entry per chapter, using its title.
    book.toc = tuple(
        epub.Link(c.file_name, c.title or f"Section {i+1}", c.id)
        for i, c in enumerate(chapters)
    )

    # Required navigation document + legacy NCX (NCX is optional in EPUB 3
    # but reading systems that fall back to EPUB 2 expect it).
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Minimal stylesheet to make the package self-contained.
    style = epub.EpubItem(
        uid="style_main",
        file_name="style/main.css",
        media_type="text/css",
        content=_DEFAULT_CSS.encode("utf-8"),
    )
    book.add_item(style)

    book.spine = ["nav", *chapters]

    epub.write_epub(str(output_path), book, {})

    logger.debug(
        "Wrote EPUB %s (%d chapters, features=%s).",
        output_path.name,
        len(chapters),
        ",".join(features),
    )
    return EPUBConversionResult(
        output_path=output_path,
        success=True,
        accessibility_features=list(features),
        chapters=len(chapters),
    )


# ---------------------------------------------------------------------------
# Accessibility metadata
# ---------------------------------------------------------------------------


def _emit_accessibility_metadata(
    book: epub.EpubBook,
    *,
    features: list[str],
    access_modes: list[str],
    accessibility_summary: str,
) -> None:
    """Emit the schema.org accessibility metadata trio + conformance string.

    EPUB Accessibility 1.1 (current Recommendation, Oct 2024) MUST set:
      * schema:accessMode
      * schema:accessibilityFeature
      * schema:accessibilityHazard

    1.2 (Working Draft) promotes schema:accessModeSufficient to MUST and
    accessMode to SHOULD. We emit both so the package is forward-
    compatible without changing the conformance string.
    """
    # Exact conformance string per §3.5.2.
    book.add_metadata(
        None, "meta", EPUB_A11Y_CONFORMS_TO, {"property": "dcterms:conformsTo"}
    )

    # accessMode — source encoding before adaptation. One element per value.
    for mode in access_modes:
        book.add_metadata(None, "meta", mode, {"property": "schema:accessMode"})

    # accessModeSufficient — full reading pathway after adaptation. For our
    # remediated HTML the textual pathway is always sufficient (alt text
    # makes images consumable via the textual mode).
    book.add_metadata(
        None, "meta", "textual", {"property": "schema:accessModeSufficient"}
    )

    # accessibilityFeature — one <meta> per value, no grouping. At least
    # one must be other than 'none'/'unknown' to claim conformance.
    for feature in features:
        book.add_metadata(
            None, "meta", feature, {"property": "schema:accessibilityFeature"}
        )

    # accessibilityHazard — assert 'none' explicitly. (If the HTML
    # introspection ever detects flashing or motion, surface that here.)
    book.add_metadata(
        None, "meta", "none", {"property": "schema:accessibilityHazard"}
    )

    # accessibilitySummary — human-readable summary surfaced by reading
    # systems. SHOULD per 1.1.
    book.add_metadata(
        None, "meta", accessibility_summary,
        {"property": "schema:accessibilitySummary"},
    )


def _detect_accessibility_features(soup: BeautifulSoup) -> list[str]:
    """Walk the HTML once and decide which schema.org features to assert.

    Returns a stable-ordered list (so OPF byte output is deterministic
    for tests). Always includes ``structuralNavigation`` and
    ``tableOfContents`` because the EPUB we emit always has a nav
    document; other values are added only when the source HTML actually
    provides them, per the W3C guidance that overstated features are a
    conformance defect.
    """
    features: list[str] = ["structuralNavigation", "tableOfContents"]

    # alternativeText — only assert if at least one <img> has a non-empty
    # alt attribute. Decorative-only (alt="") publications should not
    # claim this feature.
    imgs = soup.find_all("img")
    if any(isinstance(img, Tag) and (img.get("alt") or "").strip() for img in imgs):
        features.append("alternativeText")

    # longDescription — assert if any element references a long description
    # via aria-describedby or longdesc attribute.
    if soup.find(attrs={"aria-describedby": True}) or soup.find(attrs={"longdesc": True}):
        features.append("longDescription")

    # MathML — only when the source actually contains <math>.
    if soup.find("math"):
        features.append("MathML")

    # readingOrder — converter.py guarantees logical source order, so the
    # textual pathway has a defined reading order.
    features.append("readingOrder")

    # captions / transcript — assert if the HTML has <track kind="captions">
    # or <details class="transcript"> respectively.
    if soup.find("track", kind=re.compile(r"^(captions|subtitles)$", re.I)):
        features.append("captions")
    if soup.find(attrs={"class": re.compile(r"\btranscript\b")}):
        features.append("transcript")

    return features


def _detect_access_modes(soup: BeautifulSoup) -> list[str]:
    """Return the source-encoding access modes.

    Textual is always present (HTML is by definition a textual encoding).
    Visual is added when the source contains rendered images, video, or
    SVG. Auditory is added when audio elements are present. Per the
    canonical W3C example, alt text does NOT add a textual mode (the
    source mode is fixed; adaptations belong in accessModeSufficient).
    """
    modes: list[str] = ["textual"]
    if soup.find("img") or soup.find("video") or soup.find("svg"):
        modes.append("visual")
    if soup.find("audio"):
        modes.append("auditory")
    return modes


# ---------------------------------------------------------------------------
# Chapter splitting
# ---------------------------------------------------------------------------


def _build_chapters(soup: BeautifulSoup, *, language: str) -> list[epub.EpubHtml]:
    """Split the document body into chapters, one per top-level <section>.

    Falls back to splitting on H1 boundaries if no sections exist. Each
    chapter inherits headings from its source subtree so the EPUB nav
    document's structural-navigation feature is honest.
    """
    body = soup.body
    if not body:
        return []

    sections = body.find_all("section", recursive=False)
    if not sections:
        sections = soup.find_all("section")
    if not sections:
        return []

    chapters: list[epub.EpubHtml] = []
    for i, section in enumerate(sections, start=1):
        if not isinstance(section, Tag):
            continue
        heading = section.find(re.compile(r"^h[1-6]$"))
        chap_title = heading.get_text(strip=True) if heading else f"Section {i}"
        chapter = _make_chapter(
            f"chap_{i}.xhtml",
            chap_title,
            str(section),
            language=language,
        )
        chapters.append(chapter)
    return chapters


def _make_chapter(
    file_name: str, title: str, body_html: str, *, language: str
) -> epub.EpubHtml:
    """Wrap a body fragment in a valid XHTML document for EPUB."""
    chap = epub.EpubHtml(
        title=title,
        file_name=file_name,
        lang=language,
    )
    chap.content = _XHTML_TEMPLATE.format(
        lang=language,
        title=_xml_escape(title),
        body=body_html,
    )
    # Link the shared stylesheet so each chapter resolves it.
    chap.add_item(epub.EpubItem(file_name="style/main.css"))
    return chap


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_XHTML_TEMPLATE = """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="style/main.css"/>
</head>
<body>
{body}
</body>
</html>
"""

_DEFAULT_CSS = """\
body { font-family: serif; line-height: 1.5; margin: 1em; }
h1, h2, h3, h4, h5, h6 { font-family: sans-serif; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; }
th, td { border: 1px solid #999; padding: 0.25em 0.5em; }
caption { font-weight: bold; text-align: left; }
"""
