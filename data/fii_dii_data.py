"""
NSE FII/DII Trading Activity Provider.

Responsibilities:
- Fetch NSE's daily FII/DII (Foreign / Domestic Institutional Investor)
  net cash-market trading activity.
- Convert the combined net flow into a single signed market-wide bias
  in [-1, +1], using the SAME convention as
  market/macro_intelligence.py's sector_bias() (positive = bullish
  nudge, negative = bearish nudge), so both signals combine with the
  same downstream math.
- No strategy/decision logic — this module only fetches and normalizes
  numbers.

Context: institutional flow (FII+DII combined net buying/selling) is a
genuine market-wide sentiment signal this codebase never fetched at
all before this module — BUY/SELL decisions had zero visibility into
whether institutions were net buying or net selling the market that
day.

Why COMBINED (FII net + DII net), not FII alone: on many days FII and
DII flows partly offset each other (e.g. FII selling while domestic
funds absorb it) — the combined net is what actually moved the market
that day, not either side in isolation.

Saturation threshold: research into typical NSE daily FII/DII prints
(source in PATCH notes) shows most days sit within a few hundred crores
net either side, and combined net flows above roughly Rs 5,000 crore
are already described as "notably significant" institutional activity,
with genuinely extreme days running Rs 10,000+ crore. Rs 5,000 crore is
therefore used as the point where the bias saturates at +/-1.0 — bigger
prints don't need a bigger nudge, they're already "very bullish/bearish"
by that point.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

from core.logger import get_logger

logger = get_logger(__name__)

_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
_WARMUP_URL = "https://www.nseindia.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/reports/fii-dii",
}

_REQUEST_TIMEOUT_SECONDS = 15
_RETRY_ATTEMPTS = 2

# See module docstring for how this figure was chosen.
_SATURATION_CRORES = 5000.0

_FII_DII_CACHE_PATH = "storage/reports/fii_dii_cache.json"


class FiiDiiDataProvider:
    """Fetch the latest published FII/DII net cash-market activity and
    convert it into a signed market-wide bias."""

    def fetch_latest(self) -> dict[str, Any] | None:
        """Returns
        {"fii_net_cr": float, "dii_net_cr": float,
         "combined_net_cr": float, "bias": float, "as_of": str | None}
        or None if no live/cached data is available at all.

        Deliberately returns None (not a fabricated 0.0 "neutral") on
        total failure — same reasoning as data/delivery_data.py: a
        caller silently treating "we don't know" as "confirmed
        balanced flows" would be worse than just not nudging anything.
        """
        session = requests.Session()
        session.headers.update(_HEADERS)

        try:
            session.get(_WARMUP_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("NSE warm-up request failed (%s) — attempting FII/DII fetch anyway.", exc)

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = session.get(_FII_DII_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                result = self._parse_response(resp.json())
                if result is not None:
                    self._write_cache(result)
                    return result
            except Exception as exc:
                logger.warning(
                    "FII/DII fetch attempt %d/%d failed: %s",
                    attempt, _RETRY_ATTEMPTS, exc,
                )
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(2.0)

        logger.warning("FII/DII data unavailable live — falling back to last cached snapshot, if any.")
        return self._read_cache()

    @staticmethod
    def _parse_response(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, list):
            logger.warning("FII/DII response was not a list as expected: %r", type(payload))
            return None

        fii_net = None
        dii_net = None
        as_of = None

        for record in payload:
            if not isinstance(record, dict):
                continue
            category = str(record.get("category", "")).upper()
            net_raw = record.get("netValue")
            if net_raw is None:
                continue
            try:
                net_value = float(str(net_raw).replace(",", ""))
            except ValueError:
                continue

            if "FII" in category or "FPI" in category:
                fii_net = net_value
                as_of = as_of or record.get("date")
            elif "DII" in category:
                dii_net = net_value
                as_of = as_of or record.get("date")

        if fii_net is None or dii_net is None:
            logger.warning("FII/DII response missing FII or DII record — cannot compute bias.")
            return None

        combined_net = fii_net + dii_net
        bias = max(-1.0, min(1.0, combined_net / _SATURATION_CRORES))

        logger.info(
            "FII/DII fetched: FII net=%.1f cr, DII net=%.1f cr, combined=%.1f cr, bias=%.2f",
            fii_net, dii_net, combined_net, bias,
        )

        return {
            "fii_net_cr": round(fii_net, 1),
            "dii_net_cr": round(dii_net, 1),
            "combined_net_cr": round(combined_net, 1),
            "bias": round(bias, 3),
            "as_of": as_of,
        }

    @staticmethod
    def _write_cache(result: dict[str, Any]) -> None:
        try:
            path = Path(_FII_DII_CACHE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"cached_on": date.today().isoformat(), "data": result}, f)
        except Exception as exc:
            logger.warning("Failed to write FII/DII cache: %s", exc)

    @staticmethod
    def _read_cache() -> dict[str, Any] | None:
        try:
            path = Path(_FII_DII_CACHE_PATH)
            if not path.exists():
                return None
            with open(path) as f:
                payload = json.load(f)
            logger.warning(
                "Using cached FII/DII data from %s (live fetch failed today).",
                payload.get("cached_on", "unknown date"),
            )
            return payload.get("data")
        except Exception as exc:
            logger.warning("Failed to read FII/DII cache: %s", exc)
            return None
