"""Finalyca PDF parser — dispatch-pattern section parsers (Phase 3).

The Phase 1 spine extracted 5 fields via a single function. Phase 3
refactors to a list of per-section parsers, each owning one named region
of the Finalyca template. Per-section failures are caught and recorded
in a per-snapshot `parse_errors_json` list — they never abort the whole
parse. Exception: `_parse_header_required` (scheme_name + as_of_date)
forms the de-facto primary key; if either fails we cannot insert the
row and the parse aborts.

Library split:
  * `fitz` (PyMuPDF) for header text & font-size aware layout extraction.
  * `pdfplumber` for table extraction (trailing returns, holdings, etc).

Cross-cutting gotchas handled in `_to_float` / `_parse_date_flex`:
  * "NA"           → None
  * "0E-9", "0e-9" → 0.0
  * Date formats   → ISO via several known patterns

Backward compatibility: `parse_pdf_minimal` is preserved as a thin
wrapper around `parse_pdf` so the Phase 1 `ingest_one.py` keeps working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import fitz  # PyMuPDF
import pdfplumber

# Resolve `config` whether invoked as `python -m ingest.parse_finalyca` or
# as a script. Mirrors the pattern used in `db/init_db.py`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402
from ingest.models import ParseError, Snapshot  # noqa: E402

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
    if s.upper() in {"0E-9", "0.0E-9", "0E0"}:
        return 0.0
    cleaned = s.replace("%", "").replace(",", "")
    cleaned = re.sub(r"\s*[Cc][Rr]\.?\s*$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return float(cleaned)
    except ValueError:
        logger.debug("_to_float: unparseable value %r (cleaned=%r)", raw, cleaned)
        return None


def _to_int(raw: object) -> Optional[int]:
    """Best-effort int parsing; falls through `_to_float` then truncates."""
    f = _to_float(raw)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def _sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_AS_OF_RE = re.compile(
    r"\bAs\s*[Oo]n\b\s*[:\-]?\s*([0-9]{1,2}[\s\-/][A-Za-z]+[\s\-/][0-9]{2,4})",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%d %b %Y", "%d %B %Y",
    "%d-%b-%Y", "%d-%B-%Y",
    "%d/%b/%Y", "%d/%B/%Y",
    "%d/%m/%Y", "%d-%m-%Y",
    "%Y-%m-%d",
)


def _parse_date_flex(raw: str) -> Optional[date]:
    """Try every known Finalyca date format; return None if none match."""
    raw = (raw or "").strip()
    if not raw:
        return None
    candidate = re.sub(r"[\-/]", " ", raw)
    candidate = re.sub(r"\s+", " ", candidate)
    for fmt in (
        "%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y",
    ):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    # Fall back to formats that need original separators.
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Header span helpers (PyMuPDF)
# ---------------------------------------------------------------------------


def _collect_spans(doc: "fitz.Document", page_idx: int = 0) -> list[dict]:
    """Return all text spans of the requested page with geometry/font metadata.

    Each entry: {text, size, x0, x1, y0, font}. x/y use the span bbox (not the
    line bbox) so multi-span lines like "Age: | 2 Years 9 Months" resolve to
    distinct x positions.
    """
    page = doc[page_idx]
    raw = page.get_text("dict")
    spans: list[dict] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                bx0, by0, bx1, by1 = span.get("bbox", (0.0, 0.0, 0.0, 0.0))
                spans.append({
                    "text": text,
                    "size": round(float(span.get("size", 0.0)), 2),
                    "x0": round(float(bx0), 2),
                    "x1": round(float(bx1), 2),
                    "y0": round(float(by0), 2),
                    "font": span.get("font", ""),
                })
    return spans


# Labels we never want to mistake for a value when adjacent-scanning.
_HEADER_LABELS = {
    "benchmark", "fund aum", "inception date", "min investment",
    "expense ratio", "exit load/lock-in", "fees structure",
    "overview", "age:", "as on:",
}


def _value_for_label(
    spans: list[dict],
    label: str,
    *,
    x_tol: float = 30.0,
    y_below_max: float = 30.0,
    same_row_tol: float = 5.0,
    join_continuations: bool = True,
) -> Optional[str]:
    """Find the value associated with `label` via spatial proximity.

    Strategy:
      1. Find the span whose normalized text == label (case-insensitive).
      2. Pass 1: same-row spans to the right (y within ±same_row_tol),
         excluding other known labels and pure-parenthetical units like
         "(cr.)". The closest is the primary value.
      3. Pass 2: spans directly below the label (small dy > 0), in the
         same column (|x - lx| <= x_tol) — handles the stacked
         "Fund AUM / (cr.) / 4679.32" pattern.
      4. If `join_continuations` and the primary value's first character
         is alphabetic (text labels like benchmark / exit-load that wrap),
         append any further spans at the same x within y_below_max.
    """
    label_lower = label.lower()
    label_idx = None
    for i, s in enumerate(spans):
        if s["text"].lower() == label_lower:
            label_idx = i
            break
    if label_idx is None:
        return None
    lx = spans[label_idx]["x0"]
    ly = spans[label_idx]["y0"]

    def _is_value_ish(text: str) -> bool:
        if text.lower() == label_lower:
            return False
        if text.startswith("(") and text.endswith(")"):
            return False
        if text.lower() in _HEADER_LABELS:
            return False
        return True

    # ---- Pass 1: same row (within same_row_tol y), to the right of label.
    same_row: list[tuple[float, dict]] = []
    for j, s in enumerate(spans):
        if j == label_idx:
            continue
        if abs(s["y0"] - ly) > same_row_tol:
            continue
        if s["x0"] <= lx:
            continue
        if not _is_value_ish(s["text"]):
            continue
        same_row.append((s["x0"] - lx, s))
    primary: Optional[dict] = None
    if same_row:
        same_row.sort(key=lambda c: c[0])
        primary = same_row[0][1]

    # ---- Pass 2: below the label, same column. Picks "Fund AUM / ... / 4679.32".
    if primary is None:
        below: list[tuple[float, dict]] = []
        for j, s in enumerate(spans):
            if j == label_idx:
                continue
            dy = s["y0"] - ly
            if dy <= 0.0 or dy > y_below_max:
                continue
            if abs(s["x0"] - lx) > x_tol:
                continue
            if not _is_value_ish(s["text"]):
                continue
            below.append((dy, s))
        if below:
            below.sort(key=lambda c: c[0])
            primary = below[0][1]

    if primary is None:
        return None

    if not join_continuations:
        return primary["text"]

    # ---- Pass 3: gather wrap-around continuations (same x column as primary,
    # slightly below it, within y_below_max). Common for multi-line benchmark
    # ("40% NIFTY500 ... / Index + 20% MSCI ...") and exit-load text.
    parts = [primary["text"]]
    py = primary["y0"]
    px = primary["x0"]
    cont: list[tuple[float, dict]] = []
    for s in spans:
        if s is primary:
            continue
        if abs(s["x0"] - px) > 1.5:
            continue
        dy = s["y0"] - py
        if dy <= 0.0 or dy > y_below_max:
            continue
        if s["text"].lower() in _HEADER_LABELS:
            continue
        cont.append((dy, s))
    cont.sort(key=lambda c: c[0])
    for _dy, s in cont:
        parts.append(s["text"])
        py = s["y0"]
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Scheme name (large-font heading) and As-Of date
# ---------------------------------------------------------------------------


def _extract_scheme_name(doc: "fitz.Document") -> Optional[str]:
    spans = _collect_spans(doc)
    if not spans:
        return None
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
    name = re.sub(r"\s+-\s*$", "", name)
    name = re.sub(r"\s+", " ", name).strip(" -")
    return name or None


def _extract_as_of_date(doc: "fitz.Document") -> Optional[date]:
    page_text = doc[0].get_text("text")
    m = _AS_OF_RE.search(page_text)
    if not m:
        return None
    return _parse_date_flex(m.group(1))


def _parse_header_required(doc: "fitz.Document", snap: Snapshot) -> None:
    """Populate scheme_name + as_of_date or raise.

    These two fields together form the de-facto primary key (scheme_id
    lookup + report_month uniqueness). A snapshot without them cannot be
    inserted, so failure here aborts the whole parse.
    """
    snap.scheme_name = _extract_scheme_name(doc)
    snap.as_of_date = _extract_as_of_date(doc)
    if not snap.scheme_name:
        raise ValueError("scheme_name not found on page 1")
    if snap.as_of_date is None:
        raise ValueError("as_of_date not found on page 1")


# ---------------------------------------------------------------------------
# Section parser 3.2.1 — full header block
# ---------------------------------------------------------------------------


_AGE_RE = re.compile(
    r"(\d+)\s*(?:Year|Yr|Y)s?\s*(?:(\d+)\s*(?:Month|Mo|M)s?)?",
    re.IGNORECASE,
)


def _normalize_age(raw: Optional[str]) -> Optional[str]:
    """Convert "2 Years 9 Months" → "2Y 9M"; pass through anything else."""
    if not raw:
        return None
    m = _AGE_RE.search(raw)
    if not m:
        return raw.strip() or None
    years = m.group(1)
    months = m.group(2)
    if months:
        return f"{years}Y {months}M"
    return f"{years}Y"


def _extract_sub_category(spans: list[dict]) -> Optional[str]:
    """Find the small-font subtitle line like "Mutual Fund - Equity: Multi Cap".

    Heuristic: the subtitle is the only span at the second-largest font on
    page 1 (the title is 16pt, subtitle 12pt, body 7.5pt). Strip the
    "Mutual Fund - " prefix; keep the remainder as the sub-category.
    """
    if not spans:
        return None
    sizes = sorted({s["size"] for s in spans}, reverse=True)
    if len(sizes) < 2:
        return None
    # Look at the top region; the subtitle is right below the title.
    y_values = [s["y0"] for s in spans]
    cutoff = min(y_values) + 0.25 * (max(y_values) - min(y_values) + 1e-9)
    top = [s for s in spans if s["y0"] <= cutoff]
    if not top:
        top = spans
    subtitle_size = sizes[1]
    subtitle_spans = [s for s in top if s["size"] == subtitle_size]
    if not subtitle_spans:
        return None
    text = " ".join(s["text"] for s in sorted(subtitle_spans, key=lambda s: s["y0"]))
    text = text.strip()
    # Strip "Mutual Fund -" / "Mutual Fund:" / "Mutual Fund " prefix.
    text = re.sub(r"^Mutual Fund\s*[-:]\s*", "", text, flags=re.IGNORECASE).strip()
    return text or None


def _extract_overview(spans: list[dict]) -> Optional[str]:
    """Return the descriptive paragraph that follows the "Overview" label.

    The Finalyca template puts a small-font "Overview" header, then 1-N
    body lines underneath, then either the "Age: ..." / "As On: ..." row
    or "Fund Manager Detail". We capture body lines between the Overview
    label and the next known sentinel.
    """
    overview_idxs = [i for i, s in enumerate(spans) if s["text"].lower() == "overview"]
    if not overview_idxs:
        return None
    label = spans[overview_idxs[0]]
    label_y = label["y0"]
    # Stop sentinels — any of these signals the overview block has ended.
    STOPS = ("age:", "as on:", "fund manager detail", "performance",
             "trailing returns %", "fund manager detail")
    body: list[tuple[float, str]] = []
    for s in spans:
        if s["y0"] <= label_y + 1.0:
            continue
        if s["text"].lower() in STOPS:
            break
        if s["text"].lower().startswith("age:") or s["text"].lower().startswith("as on:"):
            break
        body.append((s["y0"], s["text"]))
        # The overview is typically < 6 lines; cap to prevent runaway.
        if len(body) > 10:
            break
    if not body:
        return None
    body.sort(key=lambda c: c[0])
    text = " ".join(t for _y, t in body).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def parse_header(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Extract every non-required header field.

    Required fields (scheme_name, as_of_date) are handled separately by
    `_parse_header_required`; failures there abort the entire parse.
    Anything here is best-effort: a missing label silently leaves the
    Snapshot field as None.
    """
    spans = _collect_spans(doc)

    # Sub-category (e.g. "Equity: Multi Cap", "Hybrid: Arbitrage").
    snap.sub_category = _extract_sub_category(spans)

    # Benchmark — multi-line capable.
    raw_bm = _value_for_label(spans, "Benchmark", y_below_max=15.0, x_tol=5.0)
    snap.benchmark = raw_bm.strip() if raw_bm else None

    # Inception Date.
    raw_inc = _value_for_label(spans, "Inception Date", join_continuations=False)
    if raw_inc:
        snap.inception_date = _parse_date_flex(raw_inc)

    # Min Investment — keep as string.
    raw_min = _value_for_label(spans, "Min Investment", join_continuations=False)
    snap.min_investment = raw_min.strip() if raw_min else None

    # Expense Ratio — strip %.
    raw_er = _value_for_label(spans, "Expense Ratio", join_continuations=False)
    snap.expense_ratio = _to_float(raw_er)

    # Exit Load — verbatim, multi-line capable.
    raw_el = _value_for_label(spans, "Exit Load/Lock-in", y_below_max=15.0)
    snap.exit_load = raw_el.strip() if raw_el else None

    # Fund AUM — value lives below the "(cr.)" unit. We pull the first
    # all-numeric continuation under "Fund AUM".
    raw_aum = _value_for_label(spans, "Fund AUM", y_below_max=40.0, x_tol=30.0)
    snap.fund_aum_cr = _to_float(raw_aum)

    # Age — appears as "Age: <value>" with both spans on one line.
    raw_age = _value_for_label(spans, "Age:", join_continuations=False)
    snap.fund_age = _normalize_age(raw_age)

    # Overview — body lines beneath the "Overview" label.
    snap.overview = _extract_overview(spans)


