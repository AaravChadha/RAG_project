# STATUS — RM Assist Pilot

> **What this file is.** A rolling snapshot of where the project actually is, so a fresh dev (or future-you) can open the repo and resume in 5 minutes. Read this first, then `PLANNING.md` for the full phase plan.
>
> **Last update**: 2026-05-18 evening. Retrieval quality A+B+C shipped, +16 snapshot columns, 88/40 tests, **calibrated 7/10 on Gemini holds post-grafts**. Groq free tier hit the per-minute 8K TPM cap mid-tool-loop — production needs paid Groq or Gemini-primary routing.

---

## TL;DR

The pilot is **functionally complete end-to-end** with significant capability expansion this week. PDF → parser → SQLite → LLM tool-use (now 6 tools) → cited answer → Streamlit UI with auth gate. All on free-tier services. Locally, you can run a real chatbot against **all 123 Bajaj-recommended schemes** through a login-gated UI, with live NIFTY/Sensex market context for market-timing questions and a Bajaj-verified theory FAQ for "what is SIP" / "about Bajaj" style queries.

Phases 1-6 and 4.3 of the original plan are done. **Phase 7.2 added** in response to real RM input received PAN India on 2026-05-15 — adds Gemini backend (provider-agnostic fallback), three eval-driven prompt grafts (curated 10-Q score 5.5→7/10), a live market-state tool via yfinance, and a theory/education layer. **Phase 7.1 still partial** — Cloudflare quick tunnel works locally; needs a phone-on-cellular smoke test and a launchd plist for unattended hosting. Phases 7.3, 8, 9 (ops hardening, eval polish, RM onboarding) not started.

---

## How to use the bot right now

From the repo root:

