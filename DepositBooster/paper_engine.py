"""
paper_engine.py — Paper trading engine with position management.
Tracks equity, positions, SL/TP, trailing stops, daily loss guard.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config as cfg

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:      str
    side:        str        # "LONG" | "SHORT"
    entry_price: float
    qty:         float      # contracts (USDT notional / price)
    sl:          float
    tp:          float
    entry_time:  str        = field(default_factory=lambda: _now())
    trail_sl:    Optional[float] = None
    score:       int        = 0
    signals:     str        = ""

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def unrealized_pnl(self, price: float) -> float:
        if self.side == "LONG":
            return (price - self.entry_price) * self.qty
        return (self.entry_price - price) * self.qty

    def unrealized_pnl_pct(self, price: float) -> float:
        return self.unrealized_pnl(price) / (self.notional + 1e-9) * 100

    def update_trail(self, price: float, atr: float):
        """Update trailing stop loss."""
        if self.side == "LONG":
            new_trail = price - cfg.TRAIL_STEP_ATR * atr
            if self.trail_sl is None:
                # Activate when price moved TRAIL_ACTIVATE_ATR * atr above entry
                if price >= self.entry_price + cfg.TRAIL_ACTIVATE_ATR * atr:
                    self.trail_sl = new_trail
            else:
                self.trail_sl = max(self.trail_sl, new_trail)
        else:
            new_trail = price + cfg.TRAIL_STEP_ATR * atr
            if self.trail_sl is None:
                # Activate when price moved TRAIL_ACTIVATE_ATR * atr below entry
                if price <= self.entry_price - cfg.TRAIL_ACTIVATE_ATR * atr:
                    self.trail_sl = new_trail
            else:
                self.trail_sl = min(self.trail_sl, new_trail)

    def effective_sl(self) -> float:
        return self.trail_sl if self.trail_sl is not None else self.sl

    def should_close(self, price: float) -> Optional[str]:
        """Return close reason or None."""
        sl = self.effective_sl()
        if self.side == "LONG":
            if price <= sl:
                return "SL"
            if price >= self.tp:
                return "TP"
        else:
            if price >= sl:
                return "SL"
            if price <= self.tp:
                return "TP"
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  PAPER ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class PaperEngine:
    def __init__(self):
        self.balance:    float = cfg.INITIAL_BALANCE
        self.equity:     float = cfg.INITIAL_BALANCE
        self.peak_equity: float = cfg.INITIAL_BALANCE
        self.positions:  Dict[str, Position] = {}
        self.trades:     List[dict] = []
        self.daily_pnl:  float = 0.0
        self.daily_start_balance: float = cfg.INITIAL_BALANCE
        self.daily_reset_date: str = _today()
        self.halted:     bool = False

        _ensure_logs()
        self._init_csvs()

    # ── Equity ────────────────────────────────────────────────────────────────

    def update_equity(self, prices: Dict[str, float]):
        """Recalculate equity from open positions."""
        unrealized = sum(
            pos.unrealized_pnl(prices[sym])
            for sym, pos in self.positions.items()
            if sym in prices
        )
        self.equity = self.balance + unrealized
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        self._log_equity()

    @property
    def drawdown_pct(self) -> float:
        return (self.peak_equity - self.equity) / (self.peak_equity + 1e-9)

    # ── Guards ────────────────────────────────────────────────────────────────

    def _check_guards(self) -> bool:
        """Return True if trading is allowed."""
        today = _today()
        if today != self.daily_reset_date:
            self.daily_pnl = 0.0
            self.daily_start_balance = self.balance  # reset daily baseline
            self.daily_reset_date = today
            self.halted = False

        if self.daily_pnl <= -cfg.MAX_DAILY_LOSS * self.daily_start_balance:
            if not self.halted:
                logger.warning("Daily loss limit hit (%.1f%%). Trading halted for today.",
                               abs(self.daily_pnl / (self.daily_start_balance + 1e-9)) * 100)
            self.halted = True
            return False

        if self.drawdown_pct >= cfg.MAX_DRAWDOWN:
            if not self.halted:
                logger.warning("Max drawdown %.1f%% hit. Trading halted.", self.drawdown_pct * 100)
            self.halted = True
            return False

        return True

    # ── Open position ─────────────────────────────────────────────────────────

    def open_position(self, symbol: str, direction: str, price: float, atr: float,
                      score: int, signals: list) -> bool:
        if not self._check_guards():
            return False
        if symbol in self.positions:
            return False
        if len(self.positions) >= cfg.MAX_POSITIONS:
            return False

        # Position sizing: risk RISK_PCT of equity
        risk_usdt = self.equity * cfg.RISK_PCT
        sl_dist   = cfg.SL_ATR_MULT * atr
        qty       = risk_usdt / (sl_dist + 1e-9)
        # Cap by leverage
        max_qty   = (self.equity * cfg.LEVERAGE) / (price + 1e-9)
        qty       = min(qty, max_qty)
        qty       = round(qty, 6)

        if qty <= 0 or price <= 0:
            return False

        if direction == "LONG":
            sl = price - sl_dist
            tp = price + cfg.TP_ATR_MULT * atr
        else:
            sl = price + sl_dist
            tp = price - cfg.TP_ATR_MULT * atr

        pos = Position(
            symbol=symbol, side=direction, entry_price=price,
            qty=qty, sl=sl, tp=tp, score=score,
            signals=",".join(signals),
        )
        self.positions[symbol] = pos

        # Deduct fee
        fee = price * qty * cfg.TAKER_FEE
        self.balance -= fee

        logger.info(
            "OPEN %s %s @ %.4f | qty=%.6f | SL=%.4f | TP=%.4f | score=%d | fee=%.4f",
            direction, symbol, price, qty, sl, tp, score, fee,
        )
        self._log_signal(symbol, direction, price, score, signals)
        return True

    # ── Manage open positions ─────────────────────────────────────────────────

    def manage_positions(self, prices: Dict[str, float], atrs: Dict[str, float]):
        """Check SL/TP for all open positions."""
        to_close = []
        for sym, pos in self.positions.items():
            price = prices.get(sym)
            atr   = atrs.get(sym, 0)
            if price is None:
                continue
            pos.update_trail(price, atr)
            reason = pos.should_close(price)
            if reason:
                to_close.append((sym, price, reason))

        for sym, price, reason in to_close:
            self._close_position(sym, price, reason)

    def _close_position(self, symbol: str, exit_price: float, reason: str):
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return

        pnl  = pos.unrealized_pnl(exit_price)
        fee  = exit_price * pos.qty * cfg.TAKER_FEE
        net  = pnl - fee
        self.balance    += net
        self.daily_pnl  += net

        duration = _elapsed(pos.entry_time)
        rr = abs(pnl) / (abs(pos.entry_price - pos.sl) * pos.qty + 1e-9)

        trade = {
            "time":        _now(),
            "symbol":      symbol,
            "side":        pos.side,
            "entry":       round(pos.entry_price, 6),
            "exit":        round(exit_price, 6),
            "qty":         round(pos.qty, 6),
            "pnl":         round(net, 4),
            "pnl_pct":     round(net / cfg.INITIAL_BALANCE * 100, 3),
            "reason":      reason,
            "duration":    duration,
            "rr":          round(rr, 2),
            "score":       pos.score,
            "signals":     pos.signals,
            "balance":     round(self.balance, 4),
        }
        self.trades.append(trade)
        self._write_trade(trade)

        emoji = "✅" if net > 0 else "❌"
        logger.info(
            "%s CLOSE %s %s @ %.4f | PnL=%.4f USDT (%.2f%%) | reason=%s | balance=%.2f",
            emoji, pos.side, symbol, exit_price, net, net / cfg.INITIAL_BALANCE * 100,
            reason, self.balance,
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        if not self.trades:
            return {
                "total_trades": 0, "win_rate": 0, "total_pnl": 0,
                "total_pnl_pct": 0, "avg_rr": 0, "profit_factor": 0,
                "max_drawdown": 0, "balance": self.balance, "equity": self.equity,
                "growth_pct": (self.equity - cfg.INITIAL_BALANCE) / cfg.INITIAL_BALANCE * 100,
            }
        wins   = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss   = abs(sum(t["pnl"] for t in losses))
        return {
            "total_trades":  len(self.trades),
            "win_rate":      round(len(wins) / len(self.trades) * 100, 1),
            "total_pnl":     round(sum(t["pnl"] for t in self.trades), 4),
            "total_pnl_pct": round(sum(t["pnl"] for t in self.trades) / cfg.INITIAL_BALANCE * 100, 2),
            "avg_rr":        round(sum(t["rr"] for t in self.trades) / len(self.trades), 2),
            "profit_factor": round(gross_profit / (gross_loss + 1e-9), 2),
            "max_drawdown":  round(self.drawdown_pct * 100, 2),
            "balance":       round(self.balance, 4),
            "equity":        round(self.equity, 4),
            "growth_pct":    round((self.equity - cfg.INITIAL_BALANCE) / cfg.INITIAL_BALANCE * 100, 2),
        }

    # ── CSV logging ───────────────────────────────────────────────────────────

    def _init_csvs(self):
        if not os.path.exists(cfg.TRADES_CSV):
            with open(cfg.TRADES_CSV, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=[
                    "time","symbol","side","entry","exit","qty","pnl","pnl_pct",
                    "reason","duration","rr","score","signals","balance",
                ])
                w.writeheader()
        if not os.path.exists(cfg.EQUITY_CSV):
            with open(cfg.EQUITY_CSV, "w", newline="") as f:
                csv.writer(f).writerow(["time", "equity", "balance"])
        if not os.path.exists(cfg.SIGNALS_CSV):
            with open(cfg.SIGNALS_CSV, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["time","symbol","direction","price","score","signals"])
                w.writeheader()

    def _write_trade(self, trade: dict):
        with open(cfg.TRADES_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trade.keys()))
            w.writerow(trade)

    def _log_equity(self):
        with open(cfg.EQUITY_CSV, "a", newline="") as f:
            csv.writer(f).writerow([_now(), round(self.equity, 4), round(self.balance, 4)])
        self._write_positions_json()

    def _write_positions_json(self):
        """Write current open positions to JSON for dashboard consumption."""
        import json
        data = []
        for sym, pos in self.positions.items():
            data.append({
                "symbol":      sym,
                "side":        pos.side,
                "entry_price": pos.entry_price,
                "qty":         pos.qty,
                "sl":          pos.effective_sl(),
                "tp":          pos.tp,
                "score":       pos.score,
                "signals":     pos.signals,
                "entry_time":  pos.entry_time,
            })
        path = f"{cfg.LOG_DIR}/positions.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _log_signal(self, symbol, direction, price, score, signals):
        with open(cfg.SIGNALS_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time","symbol","direction","price","score","signals"])
            w.writerow({
                "time": _now(), "symbol": symbol, "direction": direction,
                "price": price, "score": score, "signals": ",".join(signals),
            })


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _elapsed(start_str: str) -> str:
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        secs  = int((datetime.now(timezone.utc) - start).total_seconds())
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return "?"

def _ensure_logs():
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
