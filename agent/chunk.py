"""Markdown chunker — `md-h2-v1+drop-title-only`.

Ported from Lab 3a cell 11 with exactly one deviation: a title-only preamble chunk is
dropped rather than indexed.

Lab 3a measured that the 51-char `(preamble)` chunk — nothing but the document's `# `
title line — wins *every* short query at cosine 0.61-0.65 on length affinity alone, and
its closing notes flagged that "a second document brings a second title-only preamble and
doubles the hub". With three documents in one collection, three such hubs compete for a
k=4 window and crowd out the chunk that actually answers the question. The assistant then
answers ungrounded, which looks like a defence and is really a harness artifact.

Two properties make the deviation continuity-preserving, and both are deliberate:

- **Ids are assigned before the drop**, so every surviving chunk keeps the id Lab 3a gave
  it (`…-001` through `…-007`; only `…-000` is absent).
- **No surviving chunk's text is modified**, so every surviving vector is bit-identical to
  Lab 3a's. Folding the title into the following section would instead have mutated the
  chunk carrying the early canary, invalidating a measured baseline.

The title is not lost. It travels in the CONTEXT provenance header assembled by `rag.py`,
reaching the model on every request without entering the index.
"""

from __future__ import annotations

from agent.config import MAX_CHARS

PREAMBLE = "(preamble)"


def _split_sections(doc: str) -> list[dict]:
    sections: list[dict] = []
    heading = PREAMBLE
    buf: list[str] = []
    for line in doc.splitlines():
        if line.startswith("## "):
            sections.append({"section": heading, "text": "\n".join(buf).strip()})
            heading = line[3:].strip()
            buf = [line]
        else:
            buf.append(line)
    sections.append({"section": heading, "text": "\n".join(buf).strip()})
    return [s for s in sections if s["text"]]


def is_title_only(chunk: dict) -> bool:
    """True for a preamble chunk whose body is just the `# ` title line."""
    if chunk["section"] != PREAMBLE:
        return False
    lines = [ln for ln in chunk["text"].splitlines() if ln.strip()]
    return len(lines) == 1 and lines[0].startswith("# ")


def doc_title(doc: str) -> str:
    """The `# ` title line, without the hashes — for the CONTEXT provenance header."""
    for line in doc.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _subsplit(section: dict, max_chars: int) -> list[dict]:
    """Break an oversize section on paragraph boundaries, keeping its heading label.

    Known limitation, inherited from v1 and left in place deliberately: the split points
    are blank lines, and a long markdown table contains none — so a table larger than
    `max_chars` comes back as one oversize chunk. Nothing here raises on that; `index.py`
    gate 3 asserts the length instead, and the fixtures are written to sections that fit.
    The general fix is a line-level fallback split, which would change the chunker tag and
    re-derive every vector in the corpus, so it is not worth it for this lab.
    """
    text = section["text"]
    if len(text) <= max_chars:
        return [section]
    out: list[dict] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) > max_chars and buf:
            out.append({"section": section["section"], "text": buf.strip()})
            buf = para
        else:
            buf = candidate
    if buf.strip():
        out.append({"section": section["section"], "text": buf.strip()})
    return out


def chunk_markdown(doc: str, max_chars: int = MAX_CHARS) -> list[dict]:
    """Split markdown into `[{"section", "text"}]` chunks of at most `max_chars`.

    Returns ALL chunks, title-only included. The caller assigns ids over this full list
    and only then drops the title-only ones, which is what preserves Lab 3a's ids.
    """
    chunks: list[dict] = []
    for s in _split_sections(doc):
        chunks.extend(_subsplit(s, max_chars))
    return chunks
