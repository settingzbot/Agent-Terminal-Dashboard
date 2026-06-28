"""
trident_review_lens.py — the SHARED review-lens loader (Slice of PRD #58, issue #67).

What this is
------------
A small, reusable, DEFENSIVE loader for the curated "review lens" — the hand-
maintained checklist of Trident's highest-consequence, easy-to-miss-in-tests risk
classes that lives in ``docs/claude/review_heuristics.md``. The lens is injected
into Dr. Isaac Kleiner's review prompt (``trident_review_pipeline.build_review_prompt``)
so every review reads the diff THROUGH that lens instead of a generic
correctness/scope/risk pass.

Why a FILE, not hardcoded text (the whole point)
------------------------------------------------
The design intent of #67 is that "designing a future lesson into the bots" becomes a
ONE-LINE APPEND to ``docs/claude/review_heuristics.md`` — NO code change. This loader
reads that file fresh on every call, so a new line in the lens reaches the very next
review with no deploy.

Why a SHARED loader
-------------------
This is the SINGLE source of the review lens. Kleiner consumes it today; Eli's later
prompt surfaces (#62 ``trident_eli_architect.py`` architectural call, #63
``trident_trade_surface.py`` trading-surface wall) should consume THIS loader rather
than re-implement the list when their LLM prompt surfaces are built. (Those two
modules are PURE cores today and build no prompts — they are intentionally NOT
modified here; this loader is left importable + documented as the shared source for
when they do.)

Defensive degradation (real capital — never crash a review)
-----------------------------------------------------------
A missing OR empty lens file degrades to NO-LENS: ``load_review_lens`` returns ``""``
and NEVER raises. A review with no lens still runs (it just falls back to the generic
correctness/scope/risk pass). A read error (permission, decode) likewise degrades to
``""`` — losing the lens must never be able to halt a review.

PURE-CORE SPLIT
---------------
This module is the I/O SEAM (it reads a file). It is kept SEPARATE from the pure
prompt builder ``build_review_prompt`` (which takes the lens TEXT as a parameter), so
the prompt builder stays pure/table-testable and the file read lives here. The
pipeline (the orchestrator) calls this loader and passes the text into the builder.
"""

from __future__ import annotations

import logging
from pathlib import Path

_LOG = logging.getLogger(__name__)

# The curated lens file, relative to the repo root (this module's directory).
DEFAULT_LENS_PATH = Path(__file__).resolve().parent / "docs" / "claude" / "review_heuristics.md"


def load_review_lens(path: Path | str | None = None) -> str:
    """Read the curated review lens and return its text. DEFENSIVE — never raises.

    The lens is ``docs/claude/review_heuristics.md`` by default (override ``path`` in
    tests). The caller passes the returned text into ``build_review_prompt`` so the
    prompt builder stays pure.

    Degradation (real capital — a review must NEVER crash because the lens is gone):
      * missing file        ⇒ ``""`` (no-lens; the review falls back to the generic pass)
      * empty / whitespace  ⇒ ``""``
      * read / decode error ⇒ ``""`` (logged, swallowed)
      * present file        ⇒ the file's text, stripped of surrounding whitespace

    Because the file is read FRESH on every call, appending one line to the lens file
    is picked up by the next review with no code change — the #67 design intent.
    """
    lens_path = Path(path) if path is not None else DEFAULT_LENS_PATH
    try:
        if not lens_path.is_file():
            _LOG.info("review lens file not found at %s — running with no lens", lens_path)
            return ""
        text = lens_path.read_text(encoding="utf-8").strip()
    except Exception as e:  # never let a lens read crash a review
        _LOG.warning("could not read review lens at %s (running with no lens): %s",
                     lens_path, e)
        return ""
    if not text:
        _LOG.info("review lens at %s is empty — running with no lens", lens_path)
        return ""
    return text