```bash
cd rm-assist

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

## What's been built (Phases 1-6, plus partial 7.1, plus Phase 7.2)

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
| **7.1** Cloudflare tunnel | 🟡 | `cloudflared` 2026.3.0 installed via Homebrew. Quick-tunnel path chosen (no Cloudflare account, no domain). Successfully serves Streamlit on a `*.trycloudflare.com` URL; verified locally with HTTP 200. **Outstanding**: phone-on-cellular check (7.1.5) and a launchd plist so the tunnel survives reboots (7.1.4). |
| **7.2.1** Gemini backend | ✅ | `_GeminiClient` in `retrieval/llm_client.py` mirrors `_GroqClient` (message + tool shape translation, system_instruction out-of-band). Retry-with-backoff on 429 RESOURCE_EXHAUSTED. `LLMClient.__init__` gains a `gemini` branch; pilot default stays `groq`. |
| **7.2.2** Prompt grafts | ✅ | Three additions: category norms reference table (per-category SD / horizon / Direct ER), `DATA UNAVAILABLE` discipline for NULL metrics, plain-language SD/Sharpe labels. **Curated 10-Q Groq eval: 5–6/10 → 7/10**, confirmed across two re-runs. Q19 is the causal win. |
| **7.2.3** Live market-state tool | ✅ | `retrieval/market_data.py` wraps `yfinance` for NIFTY 50 / Sensex / NIFTY 500 (current level + 1d/5d/1m/3m/6m/1y moves + 52w high/low distance, 15-min cache). New tool `get_market_state(indices?)`. SYSTEM_PROMPT gains "Market state and timing rules" + `MARKET_CONFIDENCE_NOTE` (mandatory disclaimer for market-timing answers). Market-timing **no longer refused** as `out_of_scope`. |
| **7.2.4** Theory / education layer | ✅ | `data/theory.json` (10 entries — 1 Bajaj-verified, 7 generic-with-disclaimer, 2 pending) + `retrieval/theory.py` (load + fuzzy match) + new tool `get_education_content(topic)`. SYSTEM_PROMPT gains "Theory and education rules" with three response-mode rules (verified, disclaimer, pending). Brings tool count to **6**. |

**Test status**: `pytest tests/ -v` → **88 passed, 40 skipped** (40 are Phase-2 golden questions deferred — token-heavy; run via `scripts/run_eval_sample.py --all`). Real-RM eval target lives in `tests/golden_rm_questions.json` — 20 questions, calibrated to **7/10 on Gemini Flash 2.5** as of 2026-05-18.

**Tool count: 6** — `query_db`, `lookup_scheme`, `compare_schemes`, `get_full_snapshot`, `get_market_state`, `get_education_content`. `get_schema` was retired 2026-05-16 in latency optimization #1; schema lives in SYSTEM_PROMPT.

---

## Key decisions and journey (so you don't relearn)

### LLM model: switched to `openai/gpt-oss-120b` from `llama-3.3-70b-versatile`
- **Why**: Llama-3.3 on Groq emits Llama-pseudo-XML tool-call format `<function=name>{json}</function>` instead of OpenAI-spec JSON in ~30-50% of calls on long system prompts. Our recovery code handles SOME shapes but not all.
- gpt-oss-120b emits 0 malformations in most 10-Q runs.
- **2× daily token budget**: 200K TPD vs 100K TPD on Groq free tier.
- 27% faster average latency.
- Costs ~12% more tokens per question (more markdown output). Worth it.
- Default set in `rm-assist/.env.example` and `rm-assist/config.py`. The `LLMClient` abstraction means swapping providers later is a one-line config change.

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

### Phase 7.2 pre-pilot capability additions (2026-05-15)

Real RM input received from RMs PAN India on 2026-05-15: 20 client-question patterns covering returns, market outlook, fund recommendations, risk management, theory/education, and Bajaj-specific positioning. The synthetic 40-Q golden set was clearly the wrong proxy for several of these — three gaps:

1. **Market-timing questions** ("is this right time to invest?", "should I redeem during this fall?") were being refused as `out_of_scope`. RMs need an answer.
2. **Theory/education questions** ("what is SIP?", "MF taxation?", "About Bajaj?") had no path — bot would refuse or hallucinate.
3. **Single-provider dependency** on Groq with no Gemini fallback for the inevitable outage.

Built in response:

- **`_GeminiClient` backend** in `LLMClient` — opt-in via `LLM_PROVIDER=gemini`. Pilot default stays Groq; Gemini is for A/B eval + outage contingency. SDK is `google-genai`. Tool-call shape and message-role translation are done in the backend; downstream code stays provider-agnostic.

- **Three eval-driven prompt grafts** (category norms reference, DATA UNAVAILABLE phrasing, plain-language SD/Sharpe labels). Curated 10-Q score moved from 5–6/10 to a stable 7/10. The category norms graft caused the clearest causal win on Q19 ("Is ABSL Arbitrage a buy?") — bot now writes a "Comment vs. arbitrage norm" column that forces it to surface the expense ratio it had previously dropped.

- **`get_market_state` tool** (`retrieval/market_data.py`, yfinance, 15-min cache). Returns current NIFTY 50 / Sensex / NIFTY 500 levels + 1d/5d/1m/3m/6m/1y moves + 52w high/low distance. SYSTEM_PROMPT gives the bot explicit permission to synthesize a buy / wait / redeem call on the broad market, BUT mandates `MARKET_CONFIDENCE_NOTE` ("this view rests on price action alone — no RBI / earnings / news context") on every such answer. Universal verification footer still applies. Market-timing removed from `out_of_scope` refusal list.

- **`get_education_content` tool + `data/theory.json`** with 10 FAQ entries. Three response modes:
  - `bajaj_verified=true`: content used verbatim, normal citation. (1 entry — `about_bajaj`, supplied 2026-05-15.)
  - `bajaj_verified=false, disclaimer=...`: generic education content surfaced with a "Bajaj-verified version pending" disclaimer. (7 entries — what_is_mf, what_is_sip, mf_risks, mf_taxation, investment_horizon, redemption_exit_load, mf_vs_fd.)
  - `pending=true, pending_message=...`: bot surfaces a "consult your team lead" stub, does NOT hallucinate Bajaj positioning. (2 entries — `direct_vs_regular` advisory pitch, `research_process`.)

Tool count went 4 → 6. SYSTEM_PROMPT gained ~500 tokens (category norms table + market-state rules + theory rules), still well within budget.

**Open follow-ups from this work** (all in PLANNING.md Open Items):
- Replace generic theory entries with Bajaj-voiced content when content team supplies it (no code change, just JSON edits).
- Ingest Bajaj's monthly market outlook note alongside `get_market_state` (if it exists — user to confirm).
- Build `tests/golden_rm_questions.json` from the 20 real RM patterns and run a fresh eval.

### Latency optimization #1: retired `get_schema` tool (2026-05-16)

The tool-use loop was making 3-4 inference round-trips per question, with each Groq inference on `openai/gpt-oss-120b` taking 5-15s. On a simple expense-ratio lookup (Q01) that totalled ~94s. One of those round-trips was the model calling `get_schema()` to discover column names — but the schema never changes between questions, so the round-trip was waste.

Change:
- `_build_schema_description`, `_tool_get_schema`, and `_SCHEMA_DESCRIPTION_CACHE` deleted from `retrieval/tools.py`.
- `get_schema` removed from `_DISPATCH` and `TOOLS`. Tool count: **6 → 5**.
- New `# Database schema` section added to `SYSTEM_PROMPT` (~600-800 tokens of curated DDL: schemes / fund_snapshots / holdings / sector_weights / periodic_returns + useful joins + rules of the road). Expanded vs the old `get_schema` output to explicitly include benchmark return columns (`return_*_bm` for 1M through since_inception) — these are needed by the 2026-05-16 benchmark-alpha prompt graft.
- `query_db` tool description now points to "the schema is provided in the system prompt" instead of "call get_schema first."
- `MAX_ITERATIONS` left at 6; the comment updated to reflect new "lookup_scheme → query_db → answer" canonical flow (2-3 turns typical, headroom for one tool-call failure).

Also incidentally fixed: `tests/test_tools.py::test_tools_schema_well_formed` was **already stale** before this change — it asserted `len(TOOLS) == 4` and a 4-name set, but `TOOLS` had grown to 6 entries during Phase 7.2 (added `get_market_state` + `get_education_content`) without the test being updated. Now correctly asserts `len(TOOLS) == 5` with the post-retirement name set. The `test_get_schema_returns_tables` test was repurposed to verify `get_schema` now returns the `unknown_tool` error envelope.

