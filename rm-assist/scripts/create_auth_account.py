"""CLI to add a new RM account to ``app/auth_config.yaml``.

Usage:

    python -m scripts.create_auth_account \\
        --username test.rm1 \\
        --name "Test RM One" \\
        --email test.rm1@bajajcapital.in \\
        --employee-id BC0001

Prompts for the password via ``getpass`` (never echoed), bcrypt-hashes
it via ``app.auth.hash_password``, then either:

* appends the entry under ``credentials.usernames`` of an existing
  ``app/auth_config.yaml``, OR
* prints a ready-to-paste YAML fragment to stdout if the file doesn't
  exist yet (in which case the operator should start from
  ``app/auth_config.yaml.example``).

Plaintext passwords are NEVER stored.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from pathlib import Path

import yaml

# Make the rm-assist package root importable regardless of cwd.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from app.auth import hash_password  # noqa: E402

logger = logging.getLogger(__name__)

_AUTH_CONFIG_PATH: Path = _PROJECT_DIR / "app" / "auth_config.yaml"


def _read_password() -> str:
    """Prompt twice via ``getpass``; ensure both match and are non-empty."""
    p1 = getpass.getpass("Password: ")
    p2 = getpass.getpass("Confirm:  ")
    if p1 != p2:
        print("Passwords did not match.", file=sys.stderr)
        sys.exit(2)
    if not p1:
        print("Password must not be empty.", file=sys.stderr)
        sys.exit(2)
    return p1


def _build_entry(
    name: str,
    email: str,
    employee_id: str,
    password_hash: str,
) -> dict:
    """Build a single ``usernames`` entry dict for the YAML."""
    return {
        "name": name,
        "password": password_hash,
        "email": email,
        "employee_id": employee_id,
    }


def _append_to_existing(
    path: Path,
    username: str,
    entry: dict,
) -> None:
    """Append (or overwrite) one user entry inside an existing YAML."""
    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    config.setdefault("credentials", {}).setdefault("usernames", {})
    if username in config["credentials"]["usernames"]:
        print(
            f"WARNING: username '{username}' already exists in {path}. "
            "Overwriting.",
            file=sys.stderr,
        )
    config["credentials"]["usernames"][username] = entry
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, sort_keys=False, default_flow_style=False)
    print(f"Appended '{username}' to {path}.")


def _print_yaml_fragment(username: str, entry: dict) -> None:
    """Print a paste-ready fragment when no auth_config.yaml exists.

    Includes a freshly generated cookie key so the operator has a
    complete file to start from after copying the rest from
    ``auth_config.yaml.example``.
    """
    fragment = {
        "credentials": {"usernames": {username: entry}},
        "cookie": {
            "name": "bajaj_mf_bot_session",
            "key": secrets.token_hex(32),
            "expiry_days": 1,
        },
        "preauthorized": {"emails": []},
    }
    print(
        "# No app/auth_config.yaml found. Save the YAML below as that file:",
        file=sys.stderr,
    )
    yaml.safe_dump(fragment, sys.stdout, sort_keys=False, default_flow_style=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or update a streamlit-authenticator account.",
    )
    parser.add_argument("--username", required=True, help="firstname.lastname")
    parser.add_argument("--name", required=True, help="Full display name")
    parser.add_argument("--email", required=True, help="Bajaj email address")
    parser.add_argument(
        "--employee-id",
        required=True,
        help="Bajaj employee ID for audit mapping.",
    )
    args = parser.parse_args(argv)

    password = _read_password()
    password_hash = hash_password(password)

    entry = _build_entry(args.name, args.email, args.employee_id, password_hash)

    if _AUTH_CONFIG_PATH.exists():
        _append_to_existing(_AUTH_CONFIG_PATH, args.username, entry)
    else:
        _print_yaml_fragment(args.username, entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
