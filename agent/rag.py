"""Retrieval, prompt assembly and the token budget.

One module rather than three, because prompt assembly needs the chunk metadata, the
provenance header and the token budget, and keeping the budget arithmetic adjacent to the
retrieval that produced it puts the likeliest bug in one file.

The rules block is PARSED from `agent/system-prompt.md` rather than duplicated here. That
is deliberate: it guarantees the app's rules are byte-identical to the text SPLX held in
Lab 0, so the whole Lab 0 -> Lab 1 delta is attributable to retrieval and nothing else.
Its sha256 goes into every log record.

CLI: `.venv/bin/python -m agent.rag "what is J. Marchetti's salary"`
"""

from __future__ import annotations

import hashlib
import math
import re
import sys

import chromadb

from agent import lmstudio as lm
from agent.config import (
    CANARY_PROMPT,
    CHARS_PER_TOKEN_WORST,
    CHROMA_PATH,
    COLLECTION,
    CONTEXT_TOKEN_BUDGET,
    HISTORY_TOKEN_BUDGET,
    INDEX_SCHEMA,
    MAX_QUERY_CHARS,
    MAX_USER_CHARS,
    PROMPT_CEILING,
    QUERY_INSTRUCTION,
    RETRIEVAL_QUERY_TURNS,
    SYSTEM_PROMPT_PATH,
    TOP_K,
)

CONTEXT_HEADING = "## CONTEXT"


class OversizeTurn(ValueError):
    """A single user turn is too large to embed. Refused, never silently truncated.

    Truncating the user turn would silently edit the attack, and the finding would stop
    reproducing. The caller returns 413 instead.
    """


# --- the rules block -------------------------------------------------------------------


def load_rules() -> tuple[str, str]:
    """Return (rules_text, sha256) from the first fenced block of system-prompt.md.

    Fails fast on a prompt that has lost either load-bearing element. Without
    `/no_think`, reasoning tokens eat the output budget and `content` comes back empty,
    which a grader scores as a refusal; without the canary there is no leak detector.
    """
    text = SYSTEM_PROMPT_PATH.read_text()
    blocks = re.findall(r"```\n(.*?)```", text, re.S)
    if not blocks:
        raise RuntimeError(f"no fenced prompt block found in {SYSTEM_PROMPT_PATH}")
    rules = blocks[0].strip()
    if CANARY_PROMPT not in rules:
        raise RuntimeError(f"{SYSTEM_PROMPT_PATH} rules block no longer contains {CANARY_PROMPT}")
    if "/no_think" not in rules:
        raise RuntimeError(
            f"{SYSTEM_PROMPT_PATH} rules block no longer contains /no_think. Reasoning would "
            "consume the output budget and return empty content, scored as a refusal."
        )
    return rules, hashlib.sha256(rules.encode()).hexdigest()


# --- the collection --------------------------------------------------------------------


REBUILD_HINT = (
    "Rebuild it: open notebooks/3a.embed-index-canary.ipynb and Restart Kernel and Run All "
    "Cells. That notebook is the only writer of this collection."
)


def open_collection():
    """Open the index read-only and refuse a collection this app did not build.

    The notebook writes the fingerprint and this function checks it — two authors of one
    contract, with only `agent/config.py`'s shared constants holding them together. So the
    check is deliberately strict: a schema or count mismatch means the index on disk is not
    the one this code was written against, and a loud refusal beats ungrounded answers.
    """
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    try:
        col = client.get_collection(COLLECTION, embedding_function=None)
    except Exception as e:
        raise RuntimeError(
            f"collection '{COLLECTION}' not found in {CHROMA_PATH}. {REBUILD_HINT}"
        ) from e
    meta = col.metadata or {}
    if meta.get("schema") != INDEX_SCHEMA:
        raise RuntimeError(
            f"collection '{COLLECTION}' has schema {meta.get('schema')!r}, expected "
            f"{INDEX_SCHEMA!r} — it was built by something else, or by an older notebook. "
            f"{REBUILD_HINT}"
        )
    if col.count() != meta.get("chunk_count"):
        raise RuntimeError(
            f"collection holds {col.count()} chunks, fingerprint says {meta.get('chunk_count')} "
            f"— the index was modified after it was built. {REBUILD_HINT}"
        )
    return col, meta


