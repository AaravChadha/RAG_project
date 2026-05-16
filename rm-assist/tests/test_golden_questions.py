"""Parametrized golden-question test runner.

Each entry in `golden_questions.json` becomes one parametrized test.
The tests SKIP intentionally: a full 40-question run consumes ~320K
Groq tokens (~8K per question × 40), well over the 200K free-tier
daily cap. Running the goldens as pytest cases would either burn the
daily quota mid-test or fail mid-run with a 429.

The de facto eval surface is `scripts/run_eval_sample.py`, which is
invoked deliberately with the token budget in mind. The parametrize
wiring here is preserved so Phase 8 can flip these on for a CI hook
once we move off the free tier.
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
    """Spec: chatbot must produce expected_answer_contains substrings or the
    expected refusal_reason. Skipped at pytest level — see module docstring.

    To actually exercise the goldens, use:
        python -m scripts.run_eval_sample --all
    """
    pytest.skip(
        "Token-heavy real-LLM eval; run via "
        "`python -m scripts.run_eval_sample --all` instead of pytest."
    )
