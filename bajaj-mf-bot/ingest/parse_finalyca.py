"""Finalyca PDF parser — minimal spine (5 fields).

Phase 1.3 of the build plan: prove PDF → Snapshot end-to-end on a single
template. Phase 3 widens this with one parser function per section.

Library split (locked in for Phase 3 too):
  * `fitz` (PyMuPDF) for header text & font-size aware layout extraction.
  * `pdfplumber` for table extraction (trailing returns, holdings, etc).

Both libraries open the PDF independently because they target different
layers of the document (text geometry vs ruled-line tables).

Cross-cutting parser gotchas handled in `_to_float`:
  * "NA"          → None
  * "0E-9", "0e-9" → 0.0
  * Stripping `%`, `Cr`, commas, whitespace

If `scheme_name` or `as_of_date` is missing we raise `ValueError` — those
two fields form the de-facto primary key (scheme_id lookup + report_month
uniqueness), so a Snapshot without them cannot be inserted. Everything
else may legitimately be missing and stays as `None`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pdfplumber

# Resolve `config` whether invoked as `python -m ingest.parse_finalyca` or
# as a script. Mirrors the pattern used in `db/init_db.py`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402
from ingest.models import Snapshot  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

_NA_TOKENS = {"NA", "N/A", "-", "--", ""}


def _to_float(raw: object) -> Optional[float]:
    """Best-effort float parsing with Finalyca's quirks.

    Returns None for `NA`/`N/A`/empty; 0.0 for `0E-9`; otherwise strips `%`,
    ` Cr`, and thousands separators before casting.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s.upper() in _NA_TOKENS:
        return None
    # Effective-zero sentinel used in pure-equity rows (e.g. modified_duration).
    if s.upper() in {"0E-9", "0.0E-9", "0E0"}:
        return 0.0
    # Strip unit suffixes and separators commonly attached to numeric cells.
    cleaned = s.replace("%", "").replace(",", "")
    cleaned = re.sub(r"\s*[Cc][Rr]\.?\s*$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return float(cleaned)
    except ValueError:
        # 0E-9 with scientific notation slips through float() normally,
        # but we keep an explicit fallback for safety on locale weirdness.
        logger.debug("_to_float: unparseable value %r (cleaned=%r)", raw, cleaned)
        return None


def _sha256_of_file(path: Path) -> str:
    """SHA-256 hex digest of the file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Header extraction via PyMuPDF — font-size aware
# ---------------------------------------------------------------------------

# Match "As On 04 May 2026", "As on: 04 May 2026", "AS ON  04-May-2026", etc.
# Captures the date substring (group 1) for separate parse-with-fallbacks.
_AS_OF_RE = re.compile(
    r"\bAs\s*[Oo]n\b\s*[:\-]?\s*([0-9]{1,2}[\s\-/][A-Za-z]+[\s\-/][0-9]{2,4})",
    re.IGNORECASE,
)
_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%b/%Y", "%d/%B/%Y")


def _parse_date_flex(raw: str) -> Optional[date]:
    """Try every known Finalyca date format; return None if none match."""
    raw = raw.strip()
    # Normalize whitespace / separators so "04 May 2026" and "04-May-2026" both work
    candidate = re.sub(r"[\-/]", " ", raw)
    candidate = re.sub(r"\s+", " ", candidate)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _collect_spans(doc: "fitz.Document") -> list[dict]:
    """Return all text spans of page 1 with their geometry/font metadata."""
    page = doc[0]
    raw = page.get_text("dict")
    spans: list[dict] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # text blocks only
            continue
        for line in block.get("lines", []):
            y0 = round(line["bbox"][1], 2)
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                spans.append({
                    "text": text,
                    "size": round(float(span.get("size", 0.0)), 2),
                    "y0": y0,
                    "font": span.get("font", ""),
                })
    return spans


def _extract_scheme_name(doc: "fitz.Document") -> Optional[str]:
    """Pick the top-of-page largest-font run as the scheme name.

    Finalyca renders the title in a font visibly larger than every other
    span on page 1 (16pt vs 12pt subtitle vs 7.5pt body). We:
      1. Find the maximum font size among spans in the top ~30% of the page.
      2. Concatenate every span at that size in y-order (the title wraps to
         a second line in long names like "Canara Robeco Multi Cap Fund -
         Regular (G)").
    """
    spans = _collect_spans(doc)
    if not spans:
        logger.warning("scheme_name: no text spans on page 1")
        return None

    # "Top of page" — use the spans whose y0 is in the top 30% of seen spans.
    y_values = [s["y0"] for s in spans]
    cutoff = min(y_values) + 0.3 * (max(y_values) - min(y_values) + 1e-9)
    top = [s for s in spans if s["y0"] <= cutoff]
    if not top:
        top = spans

    max_size = max(s["size"] for s in top)
    title_spans = sorted(
        (s for s in top if s["size"] == max_size),
        key=lambda s: s["y0"],
    )
    name = " ".join(s["text"] for s in title_spans).strip()
    # Clean trailing "-" from wrapped first line ("Canara Robeco Multi Cap Fund -")
    name = re.sub(r"\s+-\s*$", "", name)
    # Collapse multiple spaces produced by the wrap-then-join.
    name = re.sub(r"\s+", " ", name).strip(" -")
    logger.info("scheme_name: extracted %r (max font %s)", name, max_size)
    return name or None


def _extract_as_of_date(doc: "fitz.Document") -> Optional[date]:
    """Find "As On <date>" anywhere on page 1 and parse it."""
    page_text = doc[0].get_text("text")
    m = _AS_OF_RE.search(page_text)
    if not m:
        logger.warning("as_of_date: 'As On' anchor not found on page 1")
        return None
    raw = m.group(1)
    parsed = _parse_date_flex(raw)
    if parsed is None:
        logger.warning("as_of_date: matched %r but no date format applied", raw)
    else:
        logger.info("as_of_date: extracted %s from %r", parsed.isoformat(), raw)
    return parsed


def _extract_label_value(doc: "fitz.Document", label: str) -> Optional[str]:
    """Find the value associated with `label` on page 1.

    Finalyca's header is a two-column grid. Label/value pairs live in the
    same column (similar x-coordinate), with the value at roughly the same y
    (same row, label on the left) OR at a small positive y-delta below
    (label on top of its value, as for "Fund AUM" → "(cr.)" → "4679.32").

    Strategy: locate the label span, then pick the nearest subsequent span
    whose x is close to the label's x (within a column-width tolerance),
    skipping pure-parenthetical units like "(cr.)".
    """
    page = doc[0]
    raw = page.get_text("dict")
    flat: list[tuple[float, float, str]] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            y = round(line["bbox"][1], 2)
            for span in line.get("spans", []):
                t = span.get("text", "").strip()
                if t:
                    x = round(line["bbox"][0], 2)
                    flat.append((y, x, t))
    flat.sort(key=lambda t: (t[0], t[1]))

    label_lower = label.lower()
    label_hits = [i for i, (_y, _x, t) in enumerate(flat) if t.lower() == label_lower]
    if not label_hits:
        return None
    i = label_hits[0]
    ly, lx, _ = flat[i]

    # Known header labels we never want to mistake for a value.
    LABEL_TOKENS = {
        "min investment", "expense ratio", "exit load/lock-in",
        "fees structure", "fund aum", "inception date", "benchmark",
        "overview", "age:", "as on:",
    }

    def _is_value_ish(text: str) -> bool:
        if text.lower() == label_lower:
            return False
        if text.startswith("(") and text.endswith(")"):
            return False  # "(cr.)" unit suffix
        if text.lower() in LABEL_TOKENS:
            return False
        return True

    # ---- Pass 1: same row, to the right of the label (label/value pairs
    # like "Expense Ratio | 1.85" sit on the same y baseline).
    same_row: list[tuple[float, str]] = []
    for j, (y, x, t) in enumerate(flat):
        if j == i:
            continue
        if abs(y - ly) > 2.0:
            continue
        if x <= lx:
            continue
        if not _is_value_ish(t):
            continue
        same_row.append((x - lx, t))
    if same_row:
        same_row.sort(key=lambda c: c[0])
        return same_row[0][1]

    # ---- Pass 2: directly below the label, same x-column. Handles the
    # "Fund AUM" / "(cr.)" / "4679.32" stacked-cell layout.
    Y_BELOW_MAX = 30.0
    X_TOL = 30.0
    below: list[tuple[float, str]] = []
    for j, (y, x, t) in enumerate(flat):
        if j == i:
            continue
        dy = y - ly
        if dy <= 0.0 or dy > Y_BELOW_MAX:
            continue
        if abs(x - lx) > X_TOL:
            continue
        if not _is_value_ish(t):
            continue
        below.append((dy, t))
    if below:
        below.sort(key=lambda c: c[0])
        return below[0][1]

    return None


def _extract_expense_ratio(doc: "fitz.Document") -> Optional[float]:
    raw = _extract_label_value(doc, "Expense Ratio")
    if raw is None:
        logger.warning("expense_ratio: label not found")
        return None
    val = _to_float(raw)
    logger.info("expense_ratio: %r -> %s", raw, val)
    return val


def _extract_fund_aum_cr(doc: "fitz.Document") -> Optional[float]:
    raw = _extract_label_value(doc, "Fund AUM")
    if raw is None:
        logger.warning("fund_aum_cr: label not found")
        return None
    val = _to_float(raw)
    logger.info("fund_aum_cr: %r -> %s", raw, val)
    return val


# ---------------------------------------------------------------------------
# Trailing returns table — pdfplumber
# ---------------------------------------------------------------------------

# Map a normalized header cell to the dataclass attribute we want to set.
_PERIOD_COLUMN_MAP = {
    "1 month": "return_1m",
    "3 months": "return_3m",
    "6 months": "return_6m",
    "1 year": "return_1y",
    "1y": "return_1y",
    "2 years": "return_2y",
    "3 years": "return_3y",
    "5 years": "return_5y",
    "10 years": "return_10y",
    "since inception": "return_since_inception",
}


def _normalize_header(cell: object) -> str:
    return re.sub(r"\s+", " ", str(cell or "").strip().lower())


def _extract_return_1y(pl: "pdfplumber.PDF") -> Optional[float]:
    """Locate the Trailing Returns table, return the Fund row 1Y cell.

    Strategy: iterate every table on every page (the table is reliably on
    page 1 but we don't assume), look for a row whose first cell normalizes
    to "trailing returns %" — its columns are the period headers; the next
    row that starts with "Fund" (case-insensitive, NOT a benchmark name)
    gives us the values to map.
    """
    for page_idx, page in enumerate(pl.pages):
        try:
            tables = page.extract_tables()
        except Exception as e:  # pdfplumber sometimes chokes on chart layers
            logger.debug("return_1y: page %d extract_tables raised %s", page_idx, e)
            continue
        for t_idx, table in enumerate(tables):
            if not table or len(table) < 2:
                continue
            # Find the header row containing "Trailing Returns" + period labels.
            header_idx = None
            for r_idx, row in enumerate(table):
                if not row:
                    continue
                first = _normalize_header(row[0])
                if first.startswith("trailing returns"):
                    header_idx = r_idx
                    break
            if header_idx is None:
                continue

            header = [_normalize_header(c) for c in table[header_idx]]
            try:
                one_y_col = next(
                    i for i, h in enumerate(header)
                    if h in _PERIOD_COLUMN_MAP and _PERIOD_COLUMN_MAP[h] == "return_1y"
                )
            except StopIteration:
                logger.debug(
                    "return_1y: trailing-returns header found but no 1Y column "
                    "(page=%d table=%d header=%s)",
                    page_idx, t_idx, header,
                )
                continue

            for r_idx in range(header_idx + 1, len(table)):
                row = table[r_idx]
                if not row:
                    continue
                first = _normalize_header(row[0])
                if first == "fund":
                    if one_y_col >= len(row):
                        continue
                    raw = row[one_y_col]
                    val = _to_float(raw)
                    logger.info(
                        "return_1y: page=%d table=%d row=%d raw=%r -> %s",
                        page_idx, t_idx, r_idx, raw, val,
                    )
                    return val
    logger.warning("return_1y: Trailing Returns 'Fund' row / 1 Year column not located")
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_pdf_minimal(path: Path) -> Snapshot:
    """Parse exactly five fields plus metadata from a Finalyca PDF.

    Args:
        path: Filesystem path to the source PDF.

    Returns:
        Populated Snapshot dataclass. Always sets `report_month`,
        `parser_version`, `pdf_sha256`, and `source_pdf_path`. Sets
        scheme_name on a non-persisted attribute so the ingest CLI can use
        it for the schemes-table lookup.

    Raises:
        ValueError: If scheme_name or as_of_date cannot be parsed (no PK).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    logger.info("parse_pdf_minimal: opening %s", path)
    snap = Snapshot()
    snap.report_month = "2026-05"
    snap.parser_version = config.PARSER_VERSION
    snap.pdf_sha256 = _sha256_of_file(path)
    snap.source_pdf_path = str(path)
    snap.revision = 1
    logger.info("pdf_sha256: %s", snap.pdf_sha256)

    # PyMuPDF for header-level layout work.
    with fitz.open(str(path)) as doc:
        snap.scheme_name = _extract_scheme_name(doc)
        snap.as_of_date = _extract_as_of_date(doc)
        snap.expense_ratio = _extract_expense_ratio(doc)
        snap.fund_aum_cr = _extract_fund_aum_cr(doc)

    # Required-for-PK validation: if either is missing, the caller cannot
    # insert this snapshot, so fail loudly here rather than swallowing it.
    if not snap.scheme_name:
        raise ValueError(f"Could not parse scheme_name from {path.name}")
    if snap.as_of_date is None:
        raise ValueError(f"Could not parse as_of_date from {path.name}")

    # pdfplumber for the Trailing Returns table.
    with pdfplumber.open(str(path)) as pl:
        snap.return_1y = _extract_return_1y(pl)

    logger.info(
        "parse_pdf_minimal: done scheme_name=%r as_of_date=%s "
        "expense_ratio=%s fund_aum_cr=%s return_1y=%s",
        snap.scheme_name, snap.as_of_date,
        snap.expense_ratio, snap.fund_aum_cr, snap.return_1y,
    )
    return snap
