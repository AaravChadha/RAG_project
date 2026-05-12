# STATUS — Bajaj MF Chatbot Pilot

> **What this file is.** A rolling snapshot of where the project actually is, so a fresh dev (or future-you) can open the repo and resume in 5 minutes. Read this first, then `PLANNING.md` for the full phase plan.
>
> **Last update**: 2026-05-12 (later same day — 123/123 coverage reached)

---

## TL;DR

The pilot is **functionally complete end-to-end**. PDF → parser → SQLite → LLM tool-use → cited answer → Streamlit UI with auth gate. All on free-tier services. 56 tests pass. Locally, you can run a real chatbot against **all 123 Bajaj-recommended schemes** (90 equity/hybrid/arbitrage/multi-asset/gold/intl + 4 Equity Savings + 29 pure debt) through a login-gated UI.

Phases 1-6 and 4.3 of the original plan are done. **Phase 4.3 closed at 123/123** — Bandhan Gilt Fund landed once the user supplied its actual URL (`Bandhan%20Gilt%20Fund%20Reg%20Gr.pdf`, not the slug `Bandhan%20Gilt%20Fund.pdf` everyone assumed). Phases 7-9 (Cloudflare tunnel, eval polish, RM onboarding) are not started.

---

## How to use the bot right now

From the repo root:

```bash
cd bajaj-mf-bot

# 1. (One-time) Install deps and set Groq key
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then put your GROQ_API_KEY in .env

# 2. (One-time per data refresh) Seed DB + download + ingest 123 PDFs
python -m db.init_db --force
python -m ingest.download_pdfs --month 2026-05   # ~2 min, downloads from Bajaj's public host (skip-if-present)
python -m ingest.ingest_month --month 2026-05    # ~2 min, parses all 123

# 3. (One-time) Create at least one auth user
python -m scripts.create_auth_account \
    --username jane.doe --name "Jane Doe" \
    --email jane.doe@example.com --employee-id BC0001
# (prompts for password — stays local, never committed)

# 4. Run the UI
streamlit run app/streamlit_app.py
# Open http://localhost:8501 — login form first, then chat
```

CLI smoke test (no UI):

```bash
python -m app.chatbot "What is the expense ratio of Canara Robeco Multi Cap Fund?"
```

---

## What's been built (Phases 1-6)