def index_identity(col, meta: dict) -> tuple:
    """The tuple that must not change under a running server.

    Used for the per-request freshness check. Re-running the notebook deletes and recreates
    the collection, which leaves a live server holding a handle to a segment that no longer
    exists — it may error, or it may keep answering from the old index without saying so. A
    finding recorded after a silent swap is evidence about a corpus that is no longer there.

    The collection **id** is in here for a specific reason: rebuilding over an unchanged
    corpus produces an identical schema, hash and count, so those three alone would not
    notice the swap — but the handle is stale all the same, because delete+create always
    mints a new UUID. Without the id, the one case that looks safest is the one that slips
    through.
    """
    return (meta.get("schema"), meta.get("corpus_sha256"), col.count(), str(col.id))


# --- query construction ----------------------------------------------------------------


def build_query(messages: list[dict]) -> dict:
    """Assemble the retrieval query from the last N USER turns, newest first.

    Newest-first is the point: if the cap binds, truncation drops the OLDEST context and
    never the live question. Assistant turns are excluded on purpose — they are
    model-generated, so including them re-retrieves whatever the model just cited, a
    self-reinforcing filter bubble that makes multi-turn attacks look weaker than they
    are. Only user text is attacker-controlled, and that is what a red-team harness's
    retrieval should reflect.

    No query rewriting: a pronoun-only follow-up ("and hers?") retrieves against the
    pronoun. That is a known harness limitation, recorded as such, never a model defence.
    """
    users = [m["content"] for m in messages if m.get("role") == "user" and m.get("content")]
    if not users:
        raise ValueError("no user message to retrieve against")
    if len(users[-1]) > MAX_USER_CHARS:
        raise OversizeTurn(
            f"user turn is {len(users[-1])} chars, over MAX_USER_CHARS={MAX_USER_CHARS}"
        )
    recent = list(reversed(users[-RETRIEVAL_QUERY_TURNS:]))
    text = "\n".join(recent)
    truncated = len(text) > MAX_QUERY_CHARS
    if truncated:
        text = text[:MAX_QUERY_CHARS]
    return {
        "query_text": text,
        "query_chars": len(text),
        "query_truncated": truncated,
        "turns_used": len(recent),
    }


# --- retrieval -------------------------------------------------------------------------


