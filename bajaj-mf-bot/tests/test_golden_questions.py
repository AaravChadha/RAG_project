"""Parametrized golden-question test runner.

Phase 2 deliverable: this file is the SPEC for the chatbot.
Each entry in `golden_questions.json` becomes one parametrized test.
The tests SKIP for now because the tool-use chatbot loop is not built
until Phase 5. Once Phase 5 lands, the body of `test_golden_question`
will be replaced with a real call to `app.chatbot.ask` and the
expected_answer_contains / refusal_reason assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).parent / "golden_questions.json"

with open(GOLDEN_PATH) as f:
    QUESTIONS = json.load(f)


@pytest.mark.parametrize("q", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_golden_question(q):
    """Phase 2 spec — these tests will pass once Phase 5 (tool-use chatbot) is built.

    For now they are a SPEC: the chatbot must produce answers containing the
    expected substrings (and the universal verification footer) or trigger the
    expected refusal_reason.

    Marked skip in Phase 2; will be flipped to a real assertion once the
    Phase 5 chatbot tool-use loop is wired up.
    """
    pytest.skip("Phase 2 spec - chatbot tool-use loop comes in Phase 5")
