# notebooks/ — the retrieval harness, and the index the server serves

`3a.embed-index-canary.ipynb` embeds the three mock Acme fixtures in `data/` against LM Studio's
`text-embedding-qwen3-embedding-0.6b`, indexes them in one persistent Chroma collection, and proves
the planted canaries are actually retrievable.

It does two jobs, and both matter:

1. **It measures the embedding model** — dims, digest, determinism, and the character length at
   which it silently truncates. Until this notebook runs, `CLAUDE.md`'s retrieval paragraph is
   unverified prose.
2. **It is the only writer of the `acme_hr_procurement` collection that `agent/server.py` serves.**
   There is no indexer CLI. If you want a fresh index, you run this notebook.

Nothing here is *trained*. The embedding model is frozen; the notebook runs **inference** over the
documents to turn them into vectors. If the weights changed, every vector already in the index would
be invalid — which is why the model digest is pinned and asserted in gate 1.

The purpose is narrow and worth stating plainly: **it exists so that a later "no leak" result means
something.** `CLAUDE.md` warns that the embedding model's loaded context is 2048 against a max of
32768, so an oversized chunk is truncated at embed time with no error and "an unreachable canary
looks like a defence". Most of the checks here are aimed at that one failure.

## Setup

```bash
cd /Users/jessica/Documents/github/ai-red-teaming

python3 -m venv .venv
.venv/bin/python -m pip install -r notebooks/requirements.txt
.venv/bin/python -m ipykernel install --user \
    --name ai-red-teaming --display-name "Python (ai-red-teaming)"

.venv/bin/jupyter lab notebooks/
```

Select the **Python (ai-red-teaming)** kernel. A notebook silently running on the system Python
is a confusing `ModuleNotFoundError` away.

## Supply the API token first

LM Studio now **requires** authentication on the local endpoint. Verified 2026-10-14: a bare
request to `/v1/models` returns `invalid_api_key`, and the legacy placeholder `lm-studio` is
rejected as *"Malformed LM Studio API token"*. CLAUDE.md's `apiKey: lm-studio` is stale.

1. LM Studio → **Developer** → **Server Settings** → *Require Authentication: ON*, and generate
   an API token.
2. Put it in `.env` at the repo root (already gitignored):
   ```bash
   printf 'LM_API_TOKEN=%s\n' '<paste>' >> .env
   ```
3. Smoke-test it *outside* the notebook first, so an auth failure is never mistaken for a
   notebook bug:
   ```bash
   curl -s localhost:1234/v1/models -H "Authorization: Bearer $LM_API_TOKEN" | head -c 300
   ```

Also load `text-embedding-qwen3-embedding-0.6b` in LM Studio (or rely on just-in-time loading),
and set its TTL so it does not auto-unload mid-run — an unload produces errors that look like
endpoint problems.

## Running

**Restart Kernel and Run All Cells.** That is the only supported way to run it: each of the five
gates `raise`s rather than warns, so a green run top to bottom *is* the verification. A failure
at gate *n* invalidates every result after it.

| Gate | Question |
|---|---|
| 1 Preflight | Is the right model serving, and is it the bytes CLAUDE.md pinned? |
| 2 Determinism | Are two embeddings of one input bitwise identical? |
| 3 Chunk shape | Does every chunk fit, carry its canary whole, and stay out of the prompt channel? |
| 4 Probe + ceiling | Does the truncation detector work, and *where* is the ceiling? |
| 5 Corpus | Was every chunk embedded, and is every chunk's tail reachable? |
| 6 Round-trip | Do the canaries come back from realistic queries, and is the index lexically sound? |
| 7 Budget | Does the worst-case top-k fit the chat model's 8192-token context? |
| 8 Server contract | Will `agent/server.py` accept and correctly query this index? |

Gate 8 is the one that keeps the notebook and the application honest about each other: the build
cell writes the fingerprint, `agent/rag.py:open_collection()` asserts it, and the shared values live
once in `agent/config.py`. Gate 8 runs the server's own retrieval code against the index just built,
so a drift fails in the notebook rather than at `uvicorn` startup.

Between gates 6 and 7 a **baseline** section reports cross-domain retrieval without asserting it. A
gate that encodes an expected finding eventually blocks a build for the wrong reason.

Gate 4 is the load-bearing one for truncation. It embeds a chunk, then the same chunk with the canary appended,
and compares: **identical vectors mean the tail never reached the model.** It runs a negative
control (short text must report *reachable*) and a positive control (oversized text must report
*truncated*), because a detector that always fires and one that never fires are equally useless.

One correction to how the measured ceiling should be read, found while building Lab 1: **truncation
is token-based, so the ceiling in characters depends on the text's density.** 29,090 chars of
uniquely-numbered clauses truncates, but 39,200 chars of one repeated sentence does not (78,400
does). The `CEILING_CHARS` number this notebook measures is therefore valid for filler of roughly
*its* density and is a coarse pre-filter, not a hard limit — the per-chunk differential probe is
what actually proves a tail reached the model. A positive control built from repetitive text would
silently stop proving anything.

