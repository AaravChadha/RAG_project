"""Phase 3.2.11–3.2.14 section parsers — factored out of `parse_finalyca.py`
to keep the main parser file under the 1700-line guidance.

Each parser has the same signature as the other section parsers in the
dispatch list:

    parser(doc: fitz.Document, pl: pdfplumber.PDF, snap: Snapshot) -> None

and mutates the snapshot in place. Failures propagate to the caller, which
catches and demotes them to a per-section `ParseError` entry.

Imports are scoped to the helpers we genuinely need from `parse_finalyca`
(`_to_float`, `_parse_date_flex`, `_NA_TOKENS`, `_NUMERIC_RE`, the page-2
word helpers, etc.) so the boundary stays clean and the new file does not
re-implement any low-level parsing logic.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

import fitz  # noqa: F401  — typing only
import pdfplumber  # noqa: F401  — typing only

from ingest.models import Snapshot
from ingest.parse_finalyca import (
    _NA_TOKENS,
    _NUMERIC_RE,
    _filter_by_x,
    _group_rows,
    _page2_words,
    _parse_date_flex,
    _row_y,
    _to_float,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section parser 3.2.11 — market cap composition
# ---------------------------------------------------------------------------
#
# The "Mkt Cap Composition" block sits in the middle-right x-band of page 2
# (x ≈ 300-395). Three rows: Large Cap / Mid Cap / Small Cap, each with a
# percentage to the right. For pure-debt / arbitrage funds this block may
# not exist or may be partial — leave the fields as None in that case.

_MKT_CAP_COMP_X = (295.0, 395.0)


def parse_market_cap_composition(
    doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot,
) -> None:
    """Populate `large_cap_pct` / `mid_cap_pct` / `small_cap_pct` on Snapshot.

    For non-equity funds (pure debt) the block may not appear — that is
    acceptable; we return without setting any fields.
    """
    words = _page2_words(pl)
    if not words:
        return

    band = _filter_by_x(words, _MKT_CAP_COMP_X)
    rows = _group_rows(band)
    if not rows:
        return

    # Anchor: find the "Composition" header (single-token row at small-font;
    # appears just below "Mkt Cap"). The header is two visual rows in this
    # template ("Mkt Cap" → "Composition"), so we match either row alone
    # and use the lower y as our cutoff.
    start_y: Optional[float] = None
    for row in rows:
        joined = " ".join(w["text"] for w in row).lower()
        if "composition" == joined.strip() or "composition" in joined.split():
            start_y = _row_y(row)
            break
    if start_y is None:
        # Older layouts: header is a single "Mkt Cap Composition" line.
        for row in rows:
            joined = " ".join(w["text"] for w in row).lower()
            if "mkt" in joined and "cap" in joined and "composition" in joined:
                start_y = _row_y(row)
                break
    if start_y is None:
        return

    label_to_attr = {
        "large cap": "large_cap_pct",
        "mid cap": "mid_cap_pct",
        "small cap": "small_cap_pct",
    }
    for row in rows:
        if _row_y(row) <= start_y:
            continue
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        last = tokens[-1]
        if not _NUMERIC_RE.match(last) and last.upper() not in {"NA", "N/A"}:
            continue
        label = " ".join(tokens[:-1]).strip().lower()
        if label in label_to_attr:
            attr = label_to_attr[label]
            if getattr(snap, attr) is None:
                setattr(snap, attr, _to_float(last))
        if (snap.large_cap_pct is not None
                and snap.mid_cap_pct is not None
                and snap.small_cap_pct is not None):
            break


# ---------------------------------------------------------------------------
# Section parser 3.2.12 — investment style (3x3 matrix)
# ---------------------------------------------------------------------------
#
# Right x-band on page 2 (x ≈ 395-580). Header row is "Blend Growth Value";
# data rows are "<Large|Mid|Small> Cap <b> <g> <v>". Output is a flat dict
# {"Large Cap_Blend": 4.52, ...}.

_INV_STYLE_X = (395.0, 595.0)


def parse_investment_style(
    doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot,
) -> None:
    """Populate `snap.investment_style_json` with the 3x3 style matrix.

    NA cells are preserved as null in the JSON output.
    """
    words = _page2_words(pl)
    if not words:
        return

    band = _filter_by_x(words, _INV_STYLE_X)
    rows = _group_rows(band)
    if not rows:
        return

    header_y: Optional[float] = None
    header_x_positions: dict[str, float] = {}
    for row in rows:
        texts_lc = [w["text"].lower() for w in row]
        if "blend" in texts_lc and "growth" in texts_lc and "value" in texts_lc:
            header_y = _row_y(row)
            for w in row:
                t_lc = w["text"].lower()
                if t_lc in ("blend", "growth", "value"):
                    header_x_positions[t_lc] = float(w["x0"])
            break
    if header_y is None or len(header_x_positions) != 3:
        return

    matrix: dict[str, Optional[float]] = {}
    row_labels = [
        ("large cap", "Large Cap"),
        ("mid cap", "Mid Cap"),
        ("small cap", "Small Cap"),
    ]
    style_cols = ["blend", "growth", "value"]

    def _assign_by_x(row_words: list[dict]) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for w in row_words:
            t = w["text"]
            if _NUMERIC_RE.match(t) or t.upper() in {"NA", "N/A"}:
                x = float(w["x0"])
                best_col = min(
                    style_cols,
                    key=lambda c: abs(header_x_positions[c] - x),
                )
                if best_col not in out:
                    out[best_col] = _to_float(t)
        return out

    for row in rows:
        if _row_y(row) <= header_y:
            continue
        tokens_lc = " ".join(w["text"] for w in row).lower()
        for needle, label_canonical in row_labels:
            if needle in tokens_lc:
                value_words = [
                    w for w in row
                    if w["text"].lower() not in ("large", "mid", "small", "cap")
                ]
                col_values = _assign_by_x(value_words)
                for col in style_cols:
                    key = f"{label_canonical}_{col.capitalize()}"
                    matrix[key] = col_values.get(col)
                break
        if len(matrix) >= 9:
            break

    if matrix:
        snap.investment_style_json = json.dumps(matrix, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Section parser 3.2.13 — periodic returns (monthly + FY + CY)
# ---------------------------------------------------------------------------
#
# Three small return tables in the right x-band of pages 6-7.
# We capture the FUND % column (benchmark is already captured by
# `parse_trailing_returns`).

_PERIODIC_X = (460.0, 600.0)

_MONTH_ABBR = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_MONTH_LABEL_RE = re.compile(r"^([A-Za-z]{3,9})\s*(\d{4})$")


def _normalize_monthly_label(label: str, report_month: Optional[str]) -> Optional[str]:
    """"May 2025" → "2025-05"; "MTD" → report_month; else None."""
    s = label.strip()
    if s.upper() == "MTD":
        return report_month
    m = _MONTH_LABEL_RE.match(s)
    if not m:
        return None
    mon_abbr = m.group(1)[:3].lower()
    yr = m.group(2)
    if mon_abbr not in _MONTH_ABBR:
        return None
    return f"{yr}-{_MONTH_ABBR[mon_abbr]}"


def _extract_periodic_table(
    rows: list[list[dict]],
    header_predicate: Callable[[str], bool],
    label_extractor: Callable[[list[str]], Optional[str]],
) -> list[tuple[str, Optional[float]]]:
    """Walk one table; emit (label, fund_pct) tuples after the header is hit."""
    out: list[tuple[str, Optional[float]]] = []
    started = False
    for row in rows:
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        joined_lc = " ".join(tokens).lower()
        if not started:
            if header_predicate(joined_lc):
                started = True
            continue
        # Filter out year tokens (e.g., "2025", "2026") and 2-digit FY/CY
        # suffixes (e.g., "24", "25") — these are part of the LABEL, not the
        # return value column.
        def _is_value_numeric(t: str) -> bool:
            if not (_NUMERIC_RE.match(t) or t.upper() in {"NA", "N/A"}):
                return False
            # Year (4 digits 19xx-20xx) belongs to the label, not the value.
            if re.match(r"^(19|20)\d{2}$", t):
                return False
            # 2-digit FY/CY year suffix ("24", "25") — only when preceded by
            # an FY/CY label. We always strip <=3-digit pure ints to be safe,
            # since return %s are always decimals like "4.28".
            if re.match(r"^\d{1,3}$", t):
                return False
            return True

        numeric_tokens = [t for t in tokens if _is_value_numeric(t)]
        if not numeric_tokens:
            # Hard stops — sub-headers that indicate we've left this table.
            # "risk & volatility" begins the next big section; "powered by" is
            # the page footer. Do NOT break on "financial year"/"calendar year"/
            # "fund index" — those re-occur as sub-headers between adjacent
            # monthly/FY/CY tables on the same page, and breaking there would
            # miss the table we're actually looking for.
            if any(kw in joined_lc for kw in (
                "risk & volatility", "risk &", "powered by", "volatility",
            )):
                break
            continue
        label = label_extractor(tokens)
        if not label:
            continue
        fund_raw = numeric_tokens[0]
        out.append(
            (label, _to_float(fund_raw)
                if fund_raw.upper() not in {"NA", "N/A"} else None)
        )
    return out


def _monthly_label_from_tokens(tokens: list[str]) -> Optional[str]:
    """Extract a "May 2025" / "MTD" label from the start of the token list."""
    if not tokens:
        return None
    label_parts: list[str] = []
    for t in tokens:
        if _NUMERIC_RE.match(t):
            if re.match(r"^(19|20)\d{2}$", t):
                label_parts.append(t)
                continue
            break
        label_parts.append(t)
    label = " ".join(label_parts).strip()
    return label or None


def _fy_label_from_tokens(tokens: list[str]) -> Optional[str]:
    """Extract "FY 24" / "FYTD" from prefix tokens."""
    if not tokens:
        return None
    first = tokens[0].upper()
    if first == "FYTD":
        return "FYTD"
    if first == "FY" and len(tokens) >= 2:
        yr = tokens[1]
        if re.match(r"^\d{2,4}$", yr):
            return f"FY {yr}"
    return None


def _cy_label_from_tokens(tokens: list[str]) -> Optional[str]:
    """Extract "CY 23" / "YTD" from prefix tokens."""
    if not tokens:
        return None
    first = tokens[0].upper()
    if first == "YTD":
        return "YTD"
    if first == "CY" and len(tokens) >= 2:
        yr = tokens[1]
        if re.match(r"^\d{2,4}$", yr):
            return f"CY {yr}"
    return None


def parse_periodic_returns(
    doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot,
) -> None:
    """Populate `snap.periodic_returns` with monthly + FY + CY rows."""
    all_rows: list[dict] = []
    for page_idx in range(len(pl.pages)):
        try:
            words = pl.pages[page_idx].extract_words()
        except Exception as e:  # pragma: no cover
            logger.debug("periodic_returns: page %d extract_words: %s", page_idx, e)
            continue
        band = _filter_by_x(words, _PERIODIC_X)
        if not band:
            continue
        rows = _group_rows(band)
        if not rows:
            continue
        all_rows.append({"page": page_idx, "rows": rows})

    out: list[dict] = []

    for entry in all_rows:
        monthly_pairs = _extract_periodic_table(
            entry["rows"],
            header_predicate=lambda j: "month on" in j or "month-on-month" in j or (
                "month" in j and "fund" in j and "index" in j
            ),
            label_extractor=_monthly_label_from_tokens,
        )
        for label, fund_pct in monthly_pairs:
            normalized = _normalize_monthly_label(label, snap.report_month)
            if normalized is None:
                continue
            out.append({
                "period_type": "monthly",
                "period_label": normalized,
                "return_pct": fund_pct,
            })
        if monthly_pairs:
            break

    # The FY/CY headers can split across visual rows. Some funds (Canara
    # Robeco) put "Financial Year" + "Fund Index" together on one row; others
    # (ABSL) split them so only "Year"/"Fund (%)" appears in the right band.
    # We detect the FY block by walking until we see an "FY"-prefixed row,
    # then back-step the header_predicate to permissively match any prior row.
    def _fy_header(j: str) -> bool:
        return "financial year" in j or (
            ("year" in j or "fund" in j) and ("(%)" in j or "fund" in j)
        ) or j.strip() == "year"

    for entry in all_rows:
        fy_pairs = _extract_periodic_table(
            entry["rows"],
            header_predicate=_fy_header,
            label_extractor=_fy_label_from_tokens,
        )
        for label, fund_pct in fy_pairs:
            out.append({
                "period_type": "fy",
                "period_label": label,
                "return_pct": fund_pct,
            })
        if fy_pairs:
            break

    def _cy_header(j: str) -> bool:
        return "calendar year" in j or (
            ("year" in j or "fund" in j) and ("(%)" in j or "fund" in j)
        ) or j.strip() == "year"

    for entry in all_rows:
        cy_pairs = _extract_periodic_table(
            entry["rows"],
            header_predicate=_cy_header,
            label_extractor=_cy_label_from_tokens,
        )
        for label, fund_pct in cy_pairs:
            out.append({
                "period_type": "cy",
                "period_label": label,
                "return_pct": fund_pct,
            })
        if cy_pairs:
            break

    if not out:
        raise ValueError("periodic_returns: no monthly/FY/CY rows found")
    snap.periodic_returns = out


# ---------------------------------------------------------------------------
# Section parser 3.2.14 — full holdings (multi-page concatenation)
# ---------------------------------------------------------------------------
#
# 8-column table spanning multiple pages from "Detailed Portfolio" header
# until a next-section sentinel (Sector Wts. Trend, Mkt Cap Trend, etc.).

# Default holdings-table column bands (kept as a fallback when the header
# row can't be detected). The Finalyca template ships in at least two
# spacings — a "wide" variant (Canara Robeco etc., weights at x≈157) and
# a "compact" variant (HDFC Small Cap etc., weights at x≈144). Per-fund
# detection from the header row replaces these constants on the happy
# path; see `_detect_holdings_cols`.
_HOLDINGS_COLS: list[tuple[str, float, float]] = [
    ("security_name", 0.0, 155.0),
    ("weight_pct", 155.0, 195.0),
    ("sector", 195.0, 265.0),
    ("market_cap", 265.0, 330.0),
    ("instrument_type", 330.0, 400.0),
    ("risk_rating", 400.0, 455.0),
    ("investment_style", 455.0, 515.0),
    ("held_since", 515.0, 600.0),
]

# Header-row words (lower-cased) that identify each column. The first
# word in each list wins if multiple match. "Wts" specifically — not
# "(%)" — because some layouts split the unit into a separate token.
_HOLDINGS_COL_HEADER_TOKENS: list[tuple[str, tuple[str, ...]]] = [
    ("security_name", ("security",)),
    ("weight_pct", ("wts",)),
    ("sector", ("sector",)),
    ("market_cap", ("market",)),
    ("instrument_type", ("instrument",)),
    ("risk_rating", ("risk",)),
    ("investment_style", ("investment",)),
    ("held_since", ("held",)),
]


def _detect_holdings_cols(words: list[dict]) -> Optional[list[tuple[str, float, float]]]:
    """Read column x positions from the holdings-table header row.

    Returns a list shaped like `_HOLDINGS_COLS` (col, x_lo, x_hi) where
    each column's band runs from its header's x0 (security_name pinned
    to 0.0 so multi-word security names extend leftward) to the next
    column's x0. Returns None if the "Security … Held Since" header row
    isn't located — caller falls back to the hardcoded `_HOLDINGS_COLS`.
    """
    sec_y: Optional[float] = None
    for w in words:
        if w["text"].lower() == "security":
            sec_y = float(w["top"])
            break
    if sec_y is None:
        return None
    header_words = [w for w in words if abs(float(w["top"]) - sec_y) < 6.0]
    if not header_words:
        return None
    positions: dict[str, float] = {}
    for col, candidates in _HOLDINGS_COL_HEADER_TOKENS:
        for w in header_words:
            t = w["text"].lower().rstrip(":")
            if t in candidates:
                # First match per column wins. security_name's x0 is later
                # forced to 0.0 so multi-word names extend leftward.
                if col not in positions:
                    positions[col] = float(w["x0"])
                break
    # Need all 8 columns to commit to detection; otherwise fall back.
    if len(positions) != len(_HOLDINGS_COL_HEADER_TOKENS):
        return None
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    cols: list[tuple[str, float, float]] = []
    for i, (col, x_lo) in enumerate(ordered):
        x_hi = ordered[i + 1][1] if i + 1 < len(ordered) else 600.0
        cols.append((col, x_lo, x_hi))
    # Force security_name's left edge to 0 so long names extend leftward.
    cols = [(c, 0.0, hi) if c == "security_name" else (c, lo, hi)
            for c, lo, hi in cols]
    return cols


_HOLDINGS_END_PHRASES = (
    "sector wts. trend", "sector wts trend", "mkt cap trend",
    "investment style trend", "aum trend", "attribution analysis",
    "alpha generators", "disclaimer", "bajaj capital",
)


def _na_to_none(val: str) -> Optional[str]:
    """Holdings-table-specific NA normalization: "Na"/"NA"/"-"/etc → None."""
    s = (val or "").strip()
    if not s or s.upper() in _NA_TOKENS or s.lower() == "na":
        return None
    return s


def _parse_holdings_page(
    pl_page,
    *,
    is_first_page: bool,
    cols: list[tuple[str, float, float]] = _HOLDINGS_COLS,
) -> tuple[list[dict], bool]:
    """Parse one page's slice of the holdings table.

    `cols` overrides the per-fund column bands when the caller has
    detected them from the header row; defaults to the hardcoded
    `_HOLDINGS_COLS` for backward compatibility.

    Returns (rows, hit_end_sentinel). hit_end_sentinel is True when this
    page contains a next-section header that terminates the holdings block.
    """
    try:
        words = pl_page.extract_words()
    except Exception as e:  # pragma: no cover
        logger.debug("holdings_full: extract_words failed: %s", e)
        return [], False

    page_text = pl_page.extract_text() or ""
    page_text_lc = page_text.lower()

    hit_end = False
    end_y: float = float("inf")
    for phrase in _HOLDINGS_END_PHRASES:
        if phrase in page_text_lc:
            for w in words:
                if phrase.split()[0].lower() in w["text"].lower():
                    end_y = min(end_y, float(w["top"]))
                    hit_end = True
                    break

    # Weight-token band derived from the detected `cols`, not hardcoded.
    weight_lo, weight_hi = next(
        ((lo, hi) for c, lo, hi in cols if c == "weight_pct"),
        (155.0, 195.0),
    )

    start_y = 0.0
    if is_first_page:
        for w in words:
            if w["text"] in ("Security", "Detailed"):
                start_y = max(start_y, float(w["top"]) + 5.0)
        first_data_y: Optional[float] = None
        for w in sorted(words, key=lambda w: float(w["top"])):
            if weight_lo <= float(w["x0"]) < weight_hi and _NUMERIC_RE.match(w["text"]):
                first_data_y = float(w["top"])
                break
        if first_data_y is not None:
            start_y = first_data_y - 2.0

    data_words = [
        w for w in words
        if start_y <= float(w["top"]) < end_y
    ]
    if not data_words:
        return [], hit_end

    weight_tokens = [
        w for w in data_words
        if weight_lo <= float(w["x0"]) < weight_hi and _NUMERIC_RE.match(w["text"])
    ]
    weight_tokens.sort(key=lambda w: float(w["top"]))
    if not weight_tokens:
        return [], hit_end

    weight_ys = [float(w["top"]) for w in weight_tokens]

    def _bucket_for(y: float) -> int:
        best_i = 0
        best_d = abs(y - weight_ys[0])
        for i, wy in enumerate(weight_ys[1:], start=1):
            d = abs(y - wy)
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    grouped: list[list[dict]] = [[wt] for wt in weight_tokens]
    for w in data_words:
        if w in weight_tokens:
            continue
        y = float(w["top"])
        b = _bucket_for(y)
        if abs(y - weight_ys[b]) <= 9.0:
            grouped[b].append(w)

    rows: list[dict] = []
    for group in grouped:
        def _join_col(name: str) -> str:
            ws = [w for w in group
                  if any(lo <= float(w["x0"]) < hi
                         for col, lo, hi in cols if col == name)]
            ws.sort(key=lambda w: (float(w["top"]), float(w["x0"])))
            return " ".join(w["text"] for w in ws).strip()

        security_name = _join_col("security_name")
        if not security_name:
            continue
        # Guard against chart-axis tokens leaking in when the Detailed
        # Portfolio ends mid-page and the AUM/Performance trend chart's
        # year labels (2019, 2020, …) land in our row bucket. A real
        # security name always contains at least one ASCII letter.
        if not any(c.isalpha() for c in security_name):
            continue
        weight_pct = _to_float(_join_col("weight_pct"))
        sector = _na_to_none(_join_col("sector"))
        market_cap = _na_to_none(_join_col("market_cap"))
        instrument_type = _na_to_none(_join_col("instrument_type"))
        risk_rating = _na_to_none(_join_col("risk_rating"))
        investment_style = _na_to_none(_join_col("investment_style"))
        held_since_raw = _join_col("held_since")
        held_since_val = _na_to_none(held_since_raw)
        held_since_iso: Optional[str] = None
        if held_since_val:
            d = _parse_date_flex(held_since_val)
            held_since_iso = d.isoformat() if d else None

        rows.append({
            "security_name": security_name,
            "weight_pct": weight_pct,
            "sector": sector,
            "market_cap": market_cap,
            "instrument_type": instrument_type,
            "risk_rating": risk_rating,
            "investment_style": investment_style,
            "held_since": held_since_iso,
        })

    return rows, hit_end


def parse_holdings_full(
    doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot,
) -> None:
    """Populate `snap.full_holdings` with the full per-security holdings.

    Concatenates across all pages of the Detailed Portfolio block. Same
    security can appear twice with different instrument_type (e.g. HDFC
    Bank as Equity AND Debt for arbitrage funds) — preserved, not deduped.
    """
    start_page: Optional[int] = None
    for i, page in enumerate(pl.pages):
        text = (page.extract_text() or "").lower()
        if "detailed portfolio" in text:
            start_page = i
            break
    if start_page is None:
        raise ValueError("Detailed Portfolio header not found in any page")

    # Detect this fund's column geometry from the first page's header row;
    # fall back to the hardcoded defaults if detection fails.
    try:
        first_words = pl.pages[start_page].extract_words()
    except Exception as e:  # pragma: no cover
        logger.debug("holdings_full: extract_words on first page failed: %s", e)
        first_words = []
    detected_cols = _detect_holdings_cols(first_words) if first_words else None
    cols = detected_cols if detected_cols else _HOLDINGS_COLS

    all_rows: list[dict] = []
    is_first = True
    for i in range(start_page, len(pl.pages)):
        page = pl.pages[i]
        rows, hit_end = _parse_holdings_page(page, is_first_page=is_first, cols=cols)
        all_rows.extend(rows)
        is_first = False
        if hit_end:
            break
        if not rows and i > start_page:
            break

    if not all_rows:
        raise ValueError("holdings_full: parsed zero rows across all pages")

    snap.full_holdings = all_rows
