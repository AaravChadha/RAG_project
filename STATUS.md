# STATUS — RM Assist Pilot

> **What this file is.** A rolling snapshot of where the project actually is, so a fresh dev (or future-you) can open the repo and resume in 5 minutes. Read this first, then `PLANNING.md` for the full phase plan.
>
> **Last update**: 2026-05-16 (post-RM-input prompt grafts + latency optimization #1: retired get_schema tool, schema embedded in SYSTEM_PROMPT)

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

**Test status**: `pytest tests/ -v` → **56 passed, 40 skipped** (40 are Phase-2 golden questions waiting on full Phase-8 eval). Phase 7.2 work was smoke-tested manually against representative questions; eval target shifts to `tests/golden_rm_questions.json` once built from real RM input.

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
| **Run RM eval against tuned bot** | `tests/golden_rm_questions.json` was built 2026-05-16 (20 real-world client questions) but eval run was blocked when Groq 200K daily TPD cap hit mid-smoke-test | Wait for Groq daily reset OR switch `LLM_PROVIDER=gemini` and rerun. Command: `python -m scripts.run_eval_sample --file tests/golden_rm_questions.json --all`. |
| **Re-verify curated 10-Q post-graft baseline** | Two prompt grafts + get_schema retirement landed 2026-05-16. Smoke test was contaminated by Groq TPD cap — only 3 of 10 questions executed cleanly (2 PASS, 1 FAIL on known Q05 ambiguity). Q01 latency dropped 94s → 71s (~25%) — directionally validates optimization #1 | Same as above — needs a clean 10-Q Groq run to confirm we're still ≥7/10 against the STATUS.md baseline. |
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
