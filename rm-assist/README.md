# RM Assist — application code

The application behind the project described in the [root README](../README.md): a tool-use LLM chatbot over a structured store of parsed mutual-fund research reports, served through an auth-gated Streamlit UI.

## Layout

| Directory | Contents |
|---|---|
| `db/` | Schema (8 tables), idempotent init/seed, migrations |
| `ingest/` | PDF download, the 14-section Finalyca parser, invariant checks, bulk ingest |
| `retrieval/` | Provider-agnostic `LLMClient` (Groq / Gemini / Mock), the 6 tool implementations, market data, FAQ matching |
| `app/` | Chatbot tool-use loop, system prompt, Streamlit UI, auth |
| `scripts/` | Auth account creation, eval runner, parser snapshot tool |
| `tests/` | 88 unit tests + golden fixtures + regression baselines (PDF-dependent tests skip if samples are absent) |

## Setup

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and set `GROQ_API_KEY` (or `GEMINI_API_KEY` with `LLM_PROVIDER=gemini`)
4. `python -m db.init_db --force`
5. Optional, needs source PDFs in `data/pdfs/<YYYY-MM>/`: `python -m ingest.ingest_month --month 2026-05`
6. `python -m scripts.create_auth_account` to create a login, then `streamlit run app/streamlit_app.py`

CLI smoke test without the UI:

```bash
python -m app.chatbot "What is the expense ratio of Canara Robeco Multi Cap Fund?"
```

## Tests

```bash
python -m pytest tests/ -q
```

The 40 token-heavy live-LLM golden-question evals are excluded from pytest and run via `python -m scripts.run_eval_sample`.
