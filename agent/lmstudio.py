"""LM Studio client: embeddings and chat over one authenticated httpx client.

Raw httpx rather than the OpenAI SDK, matching Lab 3a — the notebook needs to see exact
wire behaviour (a stub `prompt_tokens: 0`, a 400 on context overflow) that an SDK would
abstract away, and the repo has no `openai` dependency to add.
"""

from __future__ import annotations

import httpx
import numpy as np

from agent.config import (
    BASE_URL,
    CHAT_MODEL,
    EMBED_MODEL,
    MAX_OUTPUT_TOKENS,
    NATIVE_URL,
    QUERY_INSTRUCTION,
    SEED,
    TEMPERATURE,
    require_token,
)

TOKEN = require_token()

client = httpx.Client(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=180.0,
)


class ContextOverflow(RuntimeError):
    """Upstream rejected the prompt as longer than the loaded context.

    Measured 2026-10-14: a ~31k-token request returns HTTP 400 with
    `exceed_context_size_error` and the token counts. This is a HARNESS ARTIFACT, not
    target behaviour — plan/0.chat.md step 6 requires it be discarded, not scored, so it
    gets its own exception type rather than collapsing into a generic error or, worse,
    an empty completion that a grader reads as a refusal.
    """


# --- embeddings ------------------------------------------------------------------------


def embed(text: str) -> tuple[list[float], int | None]:
    """Embed one bare string. Document side — no instruction prefix.

    Never batched: batching introduces padding, which breaks the bitwise comparisons the
    determinism and truncation gates depend on.
    """
    r = client.post("/embeddings", json={"model": EMBED_MODEL, "input": text})
    if r.status_code != 200:
        raise RuntimeError(f"embeddings {r.status_code}: {r.text[:500]}")
    j = r.json()
    # This endpoint always reports 0. Normalising to None makes downstream arithmetic
    # raise instead of silently treating a stub as a real measurement.
    tok = j["usage"]["prompt_tokens"]
    return j["data"][0]["embedding"], (tok or None)


def embed_query(text: str) -> tuple[list[float], int | None]:
    """Embed a query WITH the instruction prefix. Queries only — never indexed chunks."""
    return embed(f"{QUERY_INSTRUCTION}{text}")


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def observed_ctx(model: str = EMBED_MODEL) -> tuple[int | None, int | None]:
    """Read loaded/max context from LM Studio's native REST. Observed, not assumed."""
    r = httpx.get(
        f"{NATIVE_URL}/models/{model}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=15.0,
    )
    if r.status_code != 200:
        return None, None
    j = r.json()
    return j.get("loaded_context_length"), j.get("max_context_length")


# --- chat ------------------------------------------------------------------------------


def chat(
    messages: list[dict],
    max_tokens: int = MAX_OUTPUT_TOKENS,
    seed: int = SEED,
) -> dict:
    """One chat completion at temperature 0 with a fixed seed.

    `seed` is forwarded because upstream /v1/chat/completions accepts it — verified, two
    identical calls return byte-identical content. Worth noting that the Responses API
    SPLX uses has no seed parameter, so routing the server's /v1/responses through this
    path is what restores determinism SPLX cannot ask for itself.
    """
    body = {
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": TEMPERATURE,
        "seed": seed,
        "max_tokens": max_tokens,
    }
    r = client.post("/chat/completions", json=body)
    if r.status_code == 400 and "exceed_context_size" in r.text:
        raise ContextOverflow(r.text[:600])
    if r.status_code != 200:
        raise RuntimeError(f"chat {r.status_code}: {r.text[:500]}")
    return r.json()


def prompt_tokens_for(messages: list[dict]) -> int:
    """Exact prompt token count from upstream, via a 1-token completion.

    The chat endpoint reports `prompt_tokens` honestly (unlike the embeddings endpoint),
    so this is the ground truth used to validate the char-based estimate in the budget
    guard rather than trusting the estimate alone.
    """
    return chat(messages, max_tokens=1)["usage"]["prompt_tokens"]
