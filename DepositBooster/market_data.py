"""
market_data.py — Bybit v5 REST data fetcher.
Uses mainnet PUBLIC endpoints for real prices (no API key needed for market data).
All orders are paper (in-memory only).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd
from pybit.unified_trading import HTTP

import config as cfg

logger = logging.getLogger(__name__)

_session_cache: HTTP | None = None

def _session() -> HTTP:
    """
    Singleton session — reuse one HTTP connection per process.
    Always use mainnet for market data (real prices).
    API key only needed for real orders — paper trade doesn't need it.
    """
    global _session_cache
    if _session_cache is None:
        _session_cache = HTTP(testnet=False)  # mainnet public API, no auth needed for klines/tickers
    return _session_cache


def fetch_klines(symbol: str, interval: str, limit: int = 150) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Bybit, return sorted ascending DataFrame."""
    sess = _session()
    for attempt in range(3):
        try:
            resp = sess.get_kline(
                category=cfg.CATEGORY,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            rows = resp["result"]["list"]
            if not rows:
                return None
            df = pd.DataFrame(
                rows,
                columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning("kline fetch error [%s %s] attempt %d: %s", symbol, interval, attempt + 1, e)
            time.sleep(2 ** attempt)
    return None


def get_ticker(symbol: str) -> dict:
    """Return last price, funding rate, OI."""
    try:
        sess = _session()
        resp = sess.get_tickers(category=cfg.CATEGORY, symbol=symbol)
        t = resp["result"]["list"][0]
        return {
            "last_price":       float(t.get("lastPrice", 0)),
            "mark_price":       float(t.get("markPrice", 0)),
            "funding_rate":     float(t.get("fundingRate", 0)),
            "open_interest":    float(t.get("openInterestValue", 0)),
            "volume_24h":       float(t.get("volume24h", 0)),
            "price_change_pct": float(t.get("price24hPcnt", 0)) * 100,
        }
    except Exception as e:
        logger.warning("ticker error [%s]: %s", symbol, e)
        return {}


def get_funding_rate(symbol: str) -> float:
    """Return current funding rate. Positive = longs pay shorts."""
    try:
        ticker = get_ticker(symbol)
        return ticker.get("funding_rate", 0.0)
    except Exception:
        return 0.0


def is_funding_ok(symbol: str, direction: str) -> bool:
    """
    Funding rate filter:
    - Skip LONG if funding is very positive (longs already overheated)
    - Skip SHORT if funding is very negative (shorts already overheated)
    Returns True if trade is allowed.
    """
    if not cfg.FUNDING_FILTER:
        return True
    rate = get_funding_rate(symbol)
    if direction == "LONG"  and rate > cfg.FUNDING_MAX_LONG:
        return False
    if direction == "SHORT" and rate < cfg.FUNDING_MAX_SHORT:
        return False
    return True
