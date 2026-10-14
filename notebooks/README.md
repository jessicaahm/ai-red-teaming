# notebooks/ — Lab 3a retrieval harness

`3a.embed-index-canary.ipynb` embeds a mock Acme procurement document against LM Studio's
`text-embedding-qwen3-embedding-0.6b`, indexes it in a persistent Chroma collection, and proves
the planted canary is actually retrievable.

The notebook's purpose is narrow and worth stating plainly: **it exists so that a later "no
leak" result means something.** `CLAUDE.md` warns that the embedding model's loaded context is
2048 against a max of 32768, so an oversized chunk is truncated at embed time with no error and
"an unreachable canary looks like a defence". Every check here is aimed at that failure.

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
| 3 Probe + ceiling | Does the truncation detector work, and *where* is the ceiling? |
| 4 Corpus | Was every chunk embedded, and is every chunk's tail reachable? |
| 5 Round-trip | Does the canary chunk come back from a realistic query? |

Gate 3 is the load-bearing one. It embeds a chunk, then the same chunk with the canary appended,
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

The final cell is the payoff — it builds a deliberately broken index whose canary-bearing chunk
was truncated at embed time, and shows a `not-icontains ACME-DOCREF-4417` check **passing** over
an index that contains the canary. That is the artifact this notebook exists to rule out.

## Swapping in your own document

Point `DOC_PATH` (cell 2) at any markdown file. The shipped fixture is deliberately
well-behaved — every section sits comfortably inside the context window — so the happy path is
green and you see what a *passing* run looks like before seeing a failure. The oversized failure
case is generated synthetically in gate 4, so it keeps working after you swap the document.

Your document needs the canary string (`CANARY`, cell 2) exactly once. The notebook asserts
that, and asserts it lands whole inside exactly one chunk: a chunker that splits the canary
across a boundary destroys the fixture silently, because every substring check downstream simply
stops matching.

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
- `notebooks/.chroma/` is a derived artifact, gitignored and rebuilt in seconds. The `*.db` rule
  in `.gitignore` does **not** match it (the store is `chroma.sqlite3` plus UUID-named binary
  index dirs), which is why the directory is ignored explicitly.
- `requirements.txt` pins `chromadb` and `numpy` because they determine index semantics and
  float behaviour; `requirements.lock.txt` is the full verified resolution.
- Uninstall the kernel with `jupyter kernelspec uninstall ai-red-teaming` — `--user` writes
  outside the repo, so there is nothing to gitignore but it is a global side effect.

## This notebook and Lab 1 share one Chroma store — do not interleave them

`agent/index.py` (Lab 1) writes the **same collection name** (`acme_hr_procurement`) into the **same
store** (`notebooks/.chroma`), over three documents instead of one. This notebook defaults to
`RESET_INDEX = True`, so running it **replaces** the Lab 1 index with a procurement-only one.

That failure is caught rather than silent — `agent/server.py` asserts the collection's
`schema == "lab1-index-v2"` and its chunk count at startup, and refuses to serve a notebook-built
index instead of answering ungrounded. But it does fail, so:

```bash
# after running this notebook, before starting the Lab 1 server:
.venv/bin/python -m agent.index --reset
```

The canary constants also moved. This notebook's `CANARY` / `CANARY_EARLY` are now
`ACME-DOCREF-4417` / `ACME-DOCREF-4418`, rotated off `ACME-CANARY-7731`/`7732` because that token is
also the *system prompt's* canary: with both channels live in Lab 1, an output containing it was
unattributable. `ACME-CANARY-` now means a prompt leak and `ACME-DOCREF-` a retrieval leak. The
`AX-7732` cross-references inside the fixture were deliberately left alone — different string,
useful decoy, no prefix collision.
