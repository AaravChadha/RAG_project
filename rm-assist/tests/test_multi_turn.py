"""Tests for the multi-turn conversation helpers in ``app.chatbot``.

These tests target the new history-aware building blocks added on
2026-05-16: ``_truncate_content``, ``_compact_older_turns``, and
``_build_messages``. They exercise the sliding-window + heuristic
compaction logic without invoking a real LLM. The end-to-end smoke
test (``test_ask_accepts_history_kwarg``) uses the mock provider via
the existing ``_force_mock_provider`` autouse fixture from
``test_chatbot_spine.py``-style setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the rm-assist package root importable regardless of where pytest
# is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.chatbot import (  # noqa: E402
    _RECENT_WINDOW_MESSAGES,
    _PER_MESSAGE_CAP_CHARS,
    _COMPACT_USER_Q_CAP,
    _build_messages,
    _compact_older_turns,
    _truncate_content,
    ask,
)


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """Use the mock LLM so tests stay offline + deterministic."""
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    yield


# ---------------------------------------------------------------------------
# _truncate_content
# ---------------------------------------------------------------------------

def test_truncate_under_cap_returns_unchanged() -> None:
    s = "hello world"
    assert _truncate_content(s, cap=100) == s


def test_truncate_over_cap_appends_marker() -> None:
    s = "x" * 50
    out = _truncate_content(s, cap=10)
    assert out.startswith("x" * 10)
    assert "[truncated]" in out
    # The truncated string is longer than the cap because of the marker —
    # that's intentional. The cap bounds the kept content, not the suffix.
    assert len(out) > 10


def test_truncate_empty_returns_empty() -> None:
    assert _truncate_content("", cap=10) == ""


# ---------------------------------------------------------------------------
# _compact_older_turns
# ---------------------------------------------------------------------------

def test_compact_extracts_only_user_questions() -> None:
    older = [
        {"role": "user", "content": "What's the 1Y return of Canara Robeco?"},
        {"role": "assistant", "content": "1Y return is 6.65%..."},
        {"role": "user", "content": "And the expense ratio?"},
        {"role": "assistant", "content": "Expense ratio is 1.85%..."},
    ]
    compact = _compact_older_turns(older)
    assert compact is not None
    assert compact["role"] == "system"
    body = compact["content"]
    assert "Earlier user questions in this conversation" in body
    # Both user questions present (verbatim).
    assert "What's the 1Y return of Canara Robeco?" in body
    assert "And the expense ratio?" in body
    # Assistant content is NOT included — that's the load-bearing design choice.
    assert "1Y return is 6.65" not in body
    assert "Expense ratio is 1.85" not in body


def test_compact_with_no_user_messages_returns_none() -> None:
    """Defensive: an older window of assistant-only messages produces no note."""
    older = [
        {"role": "assistant", "content": "some answer"},
        {"role": "assistant", "content": "another answer"},
    ]
    assert _compact_older_turns(older) is None


def test_compact_empty_returns_none() -> None:
    assert _compact_older_turns([]) is None


def test_compact_caps_long_user_questions() -> None:
    """Long user pastes should be capped at _COMPACT_USER_Q_CAP chars in the note."""
    long_q = "a" * (_COMPACT_USER_Q_CAP + 500)
    older = [{"role": "user", "content": long_q}]
    compact = _compact_older_turns(older)
    assert compact is not None
    # The cap applies; the full long_q must NOT be present verbatim.
    assert long_q not in compact["content"]
    # The truncation marker should be visible.
    assert "[truncated]" in compact["content"]


# ---------------------------------------------------------------------------
# _build_messages
# ---------------------------------------------------------------------------

def test_build_with_no_history_returns_system_plus_user() -> None:
    msgs = _build_messages(None, "What's the expense ratio of X?")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    # SYSTEM_PROMPT is substantive (>1000 chars).
    assert len(msgs[0]["content"]) > 1000
    assert msgs[1] == {"role": "user", "content": "What's the expense ratio of X?"}


def test_build_with_short_history_keeps_all_verbatim() -> None:
    """History at-or-below the recent window is kept in full, no compact note."""
    history = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "A2"},
    ]
    msgs = _build_messages(history, "Q3")
    # system + 4 history + current user = 6 messages.
    assert len(msgs) == 6
    assert msgs[0]["role"] == "system"
    # No SECOND system message (the compact note is a system message; its
    # absence is the load-bearing check, not a substring of SYSTEM_PROMPT
    # which itself describes the compact-note format).
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1
    # Verbatim history preserved.
    assert msgs[1] == {"role": "user", "content": "Q1"}
    assert msgs[2] == {"role": "assistant", "content": "A1"}
    assert msgs[3] == {"role": "user", "content": "Q2"}
    assert msgs[4] == {"role": "assistant", "content": "A2"}
    assert msgs[5] == {"role": "user", "content": "Q3"}


def test_build_with_long_history_compacts_older_pairs() -> None:
    """History above the threshold: oldest pairs compact into a system note."""
    # 8 messages = 4 Q+A pairs; threshold is 6 messages, so older = 2 msgs,
    # recent = 6 msgs.
    history = [
        {"role": "user", "content": "Q1 about Canara Robeco"},
        {"role": "assistant", "content": "A1 detail"},
        {"role": "user", "content": "Q2 about DSP Multi Asset"},
        {"role": "assistant", "content": "A2 detail"},
        {"role": "user", "content": "Q3 compare"},
        {"role": "assistant", "content": "A3 detail"},
        {"role": "user", "content": "Q4 follow-up"},
        {"role": "assistant", "content": "A4 detail"},
    ]
    msgs = _build_messages(history, "Q5 current")
    # system + compact note + 6 recent + current = 9 messages.
    assert len(msgs) == 9
    assert msgs[0]["role"] == "system"  # SYSTEM_PROMPT
    # Compact note for the oldest pair (Q1 + A1).
    assert msgs[1]["role"] == "system"
    assert "Earlier user questions" in msgs[1]["content"]
    assert "Q1 about Canara Robeco" in msgs[1]["content"]
    # A1 should NOT be in the compact note (only user questions get carried).
    assert "A1 detail" not in msgs[1]["content"]
    # Recent window: Q2 through A4 (the last 6 of the history).
    assert msgs[2] == {"role": "user", "content": "Q2 about DSP Multi Asset"}
    assert msgs[3] == {"role": "assistant", "content": "A2 detail"}
    assert msgs[7] == {"role": "assistant", "content": "A4 detail"}
    # Current question always last.
    assert msgs[-1] == {"role": "user", "content": "Q5 current"}


def test_build_truncates_oversize_recent_messages() -> None:
    """A 5000-char message in the recent window gets clipped to the per-msg cap."""
    long_answer = "x" * 5000
    history = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": long_answer},
    ]
    msgs = _build_messages(history, "Q2")
    # Find the assistant message — its content should be capped.
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assistant_content = assistant_msgs[0]["content"]
    assert long_answer not in assistant_content  # full original not present
    assert "[truncated]" in assistant_content
    # Kept content is at most _PER_MESSAGE_CAP_CHARS plus the truncation marker.
    assert len(assistant_content) <= _PER_MESSAGE_CAP_CHARS + 20


def test_build_drops_unknown_roles() -> None:
    """Defensive: messages with role != user/assistant are skipped."""
    history = [
        {"role": "user", "content": "Q1"},
        {"role": "tool", "content": "tool result that shouldn't appear"},
        {"role": "assistant", "content": "A1"},
    ]
    msgs = _build_messages(history, "Q2")
    # System + Q1 + A1 + Q2 = 4 messages; the tool message is dropped.
    assert len(msgs) == 4
    contents = [m["content"] for m in msgs]
    assert all("tool result" not in c for c in contents)


# ---------------------------------------------------------------------------
# ask() integration: history kwarg accepted, returns (answer, query_id)
# ---------------------------------------------------------------------------

def test_ask_accepts_history_kwarg(seeded_db) -> None:
    """End-to-end smoke: ask() with history runs through the mock LLM without crashing."""
    history = [
        {"role": "user", "content": "What's the 1Y return of Canara Robeco?"},
        {"role": "assistant", "content": "1Y return is 6.65%..."},
    ]
    answer, query_id = ask(
        "And the expense ratio?",
        history=history,
        user_id="test_user",
    )
    assert answer
    assert query_id > 0
    # The verification footer must still appear on the follow-up answer —
    # multi-turn doesn't relax the universal-footer rule.
    assert "verify against your own" in answer


def test_ask_history_default_none_back_compat(seeded_db) -> None:
    """Callers that omit `history=` still get the same (answer, query_id) shape."""
    answer, query_id = ask("What is the expense ratio?", user_id="test_user")
    assert answer
    assert query_id > 0
