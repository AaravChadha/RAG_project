"""Auth helpers for the Streamlit UI (Phase 6.1).

Three surfaces:

* ``load_auth_config()`` — read ``app/auth_config.yaml`` into a dict.
  Raises a loud, actionable RuntimeError if the file is missing, so a
  fresh clone doesn't silently start without auth.
* ``make_authenticator()`` — construct the streamlit-authenticator 0.4.x
  ``Authenticate`` instance from that config. ``streamlit_app.py`` calls
  this once and then uses the returned object for login/logout.
* ``hash_password(plain)`` — wraps ``streamlit_authenticator.Hasher.hash``
  so the CLI in ``scripts/create_auth_account.py`` and any future
  account-management code go through one entry point.

The YAML file with real creds is gitignored; the ``.example`` template
is committed (see ``app/auth_config.yaml.example``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import yaml
from streamlit_authenticator import Authenticate, Hasher

logger = logging.getLogger(__name__)


# Resolved relative to this file so the helper works regardless of the
# caller's cwd (Streamlit, pytest, the CLI script, etc.).
_AUTH_CONFIG_PATH: Path = Path(__file__).resolve().parent / "auth_config.yaml"
_AUTH_CONFIG_EXAMPLE: Path = (
    Path(__file__).resolve().parent / "auth_config.yaml.example"
)


def load_auth_config(path: Path = _AUTH_CONFIG_PATH) -> Dict[str, Any]:
    """Read the auth YAML into a dict.

    Args:
        path: Override the default ``app/auth_config.yaml`` location
            (mainly for tests).

    Raises:
        RuntimeError: If the file is missing. The message points the
            operator at the ``.example`` template + the
            ``create_auth_account`` CLI.
    """
    if not path.exists():
        raise RuntimeError(
            f"Auth config not found at {path}. "
            f"Copy {_AUTH_CONFIG_EXAMPLE.name} and populate it with real "
            "accounts via `python -m scripts.create_auth_account ...`."
        )
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    logger.debug("Loaded auth config from %s", path)
    return config


def make_authenticator(path: Path = _AUTH_CONFIG_PATH) -> Authenticate:
    """Construct a streamlit-authenticator ``Authenticate`` from the YAML.

    The 0.4.x API takes the credentials dict + cookie params as separate
    positional/keyword args (not the whole config dict). We pass
    ``auto_hash=False`` because the YAML already stores bcrypt hashes —
    setting True would attempt to re-hash on every page load.
    """
    config = load_auth_config(path)
    cookie = config.get("cookie", {})
    return Authenticate(
        credentials=config["credentials"],
        cookie_name=cookie.get("name", "bajaj_mf_bot_session"),
        cookie_key=cookie.get("key", ""),
        cookie_expiry_days=float(cookie.get("expiry_days", 1)),
        auto_hash=False,
    )


def hash_password(plain: str) -> str:
    """Bcrypt-hash a plaintext password via streamlit-authenticator.

    Used by ``scripts/create_auth_account.py``. We expose this thin
    wrapper rather than letting callers import ``Hasher`` directly so
    the bcrypt dependency stays a single-file concern.
    """
    if not plain:
        raise ValueError("Password must not be empty.")
    return Hasher.hash(plain)
