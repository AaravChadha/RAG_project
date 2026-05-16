"""Schema/shape checks for `golden_questions.json`.

These tests DO run today and gate the spec quality:

  - Every entry has the required fields.
  - IDs are unique (Qnn).
  - Categories come from the allowed enum.
  - Non-refusal entries include the universal footer substring
    ("verify against your own") AND the citation prefix ("as on")
    in expected_answer_contains; their `expected_refusal_reason` is None.
  - Refusal entries have `expected_sql_contains is None`, a valid
    refusal reason, and DO NOT include the verification footer.
  - Category minimums match PLANNING.md section 2.1.2 / 2.2.2.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

GOLDEN_PATH = Path(__file__).parent / "golden_questions.json"

REQUIRED_FIELDS = {
    "id",
    "category",
    "question",
    "expected_answer_contains",
    "expected_sql_contains",
    "must_refuse",
    "expected_refusal_reason",
    "verification_notes",
}

ALLOWED_CATEGORIES = {
    "single-fund-single-field",
    "single-fund-multi-field",
    "cross-fund-ranking",
    "side-by-side-comparison",
    "shortlist-suggestion",
    "recommendation",
    "conditional-client-profile",
    "extrapolation",
    "cross-fund-filter",
    "risk-profile",
    "holdings-lookup",
    "sector-tilt",
    "fund-manager",
    "refusal",
}

ALLOWED_REFUSAL_REASONS = {"unknown_scheme", "no_data", "out_of_scope"}

# Per PLANNING.md 2.1.2 / 2.2.2.
CATEGORY_MINIMUMS = {
    "cross-fund-ranking": 5,
    "side-by-side-comparison": 4,
    "shortlist-suggestion": 4,
    "recommendation": 3,
    "conditional-client-profile": 3,
    "extrapolation": 3,
    "single-fund-single-field": 2,
    "single-fund-multi-field": 2,
    "cross-fund-filter": 2,
    "risk-profile": 2,
    "holdings-lookup": 2,
    "sector-tilt": 2,
    "fund-manager": 2,
}

EXACT_REFUSALS = 3


@pytest.fixture(scope="module")
def questions() -> list[dict]:
    with open(GOLDEN_PATH) as f:
        return json.load(f)


def test_parses_as_json(questions: list[dict]):
    assert isinstance(questions, list)
    assert len(questions) >= 1


def test_required_fields_present(questions: list[dict]):
    for q in questions:
        missing = REQUIRED_FIELDS - set(q.keys())
        assert not missing, f"{q.get('id')} is missing fields: {missing}"


def test_ids_unique_and_well_formed(questions: list[dict]):
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "Duplicate ids present"
    for qid in ids:
        assert qid.startswith("Q"), f"id '{qid}' must start with 'Q'"
        assert qid[1:].isdigit(), f"id '{qid}' must be Q<digits>"


def test_ids_sorted(questions: list[dict]):
    ids = [q["id"] for q in questions]
    nums = [int(qid[1:]) for qid in ids]
    assert nums == sorted(nums), "Entries must be sorted by id"


def test_categories_in_enum(questions: list[dict]):
    for q in questions:
        assert q["category"] in ALLOWED_CATEGORIES, (
            f"{q['id']} has unknown category '{q['category']}'"
        )


def test_non_refusal_shape(questions: list[dict]):
    for q in questions:
        if q["must_refuse"]:
            continue
        assert q["expected_refusal_reason"] is None, (
            f"{q['id']} non-refusal must have expected_refusal_reason=None"
        )
        eac = q["expected_answer_contains"]
        assert isinstance(eac, list) and eac, (
            f"{q['id']} expected_answer_contains must be a non-empty list"
        )
        joined = " ".join(eac).lower()
        assert "verify against your own" in joined, (
            f"{q['id']} non-refusal must include 'verify against your own' "
            f"in expected_answer_contains"
        )
        assert "as on" in joined, (
            f"{q['id']} non-refusal must include 'as on' "
            f"in expected_answer_contains"
        )
        assert q["expected_sql_contains"] is not None, (
            f"{q['id']} non-refusal must have a non-null expected_sql_contains"
        )


def test_refusal_shape(questions: list[dict]):
    for q in questions:
        if not q["must_refuse"]:
            continue
        assert q["expected_refusal_reason"] in ALLOWED_REFUSAL_REASONS, (
            f"{q['id']} refusal_reason '{q['expected_refusal_reason']}' "
            f"not in {ALLOWED_REFUSAL_REASONS}"
        )
        assert q["expected_sql_contains"] is None, (
            f"{q['id']} refusal must have expected_sql_contains=None"
        )
        assert q["category"] == "refusal", (
            f"{q['id']} must_refuse=true must use category 'refusal'"
        )
        joined = " ".join(q["expected_answer_contains"]).lower()
        assert "verify against your own" not in joined, (
            f"{q['id']} refusal must NOT include the verification footer"
        )


def test_category_minimums(questions: list[dict]):
    counts = Counter(q["category"] for q in questions)
    for cat, minimum in CATEGORY_MINIMUMS.items():
        assert counts.get(cat, 0) >= minimum, (
            f"category '{cat}' has {counts.get(cat, 0)} entries, "
            f"need >= {minimum}"
        )


def test_exact_refusal_count(questions: list[dict]):
    refusals = [q for q in questions if q["must_refuse"]]
    assert len(refusals) == EXACT_REFUSALS, (
        f"need exactly {EXACT_REFUSALS} refusal entries, got {len(refusals)}"
    )
    reasons = {q["expected_refusal_reason"] for q in refusals}
    assert reasons == ALLOWED_REFUSAL_REASONS, (
        f"refusal entries must cover all reasons {ALLOWED_REFUSAL_REASONS}; "
        f"got {reasons}"
    )
