# Purpose of Repo
AI Red Teaming

## Tool used
promptfoo
lmstudio
SPLX (Probe) — SaaS red-team runner, reaches the local target over an ngrok tunnel
ngrok — exposes the LM Studio REST API so SPLX can hit it

## Target

At Lab 0 the target is **raw LM Studio** (`qwen3-0.6b`) on its OpenAI-compatible endpoint, with no
application in front of it. The HR persona, policy and canary live in the SPLX target config, not in
this repo's code — `agent/system-prompt.md` is the version-controlled mirror of that prompt.

This means Lab 0 measures the base model, not the Acme HR assistant. The RAG index, agent tools and
renderer harness in `img/hr-assistant-architecture.svg` do not exist yet, so the findings that
depend on them (indirect injection, retrieval poisoning, on-the-wire exfiltration, excessive agency)
are out of scope until a later lab builds them. See `plan/0.chat.md`.
