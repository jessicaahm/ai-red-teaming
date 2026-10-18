"""Streamlit chat front end for the Acme HR RAG assistant.

Run (with the agent already up on :8000):
    .venv/bin/streamlit run ui/chat.py --server.address 127.0.0.1

A client of the application, never of LM Studio: every turn goes through
`/v1/chat/completions` on the agent, so what you see here is what promptfoo and SPLX see.
The token is read server-side by Streamlit and never reaches the browser.

The retrieval panel reads the server's own audit record for each request rather than
re-querying `/debug/retrieve`: the record holds the chunks that actually reached the prompt
after budget trimming, and the canary flags the server derived from them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.config import MODEL_CHAT, MODEL_RAG, load_env_token  # noqa: E402

BASE = "http://127.0.0.1:8000"
TOKEN, _ = load_env_token("APP_API_TOKEN")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

st.set_page_config(page_title="Acme HR assistant", layout="wide")


@st.cache_data(ttl=30)
def audit_path() -> str | None:
    r = httpx.get(f"{BASE}/debug/index", headers=HEADERS, timeout=10)
    return r.json().get("audit_log") if r.status_code == 200 else None


def audit_record(request_id: str) -> dict | None:
    path = audit_path()
    if not path or not Path(path).exists():
        return None
    for line in reversed(Path(path).read_text().splitlines()):
        rec = json.loads(line)
        if rec.get("request_id") == request_id:
            return rec
    return None


def show_record(rec: dict | None) -> None:
    if rec is None:
        st.caption("No audit record found for this request.")
        return
    flags = rec.get("flags") or {}
    if flags.get("prompt_canary_leaked"):
        st.error("ACME-CANARY- in output: system-prompt leak (LLM07)")
    if flags.get("retrieval_canary_leaked"):
        st.error(f"ACME-DOCREF- in output: retrieval leak (LLM02) {flags['retrieval_canary_leaked']}")
    if flags.get("orphan_retrieval_canary"):
        st.warning("DOCREF token with no retrieved chunk carrying it: harness bug, not a finding")
    with st.expander("Retrieval and flags"):
        hits = (rec.get("retrieval") or {}).get("hits") or []
        if hits:
            st.dataframe(
                [{k: h.get(k) for k in ("id", "similarity", "doc_name", "section", "canary_tokens")}
                 for h in hits],
                hide_index=True,
            )
        else:
            st.caption("No chunks in context (retrieval off, or none fit the budget).")
        st.json(flags, expanded=False)


with st.sidebar:
    model = st.radio(
        "Model", [MODEL_RAG, MODEL_CHAT],
        captions=["retrieval on", "retrieval off (control)"],
    )
    max_tokens = st.slider("max_tokens", 64, 1024, 300, step=32)
    if st.button("Clear conversation"):
        st.session_state.turns = []
    try:
        ok = httpx.get(f"{BASE}/healthz", timeout=3).status_code == 200
    except httpx.HTTPError:
        ok = False
    st.caption(("🟢 agent up" if ok else "🔴 agent down") + f" at {BASE}")

st.session_state.setdefault("turns", [])

for t in st.session_state.turns:
    with st.chat_message(t["role"]):
        st.markdown(t["content"])
        if t["role"] == "assistant":
            st.caption(f"{t['model']} · {t['request_id']}")
            show_record(t.get("record"))

if prompt := st.chat_input("Ask the HR assistant"):
    st.session_state.turns.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    messages = [{"role": t["role"], "content": t["content"]} for t in st.session_state.turns]
    with st.chat_message("assistant"):
        try:
            r = httpx.post(
                f"{BASE}/v1/chat/completions", headers=HEADERS, timeout=180,
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
            )
        except httpx.HTTPError as e:
            st.error(f"Request failed: {e}")
            st.stop()
        if r.status_code != 200:
            # 503 after a notebook rebuild is by design: POST /debug/reload, then retry.
            st.error(f"{r.status_code}: {r.text}")
            st.session_state.turns.pop()
            st.stop()
        body = r.json()
        content = body["choices"][0]["message"]["content"]
        request_id = body["id"].removeprefix("chatcmpl-")
        record = audit_record(request_id)
        st.markdown(content)
        st.caption(f"{body['model']} · {request_id}")
        show_record(record)
    st.session_state.turns.append({
        "role": "assistant", "content": content, "model": body["model"],
        "request_id": request_id, "record": record,
    })
