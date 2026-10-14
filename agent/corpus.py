"""Corpus discovery: every markdown fixture in `notebooks/data/`, with a manifest.

Lab 3a indexed one document and recorded a single `doc_sha256` in the collection
metadata. The stale-index guard was built on that one hash. With three documents the
equivalent guard needs a hash *of the set*, so this module produces both:

- per-document sha256, which goes into each chunk's metadata and its id prefix, and
- `corpus_sha256`, the sha256 of the sorted `name:sha` manifest, which goes into the
  collection metadata and is what the guard compares.

Adding, removing or editing any document changes `corpus_sha256`, so a server running
against a stale index fails loudly instead of answering from the wrong corpus.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent.config import CORPUS_DIR

COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def load_doc(path: Path) -> dict:
    """Read one fixture: hash the raw bytes, then strip HTML comments before chunking.

    The provenance banner in each fixture is an HTML comment precisely so it can be
    removed here — a document that announces it is a red-team fixture would change the
    behaviour of any model later asked to answer from it. The sha256 is taken over the
    RAW bytes so editing the banner still invalidates the index.
    """
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    text = COMMENT_RE.sub("", raw.decode("utf-8"))
    if "<!--" in text:
        raise RuntimeError(f"{path.name}: unterminated HTML comment after stripping")
    return {"name": path.name, "path": path, "sha256": sha, "text": text}


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> tuple[list[dict], str]:
    """Return (docs, corpus_sha256) for every `*.md` under `corpus_dir`, name-sorted."""
    paths = sorted(corpus_dir.glob("*.md"))
    if not paths:
        raise RuntimeError(f"no markdown fixtures found in {corpus_dir}")
    docs = [load_doc(p) for p in paths]
    manifest = "\n".join(f"{d['name']}:{d['sha256']}" for d in docs)
    return docs, hashlib.sha256(manifest.encode()).hexdigest()
