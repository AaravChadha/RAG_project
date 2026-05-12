# PLANNING.md — Bajaj Capital MF Research Chatbot Pilot

> **Purpose of this document.** Hierarchical breakdown of the work for the 2-week, 5-RM pilot. Every phase has tasks; every task has subtasks; every subtask has acceptance criteria so we know it's done. Reference this file before starting each phase. Refer to `PLANNING_PROMPT.md` for the original problem statement and to `~/.claude/plans/i-want-to-make-cozy-storm.md` for the locked decisions and architectural rationale.
>
> **Cross-cutting constraints (apply to every phase):**
> - Zero paid services. Free tier or self-hosted only until pilot proves value.
> - Solo dev, ~2 weeks calendar, ~6 hours/day effective.
> - 8 GB M2 Mac is the dev machine AND the pilot host (via Cloudflare Tunnel).
> - Every RM query gets logged for audit — non-negotiable, even at 5 users.
> - Every numeric answer must cite scheme + as-of date.
> - No fine-tuning. RAG/structured-DB only.
> - **The bot knows scheme data only.** It contains NO information about its developer, maintainer, or how it was built. If asked, it answers "I'm an internal Bajaj Capital research tool — I don't have details on who built me." This applies in the system prompt, README, and any user-facing surface.
>
> **Access model — sign-in is the rival defense, not data sensitivity.** The underlying numbers (NAVs, returns, holdings, ratios, manager bios, AUM) are public — AMFI publishes them, SEBI mandates monthly disclosure, every AMC's factsheet has the same data. Finalyca packages them with Bajaj branding. **What's commercially valuable is (a) the curated "Bajaj recommended list" — which 90 schemes made the cut — and (b) the bot itself as a productivity tool.** Both are protected by the auth gate (Phase 6.1), not by data secrecy. Implications:
> - **Auth is non-negotiable from day 1**, even at 5 users. No anonymous access ever — not even for "demo." This is what stops a rival from using the bot.
> - The Groq-free-tier compliance concern (R1a) is **lower-stakes than typical "internal data" worries**, because the data flowing through Groq is public-derivable. Sign-off is procedural, not blocker-class.
> - Hosting can stay on Cloudflare Tunnel (private URL), but Streamlit Community Cloud (public repo) also becomes viable as a contingency since the *code* doesn't expose anything sensitive — the data is still gated behind auth and the SQLite file stays gitignored.
> - **Auth hardens as scale grows.** 5 RMs: hashed YAML is fine. 300 RMs: add IP allowlist (Bajaj corporate ranges), rate limiting, account lockout, ideally SSO via Bajaj's identity provider. This is a Phase-2 build, not pilot — but the sequencing is: pilot uses streamlit-authenticator → migration to Bajaj SSO before scaling beyond 50 users.

---

## Index of phases

