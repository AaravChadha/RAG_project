# bajaj-mf-bot

Internal Bajaj Capital research assistant for Relationship Managers. Answers structured questions about mutual fund schemes from monthly research reports.

## Architecture

SQLite-backed structured store + LLM tool-use over a read-only query interface, served through a Streamlit frontend.

## Setup

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and set `GROQ_API_KEY`
4. `python -m db.init_db`
5. `streamlit run app/streamlit_app.py`

## Status

Pilot phase — see PLANNING.md for the build plan.
