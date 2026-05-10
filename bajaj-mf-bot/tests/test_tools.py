"""Tests for ``retrieval.tools`` — the four LLM tools and the dispatcher.

Every test goes through ``execute_tool`` (the public surface) so we're
exercising the same code path the chatbot will. We deliberately don't
import the underscore-prefixed helpers — that would couple tests to the
internal layout. The ``seeded_db`` fixture is reused from ``conftest.py``
and gives us one known scheme (Canara Robeco) plus its snapshot, sector
weights, periodic returns, and holdings.
"""

from __future__ import annotations

import json

import pytest

from retrieval.tools import TOOLS, execute_tool


def test_query_db_tool_success(seeded_db) -> None:
    """A simple SELECT round-trips through execute_tool('query_db')."""
    raw = execute_tool(
        "query_db",
        {"sql": "SELECT scheme_name FROM schemes LIMIT 1"},
    )
    payload = json.loads(raw)

    # JSON shape: list of rows. Each row is a dict keyed by column name.
    assert isinstance(payload, list)
    assert payload, "expected at least one scheme row from seeded_db"
    assert "Canara Robeco" in payload[0]["scheme_name"]


def test_query_db_tool_refuses_ddl(seeded_db) -> None:
    """The read-only refusal must surface as an error dict, not an exception."""
    raw = execute_tool("query_db", {"sql": "DROP TABLE schemes"})
    payload = json.loads(raw)

    assert isinstance(payload, dict)
    assert "error" in payload
    # Match against the message too — gives us a regression check that the
    # error tag stays human-readable.
    assert "DROP" in payload.get("message", "").upper()


def test_lookup_scheme_finds_canara(seeded_db) -> None:
    """The fuzzy scheme lookup finds the seeded Canara row."""
    raw = execute_tool("lookup_scheme", {"name_substring": "canara"})
    payload = json.loads(raw)

    assert isinstance(payload, list)
    assert payload, "expected at least one match for 'canara'"
    first = payload[0]
    assert "Canara" in first["scheme_name"]
    # The four canonical keys are part of the contract — assert them all.
    for key in ("scheme_id", "scheme_name", "amc", "category"):
        assert key in first, f"missing key '{key}' in lookup_scheme result"


def test_lookup_scheme_no_match(seeded_db) -> None:
    """A miss produces the structured no-match envelope, not an empty list."""
    raw = execute_tool(
        "lookup_scheme",
        {"name_substring": "nonexistent-fund-xyzzy"},
    )
    payload = json.loads(raw)

    assert isinstance(payload, dict)
    assert payload.get("matches") == []
    assert "No scheme found" in payload.get("message", "")


def test_get_schema_returns_tables(seeded_db) -> None:
    """get_schema returns a JSON dict with the five tables the LLM needs."""
    raw = execute_tool("get_schema", {})
    payload = json.loads(raw)

    assert isinstance(payload, dict)
    tables = payload.get("tables", {})
    expected_tables = {
        "schemes", "fund_snapshots", "holdings",
        "sector_weights", "periodic_returns",
    }
    assert expected_tables.issubset(tables.keys()), (
        f"missing tables in schema description: "
        f"{expected_tables - set(tables.keys())}"
    )
    # Each table entry must list its columns — useful_joins and rules also
    # need to be present so the LLM has the rule-of-thumb context.
    for name in expected_tables:
        assert tables[name].get("columns"), f"no columns listed for {name}"
    assert payload.get("useful_joins"), "expected useful_joins block"
    assert payload.get("rules"), "expected rules block"


def test_compare_schemes_returns_metrics(seeded_db) -> None:
    """compare_schemes produces the {comparison: [{scheme_name, metrics}]} shape."""
    raw = execute_tool(
        "compare_schemes",
        {"scheme_names": ["Canara Robeco"]},
    )
    payload = json.loads(raw)

    assert isinstance(payload, dict)
    comparison = payload.get("comparison")
    assert isinstance(comparison, list)
    assert comparison, "expected at least one comparison entry"

    entry = comparison[0]
    assert "Canara" in entry.get("scheme_name", "")
    assert isinstance(entry.get("metrics"), dict)
    assert entry.get("as_of_date"), "expected as_of_date on comparison entry"


def test_compare_schemes_default_metrics(seeded_db) -> None:
    """Calling without `metrics` falls back to the six defaults."""
    raw = execute_tool(
        "compare_schemes",
        {"scheme_names": ["Canara Robeco"]},
    )
    payload = json.loads(raw)
    metrics = payload["comparison"][0]["metrics"]

    for expected in (
        "return_1y", "return_3y", "sharpe_3y", "std_dev_3y",
        "expense_ratio", "fund_aum_cr",
    ):
        assert expected in metrics, f"default metric missing: {expected}"


def test_tools_schema_well_formed() -> None:
    """TOOLS is a list of OpenAI-style function-tool descriptors."""
    assert isinstance(TOOLS, list)
    assert len(TOOLS) == 4

    names = set()
    for entry in TOOLS:
        assert entry.get("type") == "function"
        fn = entry.get("function")
        assert isinstance(fn, dict)
        assert fn.get("name"), "tool missing name"
        assert fn.get("description"), f"tool {fn.get('name')} missing description"
        params = fn.get("parameters")
        assert isinstance(params, dict)
        assert params.get("type") == "object"
        assert "properties" in params
        assert "required" in params
        names.add(fn["name"])

    assert names == {"query_db", "lookup_scheme", "get_schema", "compare_schemes"}
