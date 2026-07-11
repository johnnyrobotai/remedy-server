"""Stability-voting, deterministic-prominence rescue, and a rule-agnostic
failure-driven refix loop for structural remediation.

Why this module exists
----------------------
The heading vision adapter is the weakest-trained of the five task adapters and
serves as both detector and verifier, so a single acceptance pass flags
*different* headings each run — the failure-driven retag conversion plateaued at
~20%/pass. Two independent levers push past that ceiling without a better model:

* **Consensus voting** (Part A) runs the analyzer several times and keeps only
  the retag decisions that recur across a majority of runs, filtering the
  run-to-run noise that a single pass cannot distinguish from signal.
* **Deterministic-prominence rescue** (Part B) assigns a heading's *level* from
  font-size / weight measured off the rendered page — exactly the judgment the
  noisy adapter is worst at — with no model call at all.

Part C generalizes the heading-specific feedback loop (parse checker failures →
targeted fixer → re-verify) into a registry so alt-text and untagged-content
handlers dispatch through the same mechanism.

Everything here imports one-way from :mod:`project_remedy.pdf_fixer`; the thin
wiring in ``pdf_fixer`` imports this module lazily (inside function bodies) to
avoid an import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from project_remedy import pdf_fixer as PF

# ---------------------------------------------------------------------------
# Part A — consensus voting over noisy heading-retag decisions
# ---------------------------------------------------------------------------


def _issue_target_tag(issue) -> str:
    """The H-level this issue asks for, or '' if none is derivable."""
    return (
        PF._normal_heading_correct_tag(getattr(issue, "correct_tag", ""))
        or PF._heading_tag_from_suggestion(getattr(issue, "suggestion", ""))
    )


def _issue_identity_text(issue) -> str:
    """The visible heading text that anchors this decision's identity.

    ``element_index`` is unusable as identity — the model re-enumerates visual
    elements every pass — so the visible text is the only stable anchor.
    """
    candidates = PF._vision_heading_text_candidates(issue)
    if candidates:
        return candidates[0]
    return PF._normalize_extracted_text(str(getattr(issue, "description", "") or ""))


def heading_decision_key(issue) -> tuple | None:
    """A run-invariant identity for one heading-retag decision, or None.

    Returns None for non-error or non-actionable issues (no derivable target
    tag). The key deliberately excludes ``element_index`` so the same visible
    heading votes together across passes even as the model renumbers elements.
    """
    if getattr(issue, "severity", "warning") != "error":
        return None
    target = _issue_target_tag(issue)
    if not target:
        return None
    page0 = int(getattr(issue, "page", 0) or 0) - 1
    claimed = str(getattr(issue, "current_tag", "") or "").strip().lstrip("/")
    text_key = _issue_identity_text(issue).lower()[:40]
    return (page0, claimed, target, text_key)


def _richer_issue(current, candidate):
    """Prefer the representative that carries the most to act on: a located
    element_index first, then the longer visible text."""
    if current is None:
        return candidate
    cur_idx = getattr(current, "element_index", None) is not None
    cand_idx = getattr(candidate, "element_index", None) is not None
    if cand_idx and not cur_idx:
        return candidate
    if cur_idx and not cand_idx:
        return current
    if len(_issue_identity_text(candidate)) > len(_issue_identity_text(current)):
        return candidate
    return current


def consensus_heading_issues(runs, *, threshold: int) -> list:
    """Fuse ``runs`` (each a list of HeadingIssue) into the agreed retags.

    A decision must recur in at least ``threshold`` distinct runs to survive; a
    run that repeats the same decision counts only once. Returns one
    representative issue per surviving key, ordered most-agreed first for
    deterministic application.
    """
    tally: dict[tuple, list] = {}
    for run in runs or []:
        seen_in_run: set[tuple] = set()
        for issue in run or []:
            key = heading_decision_key(issue)
            if key is None or key in seen_in_run:
                continue
            seen_in_run.add(key)
            if key not in tally:
                tally[key] = [0, None]
            tally[key][0] += 1
            tally[key][1] = _richer_issue(tally[key][1], issue)
    survivors = [
        (count, key, best)
        for key, (count, best) in tally.items()
        if count >= threshold
    ]
    survivors.sort(key=lambda t: (-t[0], t[1]))
    return [best for _count, _key, best in survivors]


# ---------------------------------------------------------------------------
# Part B — deterministic-prominence rescue (no model call)
# ---------------------------------------------------------------------------

# A heading line is short; a full paragraph line is not. Anything longer is
# treated as body regardless of size, so a large-print notice paragraph is not
# mistaken for a heading.
_HEADING_MAX_WORDS = 12
# A line must be meaningfully larger than the modal body size to count on size
# alone; a bold line only needs to be at least body size.
_PROMINENCE_SIZE_RATIO = 1.15
_FITZ_BOLD_FLAG = 1 << 4  # PyMuPDF span flag bit for bold


def _measure_line_prominence(pdf_path: Path, page_idx: int) -> list[dict]:
    """Per visible text line on ``page_idx``: {text, size, bold} via PyMuPDF.

    Size is the max span size in the line (a heading rarely mixes sizes; taking
    the max avoids a trailing small glyph dragging it down). Returns [] if the
    page cannot be read — the caller then simply makes no changes.
    """
    try:
        import fitz
    except Exception:
        return []
    lines: list[dict] = []
    try:
        with fitz.open(str(pdf_path)) as doc:
            if page_idx < 0 or page_idx >= doc.page_count:
                return []
            data = doc[page_idx].get_text("dict")
    except Exception:
        return []
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", []) or []
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
            bold = any(int(s.get("flags", 0)) & _FITZ_BOLD_FLAG for s in spans)
            lines.append({"text": text, "size": round(size, 1), "bold": bold})
    return lines


def deterministic_heading_levels(pdf_path: Path, page_idx: int) -> dict[str, int]:
    """Map normalized (lowercased) line text -> heading level from font size.

    The modal line size is treated as body. Distinct sizes larger than body are
    ranked descending into H1, H2, H3… (capped at 6); a line qualifies as a
    heading when it is short and either clearly larger than body or bold at
    >= body size. This captures the *level* judgment the vision adapter is
    least reliable at, with no model call.
    """
    lines = _measure_line_prominence(pdf_path, page_idx)
    if not lines:
        return {}
    # Modal size = body. Ties resolve to the smaller size (body is the smaller,
    # more frequent text; headings are the rarer larger runs).
    from collections import Counter

    size_counts = Counter(line["size"] for line in lines)
    body_size = min(size_counts, key=lambda s: (-size_counts[s], s))

    heading_sizes = sorted(
        {line["size"] for line in lines if line["size"] > body_size}, reverse=True
    )
    size_to_level = {size: i + 1 for i, size in enumerate(heading_sizes[:6])}

    levels: dict[str, int] = {}
    for line in lines:
        text = line["text"]
        if len(text.split()) > _HEADING_MAX_WORDS:
            continue
        size = line["size"]
        level = None
        if size in size_to_level and size >= body_size * _PROMINENCE_SIZE_RATIO:
            level = size_to_level[size]
        elif line["bold"] and size >= body_size and size not in size_to_level:
            # Bold at body size (no larger tier exists for it) — a run-in
            # heading. Slot it just below the smallest size-based tier.
            level = min(len(size_to_level) + 1, 6)
        if level is None:
            continue
        key = PF._normalize_extracted_text(text).lower()
        if key and key not in levels:
            levels[key] = level
    return levels


def apply_prominence_heading_rescue(
    pdf_path: Path,
    pages: list[int],
    *,
    save: bool = True,
) -> list[str]:
    """Vision-free retag: promote text nodes whose visible text matches a
    deterministically-detected heading line to the measured H-level.

    Every retag passes through :func:`pdf_fixer._find_heading_retag_node_by_text`
    with ``require_safe_target`` set, so the same guard that protects table
    cells, list structure and image-only figures applies here — a prominent
    TABLE CELL is never promoted. Saves in place only when something changed.
    """
    import pikepdf

    target_levels: dict[int, dict[str, int]] = {}
    for page_idx in pages or []:
        levels = deterministic_heading_levels(pdf_path, page_idx)
        if levels:
            target_levels[page_idx] = levels
    if not target_levels:
        return []

    retagged = 0
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        for page_idx, levels in target_levels.items():
            if page_idx >= len(pdf.pages):
                continue
            for text, level in levels.items():
                target = f"H{min(max(int(level), 1), 6)}"
                node = PF._find_heading_retag_node_by_text(
                    pdf, page_idx, "", [text], require_safe_target=target,
                )
                if node is None:
                    continue
                node["/S"] = pikepdf.Name(f"/{target}")
                retagged += 1
        if retagged and save:
            pdf.save(pdf_path)

    if retagged:
        return [
            f"Retagged {retagged} element(s) from deterministic visual prominence"
        ]
    return []


# ---------------------------------------------------------------------------
# Part C — a rule-agnostic failure-driven refix loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefixHandler:
    """Binds a set of checker rule_ids to a targeted in-place fixer.

    The heading feedback loop (parse failures → run the fixer only where it is
    needed → re-verify) generalizes to any rule with a bound fixer; a handler is
    the registry entry that makes a rule participate.
    """

    name: str
    rule_ids: frozenset[str]
    apply: Callable[..., list[str]]
    needs_vision: bool = False


def _heading_refix(pdf_path, *, vision_provider, checker_failures) -> list[str]:
    return PF.apply_heading_retag_refix(
        pdf_path, vision_provider=vision_provider, checker_failures=checker_failures,
    )


def _alt_text_refix(pdf_path, *, vision_provider, checker_failures) -> list[str]:
    import pikepdf

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        added = PF._fix_missing_alt_text(pdf, vision_provider)
        if added:
            pdf.save(pdf_path)
    if added:
        return [f"Added alt text to {added} figure(s) (failure-driven refix)"]
    return []


def _untagged_content_refix(pdf_path, *, vision_provider, checker_failures) -> list[str]:
    import pikepdf

    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        changes = PF.fix_untagged_content(pdf)
        if changes:
            pdf.save(pdf_path)
    return list(changes)


# Registry. Heading is the fully-featured handler (page-targeted vision retag +
# consensus voting + prominence rescue live behind it); alt-text and
# untagged-content reuse their existing whole-document fixers, re-run only when
# their rule actually fired.
REFIX_HANDLERS: list[RefixHandler] = [
    RefixHandler(
        name="heading",
        rule_ids=frozenset({
            "headings-nesting", "sr-heading-skip", "sr-heading-start",
            "sr-no-headings",
        }),
        apply=_heading_refix,
        needs_vision=True,
    ),
    RefixHandler(
        name="alt-text",
        rule_ids=frozenset({
            "alt-figures", "alt-elements", "sr-figure-no-alt",
            "sr-figure-generic-alt", "sr-figure-short-alt",
        }),
        apply=_alt_text_refix,
        needs_vision=True,
    ),
    RefixHandler(
        name="untagged-content",
        rule_ids=frozenset({
            "page-content-tagged", "sr-untagged-page", "sr-no-tags",
        }),
        apply=_untagged_content_refix,
        needs_vision=False,
    ),
]


def _fired_rule_ids(checker_failures) -> set[str]:
    fired: set[str] = set()
    for failure in checker_failures or []:
        if isinstance(failure, dict):
            rule_id = failure.get("rule_id", "")
        else:
            rule_id = getattr(failure, "rule_id", "")
        if rule_id:
            fired.add(str(rule_id))
    return fired


def apply_failure_driven_refix(
    pdf_path: Path,
    *,
    vision_provider,
    checker_failures,
    only: set[str] | None = None,
) -> list[str]:
    """Dispatch every registered handler whose rule fired, to its targeted fixer.

    This is the generalized form of ``apply_heading_retag_refix``: rather than
    replaying the whole gated pipeline (which missed the failure the first
    time), it runs *only* the fixers for the rules that actually failed. Vision
    handlers are skipped when no provider is available; a raising handler is
    isolated so the others still apply. ``only`` restricts to named handlers.
    """
    import logging

    logger = logging.getLogger(__name__)
    fired = _fired_rule_ids(checker_failures)
    changes: list[str] = []
    for handler in REFIX_HANDLERS:
        if only is not None and handler.name not in only:
            continue
        if not (handler.rule_ids & fired):
            continue
        if handler.needs_vision and vision_provider is None:
            continue
        try:
            changes.extend(handler.apply(
                pdf_path,
                vision_provider=vision_provider,
                checker_failures=checker_failures,
            ))
        except Exception as exc:  # noqa: BLE001 — one handler must not sink others
            logger.warning(
                "failure-driven refix handler %s failed: %s", handler.name, exc)
    return changes
