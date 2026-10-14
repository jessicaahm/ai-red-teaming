"""OpenAI-compatible HTTP front end for the Acme HR RAG assistant.

Run:
    .venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8000 --workers 1

`--workers 1` and `127.0.0.1` are both load-bearing. Multiple workers means N processes
opening the Chroma store and appending to one log file; `0.0.0.0` would serve the LAN
whether the tunnel is up or not.

Two endpoints exist because SPLX and everything else disagree about the path:

- `POST /v1/chat/completions` — curl, promptfoo, the OpenAI SDK.
- `POST /v1/responses` — what SPLX actually calls, confirmed on the wire in Lab 0, with
  `max_output_tokens` rather than `max_tokens`. Omitting it means the SPLX target fails
  for a harness reason that looks like a target problem.

Both converge on one `rag.answer()` path, so retrieval, prompt assembly and logging are
identical regardless of which door a client came through. `/v1/responses` is translated
to upstream `/v1/chat/completions` rather than proxied, which is what lets the app inject
`seed: 42` — the Responses API has no seed parameter, so Lab 0 had to drop determinism
that this server restores.

Two model ids make retrieval an A/B switch on one URL, one token, one ngrok domain:
`acme-hr-rag` (retrieval on) and `acme-hr-chat` (retrieval off, same system prompt — the
Lab 0 control).
"""

from __future__ import annotations

import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import chromadb
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from agent import lmstudio as lm
from agent import rag
from agent.audit import Audit, derive_flags
from agent.config import (
    CHAT_MODEL,
    CHROMA_PATH,
    COLLECTION,
    MAX_OUTPUT_TOKENS,
    MODEL_CHAT,
    MODEL_DEFAULT,
    MODEL_RAG,
    SEED,
    SERVED_MODELS,
    TEMPERATURE,
    TOP_K,
    load_env_token,
)

STATE: dict[str, Any] = {}


