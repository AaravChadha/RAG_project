"""Tests for ``app.prompts`` — the system prompt and verification footer.

The footer test is byte-exact: PLANNING.md 5.2.1.6 specifies the string
character-for-character (em-dash U+2014, not a hyphen). The other two
tests are about substantive completeness — the prompt must be long enough
to actually cover the operating-mode rules, and it must mention every
key concept from PLANNING.md 5.2.1.1 through 5.2.1.9.
"""

from __future__ import annotations

from app.prompts import SYSTEM_PROMPT, VERIFICATION_FOOTER


def test_system_prompt_present() -> None:
    """SYSTEM_PROMPT is a substantive non-empty string."""
    assert isinstance(SYSTEM_PROMPT, str)
    # >1000 chars is the planning-doc threshold for "substantive". The actual
    # prompt is comfortably above this; the bound is a regression guard
    # against accidental truncation.
    assert len(SYSTEM_PROMPT) > 1000, (
        f"SYSTEM_PROMPT too short ({len(SYSTEM_PROMPT)} chars); "
        "expected >1000 to cover operating-mode rules."
    )


def test_verification_footer_exact() -> None:
    """The footer must match PLANNING.md 5.2.1.6 byte-for-byte (em-dash)."""
    expected = (
        "This is research output — please verify against your own analysis "
        "before advising clients."
    )
    assert VERIFICATION_FOOTER == expected


def test_system_prompt_mentions_key_rules() -> None:
    """Every required operating-mode keyword appears in SYSTEM_PROMPT."""
    required_substrings = [
        "developer",
        "internal Bajaj Capital research tool",
        "verify against your own",
        "Source:",
        "lookup_scheme",
        "compare_schemes",
        "query_db",
        "no_data",
        "unknown_scheme",
        "out_of_scope",
    ]
    missing = [s for s in required_substrings if s not in SYSTEM_PROMPT]
    assert not missing, f"SYSTEM_PROMPT missing required substrings: {missing}"