# ---------------------------------------------------------------------------
# Section parser 3.2.2 — fund managers
# ---------------------------------------------------------------------------

# Roles we recognize as a manager-role line. The template upper-cases the
# role (FUND MANAGER - EQUITY) but a few PDFs use mixed case ("Fund Manager
# - EQUITY"), so we match case-insensitively.
_ROLE_RE = re.compile(
    r"^\s*Fund\s*Manager\s*[-–]\s*(Equity\s*&\s*Debt|Equity|Debt|Equity-Debt)\s*$",
    re.IGNORECASE,
)

_QUALIFICATION_RE = re.compile(
    r"Qualification\s*[:\-]\s*(.*?)(?:\.\s*)?(?:Experience\b|$)",
    re.IGNORECASE | re.DOTALL,
)

# Years-of-experience hints: "over 17 years", "more than 10 years", "around 11 years",
# "an overall experience of over 10 years", or just "X years of experience".
_EXP_RE = re.compile(
    r"(?:over|more than|around|about|approximately|approx\.?|nearly)?\s*"
    r"(\d{1,2}(?:\.\d+)?)\s*\+?\s*years?",
    re.IGNORECASE,
)


def _role_for(text: str) -> Optional[str]:
    m = _ROLE_RE.match(text)
    if not m:
        return None
    raw = m.group(1).strip()
    # Normalize "Equity & Debt" / "Equity-Debt" variants.
    if "&" in raw or "-" in raw.lower().replace("debt", "").replace("equity", "").strip():
        return "Equity & Debt"
    return raw.capitalize() if raw.lower() == "equity" else "Debt" if raw.lower() == "debt" else raw


