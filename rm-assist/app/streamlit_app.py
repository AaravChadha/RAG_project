"""RM Assist — Streamlit UI (Phase 6).

Top-level structure:

1. ``st.set_page_config`` (must be the first Streamlit call).
2. Login gate — NOTHING else renders until ``authentication_status``
   is True. A scraper hitting the URL sees only the login form.
3. Once authenticated, the chat UI mounts: sidebar + chat history +
   chat input. Each assistant turn carries thumbs-up / thumbs-down
   buttons whose clicks update ``query_log.user_feedback``.

The chat-message state lives in ``st.session_state["messages"]`` as a
list of dicts ``{role, content, query_id, user_id, feedback}``. The
``query_id`` is captured directly from ``chatbot.ask`` (which returns
``(answer, query_id)`` since Phase 6) — no extra DB lookups needed.

The file stays under ~300 lines by delegating cosmetic chunks to
``_render_sidebar`` and ``_render_feedback_controls``.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

# Make the rm-assist package root importable when launched via
# `streamlit run app/streamlit_app.py` (Streamlit doesn't put the parent
# on sys.path).
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402
from app.auth import make_authenticator  # noqa: E402
from app.chatbot import ask  # noqa: E402
from retrieval.db_query import update_query_feedback  # noqa: E402

logger = logging.getLogger(__name__)


# Mailto target for the sidebar's "Report a problem" link. For the
# pilot this points at a generic ops inbox; in Phase 2 it can be
# replaced with a real `feedback` table writer.
_FEEDBACK_MAILTO: str = (
    "mailto:research-bot-ops@bajajcapital.in"
    "?subject=RM%20Assist%20-%20Problem%20report"
)


# ---------------------------------------------------------------------------
# Page-level config — MUST be the first Streamlit call.
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="RM Assist",
    page_icon=":bar_chart:",
    layout="centered",
)


# ---------------------------------------------------------------------------
# Data-status sidebar query.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60)
def _load_data_status() -> Dict[str, Any]:
    """Count live (non-superseded) schemes and the latest report month.

    Cached for 60s so the sidebar doesn't re-hit SQLite on every rerun.
    """
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT scheme_id) AS n_schemes,
                   MAX(report_month)         AS latest_month
              FROM fund_snapshots
             WHERE superseded_at IS NULL
            """
        ).fetchone()
        return {
            "n_schemes": int(row["n_schemes"] or 0),
            "latest_month": row["latest_month"] or "unknown",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auth gate — render NOTHING outside this block until authenticated.
# ---------------------------------------------------------------------------
authenticator = make_authenticator()
authenticator.login(location="main")

if st.session_state.get("authentication_status") is False:
    st.error("Invalid username or password.")
    st.stop()
elif st.session_state.get("authentication_status") is None:
    st.info("Please log in.")
    st.stop()

# Authenticated past this point. Every line below is gated.
user_id: str = st.session_state["username"]
display_name: str = st.session_state.get("name", user_id)


# ---------------------------------------------------------------------------
# Sidebar.
# ---------------------------------------------------------------------------
def _render_sidebar(authenticator_obj, name: str) -> None:
    """Sidebar with data status, identity, logout, and a problem-report link."""
    with st.sidebar:
        status = _load_data_status()
        st.markdown("### Data status")
        st.write(
            f"Loaded: **{status['latest_month']}**, "
            f"**{status['n_schemes']}** schemes"
        )
        st.markdown("---")
        st.markdown(f"**Logged in as:** {name}")
        authenticator_obj.logout(location="sidebar")
        st.markdown("---")
        st.markdown(f"[Report a problem]({_FEEDBACK_MAILTO})")


_render_sidebar(authenticator, display_name)


# ---------------------------------------------------------------------------
# Chat state initialization.
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state["messages"] = []  # type: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Feedback controls — one row of buttons + an optional comment box.
# ---------------------------------------------------------------------------
def _render_feedback_controls(msg_index: int, message: Dict[str, Any]) -> None:
    """Render thumbs-up / thumbs-down (+ optional comment) under a message.

    The buttons use distinct keys per ``msg_index`` so Streamlit can
    differentiate them across the chat history. Clicks write to
    ``query_log`` via ``update_query_feedback`` and update the in-memory
    message dict so the UI reflects the persisted state on the next
    rerun.
    """
    query_id: Optional[int] = message.get("query_id")
    if not query_id:
        return  # Refusal / no-log paths still display, just without buttons.

    current = message.get("feedback")
    col_up, col_down, col_status = st.columns([1, 1, 6])

    up_disabled = current == "thumbs_up"
    down_disabled = current == "thumbs_down"

    with col_up:
        if st.button(
            "Helpful",
            key=f"up_{msg_index}",
            disabled=up_disabled,
            help="Mark this answer as helpful.",
        ):
            update_query_feedback(query_id, "thumbs_up")
            message["feedback"] = "thumbs_up"
            st.rerun()

    with col_down:
        if st.button(
            "Not helpful",
            key=f"down_{msg_index}",
            disabled=down_disabled,
            help="Mark this answer as not helpful.",
        ):
            update_query_feedback(query_id, "thumbs_down")
            message["feedback"] = "thumbs_down"
            st.rerun()

    with col_status:
        if current == "thumbs_up":
            st.caption("Marked helpful.")
        elif current == "thumbs_down":
            st.caption("Marked not helpful. Add details below.")

    # Comment box appears once thumbs-down is recorded, so the RM can
    # leave a free-text note. Submitting writes to query_log.feedback_comment.
    if current == "thumbs_down":
        comment_key = f"comment_{msg_index}"
        submit_key = f"comment_submit_{msg_index}"
        comment = st.text_area(
            "What went wrong? (optional)",
            key=comment_key,
            placeholder="e.g. wrong number, wrong scheme, missing citation...",
            height=80,
        )
        if st.button("Submit comment", key=submit_key):
            update_query_feedback(query_id, "thumbs_down", comment or None)
            message["comment_submitted"] = True
            st.success("Thanks — comment recorded.")


# ---------------------------------------------------------------------------
# Title + chat history.
# ---------------------------------------------------------------------------
st.title("RM Assist")
st.caption(
    "Internal research assistant. Every answer is research output — "
    "verify against your own analysis before advising clients."
)

for idx, msg in enumerate(st.session_state["messages"]):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            _render_feedback_controls(idx, msg)


# ---------------------------------------------------------------------------
# Chat input — runs one LLM round per submission.
# ---------------------------------------------------------------------------
prompt = st.chat_input("Ask about a scheme, comparison, or shortlist...")

if prompt:
    st.session_state["messages"].append(
        {
            "role": "user",
            "content": prompt,
            "query_id": None,
            "user_id": user_id,
            "feedback": None,
        }
    )
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Asking the model..."):
            try:
                answer, query_id = ask(prompt, user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("ask() failed for user=%s", user_id)
                answer = (
                    "Something went wrong while answering. "
                    "Please try rephrasing or retry shortly."
                )
                query_id = 0
        st.markdown(answer)

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "content": answer,
            "query_id": query_id if query_id else None,
            "user_id": user_id,
            "feedback": None,
        }
    )
    # Rerun so the freshly appended message gets its feedback controls
    # rendered through the standard history-render path (which keys
    # buttons by message index).
    st.rerun()
