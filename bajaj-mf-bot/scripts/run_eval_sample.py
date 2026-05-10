"""Run a curated 10-question sample from golden_questions.json against the real bot.

Usage:
    LLM_PROVIDER=groq python -m scripts.run_eval_sample

Prints per-question PASS/FAIL with reason, then summary. Saves full transcripts
(answer + tool trace) to data/eval_sample_<timestamp>.json for review.
"""

from __future__ import annotations

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
    questions_path = BASE / "tests" / "golden_questions.json"
    all_questions = json.loads(questions_path.read_text())
    by_id = {q["id"]: q for q in all_questions}

    sample = [by_id[i] for i in SAMPLE_IDS if i in by_id]
    print(f"Running eval against LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'groq')}")
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
            answer = ask(q["question"], user_id="eval")
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

    # Save full transcripts
    out_path = BASE / "data" / f"eval_sample_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"sample_ids": SAMPLE_IDS, "results": results}, indent=2))
    print(f"Full transcripts saved to {out_path}")

    return 0 if pass_count == len(sample) else 1


if __name__ == "__main__":
    raise SystemExit(main())