def _parse_one_manager(name: str, role: str, body_text: str) -> dict:
    """Parse qualification + experience from the concatenated body for one manager."""
    qual = ""
    exp_years: Optional[int] = None
    qm = _QUALIFICATION_RE.search(body_text)
    if qm:
        qual = qm.group(1).strip().rstrip(".").rstrip(",").strip()
        qual = re.sub(r"\s+", " ", qual)
    em = _EXP_RE.search(body_text)
    if em:
        try:
            exp_years = int(float(em.group(1)))
        except ValueError:
            exp_years = None
    return {
        "name": name.strip(),
        "role": role,
        "qualification": qual,
        "experience_years": exp_years,
    }


def parse_fund_managers(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Extract fund manager bios into `fund_managers_json`.

    Layout: the "Fund Manager Detail" header is followed by N repetitions of
      <Name>
      FUND MANAGER - <ROLE>
      Qualification: ... Experience: ...

    We scan page-1 spans top-down, detect role-line markers (which are the
    most reliable anchor), and treat the closest preceding non-bio span at
    the same x-column as the name. Body text between role-line and the next
    name (or the "Performance" / "Trailing Returns" sentinel) is the bio.
    """
    spans = _collect_spans(doc)
    # Sort by y for top-down reading.
    spans_sorted = sorted(spans, key=lambda s: (s["y0"], s["x0"]))

    # Locate the "Fund Manager Detail" anchor and the "Performance" /
    # "Trailing Returns" sentinel that ends the block.
    start_y: Optional[float] = None
    end_y: float = float("inf")
    for s in spans_sorted:
        t = s["text"].lower()
        if start_y is None and t == "fund manager detail":
            start_y = s["y0"]
            continue
        if start_y is not None and (
            t.startswith("performance") or t.startswith("trailing returns")
        ):
            end_y = s["y0"]
            break
    if start_y is None:
        raise ValueError("Fund Manager Detail anchor not found")

    block = [s for s in spans_sorted if start_y < s["y0"] < end_y]
    if not block:
        raise ValueError("Fund Manager Detail block is empty")

    # Find role lines (FUND MANAGER - EQUITY etc).
    role_idxs: list[tuple[int, str]] = []
    for i, s in enumerate(block):
        role = _role_for(s["text"])
        if role is not None:
            role_idxs.append((i, role))
    if not role_idxs:
        raise ValueError("no FUND MANAGER role lines found in block")

    managers: list[dict] = []
    for k, (idx, role) in enumerate(role_idxs):
        # Name: closest preceding span (typically the line directly above).
        name = ""
        for j in range(idx - 1, -1, -1):
            cand = block[j]["text"]
            if _role_for(cand) is not None:
                break  # hit previous role line, stop
            if cand.lower().startswith("qualification") or cand.lower().startswith("experience"):
                continue
            if cand.lower() == "fund manager detail":
                break
            # Reject body-like fragments (long lines, sentences with verbs).
            if len(cand) > 80 or "." in cand:
                continue
            name = cand
            break

        # Body: from this role line up to the next role line (or block end).
        body_end = role_idxs[k + 1][0] if k + 1 < len(role_idxs) else len(block)
        body_parts: list[str] = []
        for j in range(idx + 1, body_end):
            t = block[j]["text"]
            # Stop if we run into the next manager's name (heuristic: short
            # capitalized line) — but only if the *next* span is a role line.
            if j + 1 < body_end:
                nxt = block[j + 1]["text"]
                if _role_for(nxt) is not None:
                    # Reaching the next manager's name — exclude it.
                    break
            body_parts.append(t)
        body_text = " ".join(body_parts)
        managers.append(_parse_one_manager(name, role, body_text))

    if not managers:
        raise ValueError("no managers parsed from block")
    snap.fund_managers_json = json.dumps(managers, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Section parser 3.2.3 — trailing returns (fund + benchmark)
# ---------------------------------------------------------------------------

# Normalized header text → Snapshot attribute name (fund side).
_PERIOD_TO_ATTR_FUND = {
    "1 month": "return_1m", "1m": "return_1m",
    "3 months": "return_3m", "3m": "return_3m",
    "6 months": "return_6m", "6m": "return_6m",
    "1 year": "return_1y", "1y": "return_1y",
    "2 years": "return_2y", "2y": "return_2y",
    "3 years": "return_3y", "3y": "return_3y",
    "5 years": "return_5y", "5y": "return_5y",
    "10 years": "return_10y", "10y": "return_10y",
    "since inception": "return_since_inception",
}

# Same map, but for the benchmark row → mirror attr names with _bm suffix.
_PERIOD_TO_ATTR_BM = {k: f"{v}_bm" for k, v in _PERIOD_TO_ATTR_FUND.items()}


def _norm_cell(cell: object) -> str:
    return re.sub(r"\s+", " ", str(cell or "").strip().lower())


def _is_fund_row(first_cell: str) -> bool:
    return _norm_cell(first_cell) == "fund"


def _is_benchmark_row_label(first_cell: str) -> bool:
    """A non-empty row label that is not 'Fund' and not the header — treat as bm.

    The benchmark row's first cell is either the literal benchmark name
    ("NIFTY500 Multicap 50:25:25 Total Return Index", "Nifty 50 Arbitrage TRI")
    or the generic "Category" (DSP Multi Asset uses this).
    """
    s = _norm_cell(first_cell)
    if not s:
        return False
    if s == "fund":
        return False
    if s.startswith("trailing returns"):
        return False
    if s.startswith("performance"):
        return False
    return True


def parse_trailing_returns(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Locate the Trailing Returns table and set all 9 period fields for both rows.

    Iterates every table on every page (Finalyca puts the table on page 1
    but we don't pin to that). Identifies the header row by its first
    cell starting with "trailing returns"; maps subsequent columns to
    Snapshot attribute names via `_PERIOD_TO_ATTR_FUND`. Captures both
    the Fund row and the immediately-following benchmark row.
    """
    found_fund = False
    found_bm = False
    for page_idx, page in enumerate(pl.pages):
        try:
            tables = page.extract_tables()
        except Exception as e:
            logger.debug("trailing returns: page %d extract_tables raised %s", page_idx, e)
            continue
        for t_idx, table in enumerate(tables):
            if not table or len(table) < 2:
                continue
            header_idx: Optional[int] = None
            for r_idx, row in enumerate(table):
                if row and _norm_cell(row[0]).startswith("trailing returns"):
                    header_idx = r_idx
                    break
            if header_idx is None:
                continue

            header = [_norm_cell(c) for c in table[header_idx]]
            # Build a column-index → attr-name map for the fund and benchmark rows.
            fund_col_map: dict[int, str] = {}
            bm_col_map: dict[int, str] = {}
            for i, h in enumerate(header):
                if h in _PERIOD_TO_ATTR_FUND:
                    fund_col_map[i] = _PERIOD_TO_ATTR_FUND[h]
                    bm_col_map[i] = _PERIOD_TO_ATTR_BM[h]
            if not fund_col_map:
                continue

            for r_idx in range(header_idx + 1, len(table)):
                row = table[r_idx]
                if not row:
                    continue
                first = row[0]
                if not found_fund and _is_fund_row(first):
                    for ci, attr in fund_col_map.items():
                        if ci < len(row):
                            setattr(snap, attr, _to_float(row[ci]))
                    found_fund = True
                    continue
                if found_fund and not found_bm and _is_benchmark_row_label(first):
                    for ci, attr in bm_col_map.items():
                        if ci < len(row):
                            setattr(snap, attr, _to_float(row[ci]))
                    found_bm = True
                    break  # done with this table
            if found_fund and found_bm:
                return

    if not found_fund:
        raise ValueError("Trailing Returns 'Fund' row not located")
    # If we found the fund row but not the benchmark, that's degraded but
    # not fatal — log and accept the partial.
    if not found_bm:
        logger.warning("trailing returns: benchmark row not located")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# All non-required section parsers, in execution order. Each must accept
# (fitz_doc, pdfplumber_pdf, snapshot) and mutate the snapshot in place.
SECTION_PARSERS: List[
    Callable[["fitz.Document", "pdfplumber.PDF", Snapshot], None]
] = [
    parse_header,
    parse_fund_managers,
    parse_trailing_returns,
    # ... more sections will be added in subsequent tasks (3.2.4 onwards)
]


# Map fund-type-ish substring → list of section parsers we expect to succeed.
# Used by later phases to distinguish "section legitimately absent for this
# fund type" from "section silently broken." Not enforced yet.
EXPECTED_SECTIONS_BY_FUND_TYPE: dict[str, list[str]] = {
    "equity": [
        "header", "fund_managers", "trailing_returns", "risk_metrics",
        "sector_weights", "top_holdings", "composition", "drawdown",
        "market_cap_composition", "investment_style",
    ],
    "debt": [
        "header", "fund_managers", "trailing_returns", "risk_metrics",
        "composition", "drawdown", "risk_rating",
    ],
    "hybrid": [
        "header", "fund_managers", "trailing_returns", "risk_metrics",
        "sector_weights", "top_holdings", "composition", "drawdown",
        "market_cap_composition", "investment_style", "risk_rating",
    ],
    "arbitrage": [
        "header", "fund_managers", "trailing_returns", "risk_metrics",
        "composition", "drawdown",
    ],
    "multi_asset": [
        "header", "fund_managers", "trailing_returns", "risk_metrics",
        "sector_weights", "top_holdings", "composition", "drawdown",
        "market_cap_composition", "investment_style",
    ],
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def parse_pdf(path: Path) -> Tuple[Snapshot, List[ParseError]]:
    """Parse a Finalyca-template MF PDF.

    Returns (snapshot, errors). The snapshot may be partial — every section
    parser is wrapped in try/except and its failures are recorded in
    `errors` (and serialized into `snap.parse_errors_json`). Only the
    required header info (scheme_name + as_of_date) can abort the parse
    with an exception, because without those two fields the row has no
    PK and cannot be inserted.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    snap = Snapshot()
    errors: List[ParseError] = []

    with fitz.open(str(path)) as fitz_doc, pdfplumber.open(str(path)) as pl_pdf:
        # Always-required identity. Failure here aborts the whole parse.
        try:
            _parse_header_required(fitz_doc, snap)
        except Exception as e:
            raise ValueError(f"Header parse failed (no PK): {e}") from e

        # All other section parsers — never propagate.
        for section_fn in SECTION_PARSERS:
            try:
                section_fn(fitz_doc, pl_pdf, snap)
            except Exception as e:
                errors.append(ParseError(
                    section=section_fn.__name__,
                    error=str(e),
                    traceback=traceback.format_exc(),
                ))
                logger.warning("section %s failed: %s", section_fn.__name__, e)

        # Common metadata — always populated.
        snap.report_month = "2026-05"
        snap.parser_version = config.PARSER_VERSION
        snap.pdf_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        snap.source_pdf_path = str(path)
        snap.revision = 1
        if errors:
            snap.parse_errors_json = json.dumps([
                {"section": e.section, "error": e.error} for e in errors
            ])

    return snap, errors


def parse_pdf_minimal(path: Path) -> Snapshot:
    """DEPRECATED — kept for backward compat with Phase 1 `ingest_one.py`.

    Calls `parse_pdf` and returns just the snapshot (mirroring the Phase 1
    return shape). Use `parse_pdf` directly for the new partial-snapshot-
    with-errors pattern.
    """
    snap, _ = parse_pdf(path)
    return snap