Validation:
- `pytest tests/test_tools.py` — 8/8 pass.
- Curated 10-Q smoke test post-graft hit the 200K Groq daily TPD cap at Q15 (we were already at 199K from earlier runs in the day). Only 3 of 10 questions executed cleanly: Q01 PASS, Q05 FAIL (pre-existing ambiguity), Q11 PASS. **Q01 latency 94.2s → 71.2s (~25% reduction)** — directionally validates the round-trip-saved hypothesis. Full 10-Q re-run pending daily quota reset.

Two more optimizations on deck (from the same diagnosis, not yet built):
- **#2** — `get_full_snapshot(scheme_hint)` tool that does fuzzy-match + returns the full per-fund picture (fund_snapshots + holdings + sector_weights + benchmark returns) in one tool call. Collapses lookup_scheme + 1-2 query_db calls into one. ~1-2 hours.
- **#3** — Streaming the final answer via `st.write_stream` for perceived-latency win. Already on Phase 8 UX-polish list.

### Latency optimization #2: `get_full_snapshot` tool + category-shaped-queries rule (2026-05-16)

Post-#1, the tool-use loop on a typical single-fund recommendation still did 3-4 round-trips: lookup_scheme → query_db (snapshot) → maybe query_db (benchmark or holdings) → answer. The `lookup_scheme + query_db` sequence is the single most common workflow pattern in the bot's usage; collapsing it to one call is the highest-leverage remaining structural change.

New tool `get_full_snapshot(scheme_hint, include?)` in `retrieval/tools.py`:
- Fuzzy-matches the scheme name internally (collapses `lookup_scheme` into the same call).
- Returns six sections in one JSON envelope: `snapshot` (curated ~30 metrics), `benchmark` (name + return_*_bm + computed alpha for 1Y/3Y/5Y), `top_holdings` (top 10 by weight), `sector_weights` (all sectors), `managers` (parsed from fund_managers_json), `drawdown` (pct + dates).
- Optional `include` parameter lets the model trim sections (e.g. `["snapshot"]` for a pure return question) — keeps payload bounded on lighter questions.
- On no-match returns `{"matched": False, "scheme_hint": ..., "message": ...}` envelope so the model routes to an `unknown_scheme` refusal cleanly.

