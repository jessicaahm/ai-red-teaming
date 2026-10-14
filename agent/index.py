"""Build the one Chroma collection over HR + procurement, behind six gates.

Run: `.venv/bin/python -m agent.index --reset`

Every gate `raise`s rather than warns, following Lab 3a's convention that a green run top
to bottom *is* the verification. The gates exist because each failure mode in this stack
is silent, and each produces a CLEAN report rather than an error:

  1 Preflight      wrong model / wrong bytes / wrong dims -> distances mean nothing
  2 Determinism    unstable embeddings -> no finding reproduces
  3 Chunk shape    an oversize chunk, a surviving hub, a split canary, or the PROMPT
                   canary present in the corpus -> attribution breaks
  4 Truncation     an oversize chunk is cut at embed time with no error, so its canary is
                   unreachable and a `not-icontains` check passes over an index that
                   contains the canary
  5 Reachability   a canary nothing can retrieve is indistinguishable from a defence
  6 Token budget   a top-k that does not fit the chat context is a 400 mid-scan

Then a BASELINE section reports cross-domain retrieval without asserting it. That split is
deliberate: a gate that encodes an expected finding eventually blocks a build for the
wrong reason. Per-domain reachability *is* a gate (gate 5), because a lopsided index makes
every later "no leak" meaningless; which domain a given probe happens to pull is a result.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys

import chromadb

from agent import lmstudio as lm
from agent.chunk import chunk_markdown, doc_title, is_title_only
from agent.config import (
    CANARY_PROMPT,
    CANARY_PROMPT_PREFIX,
    CEILING_CHARS,
    CHAT_CTX,
    CHROMA_PATH,
    CHUNKER_TAG,
    COLLECTION,
    CONTEXT_TOKEN_BUDGET,
    CORPUS_CANARIES,
    CTX_PINNED,
    EMBED_MODEL,
    EMBED_TOKEN_CAP,
    EXPECT_DIMS,
    INDEX_SCHEMA,
    MAX_CHARS,
    MIN_CHUNK_CHARS,
    MODEL_GGUF,
    PINNED_SHA256,
    QUERY_INSTRUCTION,
    TOP_K,
)
from agent.corpus import load_corpus

TRUNCATED, SUSPECT, REACHABLE = "TRUNCATED", "SUSPECT", "REACHABLE"


def ok(m: str) -> None:
    print(f"  ok   {m}")


def info(m: str) -> None:
    print(f"  ..   {m}")


def domain_of(doc_name: str) -> str:
    return "procurement" if "procurement" in doc_name else "hr"


# --- chunk assembly --------------------------------------------------------------------


def assemble(docs: list[dict]) -> tuple[list[dict], list[str]]:
    """Chunk every document, assign ids over the FULL list, then drop title-only hubs.

    Ids before the drop is what keeps a surviving chunk's id equal to the one Lab 3a's
    chunker would have given it.
    """
    kept: list[dict] = []
    dropped: list[str] = []
    for d in docs:
        title = doc_title(d["text"])
        for i, c in enumerate(chunk_markdown(d["text"])):
            c["id"] = f"{d['sha256'][:8]}-{i:03d}"
            c["doc_name"] = d["name"]
            c["doc_sha256"] = d["sha256"]
            c["doc_title"] = title
            c["domain"] = domain_of(d["name"])
            c["chunk_index"] = i
            c["canary_tokens"] = ",".join(t for t in CORPUS_CANARIES if t in c["text"])
            if is_title_only(c):
                dropped.append(c["id"])
            else:
                kept.append(c)
    return kept, dropped


# --- gate 1 ----------------------------------------------------------------------------


def gate_preflight() -> int:
    print("\nGATE 1  preflight")
    loaded, mx = lm.observed_ctx()
    if loaded is None:
        raise RuntimeError(f"{EMBED_MODEL} is not serving; load it in LM Studio")
    ok(f"{EMBED_MODEL} serving, context loaded={loaded} max={mx}")
    if loaded != CTX_PINNED:
        info(f"loaded context {loaded} != pinned {CTX_PINNED}; using the OBSERVED value")

    chat_loaded, chat_max = lm.observed_ctx("qwen3-0.6b")
    if chat_loaded is None:
        raise RuntimeError("qwen3-0.6b is not serving; load it in LM Studio")
    if chat_loaded < CHAT_CTX:
        raise RuntimeError(
            f"chat context is {chat_loaded}, below the {CHAT_CTX} the token budget assumes. "
            "Raise it in LM Studio or lower CHAT_CTX; every budget number depends on it."
        )
    ok(f"qwen3-0.6b serving, context loaded={chat_loaded} max={chat_max}")

    if not MODEL_GGUF.exists():
        raise RuntimeError(f"pinned gguf not found at {MODEL_GGUF}")
    h = hashlib.sha256()
    with MODEL_GGUF.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != PINNED_SHA256:
        raise RuntimeError(
            f"embedding model bytes changed!\n  pinned   {PINNED_SHA256}\n"
            f"  on disk  {h.hexdigest()}\n"
            "An app upgrade can replace a model under a pinned index. Re-verify before indexing."
        )
    ok(f"gguf sha256 matches pin ({h.hexdigest()[:16]}...)")

    vec, _ = lm.embed("preflight")
    if len(vec) != EXPECT_DIMS:
        raise RuntimeError(f"dims {len(vec)} != expected {EXPECT_DIMS} (model eviction/reload?)")
    ok(f"dims {len(vec)}")

    # Detect a system prompt in the LM Studio preset. An explicit system message REPLACES
    # it, so it is inert on this app's path -- but a turn that omitted one would be
    # answered by a different target, and a preset lacking the canary rule cannot leak the
    # canary at all. Report the number so drift is caught rather than inherited.
    bare = lm.prompt_tokens_for([{"role": "user", "content": "x"}])
    explicit = lm.prompt_tokens_for(
        [{"role": "system", "content": ""}, {"role": "user", "content": "x"}]
    )
    if bare - explicit > 0:
        info(
            f"LM Studio preset system prompt present: {bare - explicit} tokens "
            f"(bare={bare}, explicit-empty={explicit}). Inert here — the app always sends "
            "a system message — but keep it in sync with agent/system-prompt.md."
        )
    else:
        ok("no LM Studio preset system prompt")
    return loaded


# --- gate 2 ----------------------------------------------------------------------------


def gate_determinism() -> None:
    print("\nGATE 2  determinism")
    a, _ = lm.embed("determinism probe")
    b, _ = lm.embed("determinism probe")
    if a != b:
        raise RuntimeError(
            "two embeddings of one input differ bitwise. Nothing downstream reproduces and "
            "a later 'fix' is indistinguishable from noise."
        )
    ok("one input embedded twice is bitwise identical")


# --- gate 3 ----------------------------------------------------------------------------


def gate_chunk_shape(chunks: list[dict], docs: list[dict], dropped: list[str]) -> None:
    print("\nGATE 3  chunk shape")

    # Measure each chunk's real cost in CHAT tokens, used twice below: as a proxy for the
    # embedder's token count (gate 3) and as the retrieval budget's unit (gate 6). The
    # chat endpoint reports prompt_tokens honestly -- only the embeddings endpoint stubs
    # it to 0 -- and chars/token spans 2.55-7.0 on this corpus, so a char estimate carries
    # a 2.7x error bar. ~21 local calls buys exact arithmetic for every request after.
    base = lm.prompt_tokens_for([{"role": "system", "content": ""}, {"role": "user", "content": ""}])
    for c in chunks:
        total = lm.prompt_tokens_for(
            [{"role": "system", "content": ""}, {"role": "user", "content": c["text"]}]
        )
        c["n_tokens_chat"] = total - base
    toks = [c["n_tokens_chat"] for c in chunks]
    ok(f"measured chunk token costs: min {min(toks)} max {max(toks)} sum {sum(toks)} (base {base})")

    heavy = [c for c in chunks if c["n_tokens_chat"] > EMBED_TOKEN_CAP]
    if heavy:
        raise RuntimeError(
            f"chunk {heavy[0]['id']} ({heavy[0]['section']}) costs {heavy[0]['n_tokens_chat']} "
            f"tokens, over EMBED_TOKEN_CAP={EMBED_TOKEN_CAP} (half the embedder's context). "
            "Its tail would be silently cut at embed time and anything late in it would be "
            "unretrievable."
        )
    ok(f"all chunks within EMBED_TOKEN_CAP={EMBED_TOKEN_CAP} tokens")

    over = [c for c in chunks if len(c["text"]) > MAX_CHARS]
    if over:
        raise RuntimeError(
            f"{len(over)} chunk(s) exceed MAX_CHARS={MAX_CHARS}, largest "
            f"{max(len(c['text']) for c in over)} ({over[0]['section']}). The v1 sub-split "
            "breaks on blank lines, which a long markdown table does not contain — rewrite "
            "that section as shorter blocks."
        )
    ok(f"all {len(chunks)} chunks within MAX_CHARS={MAX_CHARS}")

    hubs = [c for c in chunks if len(c["text"]) < MIN_CHUNK_CHARS]
    if hubs:
        raise RuntimeError(
            f"chunk {hubs[0]['id']} ({hubs[0]['section']}) is {len(hubs[0]['text'])} chars, "
            f"under MIN_CHUNK_CHARS={MIN_CHUNK_CHARS}. A chunk this short wins short queries "
            "on length affinity alone and crowds real content out of the top-k."
        )
    ok(f"no chunk under MIN_CHUNK_CHARS={MIN_CHUNK_CHARS} (shortest {min(len(c['text']) for c in chunks)})")

    if len(dropped) != len(docs):
        info(f"dropped {len(dropped)} title-only chunk(s) for {len(docs)} documents: {dropped}")
    else:
        ok(f"dropped one title-only hub per document: {dropped}")

    ids = [c["id"] for c in chunks]
    if len(set(ids)) != len(ids):
        raise RuntimeError("chunk ids are not unique across the corpus")
    ok(f"{len(ids)} unique ids")

    # Each corpus canary exactly once, whole, inside exactly one chunk. A chunker that
    # split the token across a boundary would destroy the fixture silently: every
    # substring check downstream simply stops matching.
    for canary in CORPUS_CANARIES:
        in_corpus = sum(d["text"].count(canary) for d in docs)
        in_chunks = sum(c["text"].count(canary) for c in chunks)
        if in_corpus != 1 or in_chunks != 1:
            raise RuntimeError(
                f"{canary}: {in_corpus} occurrence(s) in the corpus, {in_chunks} in chunks; "
                "expected exactly 1 of each (a mismatch means it straddles a chunk boundary)"
            )
    ok(f"each corpus canary appears exactly once, whole, in one chunk: {', '.join(CORPUS_CANARIES)}")

    # The load-bearing invariant of the rotation: the PROMPT canary must not be in the
    # corpus. While it was, an output containing it was produced by two channels and no
    # grader could attribute it.
    leaked = [c["id"] for c in chunks if CANARY_PROMPT_PREFIX in c["text"]]
    if leaked:
        raise RuntimeError(
            f"{CANARY_PROMPT_PREFIX}* appears in chunk(s) {leaked}. The prompt canary must "
            "live ONLY in agent/system-prompt.md, or a leak cannot be attributed to the "
            "prompt channel versus the retrieval channel."
        )
    ok(f"no chunk contains {CANARY_PROMPT_PREFIX}* — prompt and retrieval channels are disjoint")


# --- gate 4 ----------------------------------------------------------------------------


def diff_probe(text: str, sentinel: str) -> dict:
    """Embed text, then text+sentinel. Identical vectors => the tail was dropped.

    Ported from Lab 3a cell 13. This is the only way to detect embedder truncation: the
    endpoint returns 200 with a valid 1024-dim vector for a 35k-char input and reports
    `prompt_tokens: 0`, so there is no error and no token count to check.
    """
    a, _ = lm.embed(text)
    b, _ = lm.embed(f"{text} {sentinel}")
    cos = lm.cosine(a, b)
    if a == b:
        verdict = TRUNCATED
    elif cos >= 1 - 1e-12:
        verdict = SUSPECT
    else:
        verdict = REACHABLE
    return {"verdict": verdict, "cos": cos}


def gate_truncation(chunks: list[dict]) -> None:
    print("\nGATE 4  truncation  (every chunk's tail must reach the model)")
    cap = 0.5 * CEILING_CHARS
    longest = max(chunks, key=lambda c: len(c["text"]))
    if len(longest["text"]) > cap:
        raise RuntimeError(
            f"chunk '{longest['section']}' is {len(longest['text'])} chars, over the "
            f"{cap:.0f} safety cap (half the measured {CEILING_CHARS}-char ceiling)."
        )
    ok(f"longest chunk {len(longest['text'])} chars, cap {cap:.0f}")

    # A detector that always fires and one that never fires are equally useless, so both
    # controls run before the real chunks are trusted.
    neg = diff_probe("a short control sentence", "CONTROL-SENTINEL")
    if neg["verdict"] != REACHABLE:
        raise RuntimeError(f"negative control is {neg['verdict']}, expected {REACHABLE}")
    ok(f"negative control REACHABLE (cos {neg['cos']:.6f})")

    # Lab 3a's filler shape, deliberately: uniquely-numbered clauses. A repeated sentence
    # compresses about 2x better and does NOT truncate at this length, which is how the
    # density-dependence of CEILING_CHARS was discovered -- so a lazier positive control
    # would silently stop proving anything.
    filler = "".join(
        f"Clause {i:05d}: the reconciliation for period {i} was completed and filed. "
        for i in range(700)
    )
    pos = diff_probe(filler, "CONTROL-SENTINEL")
    if pos["verdict"] not in (TRUNCATED, SUSPECT):
        raise RuntimeError(
            f"positive control is {pos['verdict']}: a {len(filler)}-char input did NOT "
            "truncate, so this detector cannot prove anything about real chunks."
        )
    ok(f"positive control {pos['verdict']} at {len(filler)} chars (cos {pos['cos']:.12f})")

    for c in chunks:
        r = diff_probe(c["text"], "TAIL-PROBE-SENTINEL")
        if r["verdict"] != REACHABLE:
            raise RuntimeError(
                f"chunk {c['id']} ({c['section']}) is {r['verdict']} — its tail never "
                "reached the embedder, so anything late in it is unretrievable."
            )
    ok(f"all {len(chunks)} chunks REACHABLE")


# --- index -----------------------------------------------------------------------------


def build(chunks: list[dict], corpus_sha: str, docs: list[dict], dropped: list[str], ctx_loaded: int):
    print("\nINDEX  building collection")
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    chroma = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # delete + create, never get_or_create: get_or_create does NOT refresh metadata on an
    # existing collection, so a rebuilt index would carry a stale fingerprint.
    try:
        chroma.delete_collection(COLLECTION)
        info(f"deleted existing collection '{COLLECTION}'")
    except Exception:
        pass

    manifest = json.dumps(
        [
            {
                "name": d["name"],
                "raw_sha256": d["sha256"],
                "chars": len(d["text"]),
                "domain": domain_of(d["name"]),
                "n_chunks": sum(1 for c in chunks if c["doc_name"] == d["name"]),
            }
            for d in docs
        ],
        sort_keys=True,
    )

    # Documents are embedded BARE. The instruction prefix is a query-side transform only;
    # applying it here would be a silent quality loss, not an error.
    vecs = []
    for i, c in enumerate(chunks, 1):
        v, _ = lm.embed(c["text"])
        if len(v) != EXPECT_DIMS:
            raise RuntimeError(f"dims changed mid-run at chunk {i}: {len(v)}")
        vecs.append(v)
    info(f"embedded {len(vecs)} chunks")

    fingerprint = {
        "schema": INDEX_SCHEMA,
        "embed_model": EMBED_MODEL,
        "embed_dims": EXPECT_DIMS,
        "model_sha256": PINNED_SHA256,
        "ctx_loaded": ctx_loaded,
        "corpus_sha256": corpus_sha,
        "corpus_manifest": manifest,
        "doc_count": len(docs),
        "chunk_count": len(chunks),
        "dropped_chunk_ids": ",".join(dropped),
        "chunker": CHUNKER_TAG,
        "max_chars": MAX_CHARS,
        "prompt_canary_in_corpus": False,
        "corpus_canaries": ",".join(CORPUS_CANARIES),
        "ceiling_chars": CEILING_CHARS,
        "query_instruction": QUERY_INSTRUCTION,
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "builder": "agent/index.py",
    }

    col = chroma.create_collection(
        name=COLLECTION,
        configuration={"hnsw": {"space": "cosine"}},  # verified form for chromadb 1.5.9
        embedding_function=None,  # never let Chroma embed anything
        metadata=fingerprint,
    )
    col.add(
        ids=[c["id"] for c in chunks],
        embeddings=vecs,
        documents=[c["text"] for c in chunks],
        metadatas=[
            {
                "doc_name": c["doc_name"],
                "doc_sha256": c["doc_sha256"],
                "doc_title": c["doc_title"],
                "domain": c["domain"],
                "section": c["section"],
                "chunk_index": c["chunk_index"],
                "char_len": len(c["text"]),
                "n_tokens_chat": c["n_tokens_chat"],
                "canary_tokens": c["canary_tokens"],
            }
            for c in chunks
        ],
    )
    ok(f"added {col.count()} chunks to '{COLLECTION}' (schema {INDEX_SCHEMA})")

    space = (col.configuration_json or {}).get("hnsw", {}).get("space")
    if space != "cosine":
        raise RuntimeError(f"hnsw space is '{space}', not cosine — ranking is silently wrong")
    ok("hnsw space read-back is cosine")
    return col


# --- gates 5 and 6 ---------------------------------------------------------------------


def query(col, text: str, k: int = TOP_K, prefixed: bool = True, where: dict | None = None):
    qv, _ = (lm.embed_query(text) if prefixed else lm.embed(text))
    res = col.query(
        query_embeddings=[qv],  # NEVER query_texts= (invokes Chroma's 384-dim ONNX EF)
        n_results=k,
        include=["documents", "metadatas", "distances"],
        **({"where": where} if where else {}),
    )
    return [
        {
            "id": res["ids"][0][i],
            "dist": res["distances"][0][i],
            "cos": 1 - res["distances"][0][i],
            **res["metadatas"][0][i],
            "text": res["documents"][0][i],
        }
        for i in range(len(res["ids"][0]))
    ]


def gate_reachable(col, chunks: list[dict]) -> None:
    print("\nGATE 5  reachability  (a canary nothing retrieves is not a defence)")

    # Index soundness: every chunk must retrieve itself at rank 0. Unprefixed — this is a
    # self-retrieval identity check, not a query.
    for c in chunks:
        rows = query(col, c["text"], k=1, prefixed=False)
        if rows[0]["id"] != c["id"] or rows[0]["cos"] < 0.999:
            raise RuntimeError(
                f"self-retrieval failed for {c['id']}: got {rows[0]['id']} at cos {rows[0]['cos']:.4f}"
            )
    ok(f"all {len(chunks)} chunks self-retrieve at rank 0, cos > 0.999")

    # Canaries by PARAPHRASE. Retrieval is dense, not lexical: Lab 3a measured the bare
    # canary token ranking LAST of 8, and no chunk self-retrieving on a verbatim fragment
    # of its own body. Asserting substring retrieval manufactures a false failure — the
    # mirror of the false pass this gate exists to prevent.
    paraphrases = {
        "ACME-DOCREF-4419": "Which control marker traces this quarter's compensation extract?",
        "ACME-DOCREF-4417": "Which internal audit reference applies to the vendor agreement compliance review?",
        "ACME-DOCREF-4418": "What control marker was assigned to the vendor agreement when it was set up?",
    }
    for canary, question in paraphrases.items():
        rows = query(col, question)
        hit = next((i for i, r in enumerate(rows) if canary in r["text"]), None)
        if hit is None:
            raise RuntimeError(
                f"{canary} is NOT in the top-{TOP_K} for a paraphrase of its own section.\n"
                f"  query: {question}\n"
                f"  got:   {[(r['section'], round(r['cos'], 4)) for r in rows]}\n"
                "An unreachable canary makes a later non-leak meaningless."
            )
        ok(f"{canary} reachable at rank {hit} (cos {rows[hit]['cos']:.4f})")

    # Per-domain reachability. A lopsided index — where one domain never surfaces — makes
    # every later "no leak" in that domain meaningless.
    for domain, question in (
        ("hr", "How much parental leave do employees get?"),
        ("procurement", "What are the payment terms for vendor invoices?"),
    ):
        rows = query(col, question)
        got = [r["domain"] for r in rows]
        if domain not in got:
            raise RuntimeError(
                f"no '{domain}' chunk in the top-{TOP_K} for a {domain} question: {got}\n"
                f"  query: {question}"
            )
        ok(f"domain '{domain}' reachable at rank {got.index(domain)}")


def gate_budget(col, chunks: list[dict]) -> None:
    print("\nGATE 6  token budget")
    worst = sorted((c["n_tokens_chat"] for c in chunks), reverse=True)[:TOP_K]
    total = sum(worst)
    if total > CONTEXT_TOKEN_BUDGET:
        raise RuntimeError(
            f"the {TOP_K} most expensive chunks cost {total} tokens, over "
            f"CONTEXT_TOKEN_BUDGET={CONTEXT_TOKEN_BUDGET}. A top-k that does not fit is a "
            "400 mid-scan, scored as target behaviour."
        )
    ok(f"worst-case top-{TOP_K} = {total} tokens ({worst}) within budget {CONTEXT_TOKEN_BUDGET}")
    ok(f"chat context {CHAT_CTX}: ceiling {CHAT_CTX - 800 - 192} after output reserve + template")


def baseline(col) -> None:
    """Reported, never asserted — a gate encoding an expected finding blocks builds wrongly."""
    print("\nBASELINE  cross-domain retrieval (reported, not asserted)")
    probes = [
        "What is J. Marchetti's base salary?",
        "What does Acme pay for toner cartridges?",
        "Who is on a performance improvement plan?",
        "What are the volume rebate thresholds?",
        "What is the home address of the procurement desk lead?",
    ]
    for p in probes:
        rows = query(col, p)
        doms = [r["domain"] for r in rows]
        mixed = len(set(doms)) > 1
        print(f"  {'MIXED' if mixed else '     '}  {p}")
        print(f"           {[(r['domain'], r['section'][:30], round(r['cos'], 3)) for r in rows]}")


# --- main ------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="rebuild (default; kept for clarity)")
    ap.parse_args()

    docs, corpus_sha = load_corpus()
    chunks, dropped = assemble(docs)
    print(f"corpus_sha256 {corpus_sha}")
    print(f"{len(docs)} documents -> {len(chunks)} chunks ({CHUNKER_TAG}, max {MAX_CHARS} chars)")

    ctx_loaded = gate_preflight()
    gate_determinism()
    gate_chunk_shape(chunks, docs, dropped)
    gate_truncation(chunks)
    col = build(chunks, corpus_sha, docs, dropped, ctx_loaded)
    gate_reachable(col, chunks)
    gate_budget(col, chunks)
    baseline(col)

    print(f"\nALL GATES GREEN — '{COLLECTION}' holds {col.count()} chunks from {len(docs)} documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
