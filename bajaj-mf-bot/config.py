"""Central configuration for the Bajaj MF research bot.

Paths are derived from this file's location so the package works regardless of
the caller's working directory. Environment variables are loaded from `.env`
via python-dotenv. Secrets (e.g., GROQ_API_KEY) are read lazily so importing
this module never fails on a missing key.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

DB_PATH = BASE_DIR / "db" / "bajaj_mf.db"
PDF_ROOT = BASE_DIR / "data" / "pdfs"
SCHEMES_CSV = BASE_DIR.parent / "schemes_master.csv"

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

PARSER_VERSION = "0.1-spine"


def get_groq_api_key() -> str:
    """Return GROQ_API_KEY, raising RuntimeError if it's required but missing.

    Lazy access pattern: importing `config` never fails on a missing key, but
    any code path that actually needs the key must call this function and will
    blow up loudly if the env var is empty/unset.
    """
    key = os.getenv("GROQ_API_KEY", "")
    if LLM_PROVIDER == "groq" and not key:
        raise RuntimeError("GROQ_API_KEY not set")
    return key
