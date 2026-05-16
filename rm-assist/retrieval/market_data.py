"""Live Indian-market state via Yahoo Finance, for market-timing answers.

The chatbot's structured DB covers fund snapshots but has no view of the
broader market. This module fetches index levels and recent moves (1d/5d/
1m/3m/6m/1y + distance from 52w high/low) so the bot can answer the
market-timing questions in real RM input (2026-05-15): "is this the right
time to invest?", "should I redeem during this fall?", "which sector now?".

Data source: ``yfinance`` (free, no key). Tickers are the headline Indian
indices on Yahoo Finance.

Public surface:

* ``get_market_state(indices=None) -> dict`` — returns a JSON-serializable
  dict with one entry per requested index. Defaults to NIFTY 50 + Sensex +
  NIFTY 500.

Cache: in-memory, 15-min TTL per ticker. Each Streamlit process keeps its
own cache, which is fine at pilot scale (few concurrent users). When we
move to a multi-process host, replace with a SQLite-backed cache.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_DEFAULT_INDICES: Dict[str, str] = {
    "NIFTY 50": "^NSEI",
    "Sensex": "^BSESN",
    "NIFTY 500": "^CRSLDX",
}


_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS: int = 900


def _fetch_one(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch level + recent moves for one ticker, using the cache when fresh."""
    now = time.time()
    cached = _CACHE.get(ticker)
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        import yfinance as yf  # noqa: WPS433 — lazy import, optional dep
        t = yf.Ticker(ticker)
        hist = t.history(period="1y", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 — yfinance can raise anything
        logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return None

    if hist is None or hist.empty or "Close" not in hist.columns:
        logger.warning("yfinance returned empty data for %s", ticker)
        return None

    closes = hist["Close"]
    current = float(closes.iloc[-1])

    def _pct_change(bars_back: int) -> Optional[float]:
        if len(closes) <= bars_back:
            return None
        prior = float(closes.iloc[-1 - bars_back])
        if prior == 0:
            return None
        return round(((current - prior) / prior) * 100, 2)

    year_high = float(closes.max())
    year_low = float(closes.min())

    payload: Dict[str, Any] = {
        "current_level": round(current, 2),
        "change_1d_pct": _pct_change(1),
        "change_5d_pct": _pct_change(5),
        "change_1m_pct": _pct_change(21),
        "change_3m_pct": _pct_change(63),
        "change_6m_pct": _pct_change(126),
        "change_1y_pct": _pct_change(252),
        "year_high": round(year_high, 2),
        "year_low": round(year_low, 2),
        "pct_off_52w_high": round(((current - year_high) / year_high) * 100, 2) if year_high else None,
        "pct_off_52w_low": round(((current - year_low) / year_low) * 100, 2) if year_low else None,
        "as_of": str(closes.index[-1].date()),
    }

    _CACHE[ticker] = (now, payload)
    return payload


def get_market_state(indices: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return current level + recent moves for the requested Indian indices.

    Parameters
    ----------
    indices : list[str] | None
        Index display names like ``["NIFTY 50", "Sensex"]``. ``None`` returns
        the default set (NIFTY 50 + Sensex + NIFTY 500).

    Returns
    -------
    dict with shape::

        {
            "indices": {
                "NIFTY 50": {"current_level": 23643.50, "change_1d_pct": -0.42, ...},
                ...
            },
            "errors": [{"index": "...", "error": "..."}],  # may be empty
        }
    """
    names = indices if indices else list(_DEFAULT_INDICES.keys())
    out: Dict[str, Any] = {"indices": {}, "errors": []}
    for name in names:
        ticker = _DEFAULT_INDICES.get(name)
        if not ticker:
            out["errors"].append({"index": name, "error": "unknown index"})
            continue
        payload = _fetch_one(ticker)
        if payload is None:
            out["errors"].append({"index": name, "error": "fetch failed"})
            continue
        out["indices"][name] = payload
    return out
