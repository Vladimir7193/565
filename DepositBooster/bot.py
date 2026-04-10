"""
bot.py — Main trading loop.
Scans all symbols every 60s, generates signals, manages positions.
Run: python bot.py
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import config as cfg
from indicators import score_signal
from market_data import fetch_klines, get_ticker, get_funding_rate
from paper_engine import PaperEngine

os.makedirs(cfg.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"{cfg.LOG_DIR}/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run():
    engine = PaperEngine()
    logger.info("=" * 60)
    logger.info("DepositBooster Paper Bot started")
    logger.info("Strategy: Momentum Breakout + SMC | R:R=3.0 | Min confluence=%d", cfg.MIN_CONFLUENCE)
    logger.info("Initial balance: $%.2f | Leverage: %dx | Risk/trade: %.0f%%",
                cfg.INITIAL_BALANCE, cfg.LEVERAGE, cfg.RISK_PCT * 100)
    logger.info("Funding filter: %s | Breakout vol_z >= %.1f",
                "ON" if cfg.FUNDING_FILTER else "OFF", cfg.BREAKOUT_VOL_Z)
    logger.info("Symbols: %s", ", ".join(cfg.SYMBOLS))
    logger.info("=" * 60)

    while True:
        try:
            _cycle(engine)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            _print_summary(engine)
            break
        except Exception as e:
            logger.error("Cycle error: %s", e, exc_info=True)
            time.sleep(10)


def _cycle(engine: PaperEngine):
    prices: dict[str, float] = {}
    atrs:   dict[str, float] = {}

    # ── Fetch data & generate signals ─────────────────────────────────────────
    for symbol in cfg.SYMBOLS:
        try:
            df5  = fetch_klines(symbol, cfg.TF_PRIMARY,  limit=cfg.LOOKBACK + 10)
            df15 = fetch_klines(symbol, cfg.TF_CONFIRM,  limit=60)
            df1h = fetch_klines(symbol, cfg.TF_TREND,    limit=60)

            if df5 is None or len(df5) < cfg.LOOKBACK:
                continue

            funding = get_funding_rate(symbol)
            sig = score_signal(df5, df15, df1h, funding_rate=funding)
            price = sig["price"]
            atr   = sig["atr"]

            if price > 0:
                prices[symbol] = price
                atrs[symbol]   = atr

            # ── Open new position if signal is strong enough ──────────────────
            if sig["direction"] in ("LONG", "SHORT") and symbol not in engine.positions:
                opened = engine.open_position(
                    symbol=symbol,
                    direction=sig["direction"],
                    price=price,
                    atr=atr,
                    score=sig["score"],
                    signals=sig["signals"],
                )
                if opened:
                    logger.info(
                        "Signal: %s %s | score=%d | funding=%.4f%% | signals=%s",
                        sig["direction"], symbol, sig["score"],
                        sig.get("funding_rate", 0) * 100, sig["signals"],
                    )

        except Exception as e:
            logger.warning("Error processing %s: %s", symbol, e)

    # ── Manage open positions ─────────────────────────────────────────────────
    engine.manage_positions(prices, atrs)
    engine.update_equity(prices)

    # ── Status log ────────────────────────────────────────────────────────────
    stats = engine.stats()
    open_pos = list(engine.positions.keys())
    logger.info(
        "Balance=%.2f | Equity=%.2f | Growth=%.1f%% | Trades=%d | WR=%.0f%% | PF=%.2f | DD=%.1f%% | Open=%s",
        stats["balance"], stats["equity"], stats["growth_pct"],
        stats["total_trades"], stats["win_rate"], stats["profit_factor"],
        stats["max_drawdown"], open_pos if open_pos else "none",
    )

    if engine.halted:
        logger.warning("Trading halted. Waiting 60s...")
        time.sleep(60)
        return

    time.sleep(60)


def _print_summary(engine: PaperEngine):
    s = engine.stats()
    print("\n" + "=" * 50)
    print("FINAL SUMMARY")
    print(f"  Balance:       ${s['balance']:.2f}")
    print(f"  Equity:        ${s['equity']:.2f}")
    print(f"  Growth:        {s['growth_pct']:+.1f}%")
    print(f"  Total trades:  {s['total_trades']}")
    print(f"  Win rate:      {s['win_rate']:.1f}%")
    print(f"  Profit factor: {s['profit_factor']:.2f}")
    print(f"  Avg R:R:       {s['avg_rr']:.2f}")
    print(f"  Max drawdown:  {s['max_drawdown']:.1f}%")
    print("=" * 50)


if __name__ == "__main__":
    run()
