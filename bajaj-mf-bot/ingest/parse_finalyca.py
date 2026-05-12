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

# Roles we recognize as a manager-role line. The Finalyca template most
# often emits "FUND MANAGER - EQUITY" (upper-case, role suffix at end),
# but actual prefixes drift wildly per AMC: "Senior Fund Manager",
# "Assistant Fund Mananger" (sic — Edelweiss has the typo),
# "Chief Dealer - Equities", "Head-Equity and Fund Manager",
# "Fund Management and Investment analyst", "Research Analyst",
# "Associate Vice President - Fund Management", "Fund Manager & Analyst",
# etc. We anchor on the *end* of the line — ` - <ROLE>` where ROLE is in
# a known set — and accept any short prefix. The 80-char cap keeps us
# from accidentally swallowing paragraph endings that happen to end with
# the word "EQUITY".
_ROLE_RE = re.compile(
    r"^.{0,80}[-–]\s*("
    r"Equity\s*&\s*Debt|"
    r"Equity-Debt|"
    r"Foreign\s*Inv(?:\.|estment|estments)?|"
    r"Equity|"
    r"Debt"
    r")\s*\.?\s*$",
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
    raw = m.group(1).strip().lower()
    # Normalize "Equity & Debt" / "Equity-Debt" variants.
    if "&" in raw or "-" in raw.replace("debt", "").replace("equity", "").strip():
        return "Equity & Debt"
    if raw == "equity":
        return "Equity"
    if raw == "debt":
        return "Debt"
    if raw.startswith("foreign"):
        return "Foreign Investment"
    return raw.capitalize()


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
# Page-2 layout helpers — shared by risk metrics / sector weights / top holdings
# ---------------------------------------------------------------------------
#
# In the Finalyca template, page index 1 ("page 2 of N" in human-readable
# numbering) packs three side-by-side blocks across the top half of the page:
#
#   x ≈ 24 – 200    Risk Analysis (label + 1Y col + 3Y col)
#   x ≈ 211 – 380   Sector Wts(%)        (multi-word sector + numeric weight)
#   x ≈ 397 – 580   Top Holdings         (multi-word security + numeric weight)
#
# `extract_tables()` does NOT recover these cleanly — the columns share no
# ruling lines and the row pitch differs between blocks (risk rows are every
# ~18pt, sector rows every ~18pt, holdings rows alternate ~9pt). Word-level
# extraction with manual row-grouping is much more reliable.

# Column boundaries (left edge, right edge). Slightly wider than the
# observed extremes to absorb minor x drift across funds.
_PAGE2_RISK_X = (15.0, 205.0)
_PAGE2_SECTOR_X = (205.0, 392.0)
_PAGE2_HOLDINGS_X = (392.0, 600.0)

# Inside the risk block the 1Y and 3Y values land at fixed x bands.
_RISK_1Y_X = (115.0, 175.0)
_RISK_3Y_X = (175.0, 205.0)


def _page2_words(pl: "pdfplumber.PDF") -> list[dict]:
    """Return all word tokens from page index 1 (the page that hosts the
    risk-metrics / sector-weights / top-holdings band).

    Returns an empty list if the PDF has fewer than 2 pages.
    """
    if len(pl.pages) < 2:
        return []
    try:
        return pl.pages[1].extract_words()
    except Exception as e:  # pragma: no cover — pdfplumber occasionally raises
        logger.debug("page2 extract_words raised: %s", e)
        return []


def _words_with_anchor(
    pl: "pdfplumber.PDF",
    anchor: str,
    candidate_pages: tuple[int, ...] = (1, 2),
) -> list[dict]:
    """Return words from whichever candidate page contains the given anchor.

    Some sections (drawdown, risk_rating, mkt cap composition, etc.) live on
    page 2 in the typical Finalyca layout but spill to page 3 for funds whose
    earlier blocks (fund-manager bios, sector weights with many sectors, etc.)
    push them past the page boundary. Pages are searched in order; the first
    page containing `anchor` (case-insensitive substring match on any word's
    text) wins. Falls back to empty list if no candidate page has the anchor.
    """
    anchor_lc = anchor.lower()
    for page_idx in candidate_pages:
        if page_idx >= len(pl.pages):
            continue
        try:
            words = pl.pages[page_idx].extract_words()
        except Exception as e:  # pragma: no cover
            logger.debug("page %d extract_words raised: %s", page_idx, e)
            continue
        if any(anchor_lc in (w.get("text") or "").lower() for w in words):
            return words
    return []


def _group_rows(words: list[dict], y_tol: float = 3.5) -> list[list[dict]]:
    """Cluster words into rows by `top` y-coordinate proximity.

    Returns rows sorted top-to-bottom; each row's words are sorted left-to-right.
    """
    rows: list[tuple[float, list[dict]]] = []
    for w in words:
        y = float(w["top"])
        placed = False
        for r in rows:
            if abs(r[0] - y) < y_tol:
                r[1].append(w)
                placed = True
                break
        if not placed:
            rows.append((y, [w]))
    rows.sort(key=lambda r: r[0])
    return [sorted(ws, key=lambda w: w["x0"]) for _y, ws in rows]


def _row_y(row: list[dict]) -> float:
    return min(float(w["top"]) for w in row) if row else 0.0


def _filter_by_x(words: list[dict], x_range: tuple[float, float]) -> list[dict]:
    lo, hi = x_range
    return [w for w in words if lo <= float(w["x0"]) < hi]


# ---------------------------------------------------------------------------
# Section parser 3.2.4 — risk metrics (1Y + 3Y blocks)
# ---------------------------------------------------------------------------

# Label substring (lower-cased) → Snapshot attribute stem (suffix _1y / _3y
# appended later). We match by substring against the joined row label text
# so multi-word labels like "Standard Deviation" or "Up Capture Ratio" survive
# whatever spacing the PDF uses.
_RISK_LABEL_TO_STEM: tuple[tuple[str, str], ...] = (
    ("standard deviation", "std_dev"),
    ("sharpe ratio", "sharpe"),
    ("sharpe", "sharpe"),
    ("r-square", "r_square"),
    ("r square", "r_square"),
    ("treynor", "treynor"),
    ("information ratio", "info_ratio"),
    ("up capture", "up_capture"),
    ("down capture", "down_capture"),
    ("tracking error", "tracking_error"),
    ("sortino", "sortino"),
    ("beta", "beta"),  # short label — keep last so it doesn't shadow longer matches
)


def _match_risk_stem(label_text: str) -> Optional[str]:
    lt = label_text.lower().strip()
    if not lt:
        return None
    for needle, stem in _RISK_LABEL_TO_STEM:
        if needle in lt:
            return stem
    return None


def parse_risk_metrics(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate the 20 risk-metric fields (10 × 1Y + 10 × 3Y).

    Reads page index 1, restricts to the risk-block x band, and walks each
    row top-down. The first all-text portion of the row is the metric label;
    the next 1 or 2 numeric tokens are the 1Y and 3Y values respectively.

    Funds younger than 3 years legitimately have "NA" for every 3Y column —
    `_to_float` returns None for that, which we accept without erroring.
    """
    words = _page2_words(pl)
    if not words:
        raise ValueError("page 2 has no extractable words (risk metrics)")

    block_words = _filter_by_x(words, _PAGE2_RISK_X)
    rows = _group_rows(block_words)
    if not rows:
        raise ValueError("risk-metrics block: no rows found")

    matched_any = False
    for row in rows:
        # Split row into (label tokens, value tokens) by x position. Values
        # live to the right of x≈115 (1Y col onward).
        label_tokens = [w["text"] for w in row if float(w["x0"]) < _RISK_1Y_X[0]]
        v1y_tokens = [w["text"] for w in row
                      if _RISK_1Y_X[0] <= float(w["x0"]) < _RISK_1Y_X[1]]
        v3y_tokens = [w["text"] for w in row
                      if _RISK_3Y_X[0] <= float(w["x0"]) < _RISK_3Y_X[1]]
        if not label_tokens or (not v1y_tokens and not v3y_tokens):
            continue
        label_text = " ".join(label_tokens)
        stem = _match_risk_stem(label_text)
        if stem is None:
            continue
        # Skip duplicates — only first match for each stem wins (just in case
        # the heading "Risk Analysis" sneaks in).
        attr_1y = f"{stem}_1y"
        attr_3y = f"{stem}_3y"
        if v1y_tokens and getattr(snap, attr_1y) is None:
            setattr(snap, attr_1y, _to_float(v1y_tokens[0]))
            matched_any = True
        if v3y_tokens and getattr(snap, attr_3y) is None:
            setattr(snap, attr_3y, _to_float(v3y_tokens[0]))
            matched_any = True

    if not matched_any:
        raise ValueError("risk-metrics block: no labels matched")


# ---------------------------------------------------------------------------
# Section parser 3.2.5 — sector weights
# ---------------------------------------------------------------------------

# Heading-noise tokens we strip when joining sector names.
_SECTOR_HEADING_TOKENS = {"sector", "wts(%)", "wts", "(%)"}

# A sector row is "<one or more text words> <one numeric weight>". We treat
# the last token as the weight if it parses as a float; the rest is the
# sector name.
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# After the actual sector list ends, the same x-band on page 2 hosts the
# Risk Rating, Mkt Cap Composition, and "Increase in Exposure" blocks —
# all of which look like "<text> <number>" rows and would otherwise be
# misclassified as sectors. We stop scanning the moment we see any of
# these row-leading tokens (lower-cased, first non-empty token). "risk"
# specifically catches the "Risk Rating %" header row that introduces the
# credit-rating block — a single stop there is more robust than enumerating
# every credit grade Finalyca uses (AAA / AA+ / Aa / Aa- / A+ / Aaa(So) / …).
_SECTOR_STOP_FIRST_TOKEN = {
    "risk",                                               # "Risk Rating %" — credit rating block header
    "equity", "debt", "cash", "derivative", "alternate",  # composition (rarely bleeds in)
    "large", "mid", "small",                              # mkt-cap composition
    "unrated", "sovereign", "aaa", "aa+", "a1+",          # legacy credit-rating safety net
    "net",                                                # "Net Ca & O"
}


def parse_sector_weights(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate `snap.sector_weights` as List[{sector, weight_pct}].

    Equity funds typically list 10–20 sectors; arbitrage / pure-debt may
    show 0 or only "Others". If the block contains no parseable rows we
    leave `snap.sector_weights = None` (do not raise) — caller treats that
    as "section legitimately absent for this fund type."

    The page-2 sector x-band [205, 392] is also occupied (above and below
    the actual sector list) by other Finalyca blocks: rolling-returns
    chart values up top, then Risk Rating / Mkt Cap Composition below.
    We anchor scanning to the literal "Sector Wts(%)" header — rows before
    it are ignored, and we stop at the first row whose leading token signals
    the next block.
    """
    words = _page2_words(pl)
    if not words:
        return
    block_words = _filter_by_x(words, _PAGE2_SECTOR_X)
    if not block_words:
        return
    rows = _group_rows(block_words)

    sectors: list[dict] = []
    seen_header = False
    for row in rows:
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        lc_tokens = [t.lower() for t in tokens]
        # Anchor to the "Sector Wts(%)" header — anything above it is
        # noise from the rolling-returns / chart row that also lives in
        # this x-band on some fund layouts (Multi Asset, Parag Parikh, etc.).
        if all(t in _SECTOR_HEADING_TOKENS for t in lc_tokens):
            seen_header = True
            continue
        if not seen_header:
            continue
        # Stop the moment we cross into the next block (Risk Rating / Mkt
        # Cap Composition / Composition) — same x-band, different semantics.
        first = lc_tokens[0]
        if first in _SECTOR_STOP_FIRST_TOKEN:
            break
        # The last token must be numeric to be a sector row.
        last = tokens[-1]
        if not _NUMERIC_RE.match(last):
            continue
        weight = _to_float(last)
        if weight is None:
            continue
        sector_name = " ".join(tokens[:-1]).strip()
        if not sector_name:
            continue
        sectors.append({"sector": sector_name, "weight_pct": weight})

    if sectors:
        snap.sector_weights = sectors


# ---------------------------------------------------------------------------
# Section parser 3.2.6 — top 10 holdings
# ---------------------------------------------------------------------------

# Header line carries an "As On: 31 Mar 2026" date for the holdings snapshot.
_HOLDINGS_AS_ON_RE = re.compile(
    r"As\s*On\s*[:\-]?\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{2,4})",
    re.IGNORECASE,
)


def _holdings_as_on(doc: "fitz.Document") -> Optional[date]:
    """Extract the "As On: <date>" embedded in the Top Holdings header on page 2."""
    if len(doc) < 2:
        return None
    text = doc[1].get_text("text") or ""
    # The header may wrap: "Top Holdings (As On: 31 Mar\n2026)" — collapse newlines.
    flat = re.sub(r"\s+", " ", text)
    # Find the segment that follows "Top Holdings".
    idx = flat.lower().find("top holdings")
    if idx < 0:
        return None
    window = flat[idx: idx + 200]
    m = _HOLDINGS_AS_ON_RE.search(window)
    if not m:
        return None
    return _parse_date_flex(m.group(1))


# Section-end sentinels in the holdings column. These can appear if the
# fund has fewer than 10 holdings shown (rare) and the column flows into
# "Composition" or "Risk Rating" labels.
_HOLDINGS_STOPS = {"composition", "risk", "rating", "drawdown"}


def parse_top_holdings(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate `snap.top_holdings` as List[{security_name, weight_pct, as_of_date}].

    The "Top Holdings" block on page 2 lists 10 (occasionally fewer)
    securities with a weight%. Long security names wrap to 2–3 visual
    rows (e.g. "Aditya Birla Sl Money Manager / Fund - Dir (G)") with the
    weight rendered on the *middle* row. We sidestep the row-clustering
    fragility by streaming all words in the holdings x-band top-to-bottom
    and emitting one holding each time a numeric token is hit; preceding
    text-only tokens (within a vertical window) form the name.

    The block's own "As On" date is captured separately — it can lag the
    snapshot's overall as_of_date by a month (month-end portfolio cut vs.
    report-publication-day).
    """
    words = _page2_words(pl)
    if not words:
        raise ValueError("page 2 has no extractable words (top holdings)")
    block_words = _filter_by_x(words, _PAGE2_HOLDINGS_X)
    if not block_words:
        raise ValueError("top-holdings block: no words in column band")

    as_on_date = _holdings_as_on(doc) or snap.as_of_date
    as_on_iso = as_on_date.isoformat() if as_on_date else None

    # Drop the header tokens before the first data row. The "Top Holdings (As
    # On: 31 Mar 2026)" title lives at y ≲ 55 on every fund we've seen.
    data_words = [w for w in block_words if float(w["top"]) > 55.0]
    # Cut off at the next-block boundary (Composition / Risk Rating etc).
    cutoff_y: Optional[float] = None
    for w in sorted(data_words, key=lambda w: float(w["top"])):
        if w["text"].lower() in _HOLDINGS_STOPS:
            cutoff_y = float(w["top"])
            break
    if cutoff_y is not None:
        data_words = [w for w in data_words if float(w["top"]) < cutoff_y]

    # Anchor each holding on its numeric weight token: the weight's y
    # is the centroid of the visual row, and the security name is every
    # text token whose y lies within ±8pt of that centroid. This sidesteps
    # the ordering ambiguity where a wrapped name's second line is rendered
    # BELOW the weight (e.g. ABSL: "Aditya Birla Sl Money Manager" / 11.35 /
    # "Fund - Dir (G)").
    weight_tokens = [w for w in data_words if _NUMERIC_RE.match(w["text"])]
    weight_tokens.sort(key=lambda w: float(w["top"]))

    # Boundaries between adjacent holdings — midpoints of consecutive
    # weight y-positions. Text tokens between two boundaries belong to
    # whichever weight's anchor is closer (i.e. their bucket).
    weight_ys = [float(w["top"]) for w in weight_tokens]

    def _bucket_for(y: float) -> Optional[int]:
        """Return the index of the closest weight anchor, or None if no
        weight is within ~12pt (looser names should still snap)."""
        if not weight_ys:
            return None
        best_i = 0
        best_d = abs(y - weight_ys[0])
        for i, wy in enumerate(weight_ys[1:], start=1):
            d = abs(y - wy)
            if d < best_d:
                best_d = d
                best_i = i
        # Reject if extremely far from any weight (12pt = ~2/3 of a row pitch).
        if best_d > 12.0:
            return None
        return best_i

    name_parts: list[list[tuple[float, str]]] = [[] for _ in weight_tokens]
    for w in data_words:
        text = w["text"]
        if _NUMERIC_RE.match(text):
            continue
        y = float(w["top"])
        b = _bucket_for(y)
        if b is None:
            continue
        name_parts[b].append((y, text))

    holdings: list[dict] = []
    for wt, parts in zip(weight_tokens, name_parts):
        if not parts:
            continue
        # Order parts by their actual y (so wrapped lines stay top-to-bottom)
        # then by x within a line.
        parts.sort(key=lambda t: t[0])
        name = " ".join(t for _y, t in parts).strip()
        if not name:
            continue
        weight = _to_float(wt["text"])
        if weight is None:
            continue
        holdings.append({
            "security_name": name,
            "weight_pct": weight,
            "as_of_date": as_on_iso,
        })
        if len(holdings) >= 10:
            break

    if holdings:
        snap.top_holdings = holdings
    else:
        raise ValueError("top-holdings block: parsed zero rows")


# ---------------------------------------------------------------------------
# Section parser 3.2.7 — portfolio characteristics
# ---------------------------------------------------------------------------
#
# The Portfolio Characteristics block lives in the page-2 left x-band
# (x ≈ 24–205) directly below Risk Analysis / above Composition. Six rows
# of "<label> <numeric>" each (Total Securities is INT; the rest REAL):
#
#   Total Securities       101.00
#   Avg Mkt Cap (Cr)       286729.50
#   Median Mkt Cap (Cr)    51489.40
#   Portfolio P/E Ratio    28.98
#   Portfolio P/B Ratio    4.27
#   Portfolio Dividend Yield 0.39
#   Modified Duration      0E-9       ← pure equity; arbitrage/hybrid ≠ 0

# Label substring (lower-cased, joined row text) → (attr, is_int).
_PORTFOLIO_CHAR_LABELS: tuple[tuple[str, str, bool], ...] = (
    ("total securities", "total_securities", True),
    ("avg mkt cap", "avg_mkt_cap_cr", False),
    ("average mkt cap", "avg_mkt_cap_cr", False),
    ("median mkt cap", "median_mkt_cap_cr", False),
    ("median market cap", "median_mkt_cap_cr", False),
    ("portfolio p/e", "portfolio_pe", False),
    ("p/e ratio", "portfolio_pe", False),
    ("portfolio p/b", "portfolio_pb", False),
    ("p/b ratio", "portfolio_pb", False),
    ("portfolio dividend yield", "portfolio_div_yield", False),
    ("dividend yield", "portfolio_div_yield", False),
    ("div yield", "portfolio_div_yield", False),
    ("modified duration", "modified_duration", False),
    ("mod duration", "modified_duration", False),
)


def _find_left_band_anchor(
    words: list[dict], label_lc: str, x_max: float = 205.0,
) -> Optional[float]:
    """Return the y of the first occurrence of `label_lc` in the page-2 left band.

    Matches by joining all words on each row (within `_PAGE2_RISK_X`) and
    checking the lowered text contains `label_lc`. Returns the row's top y,
    or None if no row matches.
    """
    band = [w for w in words if float(w["x0"]) < x_max]
    rows = _group_rows(band)
    for row in rows:
        joined = " ".join(w["text"] for w in row).lower()
        if label_lc in joined:
            return _row_y(row)
    return None


def parse_portfolio_characteristics(
    doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot,
) -> None:
    """Populate the 7 Portfolio Characteristics fields on `snap`.

    Page 2 left x-band, between the "Portfolio Characteristics" header and
    the "Composition" header. Each row is "<label words> <numeric value>"
    with the value as the last token; `Total Securities` is the only int.

    Pure-equity funds report Modified Duration as `0E-9` — `_to_float`
    already returns 0.0 for that token, so we don't need special handling
    here. Per task spec, that is an acceptable value (not a parser failure).
    """
    words = _page2_words(pl)
    if not words:
        raise ValueError("page 2 has no extractable words (portfolio characteristics)")

    start_y = _find_left_band_anchor(words, "portfolio characteristics")
    if start_y is None:
        raise ValueError("Portfolio Characteristics header not found")
    # End at the Composition header (anchors next block) or fall through.
    end_y = _find_left_band_anchor(words, "composition") or float("inf")
    if end_y <= start_y:
        end_y = float("inf")

    band = _filter_by_x(words, _PAGE2_RISK_X)
    block_words = [w for w in band if start_y < float(w["top"]) < end_y]
    rows = _group_rows(block_words)

    matched_any = False
    for row in rows:
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        joined_lc = " ".join(tokens).lower()
        # Skip the header row itself.
        if "portfolio characteristics" in joined_lc:
            continue
        # The numeric value is the last token; everything before it is the label.
        last = tokens[-1]
        if not _NUMERIC_RE.match(last) and last.upper() not in {"NA", "0E-9", "N/A", "-"}:
            continue
        label_lc = " ".join(tokens[:-1]).lower().strip()
        if not label_lc:
            continue
        for needle, attr, is_int in _PORTFOLIO_CHAR_LABELS:
            if needle in label_lc:
                if getattr(snap, attr) is not None:
                    continue  # already set — first match wins
                value = _to_int(last) if is_int else _to_float(last)
                setattr(snap, attr, value)
                matched_any = True
                break

    if not matched_any:
        raise ValueError("portfolio characteristics: no labels matched")


# ---------------------------------------------------------------------------
# Section parser 3.2.8 — composition
# ---------------------------------------------------------------------------
#
# The Composition block sits between "Composition" and "Drawdown" on page 2,
# left x-band. Each row is "<asset class> <pct>" with verbatim labels:
#
#   Equity   95.93     # equity-only funds: just Equity + Cash
#   Cash      4.07
#   Debt     37.20    # arbitrage adds Debt; Derivative may be negative
#   Derivative -0.41
#   Alternate 14.28   # multi-asset adds Alternate (Gold/Silver), Others
#   Others    10.79

# Tokens that look like asset labels we should drop if they sneak in (header).
_COMPOSITION_HEADER_TOKENS = {"composition", "wts(%)", "wts", "(%)"}


def parse_composition(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate `snap.composition_json` with the asset-class breakdown.

    Stores as JSON ({"Equity": 95.93, "Cash": 4.07, ...}). Component
    labels are captured verbatim (no canonicalization). Negative values
    are real for arbitrage funds (derivatives margin) and preserved.
    """
    words = _page2_words(pl)
    if not words:
        raise ValueError("page 2 has no extractable words (composition)")

    start_y = _find_left_band_anchor(words, "composition")
    if start_y is None:
        raise ValueError("Composition header not found")
    end_y = _find_left_band_anchor(words, "drawdown") or float("inf")
    if end_y <= start_y:
        end_y = float("inf")

    band = _filter_by_x(words, _PAGE2_RISK_X)
    block_words = [w for w in band if start_y < float(w["top"]) < end_y]
    rows = _group_rows(block_words)

    composition: dict[str, float] = {}
    for row in rows:
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        lc_tokens = [t.lower() for t in tokens]
        # Skip the header row and the "Wts(%)" column header.
        if all(t in _COMPOSITION_HEADER_TOKENS for t in lc_tokens):
            continue
        last = tokens[-1]
        if not _NUMERIC_RE.match(last):
            continue
        value = _to_float(last)
        if value is None:
            continue
        label = " ".join(tokens[:-1]).strip()
        if not label:
            continue
        if label.lower() in _COMPOSITION_HEADER_TOKENS:
            continue
        composition[label] = value

    if not composition:
        raise ValueError("composition: parsed zero rows")
    snap.composition_json = json.dumps(composition, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Section parser 3.2.9 — drawdown
# ---------------------------------------------------------------------------
#
# Drawdown Analysis is a small 6×2 grid (label | 1Y | 3Y) in the page-2
# left band. We populate the 1Y column (the 3Y column is often NA for
# young funds and we don't persist it). Rows:
#
#   Draw Down (%)     -14.58   NA
#   Duration Days      80      NA
#   Time To Recovery   NA      NA
#   Peak Date         02 Jan 2026   NA
#   Valley Date       23 Mar 2026   NA
#   Recovery Date     NA       NA   ← may be "Not Yet Recovered"

# 1Y column lives at x ≈ 100–175; 3Y at x ≈ 175–205.
_DRAWDOWN_1Y_X = (95.0, 150.0)


def parse_drawdown(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate the 5 Drawdown fields on `snap` (pct, duration, 3 dates).

    Reads only the 1Y column. If a date cell is NA / "Not Yet Recovered" /
    "-" / empty, leaves the corresponding field as None (not a failure).

    The Drawdown block sits at the bottom of page 2 in the typical Finalyca
    layout, but for funds whose page-2 content runs long (extra fund managers,
    longer overview, more sector rows) it spills onto page 3. We try page 2
    first, then page 3.
    """
    words = _words_with_anchor(pl, "drawdown", candidate_pages=(1, 2))
    if not words:
        raise ValueError("Drawdown header not found")

    start_y = _find_left_band_anchor(words, "drawdown")
    if start_y is None:
        raise ValueError("Drawdown header not found")
    # End at the "Increase in Exposure" recent-activity block (always below).
    end_y = _find_left_band_anchor(words, "increase in exposure") or float("inf")
    if end_y <= start_y:
        end_y = float("inf")

    band = _filter_by_x(words, _PAGE2_RISK_X)
    block_words = [w for w in band if start_y < float(w["top"]) < end_y]
    rows = _group_rows(block_words)

    for row in rows:
        # Split row into label tokens (x < 1Y col) and 1Y-col tokens.
        label_tokens = [w["text"] for w in row if float(w["x0"]) < _DRAWDOWN_1Y_X[0]]
        v1y_tokens = [w["text"] for w in row
                      if _DRAWDOWN_1Y_X[0] <= float(w["x0"]) < _DRAWDOWN_1Y_X[1]]
        if not label_tokens or not v1y_tokens:
            continue
        label_lc = " ".join(label_tokens).lower()
        value_text = " ".join(v1y_tokens).strip()
        if "draw down" in label_lc and "%" in label_lc:
            snap.drawdown_pct = _to_float(value_text)
        elif "duration" in label_lc and "days" in label_lc:
            snap.drawdown_duration_days = _to_int(value_text)
        elif "peak date" in label_lc:
            snap.drawdown_peak_date = _parse_date_flex(value_text)
        elif "valley date" in label_lc:
            snap.drawdown_valley_date = _parse_date_flex(value_text)
        elif "recovery date" in label_lc:
            # "Not Yet Recovered" / "NA" → None (not a failure).
            if value_text.upper() in _NA_TOKENS or "not yet" in value_text.lower():
                snap.drawdown_recovery_date = None
            else:
                snap.drawdown_recovery_date = _parse_date_flex(value_text)
        # "Time To Recovery" row is ignored — not persisted on Snapshot.


# ---------------------------------------------------------------------------
# Section parser 3.2.10 — risk rating
# ---------------------------------------------------------------------------
#
# Risk Rating lives in the middle x-band (≈ 211-300 for label, 274-300 for
# value) on page 2. Rows are "<rating label> <pct>". Equity-only funds show
# just {Equity, Unrated, Net Ca & O}; arbitrage/hybrid funds add credit
# ratings (A1+, AAA, AA+, Sovereign, etc.). We capture every row in the
# block verbatim into a dict and JSON-dump it.

# Risk-Rating block x-band: label & value share this column (≈ 211-300).
_RISK_RATING_X = (205.0, 305.0)

# Stop tokens (first token of the row, lower-cased) that signal we've left
# the risk-rating block and entered Mkt Cap Composition (which uses the
# same x-band sometimes) or the recent-activity table.
_RISK_RATING_STOP_FIRST_TOKEN = {
    "decrease",  # "Decrease in Exposure" header below
    "increase",
}


def parse_risk_rating(doc: "fitz.Document", pl: "pdfplumber.PDF", snap: Snapshot) -> None:
    """Populate `snap.risk_rating_json` with the credit-rating / equity breakdown.

    JSON dict of label → pct. Labels are verbatim from the PDF (so equity
    funds end up with {"Equity": 95.91, ...}; arbitrage funds add A1+/AAA/
    etc). If the block is genuinely missing or empty we leave the field
    as None (caller treats that as acceptable).
    """
    words = _page2_words(pl)
    if not words:
        return  # acceptable miss

    # Anchor: the "Risk Rating" header in the middle x-band.
    band = _filter_by_x(words, _RISK_RATING_X)
    rows = _group_rows(band)
    start_y: Optional[float] = None
    for row in rows:
        joined = " ".join(w["text"] for w in row).lower()
        if "risk rating" in joined:
            start_y = _row_y(row)
            break
    if start_y is None:
        return  # acceptable miss

    rating: dict[str, float] = {}
    for row in rows:
        ry = _row_y(row)
        if ry <= start_y:
            continue
        tokens = [w["text"] for w in row]
        if not tokens:
            continue
        first_lc = tokens[0].lower()
        if first_lc in _RISK_RATING_STOP_FIRST_TOKEN:
            break
        last = tokens[-1]
        if not _NUMERIC_RE.match(last):
            continue
        value = _to_float(last)
        if value is None:
            continue
        label = " ".join(tokens[:-1]).strip()
        if not label or label.lower() in {"risk rating", "%"}:
            continue
        rating[label] = value

    if rating:
        snap.risk_rating_json = json.dumps(rating, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Section parsers 3.2.11 – 3.2.14
# ---------------------------------------------------------------------------
#
# Factored into `ingest/_section_parsers.py` to keep this file under the
# 1700-line guidance from the Phase 3 task spec. The four parsers below
# share helpers (`_to_float`, `_parse_date_flex`, page-2 word helpers)
# with the parsers above, which they import lazily at module load time.
#
# The dispatch list below references them by reference so callers see no
# difference vs. the in-file parsers.

from ingest._section_parsers import (  # noqa: E402
    parse_market_cap_composition,
    parse_investment_style,
    parse_periodic_returns,
    parse_holdings_full,
)
from ingest.invariants import run_all_and_capture  # noqa: E402



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
    parse_risk_metrics,
    parse_sector_weights,
    parse_top_holdings,
    parse_portfolio_characteristics,
    parse_composition,
    parse_drawdown,
    parse_risk_rating,
    parse_market_cap_composition,
    parse_investment_style,
    parse_periodic_returns,
    parse_holdings_full,
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

        # Invariant checks (Phase 3.3). Failures are informational warnings
        # — appended to parse_errors_json under the "invariant:<name>" tag
        # but never raised; the row still gets inserted.
        _ok, invariant_failures = run_all_and_capture(snap)
        invariant_records: list[dict] = []
        for failure in invariant_failures:
            check_name, _, detail = failure.partition(": ")
            invariant_records.append({
                "section": f"invariant:{check_name}",
                "error": detail.strip() or failure,
            })

        if errors or invariant_records:
            payload = [
                {"section": e.section, "error": e.error} for e in errors
            ] + invariant_records
            snap.parse_errors_json = json.dumps(payload)

    return snap, errors


def parse_pdf_minimal(path: Path) -> Snapshot:
    """DEPRECATED — kept for backward compat with Phase 1 `ingest_one.py`.

    Calls `parse_pdf` and returns just the snapshot (mirroring the Phase 1
    return shape). Use `parse_pdf` directly for the new partial-snapshot-
    with-errors pattern.
    """
    snap, _ = parse_pdf(path)
    return snap
