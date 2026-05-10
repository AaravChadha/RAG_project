"""Bulk-download Finalyca PDFs for one report month.

# TODO(phase-2): ~33 debt-fund schemes are expected later via a separate
# CSV from Bajaj research. They are NOT included in `schemes_master.csv`
# today. When that CSV arrives, the same downloader can be pointed at it
# (or merged into the master) — no logic change should be needed.

Usage:
    python -m ingest.download_pdfs --month 2026-05

Reads `RAG_project/schemes_master.csv` (the same CSV the DB was seeded
from), slugifies the scheme name with the *exact* same rule as
`db.init_db.seed_schemes._slugify`, and downloads each URL to:

    bajaj-mf-bot/data/pdfs/<month>/<scheme_uid>.pdf

Skipped if the target file already exists with non-zero size — re-running
the script is idempotent.

Errors (4xx/5xx HTTP, timeouts, network blips) are logged to
`bajaj-mf-bot/data/download_report_<month>.json` and the loop continues
with the remaining URLs. The summary line at the end is:

    Downloaded N, Skipped M (already present), Failed K. Report saved to <path>.

Politeness: 0.5s sleep between requests; a sane User-Agent string.
Pure stdlib (urllib.request) — no `requests` dependency. The library is
not currently in requirements.txt; we intentionally do not add it.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Allow `python -m ingest.download_pdfs` and `python ingest/download_pdfs.py`.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import config  # noqa: E402

logger = logging.getLogger(__name__)


USER_AGENT = "BajajMFBot/0.1 internal-research-pilot"
REQUEST_TIMEOUT_SECONDS = 30
POLITE_DELAY_SECONDS = 0.5


def _slugify(name: str) -> str:
    """Deterministic slug — must mirror `db.init_db._slugify` exactly.

    Lowercase, non-alphanumeric → '-', collapse dashes, strip leading/
    trailing dashes. Kept duplicated (rather than imported) to keep this
    script's dependency surface tiny — it is the seam that links a CSV
    row to a `schemes.scheme_uid` value, so any drift would silently
    break the downstream ingest lookup.
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _download_one(url: str, dest: Path) -> tuple[bool, Optional[str]]:
    """Download `url` to `dest`. Returns (ok, error_message).

    Writes to a `.part` sibling first then renames, so a crashed download
    never leaves a half-written PDF behind that the "skip if exists" check
    would falsely honour.
    """
    req = Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                return False, f"HTTP {status}"
            data = resp.read()
        if not data:
            return False, "empty response body"
        tmp_path.write_bytes(data)
        tmp_path.rename(dest)
        return True, None
    except HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except URLError as e:
        return False, f"URLError: {e.reason}"
    except TimeoutError as e:
        return False, f"Timeout: {e}"
    except Exception as e:  # noqa: BLE001 — keep loop running on every error class
        return False, f"{type(e).__name__}: {e}"
    finally:
        # Clean up any half-written temp file.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def download_all(
    csv_path: Path, out_dir: Path, report_path: Path, month: str,
) -> dict:
    """Run the full download loop. Returns the report dict."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = 0
    entries: list[dict] = []

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    total = len(rows)
    logger.info("download_pdfs: %d rows to process for month=%s", total, month)

    for idx, row in enumerate(rows, start=1):
        scheme_name = (row.get("scheme") or "").strip()
        url = (row.get("url") or "").strip()
        category = (row.get("category") or "").strip()

        if not scheme_name or not url:
            logger.warning(
                "row %d: missing scheme or url (%r / %r) — skipping",
                idx, scheme_name, url,
            )
            entries.append({
                "scheme_uid": None,
                "scheme_name": scheme_name,
                "category": category,
                "url": url,
                "outcome": "failed",
                "error": "missing scheme or url",
            })
            failed += 1
            continue

        scheme_uid = _slugify(scheme_name)
        dest = out_dir / f"{scheme_uid}.pdf"

        if dest.exists() and dest.stat().st_size > 0:
            logger.info("[%d/%d] skip (already present): %s", idx, total, dest.name)
            skipped += 1
            entries.append({
                "scheme_uid": scheme_uid,
                "scheme_name": scheme_name,
                "category": category,
                "url": url,
                "outcome": "skipped",
                "size_bytes": dest.stat().st_size,
            })
            continue

        logger.info("[%d/%d] downloading %s -> %s", idx, total, scheme_name, dest.name)
        ok, err = _download_one(url, dest)
        if ok:
            downloaded += 1
            entries.append({
                "scheme_uid": scheme_uid,
                "scheme_name": scheme_name,
                "category": category,
                "url": url,
                "outcome": "downloaded",
                "size_bytes": dest.stat().st_size,
            })
        else:
            failed += 1
            logger.warning("[%d/%d] FAILED %s: %s", idx, total, scheme_name, err)
            entries.append({
                "scheme_uid": scheme_uid,
                "scheme_name": scheme_name,
                "category": category,
                "url": url,
                "outcome": "failed",
                "error": err,
            })

        # Be polite — only sleep between *actual* network calls.
        if idx < total:
            time.sleep(POLITE_DELAY_SECONDS)

    report = {
        "month": month,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "csv_source": str(csv_path),
        "out_dir": str(out_dir),
        "totals": {
            "rows": total,
            "downloaded": downloaded,
            "skipped": skipped,
            "failed": failed,
        },
        "entries": entries,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-download Finalyca PDFs for a report month.",
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Report month in YYYY-MM form, e.g. 2026-05.",
    )
    parser.add_argument(
        "--csv",
        default=str(config.SCHEMES_CSV),
        help="Path to schemes_master.csv (default: config.SCHEMES_CSV).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    csv_path = Path(args.csv)
    out_dir = Path(config.PDF_ROOT) / args.month
    report_path = (
        Path(config.PDF_ROOT).parent / f"download_report_{args.month}.json"
    )

    report = download_all(csv_path, out_dir, report_path, args.month)
    t = report["totals"]
    print(
        f"Downloaded {t['downloaded']}, Skipped {t['skipped']} "
        f"(already present), Failed {t['failed']}. Report saved to "
        f"{report_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
