"""Run a curated sample (or full set) of questions from a golden-questions JSON against the real bot.

Usage:
    # Default — curated 10-question sample from golden_questions.json
    LLM_PROVIDER=groq python -m scripts.run_eval_sample

    # Run all questions in a different file (e.g. the real-RM eval)
    LLM_PROVIDER=groq python -m scripts.run_eval_sample --file tests/golden_rm_questions.json --all

    # Run a specific subset of ids in any file
    LLM_PROVIDER=groq python -m scripts.run_eval_sample --file tests/golden_rm_questions.json --ids RM01,RM03,RM05

Prints per-question PASS/FAIL with reason, then summary. Saves full transcripts
to data/eval_<source-stem>_<timestamp>.json for review.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import config  # noqa: E402

# Quiet down the per-iteration chatter so the eval output is readable.
logging.basicConfig(level=logging.WARNING)

from app.chatbot import ask  # noqa: E402  (after sys.path setup)

SAMPLE_IDS = [
    "Q01",  # single-fund-single-field
    "Q05",  # cross-fund-ranking
    "Q11",  # side-by-side-comparison
    "Q15",  # shortlist-suggestion
    "Q19",  # recommendation (don't refuse buy/sell)
    "Q22",  # conditional-client-profile (don't refuse missing client details)
    "Q26",  # extrapolation (qualified forecast)
    "Q32",  # holdings-lookup (HDFC Bank — tests holdings table integration)
    "Q35",  # sector-tilt (tests sector_weights table)
    "Q40",  # refusal — out_of_scope (LTCG tax question)
]


_UNICODE_SPACE_VARIANTS = (
    " ",  # NO-BREAK SPACE
    " ",  # NARROW NO-BREAK SPACE  ← Groq's gpt-oss-120b emits these in proper nouns
    " ",  # THIN SPACE
    " ",  # HAIR SPACE
    " ",  # FIGURE SPACE
    " ",  # EN SPACE
    " ",  # EM SPACE
)

_UNICODE_DASH_VARIANTS = (
    "‐",  # HYPHEN
    "‑",  # NON-BREAKING HYPHEN
    "‒",  # FIGURE DASH
    "–",  # EN DASH (Groq gpt-oss-120b emits this for minus signs)
    "—",  # EM DASH
    "−",  # MINUS SIGN
)


def _normalize(s: str) -> str:
    """Lower-case + collapse all Unicode space variants down to a regular ASCII space.

    The bot occasionally emits ``HDFC Bank`` (narrow no-break space) instead of
    ``HDFC Bank``. Literal substring matching misses these. Normalising both sides
    fixes the false negatives without loosening the test semantically.
    """
    for ch in _UNICODE_SPACE_VARIANTS:
        s = s.replace(ch, " ")
    for ch in _UNICODE_DASH_VARIANTS:
        s = s.replace(ch, "-")
    return s.lower()


def grade(q: dict[str, Any], answer: str) -> tuple[bool, str]:
    """Return (passed, reason). Mirrors the eval rules in PLANNING.md 2.3.2."""
    if q.get("must_refuse"):
        # For refusals, look up the most recent query_log row's refusal_reason.
        with sqlite3.connect(config.DB_PATH) as conn:
            row = conn.execute(
                "SELECT refusal_reason FROM query_log "
                "ORDER BY query_id DESC LIMIT 1"
            ).fetchone()
        actual = (row[0] if row else None) or "(none)"
        expected = q.get("expected_refusal_reason")
        ok = actual == expected
        return ok, f"refusal_reason actual={actual!r} expected={expected!r}"

    expected_substrings = q.get("expected_answer_contains", [])
    answer_norm = _normalize(answer)
    missing = [s for s in expected_substrings if _normalize(s) not in answer_norm]
    if missing:
        return False, f"missing substrings: {missing}"
    return True, "all substrings present"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        default=str(BASE / "tests" / "golden_questions.json"),
        help="Questions JSON file (default: tests/golden_questions.json)",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="Comma-separated question ids to run (default: curated SAMPLE_IDS for golden_questions.json, all questions otherwise)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every question in the file (overrides --ids and SAMPLE_IDS)",
    )
    args = parser.parse_args()

    questions_path = Path(args.file)
    if not questions_path.is_absolute():
        questions_path = BASE / questions_path
    raw = json.loads(questions_path.read_text())
    # Filter out non-question items (e.g. _metadata header objects)
    all_questions = [q for q in raw if isinstance(q, dict) and "id" in q]
    by_id = {q["id"]: q for q in all_questions}

    if args.all:
        sample = all_questions
    elif args.ids:
        wanted = [i.strip() for i in args.ids.split(",") if i.strip()]
        sample = [by_id[i] for i in wanted if i in by_id]
    elif questions_path.name == "golden_questions.json":
        sample = [by_id[i] for i in SAMPLE_IDS if i in by_id]
    else:
        # Non-default file with no --ids and no --all → run everything in the file
        sample = all_questions

    print(f"Running eval against LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'groq')}")
    print(f"Source: {questions_path.name}")
    print(f"Questions: {len(sample)}")
    print("=" * 80)

    results: list[dict[str, Any]] = []
    pass_count = 0
    start = time.perf_counter()

    for q in sample:
        qid, cat = q["id"], q["category"]
        print(f"\n--- {qid}  [{cat}] ---")
        print(f"Q: {q['question']}")
        t0 = time.perf_counter()
        try:
            answer, _query_id = ask(q["question"], user_id="eval")
        except Exception as e:  # broad catch — log and continue the eval
            answer = f"[ASK CRASHED: {type(e).__name__}: {e}]"
        latency_s = time.perf_counter() - t0

        ok, reason = grade(q, answer)
        if ok:
            pass_count += 1
        status = "PASS" if ok else "FAIL"
        print(f"A: {answer[:240]}{'…' if len(answer) > 240 else ''}")
        print(f"-> {status} ({reason})  [{latency_s:.1f}s]")

        results.append(
            {
                "id": qid,
                "category": cat,
                "question": q["question"],
                "answer": answer,
                "passed": ok,
                "reason": reason,
                "latency_s": round(latency_s, 2),
                "expected_answer_contains": q.get("expected_answer_contains"),
                "must_refuse": q.get("must_refuse"),
                "expected_refusal_reason": q.get("expected_refusal_reason"),
            }
        )

    total = time.perf_counter() - start
    print()
    print("=" * 80)
    print(
        f"PASS: {pass_count}/{len(sample)}   "
        f"({pass_count / len(sample):.0%})   "
        f"elapsed: {total:.1f}s"
    )

    # Save full transcripts — embed source-file stem so RM-eval runs don't
    # overwrite curated-sample runs in the data/ folder.
    stem = questions_path.stem
    out_path = BASE / "data" / f"eval_{stem}_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "source_file": str(questions_path),
                "sample_ids": [q["id"] for q in sample],
                "results": results,
            },
            indent=2,
        )
    )
    print(f"Full transcripts saved to {out_path}")

    return 0 if pass_count == len(sample) else 1


if __name__ == "__main__":
    raise SystemExit(main())
