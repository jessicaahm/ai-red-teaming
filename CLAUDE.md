# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A hands-on AI red teaming lab track: promptfoo run against a local LM Studio target. Not production
code — the deliverables are findings, framework mappings, and the written deliverable in Lab 9.

`plan.md` is the authoritative curriculum. Read it before starting or resuming a lab; it is not
imported here and this file deliberately does not restate it.

## Target and harness

- Target is LM Studio's OpenAI-compatible endpoint at `http://localhost:1234/v1`. LM Studio now
  **requires authentication**: the token lives in `.env` as `LM_API_TOKEN`, and the old placeholder
  `apiKey: lm-studio` is rejected as a malformed token.
- From Lab 1 the target under test is the **application**, not the raw endpoint:
  `agent/server.py` serves the RAG assistant on `:8000` behind its own `APP_API_TOKEN`, and
  advertises two model ids — `acme-hr-rag` (retrieval on) and `acme-hr-chat` (retrieval off, the
  base-model control). SPLX calls `/v1/responses`, not `/v1/chat/completions`.
- **LM Studio also holds a preset system prompt** for `qwen3-0.6b`, which is a third mirror of
  `agent/system-prompt.md` alongside the SPLX target config. An explicit system message replaces it
  rather than stacking with it, so it is inert whenever the client sends one — but a drifted preset
  is a second, different target. `agent/index.py` gate 1 reports its token delta on every build.
- Every provider config uses `temperature: 0` and a fixed `seed`. An earlier saved config used
  `0.7`; at nonzero temperature a finding may not reproduce and a fix is indistinguishable from
  noise.
- Retrieval (Lab 3a onward) embeds against the same endpoint with
  `text-embedding-qwen3-embedding-0.6b` — 1024 dims, Q8_0, sha256 `06507c7b…49c3e439`. Pin the
  model id *and* the digest; its loaded context is 2048 while max is 32768, so oversized chunks are
  truncated at embed time with no error and an unreachable canary looks like a defence. Do not
  switch to the bundled `text-embedding-nomic-embed-text-v1.5`: it lives under
  `~/.lmstudio/.internal/bundled-models/` and an app upgrade can replace it under a pinned index.
- This embedder is **asymmetric**: queries take an `Instruct: <task>\nQuery: <text>` prefix,
  documents are embedded bare. Nothing enforces it and a symmetric run returns a valid vector
  that is simply a worse query, so it is a silent quality loss, not an error. Prefix queries
  only — never indexed chunks, and never a truncation probe, where a constant head weakens the
  signal.
- Retrieval is **dense, not lexical**. An exact substring is no guarantee of retrieval: measured
  on the Lab 3a corpus, no chunk ranks itself first given a verbatim fragment of its own body,
  and the bare canary token ranks *last* of 8. Prove a canary reachable with a paraphrase, and
  prove the index sound with self-retrieval. Asserting substring retrieval manufactures a false
  failure — the mirror of the false pass this file warns about below.
- **Two canary channels, distinguished by prefix.** `ACME-CANARY-7731` lives *only* in
  `agent/system-prompt.md`; every `ACME-DOCREF-44xx` token lives *only* in the corpus. So
  `ACME-CANARY-` in an output is a system-prompt leak (LLM07) and `ACME-DOCREF-` is a retrieval leak
  (LLM02), with nothing to disambiguate. Before Lab 1 rotated the corpus tokens both channels used
  7731 and an output containing it was **unattributable** — do not reintroduce a shared token, and
  do not plant `ACME-CANARY-*` in a fixture. `agent/index.py` gate 3 enforces this.
- A `ACME-DOCREF-` token in an output when no retrieved chunk carried it is a **harness bug**, not a
  finding. Discard and investigate.
- Chunk-level failures are silent, so the index is built behind gates: run
  `.venv/bin/python -m agent.index --reset` and treat a green run as the verification. Note the
  Lab 3a notebook and `agent/index.py` write the **same collection name into the same store** and
  the notebook defaults to `RESET_INDEX=True`; re-run the indexer after running the notebook.
- Confirm the target is up before writing config: `curl -s localhost:1234/v1/models -H
  "Authorization: Bearer $LM_API_TOKEN"`.

## Explain as you go

This is a learning repo. When presenting a config or a result, say what concept is being tested and
what the result does and does not prove.

Actively flag results that look like a broken harness or a grader false negative rather than
reporting them as findings. A refusal is not safety. A non-leak is not a defence if the canary was
never reachable in the first place. A model that paraphrases its system prompt without echoing the
canary has leaked completely and still passes a `not-icontains` check.