| Phase | Status | Highlights |
|---|---|---|
| **1.1-1.5** Vertical slice | ✅ | Schema with 7 fixes, parser stub, LLMClient (Groq+Mock), chatbot spine |
| **1.6** Normalized table writes | ✅ | `insert_snapshot_full` writes fund_snapshots + sector_weights + periodic_returns + holdings in one txn with rollback safety |
| **2** Eval-driven spec | ✅ | 40 golden questions across 13 categories + 3 refusals. Schema-validated. Runner skips pending Phase 8 full eval. |
| **3** Parser widening | ✅ | 14 section parsers (dispatch pattern, partial-snapshot-with-errors), 7 invariants, 3 hand-extracted golden samples, deepdiff regression baseline |
| **4.1/4.2** Bulk ingest | ✅ | All 90 + 4 Equity Savings + 29 pure debt = **123 PDFs** downloaded + parsed + ingested. `data/ingest_report_2026-05.json` has per-scheme outcomes. |
| **4.3** Debt-fund coverage | ✅ | **All 29 pure debt funds in.** Bandhan Gilt landed last — its URL is `Bandhan%20Gilt%20Fund%20Reg%20Gr.pdf` rather than the predicted `Bandhan%20Gilt%20Fund.pdf` (Bandhan's the only AMC that publishes Gilt under the long-form "Reg Gr" filename). CSV updated. |
| **5.1-5.4** Tool-use chatbot | ✅ | 4 tools (`query_db`, `lookup_scheme`, `get_schema`, `compare_schemes`), full operating-mode system prompt, 6-iter tool loop. Stable **7-8/10 on real Groq curated 10-Q eval.** |
| **6** Streamlit UI | ✅ | Auth gate (bcrypt + streamlit-authenticator), chat (`st.chat_message`/`st.chat_input`), thumbs-up/down feedback writes to `query_log`, sidebar with data status + logout, "Report a problem" mailto |

**Test status**: `pytest tests/ -v` → **56 passed, 40 skipped** (40 are Phase-2 golden questions waiting on full Phase-8 eval).

---

## Key decisions and journey (so you don't relearn)

### LLM model: switched to `openai/gpt-oss-120b` from `llama-3.3-70b-versatile`
- **Why**: Llama-3.3 on Groq emits Llama-pseudo-XML tool-call format `<function=name>{json}</function>` instead of OpenAI-spec JSON in ~30-50% of calls on long system prompts. Our recovery code handles SOME shapes but not all.
- gpt-oss-120b emits 0 malformations in most 10-Q runs.
- **2× daily token budget**: 200K TPD vs 100K TPD on Groq free tier.
- 27% faster average latency.
- Costs ~12% more tokens per question (more markdown output). Worth it.
- Default set in `bajaj-mf-bot/.env.example` and `bajaj-mf-bot/config.py`. The `LLMClient` abstraction means swapping providers later is a one-line config change.

### Parser fix: drawdown section on page 2 vs page 3
- 21 funds (mostly Nippon India, White Oak Capital, Bandhan, Parag Parikh, etc.) had `parse_drawdown` failing because their page-2 content runs long enough to push the Drawdown Analysis section onto page 3.
- Added `_words_with_anchor(pl, anchor, candidate_pages=(1,2))` helper. `parse_drawdown` now searches both pages.
- Also tightened `_DRAWDOWN_1Y_X` band from `(95, 175)` to `(95, 150)` so 3Y date columns don't leak into the 1Y column.
- After fix: **drawdown errors 21 → 0**, drawdown_pct populated on 78/90 schemes.

### The 12 NULL drawdowns are correct, not bugs
The 12 schemes where `drawdown_pct IS NULL` after re-ingest:
- 5 Arbitrage Funds (ABSL, Edelweiss, Invesco, Kotak, Tata)
- 4 Conservative Hybrid Funds (ABSL Regular Savings, DSP Regular Savings, ICICI Pru Regular Savings, Parag Parikh Conservative Hybrid)
- 3 Income Plus Arbitrage (Bandhan, Kotak, SBI)

**Verified by reading the PDFs directly**: the Finalyca template literally shows `NA NA` for every drawdown cell in these reports. Low-volatility / debt-heavy products → Finalyca doesn't compute meaningful drawdown stats. Source-data limitation, not parser miss. Workaround (later): compute from AMFI NAV history if RMs ask for these.

### Grader Unicode normalization
Two Unicode bugs in eval grading discovered:
1. **U+202F NARROW NO-BREAK SPACE** in `HDFC Bank` (gpt-oss adds it in proper nouns) → grader missed "HDFC Bank" substring.
2. **U+2013 EN DASH** in `–0.01` (gpt-oss formats negative numbers this way) → grader missed "-0.01" substring.
Fix: `_normalize()` in `scripts/run_eval_sample.py` replaces all 7 space variants + 6 dash variants with ASCII equivalents before substring comparison.

### Operating mode (the bot's stance)
The bot is intentionally **NOT compliance-cautious** about suggestions / recommendations / extrapolations / "best" / client-conditional questions. It answers with data and a verification footer; the RM is the safety net. Refusals reserved for only 3 reasons: `no_data`, `unknown_scheme`, `out_of_scope`. The verification footer is **universal** (every non-refusal answer) — not just for advice-style outputs — because parser bugs / fuzzy matches / stale snapshots can produce wrong numbers that look authoritative. RM verifies everything.

### Auth: the rival defense
Per cross-cutting constraints in PLANNING.md, the underlying data is public (AMFI/SEBI disclosed), but **Bajaj's curated recommended-90 list IS commercially sensitive** and the bot is a productivity tool worth protecting. The auth gate (`streamlit-authenticator` + bcrypt-hashed YAML) is the protection model, not data secrecy.

- Real `auth_config.yaml` is gitignored. `.example` template is committed.
- No content renders before authentication — verified by `streamlit.testing.v1.AppTest`.
- Phase-2 hardening sequencing (IP allowlist, rate limit, SSO) is documented but out of pilot scope.

### Bot has no developer info
Hard rule in the system prompt: if asked "who built you?" the bot answers *"I'm an internal Bajaj Capital research tool — I don't have details on who built me."* No name/email/handle anywhere in user-facing strings.

### Parser quality cleanup (2026-05-12)
A targeted audit cut `parse_errors_json`-flagged funds from **60/90 to 8/123** — and none of the remaining 8 are parser bugs (Bandhan Gilt added 0 new warnings when it landed). Fixes:
- `mkt_cap_composition_sums_to_100` invariant was wrongly specified (sectors-as-pct-of-portfolio sum to `composition.Equity`, not to 100). Renamed and updated to add unclassified-equity holdings (NVDA, Alphabet etc.) before comparing.
- `sector_weights_sum_close` → `sector_weights_sum_in_range [50, 110]`, skips debt-heavy funds (sums then mirror gross debt exposure).
- `parse_sector_weights` anchored to the literal "Sector Wts(%)" header (filters rolling-returns chart values above) + stops at "Risk Rating" (catches Aa/Aa- credit-rating leakage below).
- `parse_holdings_full` now detects column x-positions from the header row per-fund (was using hardcoded bands that broke on ~30pt-shifted compact layouts → fixed 10 zero-holdings funds). Plus alphabetic-content + `weight_pct ∈ [-25, 105]` guards filter chart-axis year labels.
- `parse_fund_managers` role regex broadened to match any `<prefix> - EQUITY/DEBT/FOREIGN INV.` line (was anchored to "Fund Manager - " literally) — fixed 9 funds with varied titles like "Senior Fund Manager", "Research Analyst", "Chief Dealer - Equities", etc.
- `holdings_min_count` invariant made fund-type-aware: Gold/FoF funds use min=1, diversified funds use min=5.

### Debt-template support (2026-05-12)
Debt PDFs share ~80% of the equity Finalyca template. Most sections work without changes; only Portfolio Characteristics differs (debt: Avg Maturity Years + YTM + Modified Duration vs equity: P/E, P/B, Mkt Cap fields).
- Schema additions: `avg_maturity_years REAL` + `yield_to_maturity REAL` (migration 002).
- `parse_portfolio_characteristics` recognizes the debt labels; equity-only attrs stay None for debt and vice versa.
- `sector_weights_sum_in_range` skipped for debt-heavy funds.
- `EXPECTED_SECTIONS_BY_FUND_TYPE["debt"]` corrected to reflect actual debt-template section presence.
- Macaulay Duration deliberately skipped — Finalyca's "Macauly Duration Years" label has silent unit switching (days for short tenor, years for long tenor); Modified Duration is the universally useful field.

---

## What's still pending — from you

| Item | Why | What unblocks it |
|---|---|---|
| **Phase 7.1 tunnel setup** | Get a URL the 5 RMs can hit | Install `cloudflared`, create a named tunnel pointing at `localhost:8501`, run as a background service, verify from a phone on cellular. PLANNING.md 7.1.1-7.1.5 has the steps. |

---

## Deferred to Phase 8 (eval & polish)

These are deliberately deferred — they're real but non-blocking, and Phase 8 is the right place to address them as a batch.

1. **Tool-format recovery in `_GroqClient`** — Q19/Q32 fail flakily because gpt-oss-120b occasionally emits a malformed tool call that escapes existing recovery logic. ~30-60 min to extend recovery for additional shapes.

2. **System prompt improvements** — reduce ambiguity refusals (Q05 "rank our 3 funds" → bot asks for clarification; should pick a sensible default and disclose). Also sharpen tool routing for client-conditional + shortlist questions.

3. **Full 40-question eval** — we only ran the curated 10. Full run uses ~330K tokens, exceeds 200K daily cap, so needs 2 calendar days to spread across token quota OR a model swap.

4. ~~**Remaining parser quirks** — `parse_fund_managers` fails on 9 funds (different role-line wording), `parse_holdings_full` on 10 (FoF/arbitrage table geometry), `parse_portfolio_characteristics` on 2. All caught by `parse_errors_json`, none fatal.~~ → **Closed in 2026-05-12 session.** Holdings and fund-manager bugs fixed; only `parse_portfolio_characteristics` still fails on 2 funds (Franklin US Opps FoF + SBI Income Plus Arbitrage FoF) and those are legitimate — the section is literally absent from the source PDF for those FoFs.

5. **`EXPECTED_SECTIONS_BY_FUND_TYPE` enforcement** — map exists in `parse_finalyca.py` and the `debt` entry was corrected in 2026-05-12, but isn't yet used to distinguish "section missing because fund type doesn't have it" from "section missing because parser broke."

6. **UX polish** (PLANNING 8.2): slow-query status messages, long-answer truncation with expander, friendlier error states.

7. **Daily backups** (PLANNING 8.3.1) of `bajaj_mf.db`.

---

## Important file locations

| Path | Purpose |
|---|---|
| `PLANNING.md` | The full phase plan with checkboxes |
| `PLANNING_PROMPT.md` | The original problem statement (frozen reference) |
| `STATUS.md` | This file (rolling state) |
| `schemes_master.csv` | The 90-scheme seed (3 columns: category, scheme, url) |
| `bajaj-mf-bot/.env.example` | Template — copy to `.env` and add `GROQ_API_KEY` |
| `bajaj-mf-bot/db/schema.sql` | DB schema (8 tables: schemes, fund_snapshots, holdings, sector_weights, periodic_returns, query_log, schema_version, scheme_aliases) |
| `bajaj-mf-bot/ingest/parse_finalyca.py` | Main parser (header + manager + returns) |
| `bajaj-mf-bot/ingest/_section_parsers.py` | Section parsers split out: mkt_cap, investment_style, periodic_returns, full_holdings |
| `bajaj-mf-bot/app/streamlit_app.py` | The UI |
| `bajaj-mf-bot/app/prompts.py` | System prompt (operating mode + tool routing + refusal rules) |
| `bajaj-mf-bot/retrieval/tools.py` | 4 tool implementations + OpenAI-style TOOLS schema |
| `bajaj-mf-bot/retrieval/llm_client.py` | Provider-agnostic LLMClient (Groq + Mock backends, tool-call normalization, Llama-pseudo-XML recovery) |
| `bajaj-mf-bot/tests/golden_questions.json` | 40 Q+A specification |
| `bajaj-mf-bot/tests/golden/*.json` | Hand-extracted ground-truth for 3 sample PDFs |
| `bajaj-mf-bot/tests/snapshots/*.json` | Parser regression baselines (deepdiff target) |
| `bajaj-mf-bot/data/ingest_report_2026-05.json` | Per-scheme parse outcomes (errors, invariant warnings) |
| `bajaj-mf-bot/data/eval_sample_*.json` | Real-Groq eval transcripts |
| `~/.claude/plans/i-want-to-make-cozy-storm.md` | Original architecture decisions plan |
| `~/.claude/projects/.../memory/` | Persistent memory: user profile, hardware, stack, operating mode, etc. |

---

## Commit log highlights

Run `git log --oneline` for the full picture. Key commits:

```
9f45bff  Phase 6 - Streamlit UI: auth gate, chat + thumbs feedback, sidebar, create_auth_account CLI
e3f16b8  5.4 - Q15 test-design fix; eval stable at 7-8/10
c235e72  5.4 - Metric-completeness prompt rule + grader Unicode dash normalization; 10-Q eval 7/10
3775af2  5.4 - Switch default LLM_MODEL to openai/gpt-oss-120b (2x daily token budget, better tool-use)
ad92b73  5.3 - Tool-use loop in chatbot.py + Llama tool_use_failed recovery
51d57a5  5.1, 5.2 - 4 tools + system prompt with operating-mode rules
c2906d7  Fix parse_drawdown: section spills to page 3 for 21 funds
4955011  4.1, 4.2 - Bulk download + ingest 90 PDFs
f4751d9  3.3, 3.4, 3.5 - Invariants + golden samples + diff regression
973d071  3.2.11-15 - Mkt cap, investment style, periodic returns, full holdings parsers
f6deb15  3.2.7-10 - Portfolio chars, composition, drawdown, risk rating
4764898  3.2.4-6 - Risk metrics, sector weights, top holdings
e4d42f5  3.1, 3.2.1-3 - Dispatch refactor + header, fund managers, trailing returns
d220172  1.6 - Normalized table writes with txn rollback safety
709ea40  1.5 - db_query (read-only), chatbot spine, query_log, 8 tests
c6cf892  1.4 - LLMClient with Groq + Mock backends, 6 unit tests
ab0d796  1.3 - Stub parser (5 fields from Canara Robeco) + ingest_one CLI
7fc7ab6  1.2 - DB schema with 7 fixes, init_db.py, migrations baseline
d927be3  1.1 - Project skeleton, requirements, config, README
```
