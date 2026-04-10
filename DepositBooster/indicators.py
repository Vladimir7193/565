"""
indicators.py — Momentum Breakout + SMC confluence engine.

Strategy: Momentum Breakout + SMC
  Primary trigger : Swing High/Low breakout with volume confirmation
  SMC filters     : Order Block, FVG, Liquidity Sweep (institutional zones)
  Trend filters   : EMA alignment on 5m + 15m + 1h
  Noise removed   : RSI oversold/overbought (too noisy), BB bounce (too frequent)
  Funding filter  : Applied in bot.py via market_data.is_funding_ok()

Signal weights:
  BREAKOUT (primary)  — 2 pts  (must-have for aggressive entry)
  SWEEP + OB/FVG      — 1 pt each (SMC confluence)
  EMA 5m alignment    — 1 pt
  HTF 15m confirm     — 1 pt
  HTF 1h macro        — 1 pt
  MACD crossover      — 1 pt

MIN_CONFLUENCE = 3 → need at least 3 pts, breakout alone is not enough.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config as cfg


# ─────────────────────────────────────────────────────────────────────────────
#  CORE INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add indicator columns to OHLCV DataFrame."""
    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]

    # ATR (volatility baseline for everything)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(span=cfg.ATR_PERIOD, adjust=False).mean()

    # EMAs
    d["ema_fast"]  = c.ewm(span=cfg.EMA_FAST,  adjust=False).mean()
    d["ema_slow"]  = c.ewm(span=cfg.EMA_SLOW,  adjust=False).mean()
    d["ema_trend"] = c.ewm(span=cfg.EMA_TREND, adjust=False).mean()

    # MACD
    ema12 = c.ewm(span=cfg.MACD_FAST, adjust=False).mean()
    ema26 = c.ewm(span=cfg.MACD_SLOW, adjust=False).mean()
    d["macd"]      = ema12 - ema26
    d["macd_sig"]  = d["macd"].ewm(span=cfg.MACD_SIGNAL, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_sig"]

    # Volume Z-score (key for breakout confirmation)
    vol_mean    = v.rolling(20).mean()
    vol_std     = v.rolling(20).std()
    d["vol_z"]  = (v - vol_mean) / (vol_std + 1e-9)

    # Bollinger Bands (kept for chart display only, not used as signal)
    bb_mid        = c.rolling(cfg.BB_PERIOD).mean()
    bb_std        = c.rolling(cfg.BB_PERIOD).std()
    d["bb_upper"] = bb_mid + cfg.BB_STD * bb_std
    d["bb_lower"] = bb_mid - cfg.BB_STD * bb_std

    return d


# ─────────────────────────────────────────────────────────────────────────────
#  SWING HIGH / LOW BREAKOUT  (primary trigger)
# ─────────────────────────────────────────────────────────────────────────────

def detect_swing_breakout(df: pd.DataFrame) -> dict | None:
    """
    Primary breakout trigger.
    Conditions:
      - Close breaks above swing high (last N bars) by at least BREAKOUT_ATR_MULT × ATR
      - OR close breaks below swing low by same margin
      - Volume Z-score >= BREAKOUT_VOL_Z (strong volume confirmation)
      - Breakout candle body > 50% of candle range (momentum candle, not wick)
    Worth 2 points — the core of the strategy.
    """
    n = cfg.SWING_LOOKBACK
    if len(df) < n + 2:
        return None

    atr   = df["atr"].iloc[-1]
    price = df["close"].iloc[-1]
    vol_z = df["vol_z"].iloc[-1]
    open_ = df["open"].iloc[-1]
    high_ = df["high"].iloc[-1]
    low_  = df["low"].iloc[-1]

    # Swing levels from previous N candles (exclude current)
    swing_high = df["high"].iloc[-(n + 1):-1].max()
    swing_low  = df["low"].iloc[-(n + 1):-1].min()

    # Momentum candle: body must be > 50% of range
    candle_range = high_ - low_ + 1e-9
    body         = abs(price - open_)
    is_momentum  = body / candle_range > 0.50

    # Volume must be strong
    vol_ok = vol_z >= cfg.BREAKOUT_VOL_Z

    if not vol_ok or not is_momentum:
        return None

    # Bullish breakout
    if price > swing_high + cfg.BREAKOUT_ATR_MULT * atr and price > open_:
        return {
            "direction":     "BULLISH",
            "breakout_level": round(swing_high, 6),
            "vol_z":          round(vol_z, 2),
            "atr_margin":     round(price - swing_high, 6),
        }

    # Bearish breakout
    if price < swing_low - cfg.BREAKOUT_ATR_MULT * atr and price < open_:
        return {
            "direction":     "BEARISH",
            "breakout_level": round(swing_low, 6),
            "vol_z":          round(vol_z, 2),
            "atr_margin":     round(swing_low - price, 6),
        }

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  SMC DETECTORS
# ─────────────────────────────────────────────────────────────────────────────

def detect_order_block(df: pd.DataFrame) -> dict | None:
    """
    Institutional order block: last opposite candle before a strong impulse.
    Price must be retesting the OB zone now.
    """
    if len(df) < 10:
        return None
    recent = df.iloc[-12:].reset_index(drop=True)
    opens  = recent["open"].values
    closes = recent["close"].values
    highs  = recent["high"].values
    lows   = recent["low"].values
    price  = closes[-1]

    for i in range(len(recent) - 3, max(0, len(recent) - 10), -1):
        # Bullish OB: bearish candle → strong bullish impulse
        if closes[i] < opens[i]:
            move = (closes[min(i + 2, len(closes) - 1)] - closes[i]) / (closes[i] + 1e-9)
            if move > 0.008 and lows[i] <= price <= highs[i] * 1.005:
                return {"direction": "BULLISH", "ob_high": highs[i], "ob_low": lows[i]}
        # Bearish OB: bullish candle → strong bearish impulse
        if closes[i] > opens[i]:
            move = (closes[i] - closes[min(i + 2, len(closes) - 1)]) / (closes[i] + 1e-9)
            if move > 0.008 and lows[i] * 0.995 <= price <= highs[i]:
                return {"direction": "BEARISH", "ob_high": highs[i], "ob_low": lows[i]}
    return None


def detect_fvg(df: pd.DataFrame) -> dict | None:
    """
    Fair Value Gap: 3-candle imbalance zone.
    Price must be inside or touching the gap.
    """
    if len(df) < 5 or "atr" not in df.columns:
        return None
    atr   = df["atr"].iloc[-1]
    price = df["close"].iloc[-1]

    for i in range(len(df) - 1, max(len(df) - 7, 2) - 1, -1):
        h1   = df["high"].iloc[i - 2]
        l1   = df["low"].iloc[i - 2]
        h3   = df["high"].iloc[i]
        l3   = df["low"].iloc[i]
        vol2 = df["volume"].iloc[i - 1]
        avg_vol = df["volume"].iloc[max(0, i - 20):i].mean()

        # Bullish FVG: gap between candle1 high and candle3 low
        if l3 > h1:
            gap = l3 - h1
            if gap > max(0.002 * price, 0.4 * atr) and vol2 > avg_vol * 1.1:
                if h1 <= price <= l3 * 1.005:
                    return {"direction": "BULLISH", "gap_top": l3, "gap_bottom": h1}
        # Bearish FVG: gap between candle1 low and candle3 high
        if h3 < l1:
            gap = l1 - h3
            if gap > max(0.002 * price, 0.4 * atr) and vol2 > avg_vol * 1.1:
                if h3 * 0.995 <= price <= l1:
                    return {"direction": "BEARISH", "gap_top": l1, "gap_bottom": h3}
    return None


def detect_liquidity_sweep(df: pd.DataFrame) -> dict | None:
    """
    Liquidity sweep: price spikes beyond a key level then reverses.
    Classic stop-hunt pattern before a real move.
    """
    if len(df) < 6:
        return None
    recent    = df.iloc[-6:]
    highs     = recent["high"].values
    lows      = recent["low"].values
    closes    = recent["close"].values
    vols      = recent["volume"].values
    prev_high = float(np.max(highs[:-1]))
    prev_low  = float(np.min(lows[:-1]))
    cur_high  = float(highs[-1])
    cur_low   = float(lows[-1])
    cur_close = float(closes[-1])
    avg_vol   = float(np.mean(vols[:-1])) + 1e-9

    # Bullish sweep: dipped below prev_low, closed back above
    if cur_low < prev_low and cur_close > prev_low:
        depth = (prev_low - cur_low) / (prev_low + 1e-9)
        ret   = (cur_close - cur_low) / (cur_low + 1e-9)
        if depth > 0.002 and ret > 0.005 and vols[-1] > avg_vol * 1.2:
            return {"direction": "BULLISH", "sweep_level": prev_low,
                    "strength": round(depth * 100 + ret * 100, 2)}

    # Bearish sweep: spiked above prev_high, closed back below
    if cur_high > prev_high and cur_close < prev_high:
        height = (cur_high - prev_high) / (prev_high + 1e-9)
        ret    = (cur_high - cur_close) / (cur_high + 1e-9)
        if height > 0.002 and ret > 0.005 and vols[-1] > avg_vol * 1.2:
            return {"direction": "BEARISH", "sweep_level": prev_high,
                    "strength": round(height * 100 + ret * 100, 2)}
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  CONFLUENCE SCORER
# ─────────────────────────────────────────────────────────────────────────────

def score_signal(df5: pd.DataFrame, df15: pd.DataFrame, df1h: pd.DataFrame,
                 funding_rate: float = 0.0) -> dict:
    """
    Momentum Breakout + SMC confluence scorer.

    Scoring (max ~9 pts):
      Swing breakout (primary)  — 2 pts
      Liquidity sweep           — 1 pt
      Order block               — 1 pt
      Fair value gap            — 1 pt
      EMA 5m alignment          — 1 pt
      MACD crossover 5m         — 1 pt
      HTF 15m EMA confirm       — 1 pt
      HTF 1h macro trend        — 1 pt

    Returns: {direction, score, signals, atr, price, funding_rate}
    """
    empty = {"direction": "HOLD", "score": 0, "signals": [],
             "atr": 0, "price": 0, "funding_rate": funding_rate}

    if df5 is None or len(df5) < cfg.LOOKBACK:
        return empty

    df5  = compute_indicators(df5)
    df15 = compute_indicators(df15) if df15 is not None and len(df15) >= 20 else None
    df1h = compute_indicators(df1h) if df1h is not None and len(df1h) >= 20 else None

    row   = df5.iloc[-1]
    price = float(row["close"])
    atr   = float(row["atr"])

    long_score  = 0
    short_score = 0
    long_signals:  list[str] = []
    short_signals: list[str] = []

    # ── 1. SWING BREAKOUT — primary trigger (2 pts) ───────────────────────────
    bo = detect_swing_breakout(df5)
    if bo:
        if bo["direction"] == "BULLISH":
            long_score  += 2
            long_signals.append(f"BREAKOUT_UP(vol_z={bo['vol_z']})")
        else:
            short_score += 2
            short_signals.append(f"BREAKOUT_DN(vol_z={bo['vol_z']})")

    # ── 2. LIQUIDITY SWEEP — stop hunt before real move (1 pt) ───────────────
    sweep = detect_liquidity_sweep(df5)
    # Filter contradicting signals: if breakout and sweep disagree, discard sweep
    if sweep and bo is not None and bo["direction"] != sweep["direction"]:
        sweep = None
    if sweep:
        if sweep["direction"] == "BULLISH":
            long_score  += 1
            long_signals.append(f"SWEEP_BULL(str={sweep['strength']})")
        else:
            short_score += 1
            short_signals.append(f"SWEEP_BEAR(str={sweep['strength']})")

    # ── 3. ORDER BLOCK — institutional zone retest (1 pt) ────────────────────
    ob = detect_order_block(df5)
    if ob:
        if ob["direction"] == "BULLISH":
            long_score  += 1
            long_signals.append("OB_BULL")
        else:
            short_score += 1
            short_signals.append("OB_BEAR")

    # ── 4. FAIR VALUE GAP — imbalance fill (1 pt) ────────────────────────────
    fvg = detect_fvg(df5)
    if fvg:
        if fvg["direction"] == "BULLISH":
            long_score  += 1
            long_signals.append("FVG_BULL")
        else:
            short_score += 1
            short_signals.append("FVG_BEAR")

    # ── 5. EMA ALIGNMENT 5m (1 pt) ───────────────────────────────────────────
    if row["ema_fast"] > row["ema_slow"] > row["ema_trend"]:
        long_score  += 1
        long_signals.append("EMA_BULL")
    elif row["ema_fast"] < row["ema_slow"] < row["ema_trend"]:
        short_score += 1
        short_signals.append("EMA_BEAR")

    # ── 6. MACD CROSSOVER 5m (1 pt) ──────────────────────────────────────────
    prev_hist = float(df5["macd_hist"].iloc[-2])
    curr_hist = float(row["macd_hist"])
    if prev_hist < 0 < curr_hist:
        long_score  += 1
        long_signals.append("MACD_X_UP")
    elif prev_hist > 0 > curr_hist:
        short_score += 1
        short_signals.append("MACD_X_DN")

    # ── 7. HTF 15m EMA confirmation (1 pt) ───────────────────────────────────
    if df15 is not None:
        r15 = df15.iloc[-1]
        if float(r15["ema_fast"]) > float(r15["ema_slow"]):
            long_score  += 1
            long_signals.append("15M_BULL")
        elif float(r15["ema_fast"]) < float(r15["ema_slow"]):
            short_score += 1
            short_signals.append("15M_BEAR")

    # ── 8. HTF 1h macro trend (1 pt) ─────────────────────────────────────────
    if df1h is not None:
        r1h = df1h.iloc[-1]
        if float(r1h["close"]) > float(r1h["ema_trend"]):
            long_score  += 1
            long_signals.append("1H_BULL")
        elif float(r1h["close"]) < float(r1h["ema_trend"]):
            short_score += 1
            short_signals.append("1H_BEAR")

    # ── FUNDING RATE FILTER (not a score, just a block) ──────────────────────
    funding_blocked = False
    if cfg.FUNDING_FILTER:
        if long_score > short_score and funding_rate > cfg.FUNDING_MAX_LONG:
            long_signals.append(f"FUNDING_BLOCK(rate={funding_rate:.4f})")
            funding_blocked = True
        if short_score > long_score and funding_rate < cfg.FUNDING_MAX_SHORT:
            short_signals.append(f"FUNDING_BLOCK(rate={funding_rate:.4f})")
            funding_blocked = True

    # ── DECISION ──────────────────────────────────────────────────────────────
    # If REQUIRE_BREAKOUT is set, must have a breakout or sweep signal
    has_trigger = bo is not None or sweep is not None
    if cfg.REQUIRE_BREAKOUT and not has_trigger:
        signals = long_signals + short_signals
        return {"direction": "HOLD",
                "score": max(long_score, short_score),
                "signals": signals, "atr": atr, "price": price,
                "funding_rate": funding_rate}

    if not funding_blocked:
        if long_score >= cfg.MIN_CONFLUENCE and long_score > short_score:
            return {"direction": "LONG",  "score": long_score,
                    "signals": long_signals, "atr": atr, "price": price,
                    "funding_rate": funding_rate}
        if short_score >= cfg.MIN_CONFLUENCE and short_score > long_score:
            return {"direction": "SHORT", "score": short_score,
                    "signals": short_signals, "atr": atr, "price": price,
                    "funding_rate": funding_rate}

    # HOLD — return signals for the dominant side
    signals = long_signals if long_score >= short_score else short_signals
    return {"direction": "HOLD",
            "score": max(long_score, short_score),
            "signals": signals, "atr": atr, "price": price,
            "funding_rate": funding_rate}