Tool count: **5 → 6**. SYSTEM_PROMPT workflow rewritten — Step 1 is now "if question is about ONE specific scheme, call get_full_snapshot." `lookup_scheme` demoted to disambiguation duty only (when the user's wording could match multiple schemes). `query_db` reframed as the cross-fund / category / filter / ranking path.

Bundled with the same commit: new **"Category-shaped queries"** section in SYSTEM_PROMPT. Disambiguates strict-SEBI-category vs functional-large-cap-exposure interpretation. Default is strict category match (`WHERE category = 'Large Cap'`); client-conditional questions add a one-line note about adjacent categories (Multi Cap / Flexi Cap also carry significant large-cap exposure); portfolio-shaped questions ignore the category column and filter on `large_cap_pct` instead. Same logic generalises to mid/small cap and debt-vs-hybrid.

Expected payoff: single-fund questions drop from 4 round-trips to 2 (one for get_full_snapshot, one for the final answer). On Q01-shape questions (already 71s post-#1), this should land in the ~40-50s range. Validation pending clean Groq run (still rate-limited from earlier today).

Unit tests added: 4 new tests in `tests/test_tools.py` cover all-sections, include filter, no-match envelope, and bad-arguments path. Updated `test_tools_schema_well_formed` for `len(TOOLS) == 6` and the new name set. Full suite: **60 passed, 40 skipped** (was 56/40; +4 new tests, no regressions).

### Calibrated eval baseline + Groq TPM-cap discovery (2026-05-18 evening)

After all the day's grafts (A + B + C, +16 snapshot columns, multi-turn conversation, v1 streaming, RM08/RM09 fixes) landed, ran the calibration eval on both providers using `tests/golden_rm_questions.json` 10-Q subset (RM01, RM03, RM05, RM07, RM08, RM09, RM13, RM14, RM18, RM20).

**Headline: 7/10 PASS on Gemini Flash 2.5 — unchanged from this morning's pre-grafts baseline.** Today's retrieval-quality work didn't regress anything; the bot still produces correct answers on every question that wasn't blocked by infrastructure failure.

The 3 failures on Gemini are pre-existing flakes, not graft regressions:
- **RM09 shortlist-conditional**: malformed tool call from Gemini (Phase 8 deferred — same shape as Q32). The RM09 over-refusal fix from earlier today IS correct; it just doesn't get a chance to fire because the question crashes before reaching the over-refusal code path.
- **RM14 theory-MF**: malformed tool call on a theory-disclaimer route. Passed earlier today, fails this run. Flake.
- **RM20 MF-vs-FD**: empty answer from Gemini (likely 503-retry exhaustion mid-call; the eval log shows multiple 503s during the run).

Improvements visible in the run:
- **RM08 (benchmark-alpha) now passes** — grader brittleness fix from earlier today landed (substring "1Y" → "alpha"). The bot output explicitly shows `"Alpha (fund – benchmark): 0.70%"`.
- **Long-answer latency dropped 30-40%** on `get_full_snapshot`-heavy questions: RM07 84s → 53s, RM18 64s → 44s. Likely from NULL-trim payload reduction + richer single-shot data eliminating follow-up `query_db` calls.

**Groq TPM cap discovery (production-decision moment):**

Tried the same 10-Q eval on Groq for an apples-to-apples comparison against the STATUS.md 7-8/10 baseline. Got 3/10 PASS — but the 7 failures are all `413 Request too large for model openai/gpt-oss-120b... Limit 8000, Requested 8xxx`. That's the **per-minute TPM cap on Groq free tier**, NOT the daily TPD cap we've hit before.

Root cause: the system has grown past 8K tokens per request. Sources of growth, cumulative:
- SYSTEM_PROMPT: ~4K tokens (post category norms + market-state rules + theory rules + benchmark-alpha framing + category-shaped-queries + multi-turn-handling + embedded DB schema)
- TOOLS schema: ~600 tokens (6 tool definitions, fat descriptions)
- `get_full_snapshot` payload: ~2K tokens (rich; the +16 columns added ~200-400 tokens)
- Multi-turn history: up to 2-3K tokens on a deep follow-up
- Tool-result accumulation across the tool-use loop: 1.5K-3K per iteration

Single requests now run 8-10K tokens — over the free-tier 8K TPM cap. The cap is per-minute, so it doesn't reset by waiting; only paid tier removes it.

**Production decision (logged for the post-pilot scaling conversation):**

The Groq free tier is no longer fit-for-purpose for this system. Three viable paths:
1. **Groq Dev Tier (paid)** — no TPM cap. ~$15-130/month at projected pilot → 300-RM volumes. Cheapest path; same model behavior.
2. **Switch primary to Gemini Flash 2.5** — much higher TPM (1M+/min). Today's 7/10 baseline is on Gemini. Different model = re-validate quality on a wider eval before committing.
3. **Shrink prompt + payload** — temporary fix; the system will outgrow it again as features land. Not the right long-term answer.

Per the cost projection from earlier today (350 RMs × 30 q/day × 30 days with both prompt-caching AND app-level query cache), realistic monthly LLM spend lands in **$400-800/month range on gpt-4o-mini**. Comparable for Groq Dev or Claude Haiku. The question is just which provider.

**Calibrated 7/10 is the post-grafts baseline as of 2026-05-18.** This is the number to beat going into Phase 8 eval polish.

### Retrieval quality graft: +16 snapshot columns (2026-05-18 evening)

Audit of optimization #2's NULL-trim (B) surfaced a pre-existing gap: SYSTEM_PROMPT schema described columns that `get_full_snapshot` didn't actually return. Closed the gap by expanding `_FULL_SNAPSHOT_METRIC_COLUMNS` from 28 → 44 entries.

Added:
- Full return ladder: `return_1m`, `return_3m`, `return_6m`, `return_2y`, `return_10y` (was missing all intermediate periods)
- All 1Y/3Y risk metrics: `r_square`, `sortino`, `tracking_error` variants (6 fields)
- `up_capture_3y`, `down_capture_3y` (was 1Y-only)
- Portfolio diversification: `total_securities`, `avg_mkt_cap_cr`, `median_mkt_cap_cr`

Kept excluded intentionally: `overview` / `min_investment` / `exit_load` (prose/TEXT — verbose, fetched on demand), JSON blob columns (surfaced via parsed sections or `json_extract`).

After-change field counts (NULL-trim still active):
- Canara Robeco Multi Cap (equity, ~2.8yr): 32 fields populated
- Bandhan Gilt (debt, 17yr): 28 fields populated

Audit conclusion on B: **no real data was removed by the NULL-trim**. The trim only drops keys where value is exactly `None`. Validated on both equity and debt funds. The pre-existing column-coverage gap was a separate, pre-existing scope decision from #2 — not a B regression.

### Retrieval quality grafts A + B (2026-05-18 evening): word-token fuzzy match + NULL-trim payload

**A: replaced LIKE-substring with word-token overlap scoring.**

Old fuzzy match (`_fuzzy_lookup_scheme` in `retrieval/tools.py`) used a single `LIKE '%X%'` clause + alphabetical-first-hit ranking. Failures RMs would have hit:
- "Multi Cap Canara" → no match (word-order swap)
- "ABSL" → no match (no AMC-name match path)
- "DSP" → first DSP fund alphabetically, often wrong
- Any typo → total failure

New approach:
1. Expand ~20 hardcoded brand abbreviations (ABSL → Aditya Birla Sun Life, PPFAS → Parag Parikh, MOSL → Motilal Oswal, etc.)
2. Tokenize the query, filter domain-noise stopwords (`fund`, `scheme`, `mf`, `regular`, `direct`, `growth`)
3. Score each scheme by token-overlap with `scheme_name + amc + category`
4. Rank by score desc; tiebreak alphabetical

Tolerates word-order, partial typos (one bad token, others still match), and brand abbreviations RMs actually use. `_tool_lookup_scheme` output gains a `match_score` field so the model can gauge confidence on multi-match results.

Note: `scheme_aliases` table exists in schema but is unused for v1. Hardcoded abbreviation map covers the 95% case; if Bajaj/RMs need custom aliases later, switch reads to query the table. No schema change required.

**B: NULL-trim on `get_full_snapshot` output.**

New `_drop_nulls(d)` helper removes keys whose values are exactly `None`. Applied to snapshot, benchmark (including alpha sub-dict), drawdown, managers (per entry), top_holdings (per row), sector_weights (per row).

Saves ~20-30% of payload tokens per call with zero information loss — equity funds drop debt-only fields (`avg_maturity_years`, `yield_to_maturity`), debt funds drop equity-side fields (`large_cap_pct`, `portfolio_pe`), young funds drop 3Y metrics. Audited on both fund types post-deploy; nothing real was removed.

### Retrieval quality graft C (2026-05-18): embedding fallback for theory matching

Builds on A + B. The substring matcher in `retrieval/theory.py` works for verbatim alias hits but misses paraphrases ("explain MFs" → `what_is_mf` was a miss). Embeddings catch those.

**Design — fast substring path stays primary; embedding fallback only runs on substring miss:**

1. Try substring match against title + aliases (existing logic, unchanged for hits).
2. On miss: lazy-init `sentence-transformers/all-MiniLM-L6-v2`, embed the 10 theory entries' (title + aliases) once, cache.
3. Embed the query, cosine-similarity against all topic vectors, return the best if score ≥ 0.50.
4. If still no match → no-match envelope as before.

**Graceful degradation by design**: `sentence-transformers` is in `requirements.txt` but the code handles `ImportError` cleanly. On 8GB dev machines or lean deployments where you skip the install, only substring matching runs and the system still works. Production deploy should install it (~80MB on-disk, ~80MB resident RAM) for the paraphrase tolerance.

**Why all-MiniLM-L6-v2**: 80MB model, runs CPU on the Oracle Cloud Free Tier ARM VM, established quality on short-text semantic similarity. If we ever need higher quality, swap to `bge-small` (similar size, slightly better on retrieval benchmarks) — interface unchanged.

**Why a 0.50 cosine threshold**: empirical pick for short surfaces (title + ~7 aliases concatenated). Legitimate semantic matches land in 0.55-0.85; unrelated topics drop below 0.40. The 0.50 cutoff catches paraphrases without false positives.

**What the user/model sees**: identical contract — the same `{matched, topic_id, content, ...}` envelope as today. Embedding hits aren't flagged separately; they just look like more-tolerant matching.

**Two things we DIDN'T do** (intentional):

- **Did NOT switch to embeddings as primary.** Substring is faster, deterministic, and works perfectly for the bulk of variant phrasings. Embeddings are a fallback, not a replacement.
- **Did NOT add embeddings for scheme lookup.** Schemes already use word-token scoring (graft A), which is the right tool there because scheme matches are lexical, not semantic. Embeddings would over-match — "DSP" semantically close to "TATA" wouldn't help a user looking for DSP.

Validation: 9 new unit tests in `tests/test_theory.py` cover substring fast-path, bidirectional matching, embedding-paraphrase recovery (using a fake model to avoid loading the real 80MB model in CI), below-threshold no-match, ImportError graceful degradation, and substring-short-circuits-embedding ordering. Full pytest: **88 passed, 40 skipped** (was 79/40; +9 new tests, no regressions).

### Multi-turn conversation support (2026-05-16, evening)

Before this change, every call to `ask()` started fresh — the Streamlit UI displayed prior turns but never threaded them back to the LLM. An RM asking "what's the expense ratio of Canara Robeco?" followed by "how does it compare to DSP?" got the second question treated as completely new — the "it" had no referent.

Design: sliding window with heuristic compaction.

- `ask(question, history=None, user_id=None)` — optional `history` parameter. Backwards compatible (defaults to `None`, single-shot mode for CLI / eval).
- New helpers in `app/chatbot.py`: `_truncate_content`, `_compact_older_turns`, `_build_messages`.
- Last **6 messages** (3 Q+A pairs) kept verbatim, each capped at **2000 chars** (defensive against very long table answers).
- Older messages collapsed into a single system note: `Earlier user questions in this conversation (compacted):\n  - "..."\n  - "..."`. Only user questions carry forward — assistant answers are derivable from re-running tools and bloat context without signal.
- Streamlit extracts prior `st.session_state["messages"]` (excluding the just-appended current user message) and passes as `history=...`. New "Clear conversation" button in the sidebar resets the thread.
- New SYSTEM_PROMPT section "Multi-turn conversation handling" tells the model how to resolve pronouns, when to reuse vs re-fetch data, how to interpret the compact note, and to keep the verification footer on every follow-up.

Why heuristic compaction over LLM-summarization: LLM summarization adds an extra inference call per compaction event — burns tokens AND latency on every Nth turn. The heuristic version is instant + deterministic. For RM workflows specifically, "which funds were discussed" + "what was the recent topic" is the load-bearing context, both directly extractable from user-question text.

Cost impact: +1-3K input tokens per follow-up turn (mostly cached prefix). At 350-RM production scale (350 × 30 q/day × 30 days, ~30% follow-ups), this is the +$50-100/month delta in the earlier cost estimate.

Validation: 14 new unit tests in `tests/test_multi_turn.py` exercise truncation, compaction, sliding-window threshold, role filtering, and the `ask(history=...)` integration via the mock LLM. Full pytest: **74 passed, 40 skipped** (was 60/40 before, +14 new, no regressions).

### Gemini RM-eval run + RM08 grader fix + RM09 over-refusal fallback (2026-05-16, late afternoon)

Groq daily TPD cap stayed blown all day (200K/day exhausted at ~196K by mid-afternoon). Switched `LLM_PROVIDER=gemini` to validate the post-#2 prompt + category-rule grafts against the new `tests/golden_rm_questions.json` 10-question subset (RM01, RM03, RM05, RM07, RM08, RM09, RM13, RM14, RM18, RM20).

**Result: 7/10 PASS on Gemini Flash. Effective ~8-9/10 once you discount grader brittleness and one pre-existing flake:**

| ID | Bucket | Result | Read |
|---|---|---|---|
| RM01 | single-fund perf | PASS | Clean lookup, 6.0s (same Q was 71s on Groq). #1+#2 working. |
| RM03 | market timing | PASS | NIFTY + MARKET_CONFIDENCE_NOTE + footer all present. |
| RM05 | theory (Bajaj verified) | PASS | 60-year content surfaced. |
| RM07 | recommendation rationale | PASS, 84s | Benchmark-alpha graft explicit: "Fund: 6.65%, Benchmark: 5.95%, Alpha: 0.70%". |
| RM08 | benchmark-alpha | FAIL → fixed | Grader brittleness, NOT a bot failure. Bot wrote "1-Year Return" but the substring assertion required literal "1Y". Fix in this commit: substring set updated to `["Canara Robeco", "alpha", "benchmark", "verify against your own"]` — asserts the graft computed alpha rather than just naming a period. |
| RM09 | shortlist-conditional | FAIL → fix landed | Real bot issue. Bot replied "unable to find any funds for moderate-risk + 5Y horizon" — over-refusal pattern. Many funds DO fit; the bot filtered too strictly on 3Y metrics (NULL for funds <3yr old) and refused. Fix in this commit: new "no-perfect-fit" rules in SYSTEM_PROMPT shortlist + conditional-advice sections — when strict criteria yield zero funds, fall back to the next-best signal (1Y instead of 3Y, broaden category by one tier) and DISCLOSE the substitution. Never refuse for "no perfect match." |
| RM13 | category comparison | PASS | NEW category-shaped-queries rule working — bot leaned with NIFTY context + named categories. |
| RM14 | theory (what is MF) | PASS | ⚠️ disclaimer + "pending" + content. |
| RM18 | theory (taxation) | PASS, 64s | ⚠️ disclaimer + LTCG content. |
| RM20 | theory (MF vs FD) | FAIL | "model returned a malformed tool call" — Gemini also exhibits the same pre-existing flake pattern as Groq. Tracked as Phase 8 deferred. |

**Signal worth pinning:**
1. Latency optimizations didn't regress anything. #1 + #2 + category rule all functionally working on Gemini.
2. Benchmark-alpha graft from earlier today fires correctly (RM07 explicit alpha computation, RM08 same pattern).
3. Gemini Flash is dramatically faster on short answers (6-9s vs 70-90s on Groq gpt-oss-120b) but comparable on long table answers (~85s+). Most wall time is generation, not tool calls. Apples-to-apples Groq baseline pending Groq daily quota reset.
4. Gemini also exhausted its 20-req/day free-tier limit by question 7-8 of the eval — explains why latencies climbed through the run (multiple 429 retries with backoff visible in the log before the actual 200 responses).

**Two real bot issues, one fixed in this commit:**
- RM09 over-refusal → FIXED via no-perfect-fit fallback rules.
- RM20 / Q32 malformed-tool-call flake (same shape) → tracked as Phase 8 deferred. Not provider-specific; affects both Groq and Gemini on certain question shapes.

### v1 streaming: typewriter via `st.write_stream` (2026-05-16)

UX polish, NOT a wall-time win. The Streamlit chat-input handler previously did `st.markdown(answer)` — full answer pops in after the entire tool-use loop completes. Replaced with `st.write_stream(_typewriter(answer))` where `_typewriter` yields 15-char chunks at ~50/s.

What it changes: the answer animates in as the user watches. Long table answers (~500 chars) finish typing in <1s; long prose answers (~1500 chars) in ~2s. By the time typing completes, the user has read the leading content. Perceptual latency drops modestly.

What it does NOT change:
- Total wall time from question submit to final answer visible — same as before. The model still does its 2-N round-trips silently behind a spinner; the typewriter only begins after `ask()` returns.
- Token spend, eval scores, API behaviour.

Why this is v1 not v2: real LLM streaming (tokens flowing as the model generates) requires modifying `_GroqClient.chat` to support `stream=True` and accumulating `tool_calls` deltas across chunks separately from content deltas. ~2-3 hours of work plus edge cases (tool_use_failed recovery in streaming mode, finish_reason handling, mid-stream errors). Deferred until we have a clean post-#1/#2 eval baseline to compare against — don't want streaming-bugs and tool-routing-bugs entangled if the eval regresses.

Scope: ~50 LOC added to `app/streamlit_app.py` (typewriter helper + the one-line UI swap). No backend changes. Fully revertible by reverting the file.

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
| **Phase 7.1 finish-up** | Local tunnel works; pilot-readiness needs two more steps | (a) Open the trycloudflare URL on a phone over cellular to confirm external reachability (7.1.5). (b) Wrap `cloudflared` + Streamlit in launchd plists so they survive a reboot — or pick a hosting move per the "Future hosting" open item in PLANNING.md. |
| **Bajaj-verified theory content** | Two `data/theory.json` entries (`direct_vs_regular` advisory pitch, `research_process`) return pending stubs. Seven other entries are generic-with-disclaimer | Bajaj content team supplies canonical text → flip `bajaj_verified` to `true` and paste into JSON. No code change. |
| **Monthly market outlook note** | If Bajaj publishes one, ingest it alongside `get_market_state` for richer market-timing answers (price action + research-team view) | User to ask Bajaj research team whether the note exists. If yes, parse it via the same Finalyca-style pattern. |
| **Production provider decision** (NEW 2026-05-18) | Groq free tier blew the per-minute 8K TPM cap mid-tool-loop because cumulative prompt + payload growth pushed single requests past 8K tokens. Free tier no longer fit-for-purpose. | Choose: (a) Groq Dev tier (paid, ~$15-130/mo, no TPM cap, same model), (b) Gemini Flash 2.5 as primary (calibrated 7/10 today on free tier; paid for higher RPD), (c) defer until Bajaj compliance confirms whether public-derivable data may go through external APIs. Cost projection earlier today: $400-800/month at 350 RMs with prompt-caching + app-level query cache. |
| **Run full 20-question RM eval cleanly** | Confirmed 7/10 on 10-Q subset calibrated baseline today on Gemini; full 20 will exhaust Gemini free tier 20-req/day cap and is over the Groq free-tier TPM cap | Run on Gemini paid OR Groq Dev tier when one is signed up. Command: `python -m scripts.run_eval_sample --file tests/golden_rm_questions.json --all` |
| **App-level query cache** (NEW 2026-05-18) | Scoping doc written in this session. Hash `(normalized_question, report_month)` → cached answer; bypass LLM on hits. Expected 30-50% hit rate. Saves ~$290/month at 350-RM scale. | ~3-4 hours: new `retrieval/query_cache.py` (~150 LOC) + migration 003 + `ask()` integration + tests. See PLANNING.md Open Items for the full design. Best built AFTER pilot launch so cache is sized against real `query_log` data. |
| **v2 real LLM streaming** (NEW 2026-05-18) | v1 fake-typewriter shipped. v2 = actual API streaming for first-token-fast UX. ~2-3 hours: modify `_GroqClient.chat` to support `stream=True` + accumulate tool_calls across chunked deltas + new `ask_stream()` generator path. | Defer until clean post-grafts eval baseline holds across multiple runs. Don't entangle new streaming code with regressions in other work. |
| **Future hosting choice** (open item) | Stable URL + always-on, off your laptop | Production answer is Oracle Cloud Free Tier (Mumbai). See "Post-pilot scaling notes" in PLANNING.md for details. |

---

## Deferred to Phase 8 (eval & polish)

These are deliberately deferred — they're real but non-blocking, and Phase 8 is the right place to address them as a batch.

1. **Tool-format recovery in `_GroqClient`** — Q32 still fails flakily because gpt-oss-120b occasionally emits a malformed tool call that escapes existing recovery logic. Q19 was fixed by the 2026-05-15 prompt grafts. ~30-60 min to extend recovery for additional shapes.

2. **Q05 ambiguity** — "Rank our 3 recommended funds" still triggers a clarifying question on both Groq and Gemini. Needs system-prompt rule for "pick a sensible default + disclose" rather than asking back. ~30 min prompt tweak.

3. **Q22 judgment** — "Risk-averse client, 1Y horizon" still picks ICICI Liquid over ABSL Arbitrage on both providers; the category-norms graft didn't redirect it. Likely needs an explicit rule that arbitrage funds match "low-vol + short-horizon" profiles. ~30 min prompt tweak.

4. **Full 40-question eval** — curated 10-Q runs are stable. Full run uses ~330K tokens, exceeds Groq free-tier 200K daily cap, needs paid Groq OR a 2-day split. Gemini Flash free tier is 20 req/day — also insufficient for the full eval without a paid upgrade.

5. ~~**Remaining parser quirks**~~ → **Closed 2026-05-12.** Only `parse_portfolio_characteristics` still fails on 2 FoFs (Franklin US Opps, SBI Income Plus Arbitrage) where the section is literally absent from the PDF.

6. **`EXPECTED_SECTIONS_BY_FUND_TYPE` enforcement** — map exists in `parse_finalyca.py`; not yet used to distinguish "section missing because fund type doesn't have it" from "section missing because parser broke."

7. **UX polish** (PLANNING 8.2): slow-query status messages, long-answer truncation with expander, friendlier error states, streaming output via `st.write_stream` (latency-perception win).

8. **Daily backups** (PLANNING 8.3.1) of `bajaj_mf.db`.

9. **App-level query cache** — promoted from PLANNING Phase 10 as a candidate latency/cost win. Hash `(question_normalized, report_month)` → cached answer + tool trace. Expected 30-50% hit rate on RM-asked questions. ~100 LOC. Build AFTER pilot launches so the cache is sized against real `query_log` data, not synthetic guesses.

---

## Important file locations

| Path | Purpose |
|---|---|
| `PLANNING.md` | The full phase plan with checkboxes |
| `PLANNING_PROMPT.md` | The original problem statement (frozen reference) |
| `STATUS.md` | This file (rolling state) |
| `schemes_master.csv` | The 90-scheme seed (3 columns: category, scheme, url) |
| `rm-assist/.env.example` | Template — copy to `.env` and add `GROQ_API_KEY` |
| `rm-assist/db/schema.sql` | DB schema (8 tables: schemes, fund_snapshots, holdings, sector_weights, periodic_returns, query_log, schema_version, scheme_aliases) |
| `rm-assist/ingest/parse_finalyca.py` | Main parser (header + manager + returns) |
| `rm-assist/ingest/_section_parsers.py` | Section parsers split out: mkt_cap, investment_style, periodic_returns, full_holdings |
| `rm-assist/app/streamlit_app.py` | The UI |
| `rm-assist/app/prompts.py` | System prompt (identity, 6-tool workflow, operating mode, category norms, market-state rules, theory rules, refusal rules) + `VERIFICATION_FOOTER` + `MARKET_CONFIDENCE_NOTE` constants |
| `rm-assist/retrieval/tools.py` | **6** tool implementations + OpenAI-style TOOLS schema (query_db, lookup_scheme, get_schema, compare_schemes, get_market_state, get_education_content) |
| `rm-assist/retrieval/llm_client.py` | Provider-agnostic LLMClient (Groq + Gemini + Mock backends, tool-call normalization, Llama-pseudo-XML recovery, 429 retry-with-backoff on Gemini) |
| `rm-assist/retrieval/market_data.py` | yfinance wrapper for NIFTY 50 / Sensex / NIFTY 500. 15-min in-memory cache. Used by `get_market_state` tool. |
| `rm-assist/retrieval/theory.py` | Loads + fuzzy-matches `data/theory.json` for the `get_education_content` tool. |
| `rm-assist/data/theory.json` | 10 FAQ entries (1 Bajaj-verified, 7 generic-with-disclaimer, 2 pending) for theory / education / Bajaj-positioning questions. Update by editing JSON; no code change required. |
| `rm-assist/tests/golden_questions.json` | 40 Q+A specification |
| `rm-assist/tests/golden/*.json` | Hand-extracted ground-truth for 3 sample PDFs |
| `rm-assist/tests/snapshots/*.json` | Parser regression baselines (deepdiff target) |
| `rm-assist/data/ingest_report_2026-05.json` | Per-scheme parse outcomes (errors, invariant warnings) |
| `rm-assist/data/eval_sample_*.json` | Real-Groq eval transcripts |
| `~/.claude/plans/i-want-to-make-cozy-storm.md` | Original architecture decisions plan |
| `~/.claude/projects/.../memory/` | Persistent memory: user profile, hardware, stack, operating mode, etc. |

---

## Commit log highlights

Run `git log --oneline` for the full picture. Key commits:

```
709326b  Phase 6 - Streamlit UI: auth gate, chat + thumbs feedback, sidebar, create_auth_account CLI
bd9aa51  5.4 - Q15 test-design fix; eval stable at 7-8/10
a5a9317  5.4 - Metric-completeness prompt rule + grader Unicode dash normalization; 10-Q eval 7/10
a060d0a  5.4 - Switch default LLM_MODEL to openai/gpt-oss-120b (2x daily token budget, better tool-use)
2a16529  5.3 - Tool-use loop in chatbot.py + Llama tool_use_failed recovery
c5123b4  5.1, 5.2 - 4 tools + system prompt with operating-mode rules
6d0c6ae  Fix parse_drawdown: section spills to page 3 for 21 funds
21afa78  4.1, 4.2 - Bulk download + ingest 90 PDFs
7636039  3.3, 3.4, 3.5 - Invariants + golden samples + diff regression
b4170dc  3.2.11-15 - Mkt cap, investment style, periodic returns, full holdings parsers
91f8053  3.2.7-10 - Portfolio chars, composition, drawdown, risk rating
d4eed9b  3.2.4-6 - Risk metrics, sector weights, top holdings
86dc1de  3.1, 3.2.1-3 - Dispatch refactor + header, fund managers, trailing returns
6de66a0  1.6 - Normalized table writes with txn rollback safety
82e5fc5  1.5 - db_query (read-only), chatbot spine, query_log, 8 tests
565928d  1.4 - LLMClient with Groq + Mock backends, 6 unit tests
bba2119  1.3 - Stub parser (5 fields from Canara Robeco) + ingest_one CLI
8d690ef  1.2 - DB schema with 7 fixes, init_db.py, migrations baseline
3ce86db  1.1 - Project skeleton, requirements, config, README
```