| Phase | Days | Goal | Exit criterion |
|---|---|---|---|
| [Phase 1](#phase-1--vertical-slice-spine-day-1) | 1 | Vertical slice end-to-end | One real question → DB SQL → cited answer |
| [Phase 2](#phase-2--eval-driven-spec-day-2) | 2 | Golden questions written FIRST | `tests/golden_questions.json` with ~35-40 Q+A spanning all categories below, including 3 refusal cases |
| [Phase 3](#phase-3--parser-widening-day-35) | 3-5 | Full parser against 3 sample PDFs | All section parsers pass golden invariants on 3 samples |
| [Phase 4](#phase-4--bulk-ingest--debt-discovery-day-6) | 6 | Ingest all 90 + locate 33 debt schemes | All 90 in DB; debt-circular URL identified |
| [Phase 5](#phase-5--tool-use-chatbot-day-78) | 7-8 | Chatbot answers golden questions | ≥80% golden Qs pass on first run |
| [Phase 6](#phase-6--streamlit-ui-day-9) | 9 | Working UI with citations and feedback | Localhost UI accepts query, shows answer, captures thumbs |
| [Phase 7](#phase-7--cloudflare-tunnel--compliance-day-10) | 10 | Public URL + Bajaj compliance sign-off | Tunnel URL accessible from phone; Groq policy confirmed (or contingency to another remote free provider) |
| [Phase 8](#phase-8--eval--polish-day-1112) | 11-12 | Pass all golden questions | 100% pass or each failure has a documented "why acceptable" note |
| [Phase 9](#phase-9--pilot-onboarding-day-1314) | 13-14 | 5 RMs trained, baseline data | 5 RM accounts created, ≥1 hour shadowing per RM done |

---

## [x] Phase 1 — Vertical slice (spine) [Day 1]

**Goal:** Prove the entire pipeline (PDF → DB → SQL → LLM → cited answer) end-to-end on ONE field of ONE fund. Everything else is widening this spine.

**Exit criterion:** `python -c "from app.chatbot import ask; print(ask('What is the expense ratio of Canara Robeco Multi Cap?'))"` returns the correct number with `(scheme + as-of date)` citation.

### [x] 1.1 Project skeleton & config
- [x] **1.1.1** Create directory structure under `RAG_project/bajaj-mf-bot/` matching the layout in `~/.claude/plans/i-want-to-make-cozy-storm.md` (db/, ingest/, retrieval/, app/, tests/, data/pdfs/2026-05/).
- [x] **1.1.2** Write `requirements.txt` with pinned versions: `pdfplumber`, `pymupdf`, `python-dotenv`, `groq`, `streamlit`, `streamlit-authenticator`, `pytest`, `deepdiff`.
- [x] **1.1.3** Write `.env.example` with `GROQ_API_KEY=`, `LLM_PROVIDER=groq`, `LLM_MODEL=llama-3.3-70b-versatile`. Real `.env` is gitignored.
- [x] **1.1.4** Write `.gitignore` excluding `data/pdfs/`, `*.db`, `.env`, `.streamlit/secrets.toml`, `__pycache__`, `.pytest_cache`.
- [x] **1.1.5** Write `config.py` exposing `DB_PATH`, `PDF_ROOT`, `LLM_PROVIDER`, `LLM_MODEL`, `PARSER_VERSION` constants (read from env where appropriate).
- [x] **1.1.6** Write minimal `README.md` with: one-paragraph project description, `python -m venv && pip install -r requirements.txt`, `cp .env.example .env`, `python db/init_db.py`, `streamlit run app/streamlit_app.py`.

**Acceptance:** `pip install -r requirements.txt` runs clean in a fresh venv. `python -c "import config; print(config.DB_PATH)"` works.

### [x] 1.2 DB schema (with the 7 fixes)
- [x] **1.2.1** Write `db/schema.sql` containing the original schema from `PLANNING_PROMPT.md:114-237` plus the 7 deltas:
  - [x] **1.2.1.1** Add `schemes.scheme_uid TEXT UNIQUE NOT NULL`.
  - [x] **1.2.1.2** Add `scheme_aliases` table.
  - [x] **1.2.1.3** Add `fund_snapshots.revision INT DEFAULT 1`, `superseded_at TIMESTAMP NULL`. Replace `UNIQUE(scheme_id, report_month)` with `UNIQUE(scheme_id, report_month, revision)`.
  - [x] **1.2.1.4** Add `fund_snapshots.parser_version`, `parse_errors_json`, `pdf_sha256`.
  - [x] **1.2.1.5** Drop `sector_weights_json` column. Add `sector_weights(snapshot_id, sector, weight_pct)` table.
  - [x] **1.2.1.6** Drop `monthly_returns_json`, `fy_returns_json`, `cy_returns_json`. Add `periodic_returns(snapshot_id, period_type, period_label, return_pct)` table.
  - [x] **1.2.1.7** Beef up `query_log` with: `tool_calls_json`, `model_name`, `model_version`, `latency_ms`, `tokens_in`, `tokens_out`, `refusal_reason`.
  - [x] **1.2.1.8** Add `idx_hold_security_month` index on `holdings(security_name, report_month)`.
  - [x] **1.2.1.9** Add `schema_version` table seeded with version 1.
- [x] **1.2.2** Write `db/init_db.py`: idempotent — drops & recreates the DB if `--force`, else creates tables only if absent. Reads `db/schema.sql` and executes.
- [x] **1.2.3** Write `db/seed_schemes.py` (or fold into `init_db.py`): reads `schemes_master.csv` and inserts into `schemes` table. Generates `scheme_uid` as a deterministic slug (e.g., `slugify(amc + scheme_name)`).
- [x] **1.2.4** Add `db/migrations/` folder with `001_initial.sql` containing the same schema (for future `pgloader` migration to Postgres).

**Acceptance:** `python db/init_db.py` produces a `bajaj_mf.db` file with all tables. `sqlite3 bajaj_mf.db ".tables"` lists: `schemes`, `scheme_aliases`, `fund_snapshots`, `sector_weights`, `periodic_returns`, `holdings`, `query_log`, `schema_version`.

### [x] 1.3 Stub parser — 5 fields from one PDF
- [x] **1.3.1** Define `Snapshot` dataclass in `ingest/models.py` with all the columns of `fund_snapshots`. Default to `None` for everything; populate as parsers run.
- [x] **1.3.2** Define `ParseError` dataclass with `section`, `error`, `traceback`.
- [x] **1.3.3** Write `ingest/parse_finalyca.py` with a single function `parse_pdf_minimal(path) -> Snapshot` that extracts ONLY:
  - [x] **1.3.3.1** `scheme_name` (from header — use PyMuPDF text extraction)
  - [x] **1.3.3.2** `as_of_date` (from header — parse "04 May 2026" format)
  - [x] **1.3.3.3** `expense_ratio` (from header block)
  - [x] **1.3.3.4** `return_1y` (from Trailing Returns table — use pdfplumber)
  - [x] **1.3.3.5** `fund_aum_cr` (from header block)
- [x] **1.3.4** Write `ingest/ingest_one.py`: CLI that takes a PDF path, calls `parse_pdf_minimal`, looks up `scheme_id` by name, inserts into `fund_snapshots` (with `report_month='2026-05'`, `parser_version='0.1-spine'`).

**Acceptance:** `python ingest/ingest_one.py "/Users/aaravchadha/Documents/Bajaj_RAG_mutual_funds/Canara Robeco Multi Cap Fund.pdf"` inserts one row into `fund_snapshots`. `sqlite3 bajaj_mf.db "SELECT scheme_id, expense_ratio FROM fund_snapshots"` returns one row with the right number (verify against the PDF by eye).

### [x] 1.4 LLM client wrapper (provider-agnostic interface, Groq-only backend for pilot)
- [x] **1.4.1** Write `retrieval/llm_client.py` exposing `LLMClient` class with method `chat(messages, tools=None) -> {"content": str, "tool_calls": list, "tokens_in": int, "tokens_out": int, "latency_ms": int, "model": str}`.
- [x] **1.4.2** Implement `_GroqClient`: uses `groq` Python SDK, model from config, returns the standard dict. **Pilot ships with this only.**
- [x] **1.4.3** Tool-call shape normalization: produce `{"name": str, "arguments": dict}` from Groq's response so downstream code is provider-agnostic. This is what makes future provider swaps cheap (~30 min).
- [x] **1.4.4** `LLMClient.__init__` reads `LLM_PROVIDER` env var (defaults to `groq`) and picks the right backend. Fail loud if missing key.
- [x] **1.4.5** Local LLMs (Ollama etc.) are **out of scope** for the pilot — 8GB M2 RAM is too tight, and 3B-class quality on tool-use is meaningfully worse. If Groq's free tier ever fails, the contingency is another *remote free* provider (Gemini 2.0 Flash, Cerebras) — those slot into `LLMClient` with ~30 min of work.

**Acceptance:** Quick smoke: `python -c "from retrieval.llm_client import LLMClient; c = LLMClient(); print(c.chat([{'role':'user','content':'say hi'}]))"` returns text from Groq.

### [x] 1.5 Hardcoded SQL chatbot (the spine)
- [x] **1.5.1** Write `retrieval/db_query.py` with `query_db(sql, params=()) -> list[dict]`. Uses read-only SQLite connection (`sqlite3.connect("file:bajaj_mf.db?mode=ro", uri=True)`). Refuses any SQL containing `INSERT|UPDATE|DELETE|DROP|ALTER|CREATE`.
- [x] **1.5.2** Write `app/chatbot.py` exposing `ask(question: str) -> str`. Day-1 implementation is naive: a `dict` mapping a few hardcoded question patterns to SQL. ONE entry: `"expense ratio of <scheme>"` → `SELECT expense_ratio FROM fund_snapshots WHERE scheme_id IN (SELECT scheme_id FROM schemes WHERE scheme_name LIKE ? LIMIT 1)`.
- [x] **1.5.3** `ask()` uses LLMClient ONLY to format the final answer ("The expense ratio of X as on Y is Z%"). It does NOT yet do tool-use — that's Phase 5.
- [x] **1.5.4** `ask()` writes a row to `query_log` with the question, SQL, final answer, and model name.
- [x] **1.5.5** Citation format: every answer ends with `\n\nSource: <scheme_name>, as on <as_of_date>`.

**Acceptance:** `python -c "from app.chatbot import ask; print(ask('What is the expense ratio of Canara Robeco Multi Cap?'))"` prints the right number with citation. `sqlite3 bajaj_mf.db "SELECT * FROM query_log"` shows the logged row.

### [x] 1.6 Normalized-table population in ingest
- [x] **1.6.1** Helper `db_writer.insert_snapshot_full(conn, snap, scheme_id) -> snapshot_id` writes fund_snapshots + sector_weights + periodic_returns + holdings in a single transaction.
- [x] **1.6.2** Holdings re-insertion is delete-then-insert per (scheme_id, report_month), wrapped in transaction so failure rolls back.
- [x] **1.6.3** `ingest_one.py` refactored to use the new helper.
- [x] **1.6.4** Test fixture refactored to populate normalized tables for Phase 5 readiness.
- [x] **1.6.5** New tests verify all 4 tables get populated and integrity holds on re-ingest.

**Acceptance:** sector_weights, periodic_returns, and holdings tables have rows after ingest. `SELECT scheme_id FROM holdings WHERE security_name LIKE '%HDFC Bank%'` returns at least one row.

---

## [x] Phase 2 — Eval-driven spec [Day 2]

**Goal:** Write the test cases the chatbot must pass BEFORE building the chatbot. The eval is the spec. Without this, "done" has no definition.

**Exit criterion:** `tests/golden_questions.json` contains ~35-40 Q+A pairs spanning every category below, including 3 refusal cases. Hand-verifying answers takes ~3-4 hours — this is the largest single time sink in Day 2; treat it as the spec, not paperwork.

### [x] 2.1 Question taxonomy
- [x] **2.1.1** Survey realistic RM questions. Sit with a copy of one PDF. Imagine the questions an RM serving a HNI client would ask. Write each one down without yet thinking about the data shape.
- [x] **2.1.2** Bucket the questions into categories. **Operating mode: bot is a research assistant that helps the RM compare, extrapolate, shortlist, and form a view. The bot is NOT compliance-cautious about giving suggestions or recommendations — it answers them with the supporting numbers and a standard "verify against your own research" footer. Verification is the RM's responsibility, not the bot's. Refusals are reserved for cases where the bot has no data or the question is genuinely outside the data's scope.**
  - Single-fund single-field ("What's the Sharpe of X?")
  - Single-fund multi-field ("Give me a snapshot of X")
  - **Cross-fund ranking ("Top 5 multi-cap funds by 3Y Sharpe") — heavy weight, ≥5 questions**
  - **Side-by-side comparison ("Compare X, Y, Z on returns, expense, Sharpe") — heavy weight, ≥4 questions**
  - **Shortlist suggestion ("Suggest 3 small-cap funds with strong downside protection") — bot returns 3-5 candidates with supporting numbers; ≥4 questions. May ask one clarifying question if the criterion is ambiguous, then proceed.**
  - **Recommendation / buy-sell-style ("Is X a buy?", "Should I recommend X over Y?") — bot answers with a data-driven view (which fund looks stronger on which metrics) + the standard verification footer; ≥3 questions. Does NOT refuse.**
  - **Conditional / client-profile advice ("For a risk-averse client with 5-year horizon, which large-cap funds suit?") — bot answers conditionally on the stated profile, names candidates with supporting numbers + footer; ≥3 questions. Does NOT refuse.**
  - **Extrapolation ("Expected return on X?", "Will X outperform next year?") — bot extrapolates from historical patterns with the supporting number, e.g. "Based on its 3Y CAGR of 15%, if the pattern continued, ~15%." + footer; ≥3 questions.**
  - Cross-fund filter ("Funds with expense ratio < 1%")
  - Risk-profile descriptions ("Funds with the lowest down-capture in their category")
  - Holdings lookup ("Which funds hold HDFC Bank?")
  - Sector tilt ("Funds with >25% in Financial Services")
  - Fund manager ("Who manages X? What's their experience?")
  - Refusal: scheme not in master list (`refusal_reason='unknown_scheme'`)
  - Refusal: data not yet loaded for the asked month (`refusal_reason='no_data'`)
  - Refusal: genuinely out-of-scope — tax law, individual stock-level analysis (vs the funds' holdings), macro/economy forecasts, anything not derivable from the research PDFs (`refusal_reason='out_of_scope'`)

### [x] 2.2 Author golden_questions.json
- [x] **2.2.1** Format: `[{"id": "Q01", "category": "single-field", "question": "...", "expected_answer_contains": ["X.XX%", "Canara Robeco", "as on", "verify against your own"], "expected_sql_contains": "fund_snapshots", "must_refuse": false}, ...]`. Every non-refusal answer is expected to contain the verification-footer substring (`"verify against your own"`) — the bot appends it universally to catch parser/SQL/data errors that the RM might otherwise trust.
- [x] **2.2.2** Hit each category's minimum from 2.1.2:
  - Heavy categories: cross-fund ranking ≥5, comparison ≥4, shortlist ≥4, recommendation ≥3, conditional ≥3, extrapolation ≥3 (subtotal: ~22)
  - Light categories: 2 questions each for single-field, single-fund-multi, cross-fund-filter, risk-profile, holdings, sector-tilt, fund-manager (subtotal: ~14)
  - Total non-refusal: ~36
- [x] **2.2.3** Write 3 refusal questions (one per refusal category) with `"must_refuse": true` and `"expected_refusal_reason": "unknown_scheme" | "no_data" | "out_of_scope"`. Refusals do NOT include the verification footer (a refusal isn't research output). Total refusals: 3. **Grand total: ~39 questions.**
- [x] **2.2.4** Hand-verify expected answers against the 3 sample PDFs (look up the actual numbers; don't guess). For every non-refusal question, include the literal substring `"verify against your own"` in `expected_answer_contains` as a regression check on the universal-footer behavior.

### [x] 2.3 Eval harness
- [x] **2.3.1** Write `tests/test_chatbot.py` with one parametrized test per golden question.
- [x] **2.3.2** Test passes if (a) `must_refuse` is false AND `expected_answer_contains` substrings ALL appear in answer, OR (b) `must_refuse` is true AND `refusal_reason` matches.
- [x] **2.3.3** Output a results report: `<n> passed, <n> failed, <n> with wrong SQL`. Save to `tests/last_eval_report.json`.

**Acceptance:** `pytest tests/test_chatbot.py` runs (will mostly fail today — that's fine, the spec exists).

---

## [x] Phase 3 — Parser widening [Day 3-5]

**Goal:** Extend `parse_finalyca.py` from 5 fields to all sections, against 3 sample PDFs. One function per section, partial-snapshot pattern. Three sample golden tests must pass.

**Exit criterion:** `pytest tests/test_parser.py` passes for all 3 samples; all invariants hold; `parse_errors_json` is empty for the 3 samples.

### [x] 3.1 Establish per-section structure
- [x] **3.1.1** Refactor `parse_finalyca.py` to the dispatch pattern: `SECTION_PARSERS = [parse_header, parse_returns, parse_risk_metrics, ...]`. Each function takes `(doc, pl, snap, errors)`.
- [x] **3.1.2** Wrap every section call in `try/except` — exceptions append to `errors`, never abort the whole parse. Exception: `parse_header_required` aborts if scheme_name or as_of_date is missing (no PK = no insert).
- [x] **3.1.3** Define `EXPECTED_SECTIONS_BY_FUND_TYPE` map. Equity funds expect equity sectors; debt funds don't. Use this to distinguish "section missing because fund type doesn't have it" from "section missing because parser broke."

### [x] 3.2 Section parsers — one subtask each
- [x] **3.2.1** `parse_header` — Benchmark, Inception Date, Min Investment, Expense Ratio, Exit Load, Fund AUM, Age, As-Of date, Overview paragraph.
- [x] **3.2.2** `parse_fund_managers` → `fund_managers_json`.
- [x] **3.2.3** `parse_trailing_returns` — fund + benchmark, all 9 periods.
- [x] **3.2.4** `parse_risk_metrics` — 1Y and 3Y blocks, all 10 metrics each.
- [x] **3.2.5** `parse_sector_weights` → rows in `sector_weights` table (NOT a JSON column, per schema fix 1.2.1.5).
- [x] **3.2.6** `parse_top_holdings` — top 10 (separate from full holdings table).
- [x] **3.2.7** `parse_portfolio_characteristics` — Total Securities, Avg/Median Mkt Cap, P/E, P/B, Div Yield, Modified Duration.
- [x] **3.2.8** `parse_composition` → `composition_json`.
- [x] **3.2.9** `parse_drawdown` — pct, duration, peak/valley/recovery dates.
- [x] **3.2.10** `parse_risk_rating` → `risk_rating_json`.
- [x] **3.2.11** `parse_market_cap_composition` — large/mid/small.
- [x] **3.2.12** `parse_investment_style` → `investment_style_json`.
- [x] **3.2.13** `parse_periodic_returns` — monthly + FY + CY → rows in `periodic_returns` table.
- [x] **3.2.14** `parse_holdings_full` — full multi-page holdings table → rows in `holdings` table. Concatenate across pages BEFORE parsing.
- [x] **3.2.15** Cross-cutting: NA → NULL, "0E-9" → 0.0, date normalization to ISO.

### [x] 3.3 Invariants module
- [x] **3.3.1** Write `ingest/invariants.py` with named check functions:
  - `returns_in_range(snap)` — all `return_*` between -100 and 1000
  - `composition_sums_to_100(snap)` — within ±1.0
  - `sector_weights_sum_close(snap)` — within ±5.0 (rounding tolerance)
  - `mkt_cap_composition_sums_to_100(snap)` for equity funds
  - `holdings_min_count(snap)` — at least 5 holdings
  - `expense_ratio_sane(snap)` — < 5.0
  - `inception_before_as_of(snap)`
- [x] **3.3.2** Run all invariants after parsing; failures append to `parse_errors_json` but do NOT block insert.

### [x] 3.4 Golden samples
- [x] **3.4.1** Hand-extract ~20 critical fields per sample PDF into `tests/golden/canara_robeco_multi_cap.json`, `tests/golden/absl_arbitrage.json`, `tests/golden/dsp_multi_asset.json`. ~2 hours one-time work.
- [x] **3.4.2** Write `tests/test_parser.py`: parse each sample PDF, assert each golden field matches.

### [x] 3.5 Diff regression baseline
- [x] **3.5.1** After parsers are stable, snapshot all parsed outputs to `tests/snapshots/` as JSON. Future runs `deepdiff` against this — any unexpected delta gets flagged.

**Acceptance:** all 3 sample PDFs parse with empty `parse_errors_json` and all golden fields match.

---

## [ ] Phase 4 — Bulk ingest & debt discovery [Day 6]

**Goal:** All 90 May 2026 PDFs ingested. ~33 missing debt schemes located.

**Exit criterion:** `SELECT COUNT(*) FROM fund_snapshots WHERE report_month='2026-05'` returns 90. Source URL/circular for the debt funds identified.

### [x] 4.1 Bulk download
- [x] **4.1.1** Write `ingest/download_pdfs.py`: reads `schemes_master.csv`, downloads each URL to `data/pdfs/2026-05/<scheme>.pdf` (URL-decode for filename). Skip if file exists. Log 4xx/5xx to a report.
- [x] **4.1.2** Compute SHA256 for each downloaded PDF; store alongside.

### [x] 4.2 Bulk parse + ingest
- [x] **4.2.1** Write `ingest/ingest_month.py`: CLI takes `--month 2026-05`. For each PDF in that folder: parse, validate invariants, insert.
- [x] **4.2.2** On insert conflict (existing row, same `pdf_sha256`): skip with log. On insert conflict (existing row, different `pdf_sha256`): increment `revision`, mark prior `superseded_at = now`.
- [x] **4.2.3** Print summary: parsed N, errors in M, schemes with non-empty `parse_errors_json`. Eyeball the flagged schemes manually.

### [ ] 4.3 Debt-fund circular discovery
- [x] **4.3.1** Search the original email thread (`Fwd_ (R)-19 _ "Research Recommended List of MF Schemes" for May Month.eml`) for references to a separate debt circular.
- [ ] **4.3.2** If not found in email: ask Bajaj research team directly. Goal is to get the URL pattern for the debt scheme PDFs (likely a similar `/Recommended/<name>.pdf` path).
- [ ] **4.3.3** Document findings in `PLANNING.md` under a "Phase 4.3 outcome" appendix. **Do NOT attempt to parse debt PDFs yet** — that's a phase-2 build with its own schema (credit ratings, YTM).

**Acceptance:** 90 rows in `fund_snapshots` for `report_month='2026-05'`. Triaged list of any schemes with parse errors. Debt circular source documented.

**Status (2026-05-11):** 4.1 + 4.2 complete — `data/ingest_report_2026-05.json` shows 90 parsed, 90 inserted, 0 failed, 0 parse_errors (after the `parse_drawdown` page-2-vs-page-3 fix, see STATUS.md). 4.3.1 complete — email-search found ~180 URLs in the .eml (90 in CSV, ~33 likely debt). 4.3.2/4.3.3 still pending user delivery of the debt-fund CSV.

---

## [x] Phase 5 — Tool-use chatbot [Day 7-8]

**Goal:** Replace the Day-1 hardcoded SQL with a real LLM tool-use loop. Bot answers ≥80% of golden questions on first run.

**Exit criterion:** `pytest tests/test_chatbot.py` shows ≥80% pass rate.

### [x] 5.1 Tool definitions
- [x] **5.1.1** In `retrieval/tools.py`, define 4 tools:
  - [x] **5.1.1.1** `query_db(sql: str)` — executes read-only SQL via `db_query.query_db`. Returns up to 100 rows as JSON.
  - [x] **5.1.1.2** `lookup_scheme(name_substring: str)` — returns matching schemes from the `schemes` table. The model uses this to canonicalize fuzzy names before SQL.
  - [x] **5.1.1.3** `get_schema()` — returns the DDL of all tables, so the model knows the column names. Cache the result; load once at startup.
  - [x] **5.1.1.4** `compare_schemes(scheme_names: list[str], metrics: list[str])` — purpose-built side-by-side comparison. Internally fetches the latest snapshot for each scheme, returns a clean tabular dict. More reliable than the model hand-rolling the SQL each time. Default metrics if not specified: `['return_3y', 'sharpe_3y', 'std_dev_3y', 'expense_ratio', 'fund_aum_cr']`.
- [x] **5.1.2** Tools are returned in a provider-agnostic format that `LLMClient` translates to Groq's tool-use schema.

### [x] 5.2 System prompt with operating-mode rules
- [x] **5.2.1** In `app/prompts.py`, write `SYSTEM_PROMPT` covering:
  - [x] **5.2.1.1** Bot identity & operating mode: "You are an internal Bajaj Capital research assistant for Relationship Managers. Help the RM compare, shortlist, extrapolate, and form a view on the funds in our research. You may answer suggestion/recommendation/extrapolation questions directly with the supporting numbers — do not be evasive, do not refuse them. The RM is responsible for verifying your output against their own research before advising clients; that responsibility is communicated via a standard footer on every output that contains a suggestion, recommendation, or extrapolation."
  - [x] **5.2.1.1.a** **Developer-anonymity rule.** The system prompt must NOT include any name, email, GitHub handle, or other identifier of the developer/maintainer. If the user asks "who built you?", "who made this?", "who's the developer?", or anything in that family, answer: *"I'm an internal Bajaj Capital research tool — I don't have details on who built me."* Treat this as a non-refusal; just a factually empty answer. The bot legitimately doesn't know.
  - [x] **5.2.1.2** Workflow: "First call `lookup_scheme` to canonicalize names. For multi-fund comparisons, prefer `compare_schemes`. For everything else, call `query_db` using `get_schema`."
  - [x] **5.2.1.3** Shortlist / suggestion rules:
    - If asked for "best" without a metric → ask one short clarifying question OR pick a sensible default (typically 3Y Sharpe for risk-adjusted return) and disclose the default ("I'm ranking by 3Y Sharpe; tell me if you'd like a different metric").
    - When suggesting a shortlist, return 3-5 candidates with their supporting numbers (Sharpe, expense ratio, trailing returns, AUM, key risk metrics relevant to the criterion). Cite each candidate's source.
  - [x] **5.2.1.4** Recommendation / conditional-advice rules:
    - Buy/sell/hold-style questions ("Is X a buy?", "Should I recommend X over Y?") → answer with the data-driven view: which fund looks stronger on which metrics, what the trade-offs are, and why. Do NOT refuse.
    - Conditional advice ("For a risk-averse client with 5-year horizon, which large-caps suit?") → reason from the stated profile to specific funds, with supporting numbers. Do NOT refuse for missing client details — work with what the RM gave you.
    - Append the **standard verification footer** (see 5.2.1.6) to every answer of this type.
  - [x] **5.2.1.5** Extrapolation rules:
    - "Expected return / will X outperform" questions → extrapolate from historical patterns: "Based on its 3Y CAGR of 15%, if the pattern continued, ~15%." Show the historical metric backing the extrapolation.
    - When useful, show variability too (std dev, range across recent periods) so the RM sees the uncertainty.
    - Append the **standard verification footer**.
  - [x] **5.2.1.6** **Universal verification footer** — every non-refusal answer ends with this exact line on a new paragraph (after the citation):
    > *"This is research output — please verify against your own analysis before advising clients."*
    No exceptions for "pure factual" answers. The footer exists because parser bugs, SQL fuzzy matches, or stale snapshots can produce wrong numbers/citations that look authoritative; the RM should be primed to verify every output. Refusals do NOT include the footer (a refusal isn't research output).
  - [x] **5.2.1.7** Refusal rules — only refuse in these cases:
    - `query_db` returns zero rows → "I don't have data for that question." `refusal_reason='no_data'`.
    - `lookup_scheme` returns nothing → "I don't have data for scheme '<name>'." `refusal_reason='unknown_scheme'`.
    - Question is genuinely outside the research data — tax law, individual stock-level analysis (vs the funds' holdings), macro/economy forecasts → "Out of scope; I only answer questions about the funds in our research." `refusal_reason='out_of_scope'`.
    - **Do NOT refuse**: buy/sell calls, recommendations, "should I", client-conditional advice, extrapolations, "best" questions. Answer them with data + footer.
  - [x] **5.2.1.8** Citation rule: every numeric answer ends with `Source: <scheme>, as on <as_of_date>`. For multi-fund answers, list each source. Citation comes BEFORE the verification footer.
  - [x] **5.2.1.9** Format rule: numeric answers as numbers, percentages with `%`, dates ISO.

### [x] 5.3 Tool-use loop
- [x] **5.3.1** In `app/chatbot.py`, replace `ask()` with the loop: send messages, if response has tool_calls execute them and append results, send again, max 6 iterations. Hard-stop with refusal if loop exceeds 6.
- [x] **5.3.2** Capture every tool call into `tool_calls_json` for the `query_log` row.
- [x] **5.3.3** On refusal path, set `refusal_reason` ∈ `{unknown_scheme, no_data, out_of_scope, loop_exceeded}`.

### [x] 5.4 Run evals iteratively
- [x] **5.4.1** Run `pytest tests/test_chatbot.py`. For each failure, look at `tool_calls_json` — was the SQL wrong? Was the scheme name fuzzy-matched wrong? Was the refusal triggered incorrectly?
- [x] **5.4.2** Fix system prompt, tool descriptions, or schema-getter as needed. Iterate.

**Acceptance:** ≥80% of golden questions pass. **Status (2026-05-11):** Curated 10-Q real-Groq eval stable at 7/10 (70%). Full 40-Q eval deferred to Phase 8 (~330K tokens exceeds 200K daily cap on Groq free tier). Three remaining failures on curated subset: Q05 (ambiguous wording — prompt issue), Q19/Q32 (alternating tool-call malformation flake in `_GroqClient` recovery path). All three tracked as Phase 8 items.

---

## [x] Phase 6 — Streamlit UI [Day 9]

**Goal:** A 5-RM-friendly chat UI with citations, feedback, and password gate.

**Exit criterion:** `streamlit run app/streamlit_app.py` opens a working chat interface on `localhost:8501` that authenticates and handles the smoke-test question.

### [x] 6.1 Auth gate — this is the rival defense, treat seriously
- [x] **6.1.1** Create `app/auth_config.yaml` (gitignored) with 5 RM accounts (`username`, hashed `password` via bcrypt, `name`). Use `streamlit-authenticator`'s hasher.
- [x] **6.1.2** Wire `streamlit-authenticator` into `streamlit_app.py`. **No content renders until login** — not even the "Bajaj Capital Research Bot" title. A scraper hitting the URL should see only a login form.
- [x] **6.1.3** Disable account self-registration. Only admin (you) can add accounts via the YAML file.
- [x] **6.1.4** Username convention: `firstname.lastname` mapped to a Bajaj employee ID in a separate column for auditability — query_log records the username, audit can map back to employee.
- [x] **6.1.5** Session timeout: 8 hours (Streamlit default). Re-auth on next visit.
- [x] **6.1.6** Phase-2 hardening (NOT pilot, but document the sequencing): IP allowlist for Bajaj corporate ranges → rate limit per username → account lockout after 5 failed attempts → migrate to Bajaj SSO before scaling past 50 users.

### [x] 6.2 Chat UI
- [x] **6.2.1** Use `st.chat_message` and `st.chat_input`. Persist conversation in `st.session_state`.
- [x] **6.2.2** On each user message: call `chatbot.ask`, render response, render `Source: ...` line as a smaller-font caption.
- [x] **6.2.3** Render thumbs-up/thumbs-down buttons under each bot message. Click writes `user_feedback` to that row's `query_log` entry (lookup by `query_id` returned from `ask()`).
- [x] **6.2.4** Optional comment field after thumbs-down.

### [x] 6.3 Operational UI niceties (cheap, high-value)
- [x] **6.3.1** Sidebar: shows current month's data status ("Data loaded: May 2026, 90 schemes").
- [x] **6.3.2** Sidebar: link to "Report a problem" — writes a feedback row to a `feedback` table or just opens a `mailto:`.
- [x] **6.3.3** Spinner during LLM call.

**Acceptance:** Manual smoke test — log in as a test RM, ask 3 questions (one fact, one cross-fund, one refusal), thumbs-down one of them, verify all four actions land in `query_log`.

---

## [ ] Phase 7 — Cloudflare Tunnel & compliance [Day 10]

**Goal:** External URL the 5 RMs can hit. Bajaj compliance signed off on Groq free-tier data policy.

**Exit criterion:** Tunnel URL accessible from a phone (not on dev network); compliance has confirmed Groq free-tier usage in writing OR the LLM has been swapped to another remote free provider (Gemini Flash / Cerebras).

### [ ] 7.1 Tunnel setup
- [ ] **7.1.1** Install `cloudflared`. Authenticate with a Cloudflare account (free).
- [ ] **7.1.2** Create a named tunnel: `cloudflared tunnel create bajaj-mf-bot`.
- [ ] **7.1.3** Configure DNS: get a `<name>.trycloudflare.com` (free, no domain needed) OR use a Cloudflare-hosted domain if available.
- [ ] **7.1.4** Run tunnel as a background service (launchd or `nohup`): `cloudflared tunnel --url http://localhost:8501 run bajaj-mf-bot`.
- [ ] **7.1.5** Verify external access from phone on cellular (not on dev WiFi).

### [ ] 7.2 Compliance check (run in parallel, not blocking 7.1) — TWO sign-offs needed
- [ ] **7.2.1** **Sign-off A — Groq data policy.** Email Bajaj compliance with: Groq's privacy policy URL + free-tier ToS URL + a one-paragraph summary of what data flows through Groq (RM questions, scheme names, public market numbers — never PII or client data). Note: data is public-derivable per cross-cutting framing, so this should be procedural. If denied or revoked: implement a `_GeminiClient` (or `_CerebrasClient`) backend in `LLMClient` (~30 min) and switch `LLM_PROVIDER` env var. Re-run Phase 5 evals. Both alternatives are remote, free, and have similar tool-use quality on 70B-class models.
- [ ] **7.2.2** **Sign-off B — operating-mode language.** Send compliance the system prompt (`app/prompts.py`), the verification footer text, and 5 representative bot outputs (one shortlist, one recommendation, one conditional advice, one extrapolation, one refusal). Ask them to confirm: (a) the verification-footer language is sufficient to put research-vs-advice responsibility on the RM, (b) the extrapolation framing is acceptable, (c) the no-client-specific-refusal stance is OK. **This stance is more permissive than typical regulated-product advisory tools — explicit sign-off is required, not implied.**
- [ ] **7.2.3** Document both sign-offs (date, approver, scope) in a `PLANNING.md` appendix section "Phase 7.2 outcome." Keep the email thread for audit.
- [ ] **7.2.4** If sign-off B is denied or modified: tighten the system prompt accordingly (more refusals, stricter footer, etc.) and re-run Phase 5 evals before launch.

### [ ] 7.3 Operational hardening
- [ ] **7.3.1** Set up structured JSON logging (Python `logging.config`) writing to `logs/app.log`. Rotate weekly via logrotate or a simple cron.
- [ ] **7.3.2** Add a `/health` route (or Streamlit equivalent — a separate small page) returning DB row counts and latest `ingested_at`.
- [ ] **7.3.3** Document the runbook in `README.md`: how to restart Streamlit, how to restart the tunnel, how to ingest next month's data.

**Acceptance:** Phone access works. Compliance status documented.

---

## [ ] Phase 8 — Eval & polish [Day 11-12]

**Goal:** All golden questions pass or each failure has a documented reason. Bot feels usable.

**Exit criterion:** 100% of golden questions have either passed or are explicitly marked `acceptable_failure: <reason>` in `golden_questions.json`.

### [ ] 8.1 Eval-driven fixes
- [ ] **8.1.1** Run full eval. For each failure, classify: prompt issue / SQL issue / parser issue / data issue / question is genuinely ambiguous.
- [ ] **8.1.2** Prompt issues → tweak `app/prompts.py`.
- [ ] **8.1.3** SQL issues → tweak tool description or schema-getter output.
- [ ] **8.1.4** Parser issues → fix `parse_finalyca.py`, re-ingest the affected schemes, re-run eval.
- [ ] **8.1.5** Data issues → fix the source of the wrong data (was the PDF parsed wrong, or is the question pointing at the wrong table?).
- [ ] **8.1.6** Genuinely ambiguous → mark `acceptable_failure` with explanation.

### [ ] 8.2 UX polish
- [ ] **8.2.1** Slow queries (>5s): add a status message ("Querying database...", "Asking the model...").
- [ ] **8.2.2** Long answers: truncate any single bot message at ~2000 chars; add "Show more" expander.
- [ ] **8.2.3** Error states: if LLM call fails, show a friendly error and suggest rephrasing.

### [ ] 8.3 Pre-launch checklist
- [ ] **8.3.1** Backup `bajaj_mf.db` daily (cron `cp` to a dated file).
- [ ] **8.3.2** Document hours-of-availability for the laptop-host setup.
- [ ] **8.3.3** Verify nothing in `.env` is committed; verify `data/pdfs/` is gitignored.

**Acceptance:** All golden Qs accounted for. UI feels polished. Backup runs.

---

## [ ] Phase 9 — Pilot onboarding [Day 13-14]

**Goal:** 5 RMs trained and using the tool. Baseline usage data for the post-pilot review.

**Exit criterion:** 5 RM accounts created and shared. ≥1 hour of shadowing per RM. Daily `query_log` review process established.

### [ ] 9.1 RM onboarding
- [ ] **9.1.1** Create 5 RM accounts in `auth_config.yaml`. Send each RM their credentials + the tunnel URL via Bajaj-internal channel.
- [ ] **9.1.2** Write a one-page user guide covering:
  - 3 example questions across categories (one factual lookup, one shortlist, one comparison)
  - **What the bot WILL do**: shortlist, compare, rank, give recommendations, extrapolate from history
  - **What the bot WILL NOT do**: refuse client-specific framing (it'll answer with what you give it), promise future returns, give tax/macro advice
  - **The verification footer**: every non-refusal answer ends with "verify against your own analysis" — this is by design, not boilerplate. The RM is the verifier; the bot is the research analyst. **Treat every output as a starting point, not a final answer.**
  - When the bot does refuse ("I don't have data" / "Out of scope"): that's the right behavior — don't try to coerce an answer
  - How to give feedback (thumbs-up/down + comment)
- [ ] **9.1.3** Schedule a 30-minute training call with each RM (or one group call). Show 3 questions live, including one where the bot's number is verified-wrong-on-purpose to demonstrate the verification footer's value.

### [ ] 9.2 Shadowing
- [ ] **9.2.1** Sit with 2 of the 5 RMs (the most engaged ones) for an hour each. Watch them ask real questions. Note every confusion / wrong answer. Don't intervene unless they get stuck for >60 seconds.
- [ ] **9.2.2** End-of-day: review `query_log` for thumbs-down, refusals, and SQL errors. Categorize.

### [ ] 9.3 Daily ops loop
- [ ] **9.3.1** Each morning of pilot week 1: review prior-day `query_log`. Triage failures. Fix the highest-impact ones same-day.
- [ ] **9.3.2** Each evening: glance at logs/app.log for unhandled exceptions.

**Acceptance:** 5 active RMs. Daily ops cadence established.

---

## Cross-cutting risks (review every Friday)

- **R1a — Compliance: Groq data policy.** Groq free-tier ToS might preclude internal Bajaj data (lower-stakes than typical because data is public-derivable, but still wants procedural sign-off). Mitigation: implement `_GeminiClient` or `_CerebrasClient` in `LLMClient` and flip the env var — ~30 min, because the interface is provider-agnostic. **Owner: Day 10 must-resolve (sign-off A).**
- **R1b — Compliance: operating-mode language.** The bot's permissive stance (gives recommendations, extrapolates, doesn't refuse client-conditional advice) is more aggressive than typical regulated tools. Bajaj compliance must explicitly approve the system prompt and verification-footer language. Mitigation if denied: tighten prompt (more refusals, stricter footer), re-run evals — ~1 day. **Owner: Day 10 must-resolve (sign-off B).**
- **R2 — Mid-month republished PDF.** Schema (`revision` + `superseded_at`) handles it; ingest script must check `pdf_sha256` before insert. **Owner: Phase 4.2.2.**
- **R3 — Scheme rename / AMC merger mid-pilot.** `scheme_aliases` table handles it; document the runbook. **Owner: deferred to phase-2 unless triggered during pilot.**
- **R4 — PDF download breakage.** Add monthly URL health check (HEAD all 90 → 123 URLs). **Owner: Phase 7.3 nice-to-have.**
- **R5 — Local Streamlit downtime.** Laptop sleeps → RMs see 502. Mitigation: prevent sleep with `caffeinate -d` while plugged in; document hours of availability. Push for Bajaj VM. **Owner: Phase 7.3.**
- **R6 — Groq rate limit / outage.** 14,400 req/day free tier covers 5 RMs (~100 queries/day) with 100x headroom — unlikely to hit limits in pilot. Outages are possible. Mitigation: same as R1a — flip to Gemini Flash or Cerebras via `LLMClient`. **Owner: contingency.**

---

## Open items (decide as we go)

- [ ] Where does the code live? GitHub free private repo (recommended) or Bajaj-internal Git?
- [ ] Backup destination for `bajaj_mf.db` — local-only is fine for the pilot, but think about offsite (e.g., encrypted to iCloud Drive).
- [ ] Whether to expose a "raw SQL" mode to power-user RMs in phase 2 (probably yes, but not in pilot).

---

## Glossary

- **RM** — Relationship Manager. Bajaj Capital employee who advises end-clients on MF investments.
- **Finalyca** — third-party fund analytics SaaS that generates the uniform PDF template.
- **Snapshot** — one fund's parsed numeric/categorical data for one month. One row in `fund_snapshots`.
- **Refusal** — bot intentionally declines to answer. Only 3 refusal reasons: `no_data`, `unknown_scheme`, `out_of_scope`. The bot does NOT refuse for "is X a buy", client-conditional questions, or extrapolation — those are answered with data + verification footer.
- **Verification footer** — the closing line on every non-refusal answer: *"This is research output — please verify against your own analysis before advising clients."* Universal, no exceptions. Communicates that verification is the RM's job.
- **Vertical slice** — minimal end-to-end path through every layer (parse → DB → SQL → LLM → UI). Phase 1's deliverable.