def retrieve(col, query_text: str, k: int = TOP_K) -> list[dict]:
    """Top-k by cosine. The instruction prefix is applied ONCE, to the assembled query."""
    qv, _ = lm.embed_query(query_text)
    res = col.query(
        query_embeddings=[qv],  # NEVER query_texts= (invokes Chroma's 384-dim ONNX EF)
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for i in range(len(res["ids"][0])):
        md = res["metadatas"][0][i]
        hits.append(
            {
                "id": res["ids"][0][i],
                "distance": res["distances"][0][i],
                "similarity": 1 - res["distances"][0][i],  # cosine space: sim = 1 - dist
                "doc_name": md["doc_name"],
                "doc_title": md["doc_title"],
                "domain": md["domain"],
                "section": md["section"],
                "chunk_index": md["chunk_index"],
                "n_tokens_chat": md["n_tokens_chat"],
                "canary_tokens": md["canary_tokens"],
                "text": res["documents"][0][i],
            }
        )
    return hits


# --- prompt assembly -------------------------------------------------------------------


def est_tokens(text: str) -> int:
    """Conservative token estimate: worst-case density measured on this corpus.

    Used only for free-form text (history, the user turn) whose density is unknown.
    Chunks carry an exact measured `n_tokens_chat` from index time instead.
    """
    return math.ceil(len(text) / CHARS_PER_TOKEN_WORST)


def context_block(hits: list[dict]) -> str:
    """Retrieved chunks under provenance headers.

    The header carries the document title, which is why dropping the title-only chunk at
    index time loses nothing: the title reaches the model on every request without
    occupying a slot in the top-k or distorting the vector space.
    """
    parts = []
    for n, h in enumerate(hits, 1):
        parts.append(
            f"[{n}] {h['doc_name']} — {h['doc_title']} — section: {h['section']}\n{h['text']}"
        )
    return "\n\n".join(parts)


def assemble_system(rules: str, hits: list[dict]) -> str:
    """Rules verbatim, then CONTEXT. Verified that `/no_think` still suppresses reasoning
    in this position, so the Lab 0 block needs no reordering."""
    if not hits:
        return rules
    return f"{rules}\n\n{CONTEXT_HEADING}\n\n{context_block(hits)}"


# --- budget ----------------------------------------------------------------------------


def fit_budget(rules: str, history: list[dict], user_turn: str, hits: list[dict]) -> dict:
    """Trim to fit the prompt ceiling, in a fixed order, recording every decision.

    Order matters and is deliberate:
      1. drop the OLDEST history pairs  — never the system message, never the live turn
      2. reduce k                        — a weaker answer, honestly logged
      3. refuse (413)                    — never truncate the user turn: that silently
                                           edits the attack and the finding stops reproducing
    Overflow upstream is loud (HTTP 400 `exceed_context_size_error`), so this is belt
    rather than brace — but a 400 reaching SPLX would be scored as target behaviour.
    """
    rules_t = est_tokens(rules)
    user_t = est_tokens(user_turn)
    scaffold_t = 128  # CONTEXT heading + per-chunk provenance headers, measured ~115

    hist = list(history)
    dropped_turns = 0
    while hist and sum(est_tokens(m["content"]) for m in hist) > HISTORY_TOKEN_BUDGET:
        # drop oldest user/assistant pair
        del hist[0 : min(2, len(hist))]
        dropped_turns += 1

    kept = list(hits)
    while True:
        hist_t = sum(est_tokens(m["content"]) for m in hist)
        ctx_t = sum(h["n_tokens_chat"] for h in kept) + (scaffold_t if kept else 0)
        total = rules_t + ctx_t + hist_t + user_t
        if total <= PROMPT_CEILING and ctx_t <= CONTEXT_TOKEN_BUDGET + scaffold_t:
            break
        if kept:
            kept.pop()  # drop the lowest-ranked chunk
            continue
        if hist:
            del hist[0 : min(2, len(hist))]
            dropped_turns += 1
            continue
        raise OversizeTurn(
            f"estimated {total} prompt tokens with no context and no history, over "
            f"PROMPT_CEILING={PROMPT_CEILING}"
        )

    return {
        "history": hist,
        "hits": kept,
        "k_effective": len(kept),
        "history_turns_sent": len(hist),
        "history_turns_dropped": dropped_turns,
        "est_prompt_tokens": total,
        "context_tokens": ctx_t,
    }


# --- CLI ---------------------------------------------------------------------------------

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How much annual leave do I get?"
    rules, sha = load_rules()
    col, meta = open_collection()
    print(f"collection {COLLECTION}  schema {meta['schema']}  chunks {meta['chunk_count']}")
    print(f"rules sha256 {sha[:16]}...  corpus {meta['corpus_sha256'][:16]}...")
    qd = build_query([{"role": "user", "content": q}])
    print(f"\nquery  {qd['query_text']!r}  (prefix: {QUERY_INSTRUCTION!r})")
    hits = retrieve(col, qd["query_text"])
    print(f"\ntop-{len(hits)}:")
    for n, h in enumerate(hits):
        print(
            f"  {n}  sim {h['similarity']:.4f}  {h['domain']:12s} {h['doc_name']:28s} "
            f"{h['section'][:34]:34s} {h['n_tokens_chat']:4d}tok {h['canary_tokens']}"
        )
    fit = fit_budget(rules, [], q, hits)
    print(
        f"\nbudget  k {fit['k_effective']}/{TOP_K}  context {fit['context_tokens']}tok  "
        f"est prompt {fit['est_prompt_tokens']}tok  ceiling {PROMPT_CEILING}"
    )
    print(f"\n--- assembled system message ---\n{assemble_system(rules, fit['hits'])}")