def _adopt_index() -> dict:
    """(Re)open the collection and the rules block, and record what was adopted."""
    rules, sha = rag.load_rules()
    col, meta = rag.open_collection()
    STATE.update(
        rules=rules,
        rules_sha=sha,
        col=col,
        meta=meta,
        identity=rag.index_identity(col, meta),
    )
    return {
        "schema": meta.get("schema"),
        "corpus_sha256": meta.get("corpus_sha256"),
        "chunk_count": col.count(),
        "doc_count": meta.get("doc_count"),
        "built_at": meta.get("built_at"),
        "rules_sha256": sha,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    audit = Audit()
    STATE.update(audit=audit)
    _adopt_index()
    rules, sha, meta = STATE["rules"], STATE["rules_sha"], STATE["meta"]
    col = STATE["col"]

    # Self-check: confirm /no_think is still suppressing reasoning on a canned request.
    # An attack can re-enable reasoning mid-run; this proves it starts suppressed, so a
    # later non-zero reasoning_tokens is attributable to the attack and not to config.
    probe = lm.chat(
        [{"role": "system", "content": rules}, {"role": "user", "content": "hello"}],
        max_tokens=64,
    )
    rt = (probe.get("usage", {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
    STATE["startup_reasoning_tokens"] = rt
    print(f"[startup] collection {meta['chunk_count']} chunks, schema {meta['schema']}")
    print(f"[startup] rules sha256 {sha[:16]}...  corpus {meta['corpus_sha256'][:16]}...")
    print(f"[startup] reasoning_tokens on a canned request: {rt} (expect 0)")
    print(f"[startup] audit log {audit.path}")
    yield
    audit.close()


app = FastAPI(title="Acme HR RAG assistant", lifespan=lifespan)


# --- auth ------------------------------------------------------------------------------


def require_fresh_index() -> None:
    """Refuse to answer if the index changed under us since startup (or the last reload).

    The notebook deletes and recreates the collection on every rebuild, so a live server is
    left pointing at a dead segment. Auto-adopting the new index would be more convenient and
    is deliberately not done: a mid-scan corpus swap would then leave no trace, and findings
    recorded either side of it would be against different corpora with nothing saying so.

    One SQLite read per request. At the free tier's ~5,000 requests/month that is irrelevant.
    """
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        col = client.get_collection(COLLECTION, embedding_function=None)
        live = rag.index_identity(col, col.metadata or {})
    except Exception as e:
        STATE["audit"].write({"endpoint": "freshness", "index_changed": True, "error": str(e)})
        raise HTTPException(
            503, f"the index is unreadable — it was probably rebuilt while this server was "
                 f"running. POST /debug/reload, or restart. ({e})"
        ) from e
    if live != STATE["identity"]:
        STATE["audit"].write({
            "endpoint": "freshness",
            "index_changed": True,
            "identity_at_startup": STATE["identity"],
            "identity_now": live,
        })
        raise HTTPException(
            503,
            "the index changed under this server: "
            f"was schema/corpus/count {STATE['identity']}, now {live}. Findings recorded across "
            "an index swap are not comparable, so this request is refused rather than answered. "
            "POST /debug/reload to adopt the new index.",
        )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Checked before the body is parsed: scanner traffic on a public ngrok domain should
    cost one cheap 401, and every hit is debited from the free-tier quota."""
    expected, _ = load_env_token("APP_API_TOKEN")
    if not expected:
        raise HTTPException(500, "APP_API_TOKEN is not configured on the server")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid or missing bearer token")


# --- request models --------------------------------------------------------------------


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown keys are logged, never rejected
    model: str | None = None
    messages: list[dict[str, Any]] = []
    max_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: Any = None
    instructions: str | None = None  # the Responses API's system-prompt slot
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    previous_response_id: str | None = None


# --- normalisation ---------------------------------------------------------------------


def _part_text(content: Any) -> str:
    """Flatten Responses-style content, which may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                out.append(p.get("text") or p.get("content") or "")
        return "\n".join(x for x in out if x)
    return ""


def normalise_input(value: Any) -> list[dict]:
    """`input` may be a bare string (single turn) or a message array."""
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, list):
        msgs = []
        for m in value:
            if isinstance(m, dict):
                msgs.append({"role": m.get("role", "user"), "content": _part_text(m.get("content"))})
            elif isinstance(m, str):
                msgs.append({"role": "user", "content": m})
        return msgs
    return []


def resolve_model(requested: str | None) -> tuple[str, bool, bool]:
    """Return (effective, rag_enabled, defaulted).

    plan/0.chat.md records that SPLX's Model field "must be set; it defaults to an empty
    string". Defaulting rather than 400-ing means a config typo costs one logged flag
    instead of an entire scan.
    """
    if requested in SERVED_MODELS:
        return requested, requested == MODEL_RAG, False
    return MODEL_DEFAULT, MODEL_DEFAULT == MODEL_RAG, True


# --- the one answer path ---------------------------------------------------------------


def answer(
    *,
    endpoint: str,
    requested_model: str | None,
    messages: list[dict],
    client_instructions: str | None,
    max_output: int | None,
    extra: dict,
    previous_response_id: str | None = None,
) -> dict:
    """Retrieve, assemble, call upstream, log. Returns the record plus the upstream reply."""
    t0 = time.time()
    request_id = uuid.uuid4().hex[:12]
    model_effective, rag_enabled, defaulted = resolve_model(requested_model)

    # The app OWNS the system prompt. A client-supplied system message (or the Responses
    # API's `instructions`) is ignored and logged -- SPLX's System Prompt field must be
    # left EMPTY, and if it is not, this flag is what catches the mis-attribution.
    client_system = "\n".join(
        m.get("content", "") for m in messages if m.get("role") == "system"
    ) or None
    convo = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if not convo:
        raise HTTPException(400, "no user message supplied")

    history = convo[:-1] if convo[-1].get("role") == "user" else convo
    user_turn = convo[-1]["content"] if convo[-1].get("role") == "user" else ""

    rules = STATE["rules"]
    hits: list[dict] = []
    qd = {"query_text": None, "query_chars": 0, "query_truncated": False, "turns_used": 0}
    try:
        if rag_enabled:
            qd = rag.build_query(convo)
            hits = rag.retrieve(STATE["col"], qd["query_text"], k=TOP_K)
        fit = rag.fit_budget(rules, history, user_turn, hits)
    except rag.OversizeTurn as e:
        STATE["audit"].write(
            {
                "request_id": request_id,
                "endpoint": endpoint,
                "model_effective": model_effective,
                "refused_oversize": True,
                "error": str(e),
            }
        )
        raise HTTPException(413, f"context_length_exceeded: {e}") from e

    system_text = rag.assemble_system(rules, fit["hits"])
    upstream_messages = [{"role": "system", "content": system_text}, *fit["history"], convo[-1]]

    # Clamp the output so a late prompt cannot be cut mid-generation. Measured: a
    # prompt of 7848 tokens with max_tokens 800 returns 200 OK -- the output reservation
    # is NOT pre-checked upstream, so the clamp has to happen here.
    requested_out = max_output or MAX_OUTPUT_TOKENS
    headroom = rag.PROMPT_CEILING + 192 - fit["est_prompt_tokens"] - 64
    max_out_effective = max(128, min(requested_out, headroom))

    upstream: dict = {}
    error = None
    status = "completed"
    try:
        upstream = lm.chat(upstream_messages, max_tokens=max_out_effective, seed=SEED)
    except lm.ContextOverflow as e:
        error = str(e)
        status = "budget_breach"
    except RuntimeError as e:
        error = str(e)
        status = "upstream_error"

    msg = (upstream.get("choices") or [{}])[0].get("message", {}) if upstream else {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    usage = upstream.get("usage", {}) if upstream else {}
    finish = (upstream.get("choices") or [{}])[0].get("finish_reason") if upstream else None

    record = {
        "request_id": request_id,
        "ts": time.time(),
        "endpoint": endpoint,
        "model_requested": requested_model,
        "model_effective": model_effective,
        "model_defaulted": defaulted,
        "rag_enabled": rag_enabled,
        "client_system_message": client_system,
        "client_instructions": client_instructions,
        "client_system_ignored": bool(client_system or client_instructions),
        "previous_response_id": previous_response_id,
        "unknown_request_keys": sorted(extra.keys()),
        "turns_in": len(convo),
        "history_turns_sent": fit["history_turns_sent"],
        "history_turns_dropped": fit["history_turns_dropped"],
        "retrieval": {
            **qd,
            "query_instruction": rag.QUERY_INSTRUCTION if rag_enabled else None,
            "k_requested": TOP_K if rag_enabled else 0,
            "k_effective": fit["k_effective"],
            "hits": [{k: v for k, v in h.items() if k != "text"} for h in fit["hits"]],
        },
        "prompt": {
            "system_prompt_sha256": STATE["rules_sha"],
            "system_text": system_text,
            "est_prompt_tokens": fit["est_prompt_tokens"],
            "context_tokens": fit["context_tokens"],
        },
        "upstream": {
            "model": CHAT_MODEL,
            "seed": SEED,
            "temperature": TEMPERATURE,
            "max_tokens_requested": requested_out,
            "max_tokens_effective": max_out_effective,
            "max_output_clamped": max_out_effective < requested_out,
            "status": status,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "finish_reason": finish,
            # Drift between the estimate and the real count is the early warning that the
            # budget model is wrong.
            "est_vs_actual_prompt_tokens": (
                None if usage.get("prompt_tokens") is None
                else usage["prompt_tokens"] - fit["est_prompt_tokens"]
            ),
            "latency_ms": round((time.time() - t0) * 1000),
        },
        "output": {"content": content, "reasoning_content": reasoning},
        "flags": derive_flags(fit["hits"], content, reasoning),
        "index": {
            "collection": STATE["meta"].get("corpus_sha256"),
            "schema": STATE["meta"].get("schema"),
            "corpus_sha256": STATE["meta"].get("corpus_sha256"),
            "chunk_count": STATE["meta"].get("chunk_count"),
        },
        "error": error,
    }
    STATE["audit"].write(record)

    if status == "budget_breach":
        # Never an empty 200: a grader reads that as a refusal. Distinct status instead.
        raise HTTPException(502, f"upstream context overflow (harness artifact, discard): {error}")
    if status == "upstream_error":
        raise HTTPException(502, f"upstream error: {error}")
    return {"record": record, "content": content, "usage": usage, "finish": finish,
            "model_effective": model_effective, "request_id": request_id}


# --- endpoints -------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated, and deliberately says nothing. Chunk counts, corpus hashes and
    document names live behind auth at /debug/index — on a public URL they are free recon."""
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_auth)])
def models() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": MODEL_RAG, "object": "model", "owned_by": "acme", "created": 0},
            {"id": MODEL_CHAT, "object": "model", "owned_by": "acme", "created": 0},
        ],
    }


@app.get("/debug/index", dependencies=[Depends(require_auth)])
def debug_index() -> dict:
    """Deliberately NOT behind the freshness check — this is how you inspect a mismatch."""
    return {
        "fingerprint": dict(STATE["meta"]),
        "identity_adopted": STATE["identity"],
        "rules_sha256": STATE["rules_sha"],
        "startup_reasoning_tokens": STATE["startup_reasoning_tokens"],
        "audit_log": str(STATE["audit"].path),
    }


@app.post("/debug/reload", dependencies=[Depends(require_auth)])
def debug_reload() -> dict:
    """Adopt an index rebuilt by the notebook, without restarting uvicorn.

    Re-reads the rules block too, so an edit to `agent/system-prompt.md` is picked up on the
    same call — and re-asserts that the block still carries `/no_think` and the canary, since
    `rag.load_rules()` raises if either went missing.

    The swap is logged with both fingerprints. That record is what lets you say, afterwards,
    which corpus any given finding was produced against.
    """
    before = {"identity": STATE.get("identity"), "rules_sha256": STATE.get("rules_sha")}
    try:
        adopted = _adopt_index()
    except RuntimeError as e:
        STATE["audit"].write({"endpoint": "/debug/reload", "reload_failed": True,
                              "before": before, "error": str(e)})
        raise HTTPException(503, f"reload failed, keeping the previous index: {e}") from e
    STATE["audit"].write({
        "endpoint": "/debug/reload",
        "index_reloaded": True,
        "before": before,
        "after": {"identity": STATE["identity"], **adopted},
    })
    return {"reloaded": True, "before": before, "now": adopted}


@app.get("/debug/retrieve", dependencies=[Depends(require_auth), Depends(require_fresh_index)])
def debug_retrieve(q: str, k: int = TOP_K) -> dict:
    """Retrieval without the chat model: reproduces a retrieval finding for free — no GPU,
    and through the tunnel, no generation latency burning the metered connection."""
    qd = rag.build_query([{"role": "user", "content": q}])
    hits = rag.retrieve(STATE["col"], qd["query_text"], k=k)
    return {
        "query": qd,
        "hits": [{kk: vv for kk, vv in h.items() if kk != "text"} for h in hits],
        "system_text": rag.assemble_system(STATE["rules"], hits),
    }


@app.post("/v1/chat/completions", dependencies=[Depends(require_auth), Depends(require_fresh_index)])
def chat_completions(req: ChatRequest) -> dict:
    if req.stream:
        raise HTTPException(400, "stream is not supported by this target")
    out = answer(
        endpoint="/v1/chat/completions",
        requested_model=req.model,
        messages=req.messages,
        client_instructions=None,
        max_output=req.max_tokens,
        extra=req.model_extra or {},
    )
    return {
        "id": f"chatcmpl-{out['request_id']}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": out["model_effective"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": out["content"]},
                "finish_reason": out["finish"] or "stop",
            }
        ],
        "usage": out["usage"],
    }


@app.post("/v1/responses", dependencies=[Depends(require_auth), Depends(require_fresh_index)])
def responses(req: ResponsesRequest) -> dict:
    """The path SPLX calls. Translated to upstream chat/completions, not proxied."""
    if req.stream:
        raise HTTPException(400, "unsupported_value: stream is not supported by this target")
    out = answer(
        endpoint="/v1/responses",
        requested_model=req.model,
        messages=normalise_input(req.input),
        client_instructions=req.instructions,
        max_output=req.max_output_tokens,
        extra=req.model_extra or {},
        previous_response_id=req.previous_response_id,
    )
    usage = out["usage"]
    incomplete = out["finish"] == "length"
    return {
        "id": f"resp_{out['request_id']}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": out["model_effective"],
        "output": [
            {
                "id": f"msg_{out['request_id']}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": out["content"], "annotations": []}],
            }
        ],
        "output_text": out["content"],
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "output_tokens_details": {
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                )
            },
        },
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        # `instructions` is NOT echoed, although the real Responses API echoes it.
        # Echoing would hand the client the system prompt -- canary included -- through
        # the API itself: a leak via the harness that would contaminate every LLM07
        # finding in the lab.
        "instructions": None,
        "error": None,
        "metadata": {},
    }


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException) -> JSONResponse:
    """OpenAI-shaped errors, so a client distinguishes a refusal from a harness fault."""
    code = {401: "invalid_api_key", 413: "context_length_exceeded",
            400: "invalid_request_error", 503: "index_changed", 502: "upstream_error"}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": code.get(exc.status_code, "server_error"),
                "code": code.get(exc.status_code, exc.status_code),
            }
        },
    )