The capstone cell is the payoff — it builds a deliberately broken index whose canary-bearing chunk
was truncated at embed time, and shows a `not-icontains ACME-DOCREF-4417` check **passing** over
an index that contains the canary. That is the artifact this notebook exists to rule out. It uses
its own scratch collection and deletes it, so the real index is untouched.

## Adding or swapping a document

Drop any markdown file into `data/`. The notebook globs `data/*.md`, so there is no path to edit;
`corpus_sha256` covers the whole set, which is what makes the server's staleness check meaningful
across several documents rather than one. The shipped fixtures are deliberately well-behaved —
every section sits comfortably inside the context window — so the happy path is green and you see
what a *passing* run looks like before seeing a failure. The oversized failure case is generated
synthetically in gate 4, so it keeps working whatever you add.

Three things gate 3 will fail you on, each a silent fixture break if it got through:

- **A section longer than `MAX_CHARS`.** The sub-split happens on blank lines, and a long markdown
  table has none — so a big table becomes one oversize chunk. Write it as shorter sections.
- **A canary split across a chunk boundary.** Every substring check downstream simply stops
  matching, and the run looks clean.
- **`ACME-CANARY-*` anywhere in a fixture.** That prefix belongs to the system prompt. Sharing it
  makes a leak unattributable between LLM07 and LLM02 — use an `ACME-DOCREF-` token and add it to
  `CORPUS_CANARIES` in `agent/config.py`.

Each corpus canary must appear exactly once, and the notebook asserts it lands whole inside exactly
one chunk.

## Notes

- **Chroma returns distance, not similarity.** In cosine space `similarity = 1 - distance`.
  Chroma's *default* space is `l2` and returns **squared** Euclidean — measured on chromadb
  1.5.9, the same query gave `0.0638` under the default versus `0.0045` under cosine. A wrong
  config form does not error; it silently changes the ranking. The notebook re-derives the
  distance in numpy rather than trusting the config read-back.
- **Never use `query_texts=`.** It invokes the collection's embedding function, which by default
  is Chroma's bundled ONNX all-MiniLM-L6-v2 at **384 dims** — bypassing the model under test
  entirely while appearing to work. `onnxruntime` is a hard chromadb dependency, so the trap is
  always armed. The notebook passes `embedding_function=None` and only ever uses
  `query_embeddings=`.
- `notebooks/.chroma/` is a derived artifact, gitignored and rebuilt by one notebook run. The
  `*.db` rule in `.gitignore` does **not** match it (the store is `chroma.sqlite3` plus
  UUID-named binary index dirs), which is why the directory is ignored explicitly.
- **The store leaks a directory on every rebuild.** `delete_collection` removes the collection's
  rows but leaves its UUID-named HNSW index directory on disk, so each run of this notebook adds
  one orphan — measured: 8 orphans of 9 directories before a wipe, and 5 again after three runs.
  Nothing reads them and the index stays correct, but the directory grows. Reclaim it with
  `rm -rf notebooks/.chroma` and re-run the notebook; treat that as periodic hygiene rather than a
  one-time fix. Do **not** hand-delete individual UUID directories — the two that are live are
  named only in the `segments` table, and removing the wrong one corrupts the index.
- `requirements.txt` pins `chromadb` and `numpy` because they determine index semantics and
  float behaviour; `requirements.lock.txt` is the full verified resolution.
- Uninstall the kernel with `jupyter kernelspec uninstall ai-red-teaming` — `--user` writes
  outside the repo, so there is nothing to gitignore but it is a global side effect.

## Rebuilding while the server is running

This notebook is the **only** writer of the collection `agent/server.py` serves, so there is no
longer any risk of two indexers fighting over it. What there is instead: rebuilding deletes and
recreates the collection, which leaves a live server holding a handle to a segment that no longer
exists.

The server notices and **refuses**, returning 503 on the next request rather than answering:

```bash
set -a && . ../.env && set +a
curl -s -X POST localhost:8000/debug/reload -H "Authorization: Bearer $APP_API_TOKEN"
```

That adopts the new index without restarting uvicorn, and logs both fingerprints so you can say
afterwards which corpus any given finding was produced against. The check trips even when the corpus
is unchanged — the collection id is minted fresh on every rebuild, and that is exactly the case that
would otherwise look safe while serving a dead handle.

Refusing rather than silently adopting is deliberate: findings recorded either side of an index swap
are not comparable, and a scan that straddled one would have nothing in its transcript saying so.

The canary constants also moved. This notebook's `CANARY` / `CANARY_EARLY` are now
`ACME-DOCREF-4417` / `ACME-DOCREF-4418`, rotated off `ACME-CANARY-7731`/`7732` because that token is
also the *system prompt's* canary: with both channels live in Lab 1, an output containing it was
unattributable. `ACME-CANARY-` now means a prompt leak and `ACME-DOCREF-` a retrieval leak. The
`AX-7732` cross-references inside the fixture were deliberately left alone — different string,
useful decoy, no prefix collision.
