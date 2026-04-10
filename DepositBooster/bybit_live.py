"""
bybit_live.py — REAL trading bot on Bybit mainnet.
Deposit: ~$10 | Leverage: 10x | Risk: 2% per trade

ВАЖНО / IMPORTANT:
  - Это реальные деньги. Бот открывает настоящие ордера на Bybit.
  - Вставь API ключи в переменные окружения BYBIT_LIVE_KEY / BYBIT_LIVE_SECRET.
  - Убедись что на счёте есть USDT в Unified Trading Account.
  - Плечо выставляется автоматически перед каждой сделкой.
  - Минимальный ордер Bybit: ~$5 notional.

Run: python bybit_live.py
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pybit.unified_trading import HTTP

import config as cfg
from indicators import score_signal
from market_data import fetch_klines, get_ticker, get_funding_rate


# ════════════════════════════════════════════════════════��═════════════════════
#  LIVE CONFIG  (переопредели здесь или через env)
# ══════════════════════════════════════════════════════════════════════════════

LIVE_API_KEY    = os.getenv("BYBIT_LIVE_KEY",    cfg.API_KEY)
LIVE_API_SECRET = os.getenv("BYBIT_LIVE_SECRET", cfg.API_SECRET)

LIVE_LEVERAGE   = 10       # 10x — безопаснее чем 20x для реальных денег
LIVE_RISK_PCT   = 0.02     # 2% от equity на сделку
LIVE_MAX_POS    = 1        # максимум 1 позиция (маленький депозит)
LIVE_SL_ATR     = 1.2      # стоп-лосс = 1.2 × ATR
LIVE_TP_ATR     = 3.6      # тейк-профит = 3.6 × ATR → R:R = 3.0
LIVE_TRAIL_ACTIVATE = 2.0  # активировать трейлинг после 2×ATR профита
LIVE_TRAIL_STEP     = 0.8  # шаг трейлинга
MIN_NOTIONAL    = 5.0      # минимальный размер ордера Bybit ($5)
MIN_CONFLUENCE  = 4        # минимум 4 сигнала (согласовано с indicators.py)
MAX_DAILY_LOSS  = 0.10     # остановка при дневном убытке > 10%
MAX_DRAWDOWN    = 0.25     # остановка при общей просадке > 25%
CYCLE_INTERVAL  = 60       # секунд между циклами сканирования
ERROR_COOLDOWN  = 15       # секунд ожидания после ошибки

# Пары с низкой ценой (удобнее для маленьких позиций)
LIVE_SYMBOLS = [
    "DOGEUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT",
    "LINKUSDT", "ARBUSDT", "APTUSDT", "FETUSDT",
]

# ── Logging & CSV ────────────────────────────────────────────────────────────

LIVE_LOG_DIR    = os.path.join(cfg.LOG_DIR, "live")
LIVE_TRADES_CSV = os.path.join(LIVE_LOG_DIR, "live_trades.csv")
LIVE_EQUITY_CSV = os.path.join(LIVE_LOG_DIR, "live_equity.csv")
LIVE_STATE_JSON = os.path.join(LIVE_LOG_DIR, "live_state.json")

os.makedirs(LIVE_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(LIVE_LOG_DIR, "live_bot.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("bybit_live")


# ══════════════════════════════════════════════════════════════════════════════
#  BYBIT SESSION  (singleton — one connection per process)
# ══════════════════════════════════════════════════════════════════════════════

_session: HTTP | None = None


def get_session() -> HTTP:
    global _session
    if _session is None:
        _session = HTTP(
            testnet=False,
            api_key=LIVE_API_KEY,
            api_secret=LIVE_API_SECRET,
            recv_window=10000,
        )
    return _session


# ══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT
# ══════════════════════════════════════════════════════════════════════════════

def get_wallet_balance() -> float:
    """Return total USDT equity in Unified account."""
    try:
        sess = get_session()
        resp = sess.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                # Используем walletBalance (полный баланс включая unrealized PnL)
                # availableToWithdraw не учитывает заблокированную маржу
                return float(c["walletBalance"])
    except Exception as e:
        logger.error("Balance fetch error: %s", e)
    return 0.0


def get_available_balance() -> float:
    """Return available USDT (free to use for new orders)."""
    try:
        sess = get_session()
        resp = sess.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        coins = resp["result"]["list"][0]["coin"]
        for c in coins:
            if c["coin"] == "USDT":
                return float(c["availableToWithdraw"])
    except Exception as e:
        logger.error("Available balance fetch error: %s", e)
    return 0.0


def get_open_positions() -> Dict[str, dict]:
    """Return dict of open positions {symbol: position_data}."""
    try:
        sess = get_session()
        resp = sess.get_positions(category="linear", settleCoin="USDT")
        positions: Dict[str, dict] = {}
        for p in resp["result"]["list"]:
            size = float(p.get("size", 0))
            if size > 0:
                positions[p["symbol"]] = {
                    "side":            p["side"],        # "Buy" | "Sell"
                    "size":            size,
                    "entry_price":     float(p["avgPrice"]),
                    "unrealized_pnl":  float(p.get("unrealisedPnl", 0)),
                    "liq_price":       float(p.get("liqPrice", 0)),
                    "stop_loss":       float(p.get("stopLoss", 0)),
                    "take_profit":     float(p.get("takeProfit", 0)),
                    "leverage":        float(p.get("leverage", 0)),
                    "mark_price":      float(p.get("markPrice", 0)),
                }
        return positions
    except Exception as e:
        logger.error("Positions fetch error: %s", e)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
#  LEVERAGE & POSITION MODE
# ══════════════════════════════════════════════════════════════════════════════

def set_leverage(symbol: str, leverage: int) -> bool:
    """Set leverage for symbol. Returns True if OK or already set."""
    try:
        sess = get_session()
        sess.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        return True
    except Exception as e:
        err = str(e)
        # "leverage not modified" = уже установлен, не ошибка
        if "leverage not modified" in err.lower() or "110043" in err:
            return True
        logger.warning("Leverage set error [%s]: %s", symbol, e)
        return False


def ensure_one_way_mode(symbol: str) -> bool:
    """Ensure position mode is One-Way (not Hedge). Required for SL/TP."""
    try:
        sess = get_session()
        sess.switch_position_mode(
            category="linear",
            symbol=symbol,
            mode=0,  # 0 = One-Way Mode
        )
        return True
    except Exception as e:
        err = str(e)
        if "position mode is not modified" in err.lower() or "110025" in err:
            return True
        logger.warning("Position mode switch error [%s]: %s", symbol, e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_instrument_info(symbol: str) -> dict:
    """Get min qty, qty step, price tick for symbol."""
    try:
        sess = get_session()
        resp = sess.get_instruments_info(category="linear", symbol=symbol)
        item = resp["result"]["list"][0]
        lot = item["lotSizeFilter"]
        price_f = item["priceFilter"]
        return {
            "min_qty":    float(lot["minOrderQty"]),
            "max_qty":    float(lot["maxOrderQty"]),
            "qty_step":   float(lot["qtyStep"]),
            "tick_size":  float(price_f["tickSize"]),
            "min_price":  float(price_f["minPrice"]),
            "max_price":  float(price_f["maxPrice"]),
        }
    except Exception as e:
        logger.warning("Instrument info error [%s]: %s", symbol, e)
        return {
            "min_qty": 0.001, "max_qty": 1e9, "qty_step": 0.001,
            "tick_size": 0.0001, "min_price": 0, "max_price": 1e9,
        }


def round_qty(qty: float, step: float) -> float:
    """Round qty DOWN to exchange step size (always round down to avoid rejection)."""
    if step <= 0:
        return qty
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(qty / step) * step, decimals)


def round_price(price: float, tick: float) -> float:
    """Round price to tick size."""
    if tick <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(round(price / tick) * tick, decimals)


def calc_qty(
    price: float, atr: float, balance: float, info: dict
) -> Optional[float]:
    """
    Calculate position size:
      1. По риску: risk_usdt / SL_distance
      2. Ограничение по плечу: balance * leverage / price
      3. Проверка min notional ($5)
      4. Округление по qty_step ВНИЗ
    """
    if price <= 0 or atr <= 0:
        return None

    # Размер по риску
    risk_usdt = balance * LIVE_RISK_PCT
    sl_dist   = LIVE_SL_ATR * atr
    qty       = risk_usdt / sl_dist

    # Ограничение по плечу
    max_notional = balance * LIVE_LEVERAGE
    max_qty = max_notional / price
    qty = min(qty, max_qty)

    # Проверка минимального notional — поднимаем qty если нужно
    if qty * price < MIN_NOTIONAL:
        qty = (MIN_NOTIONAL / price) * 1.05  # +5% буфер

    # Проверка что не превысили лимит по плечу после поднятия
    if qty * price > max_notional:
        logger.warning(
            "Qty for %s requires $%.2f notional but max is $%.2f",
            "?", qty * price, max_notional,
        )
        return None

    # Округление ВНИЗ по шагу биржи
    qty = round_qty(qty, info["qty_step"])

    # Финальные проверки
    if qty < info["min_qty"]:
        logger.warning("Qty %.6f below min %.6f", qty, info["min_qty"])
        return None
    if qty > info["max_qty"]:
        qty = round_qty(info["max_qty"], info["qty_step"])
    if qty * price < MIN_NOTIONAL:
        logger.warning("Notional $%.2f below min $%.2f", qty * price, MIN_NOTIONAL)
        return None

    return qty


# ══════════════════════════════════════════════════════════════════════════════
#  PLACE / CLOSE ORDERS
# ══════════════════════════════════════════════════════════════════════════════

def place_order(
    symbol: str, side: str, qty: float,
    sl: float, tp: float, info: dict,
) -> Optional[str]:
    """
    Place market order with SL and TP.
    Returns orderId on success, None on failure.
    """
    try:
        sess = get_session()
        bybit_side = "Buy" if side == "LONG" else "Sell"
        tick = info["tick_size"]

        sl_rounded = round_price(sl, tick)
        tp_rounded = round_price(tp, tick)

        # Валидация цен SL/TP
        if sl_rounded <= 0 or tp_rounded <= 0:
            logger.error("Invalid SL=%.6f or TP=%.6f for %s", sl_rounded, tp_rounded, symbol)
            return None

        resp = sess.place_order(
            category="linear",
            symbol=symbol,
            side=bybit_side,
            orderType="Market",
            qty=str(qty),
            stopLoss=str(sl_rounded),
            takeProfit=str(tp_rounded),
            slTriggerBy="MarkPrice",
            tpTriggerBy="MarkPrice",
            timeInForce="IOC",
            reduceOnly=False,
        )

        ret_code = resp.get("retCode", -1)
        if ret_code == 0:
            order_id = resp["result"].get("orderId", "?")
            logger.info(
                "✅ ORDER PLACED | %s %s | qty=%.6f | SL=%.4f | TP=%.4f | orderId=%s",
                side, symbol, qty, sl_rounded, tp_rounded, order_id,
            )
            return order_id
        else:
            logger.error(
                "❌ Order rejected [%s]: code=%d msg=%s",
                symbol, ret_code, resp.get("retMsg"),
            )
            return None
    except Exception as e:
        logger.error("❌ Place order exception [%s]: %s", symbol, e)
        return None


def close_position(symbol: str, side: str, qty: float) -> bool:
    """Close position with market order (reduceOnly)."""
    try:
        sess = get_session()
        close_side = "Sell" if side == "Buy" else "Buy"
        resp = sess.place_order(
            category="linear",
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=str(qty),
            reduceOnly=True,
            timeInForce="IOC",
        )
        if resp.get("retCode") == 0:
            logger.info("✅ CLOSED %s %s | qty=%.6f", side, symbol, qty)
            return True
        else:
            logger.error("❌ Close failed [%s]: %s", symbol, resp.get("retMsg"))
            return False
    except Exception as e:
        logger.error("❌ Close exception [%s]: %s", symbol, e)
        return False


def update_trailing_stop(symbol: str, new_sl: float, info: dict) -> bool:
    """Update SL on an existing position (server-side trailing)."""
    try:
        sess = get_session()
        tick = info["tick_size"]
        resp = sess.set_trading_stop(
            category="linear",
            symbol=symbol,
            stopLoss=str(round_price(new_sl, tick)),
            slTriggerBy="MarkPrice",
        )
        if resp.get("retCode") == 0:
            return True
        else:
            logger.warning("Trailing SL update failed [%s]: %s", symbol, resp.get("retMsg"))
            return False
    except Exception as e:
        logger.warning("Trailing SL exception [%s]: %s", symbol, e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TRADE TRACKER  (локальный трекер для статистики и трейлинга)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LiveTrade:
    symbol:      str
    direction:   str        # "LONG" | "SHORT"
    entry_price: float
    qty:         float
    sl:          float
    tp:          float
    trail_sl:    Optional[float] = None
    order_id:    str        = ""
    entry_time:  str        = field(default_factory=lambda: _now())
    score:       int        = 0
    signals:     str        = ""

    def should_trail(self, price: float, atr: float) -> Optional[float]:
        """Check if trailing should activate/update. Returns new SL or None."""
        if atr <= 0:
            return None

        if self.direction == "LONG":
            profit_atr = (price - self.entry_price) / atr
            if profit_atr >= LIVE_TRAIL_ACTIVATE:
                new_sl = price - LIVE_TRAIL_STEP * atr
                current_sl = self.trail_sl or self.sl
                if new_sl > current_sl:
                    return new_sl
        else:
            profit_atr = (self.entry_price - price) / atr
            if profit_atr >= LIVE_TRAIL_ACTIVATE:
                new_sl = price + LIVE_TRAIL_STEP * atr
                current_sl = self.trail_sl or self.sl
                if new_sl < current_sl:
                    return new_sl
        return None


class TradeTracker:
    """Tracks live trades locally for statistics, CSV logging, and trailing stops."""

    def __init__(self):
        self.active:   Dict[str, LiveTrade] = {}
        self.history:  List[dict] = []
        self._init_csvs()

    def register(self, trade: LiveTrade):
        self.active[trade.symbol] = trade
        logger.info(
            "📋 Tracked: %s %s @ %.4f | qty=%.6f | SL=%.4f | TP=%.4f | score=%d",
            trade.direction, trade.symbol, trade.entry_price,
            trade.qty, trade.sl, trade.tp, trade.score,
        )

    def unregister(self, symbol: str, exit_price: float, reason: str):
        trade = self.active.pop(symbol, None)
        if trade is None:
            return

        if trade.direction == "LONG":
            pnl = (exit_price - trade.entry_price) * trade.qty
        else:
            pnl = (trade.entry_price - exit_price) * trade.qty

        fee = (trade.entry_price * trade.qty + exit_price * trade.qty) * cfg.TAKER_FEE
        net = pnl - fee
        rr  = abs(pnl) / (abs(trade.entry_price - trade.sl) * trade.qty + 1e-9)

        record = {
            "time":       _now(),
            "symbol":     symbol,
            "side":       trade.direction,
            "entry":      round(trade.entry_price, 6),
            "exit":       round(exit_price, 6),
            "qty":        round(trade.qty, 6),
            "pnl":        round(net, 4),
            "pnl_pct":    round(net / (trade.entry_price * trade.qty) * 100, 2),
            "reason":     reason,
            "duration":   _elapsed(trade.entry_time),
            "rr":         round(rr, 2),
            "score":      trade.score,
            "signals":    trade.signals,
        }
        self.history.append(record)
        self._write_trade(record)

        emoji = "✅" if net > 0 else "❌"
        logger.info(
            "%s CLOSED %s %s @ %.4f → %.4f | PnL=%.4f (%.2f%%) | %s | R:R=%.1f",
            emoji, trade.direction, symbol, trade.entry_price, exit_price,
            net, record["pnl_pct"], reason, rr,
        )

    def stats(self) -> dict:
        if not self.history:
            return {"total": 0, "wins": 0, "win_rate": 0, "total_pnl": 0,
                    "profit_factor": 0, "avg_rr": 0}
        wins = [t for t in self.history if t["pnl"] > 0]
        losses = [t for t in self.history if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in losses))
        return {
            "total":         len(self.history),
            "wins":          len(wins),
            "win_rate":      round(len(wins) / len(self.history) * 100, 1),
            "total_pnl":     round(sum(t["pnl"] for t in self.history), 4),
            "profit_factor": round(gp / (gl + 1e-9), 2),
            "avg_rr":        round(sum(t["rr"] for t in self.history) / len(self.history), 2),
        }

    def _init_csvs(self):
        if not os.path.exists(LIVE_TRADES_CSV):
            with open(LIVE_TRADES_CSV, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=[
                    "time", "symbol", "side", "entry", "exit", "qty",
                    "pnl", "pnl_pct", "reason", "duration", "rr", "score", "signals",
                ]).writeheader()

    def _write_trade(self, record: dict):
        with open(LIVE_TRADES_CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=list(record.keys())).writerow(record)


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY LOSS GUARD + DRAWDOWN GUARD
# ═══════════════════════════════════════════════════════════════════════��══════

class RiskGuard:
    """Monitors daily loss and total drawdown."""

    def __init__(self, initial_balance: float):
        self.initial_balance = initial_balance
        self.peak_balance    = initial_balance
        self.day_start_bal   = initial_balance
        self.date            = _today()
        self.halted          = False
        self.halt_reason     = ""

    def check(self, current_balance: float) -> bool:
        """Return True if trading is allowed."""
        today = _today()
        if today != self.date:
            # Новый день — сбрасываем дневной лимит
            self.day_start_bal = current_balance
            self.date = today
            self.halted = False
            self.halt_reason = ""
            logger.info("📅 New day — daily loss counter reset. Balance: $%.4f", current_balance)

        # Обновляем пик
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance

        # Дневной лимит убытков
        daily_loss = (self.day_start_bal - current_balance) / (self.day_start_bal + 1e-9)
        if daily_loss >= MAX_DAILY_LOSS:
            if not self.halted:
                self.halt_reason = f"Daily loss {daily_loss*100:.1f}% >= {MAX_DAILY_LOSS*100:.0f}%"
                logger.warning("🛑 %s — HALTED", self.halt_reason)
            self.halted = True
            return False

        # Общая просадка от пика
        drawdown = (self.peak_balance - current_balance) / (self.peak_balance + 1e-9)
        if drawdown >= MAX_DRAWDOWN:
            if not self.halted:
                self.halt_reason = f"Drawdown {drawdown*100:.1f}% >= {MAX_DRAWDOWN*100:.0f}%"
                logger.warning("🛑 %s — HALTED", self.halt_reason)
            self.halted = True
            return False

        self.halted = False
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE  (восстановление после перезапуска)
# ══════════════════════════════════════════════════════════════════════════════

def save_state(guard: RiskGuard, tracker: TradeTracker):
    """Atomically save state to JSON."""
    data = {
        "peak_balance":  guard.peak_balance,
        "day_start_bal": guard.day_start_bal,
        "date":          guard.date,
        "active_trades": {
            sym: {
                "direction":   t.direction,
                "entry_price": t.entry_price,
                "qty":         t.qty,
                "sl":          t.sl,
                "tp":          t.tp,
                "trail_sl":    t.trail_sl,
                "order_id":    t.order_id,
                "entry_time":  t.entry_time,
                "score":       t.score,
                "signals":     t.signals,
            }
            for sym, t in tracker.active.items()
        },
        "updated": _now(),
    }
    # Атомарная запись через tempfile (избегаем race condition с дашбордом)
    try:
        fd, tmp = tempfile.mkstemp(dir=LIVE_LOG_DIR, suffix=".json.tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LIVE_STATE_JSON)  # атомарная операция
    except Exception as e:
        logger.warning("State save error: %s", e)


def load_state(guard: RiskGuard, tracker: TradeTracker):
    """Restore state after restart."""
    if not os.path.exists(LIVE_STATE_JSON):
        return
    try:
        with open(LIVE_STATE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        guard.peak_balance  = data.get("peak_balance", guard.peak_balance)
        guard.day_start_bal = data.get("day_start_bal", guard.day_start_bal)
        guard.date          = data.get("date", guard.date)

        for sym, td in data.get("active_trades", {}).items():
            tracker.active[sym] = LiveTrade(
                symbol=sym,
                direction=td["direction"],
                entry_price=td["entry_price"],
                qty=td["qty"],
                sl=td["sl"],
                tp=td["tp"],
                trail_sl=td.get("trail_sl"),
                order_id=td.get("order_id", ""),
                entry_time=td.get("entry_time", _now()),
                score=td.get("score", 0),
                signals=td.get("signals", ""),
            )
        logger.info(
            "📂 State restored: peak=$%.4f | active=%d trades | date=%s",
            guard.peak_balance, len(tracker.active), guard.date,
        )
    except Exception as e:
        logger.warning("State load error (starting fresh): %s", e)


# ══════════════════════════════════════════════════════════════════════════════
#  EQUITY LOGGING
# ══════════════════════════════════════════════════════════���═══════════════════

def log_equity(balance: float, equity: float):
    """Append equity snapshot to CSV."""
    try:
        write_header = not os.path.exists(LIVE_EQUITY_CSV)
        with open(LIVE_EQUITY_CSV, "a", newline="") as f:
            if write_header:
                csv.writer(f).writerow(["time", "balance", "equity"])
            csv.writer(f).writerow([_now(), round(balance, 4), round(equity, 4)])
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  SYNC TRACKER WITH EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════

def sync_positions(tracker: TradeTracker, exchange_pos: Dict[str, dict]):
    """
    Сверяет локальный трекер с реальными позициями на бирже.
    Если позиция закрылась по SL/TP на сервере — фиксируем в статистике.
    """
    # Позиции, которые были в трекере, но закрылись на бирже
    tracked_symbols = set(tracker.active.keys())
    exchange_symbols = set(exchange_pos.keys())
    closed_symbols = tracked_symbols - exchange_symbols

    for sym in closed_symbols:
        trade = tracker.active[sym]
        # Определяем цену закрытия: TP или SL
        # Поскольку мы не знаем точную цену, используем TP если прибыль вероятна
        ticker = get_ticker(sym)
        last_price = ticker.get("last_price", trade.entry_price)

        # Определяем reason по тому, ближе ли цена к SL или TP
        if trade.direction == "LONG":
            if last_price >= trade.tp:
                reason = "TP"
                exit_price = trade.tp
            else:
                reason = "SL"
                exit_price = trade.trail_sl if trade.trail_sl else trade.sl
        else:
            if last_price <= trade.tp:
                reason = "TP"
                exit_price = trade.tp
            else:
                reason = "SL"
                exit_price = trade.trail_sl if trade.trail_sl else trade.sl

        tracker.unregister(sym, exit_price, reason)

    # Позиции, которые есть на бирже но нет в трекере (открыты вручную?)
    new_on_exchange = exchange_symbols - tracked_symbols
    for sym in new_on_exchange:
        ep = exchange_pos[sym]
        direction = "LONG" if ep["side"] == "Buy" else "SHORT"
        logger.info(
            "⚠️  Untracked position found: %s %s | entry=%.4f | size=%.6f",
            direction, sym, ep["entry_price"], ep["size"],
        )


# ══════════════════════════════════════════════════════════════════════════════
#  TRAILING STOP MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def manage_trailing(tracker: TradeTracker, exchange_pos: Dict[str, dict]):
    """Update trailing stops on exchange for active positions."""
    for sym, trade in tracker.active.items():
        if sym not in exchange_pos:
            continue

        ep = exchange_pos[sym]
        current_price = ep.get("mark_price", 0)
        if current_price <= 0:
            continue

        # Получаем текущий ATR
        try:
            df5 = fetch_klines(sym, cfg.TF_PRIMARY, limit=cfg.LOOKBACK + 10)
            if df5 is None or len(df5) < 20:
                continue
            from indicators import compute_indicators
            df5 = compute_indicators(df5)
            atr = float(df5["atr"].iloc[-1])
        except Exception:
            continue

        new_sl = trade.should_trail(current_price, atr)
        if new_sl is not None:
            info = get_instrument_info(sym)
            if update_trailing_stop(sym, new_sl, info):
                old_sl = trade.trail_sl or trade.sl
                trade.trail_sl = new_sl
                logger.info(
                    "📈 Trailing SL updated: %s %s | %.4f → %.4f | price=%.4f",
                    trade.direction, sym, old_sl, new_sl, current_price,
                )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run():
    # ── Banner ──
    logger.info("=" * 65)
    logger.info("  DepositBooster LIVE BOT v2.0")
    logger.info("=" * 65)
    logger.info("  Leverage:       %dx", LIVE_LEVERAGE)
    logger.info("  Risk/trade:     %.0f%%", LIVE_RISK_PCT * 100)
    logger.info("  Max positions:  %d", LIVE_MAX_POS)
    logger.info("  SL:             %.1f×ATR  |  TP: %.1f×ATR  |  R:R: %.1f",
                LIVE_SL_ATR, LIVE_TP_ATR, LIVE_TP_ATR / LIVE_SL_ATR)
    logger.info("  Trailing:       activate at %.1f×ATR, step %.1f×ATR",
                LIVE_TRAIL_ACTIVATE, LIVE_TRAIL_STEP)
    logger.info("  Min confluence: %d pts", MIN_CONFLUENCE)
    logger.info("  Daily loss cap: %.0f%%  |  Max DD: %.0f%%",
                MAX_DAILY_LOSS * 100, MAX_DRAWDOWN * 100)
    logger.info("  Symbols:        %s", ", ".join(LIVE_SYMBOLS))
    logger.info("=" * 65)

    # ── Verify API ──
    balance = get_wallet_balance()
    if balance <= 0:
        logger.error("❌ Cannot fetch balance. Check API keys and permissions.")
        logger.error("   Required: Contract → Trade (Read + Write)")
        logger.error("   API key env vars: BYBIT_LIVE_KEY, BYBIT_LIVE_SECRET")
        return

    available = get_available_balance()
    logger.info("✅ Connected to Bybit mainnet")
    logger.info("   Wallet balance:    $%.4f USDT", balance)
    logger.info("   Available balance: $%.4f USDT", available)

    # ── Init components ──
    guard   = RiskGuard(initial_balance=balance)
    tracker = TradeTracker()

    # Восстановление состояния после перезапуска
    load_state(guard, tracker)

    # ── Graceful shutdown ──
    shutdown_flag = False

    def signal_handler(signum, frame):
        nonlocal shutdown_flag
        shutdown_flag = True
        logger.info("🔴 Shutdown signal received. Finishing cycle...")

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── Main loop ──
    cycle_count = 0
    while not shutdown_flag:
        try:
            cycle_count += 1
            _cycle(guard, tracker, cycle_count)
            save_state(guard, tracker)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user (KeyboardInterrupt).")
            break
        except Exception as e:
            logger.error("Cycle error: %s", e, exc_info=True)
            time.sleep(ERROR_COOLDOWN)

    # ── Shutdown summary ──
    _print_summary(tracker, guard)
    save_state(guard, tracker)
    logger.info("Bot shut down cleanly.")


def _cycle(guard: RiskGuard, tracker: TradeTracker, cycle_num: int):
    """Single scan → signal → trade cycle."""

    # ── Fetch balance ──
    balance = get_wallet_balance()
    if balance <= 0:
        logger.warning("Balance fetch failed, skipping cycle.")
        time.sleep(CYCLE_INTERVAL)
        return

    # ── Risk check ──
    if not guard.check(balance):
        logger.info("🛑 Halted: %s | Balance: $%.4f", guard.halt_reason, balance)
        time.sleep(CYCLE_INTERVAL)
        return

    # ── Get exchange positions ──
    exchange_pos = get_open_positions()

    # ── Sync tracker with exchange (detect SL/TP fills) ──
    sync_positions(tracker, exchange_pos)

    # ── Manage trailing stops ──
    if tracker.active:
        manage_trailing(tracker, exchange_pos)

    # ─��� Log equity ──
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in exchange_pos.values())
    equity = balance + total_unrealized
    log_equity(balance, equity)

    # ── Status ──
    stats = tracker.stats()
    pos_list = list(exchange_pos.keys()) if exchange_pos else ["none"]
    logger.info(
        "[#%d] Bal=$%.4f | Eq=$%.4f | Pos=%s | Trades=%d WR=%.0f%% PF=%.2f",
        cycle_num, balance, equity, pos_list,
        stats["total"], stats["win_rate"], stats["profit_factor"],
    )

    # ── Check capacity ──
    if len(exchange_pos) >= LIVE_MAX_POS:
        logger.info("Max positions reached (%d/%d). Waiting...",
                     len(exchange_pos), LIVE_MAX_POS)
        time.sleep(CYCLE_INTERVAL)
        return

    # ── Scan for signals ──
    for symbol in LIVE_SYMBOLS:
        if symbol in exchange_pos:
            continue

        try:
            # Fetch candles on 3 timeframes
            df5  = fetch_klines(symbol, cfg.TF_PRIMARY, limit=cfg.LOOKBACK + 10)
            df15 = fetch_klines(symbol, cfg.TF_CONFIRM, limit=60)
            df1h = fetch_klines(symbol, cfg.TF_TREND,   limit=60)

            if df5 is None or len(df5) < cfg.LOOKBACK:
                continue

            funding = get_funding_rate(symbol)
            sig = score_signal(df5, df15, df1h, funding_rate=funding)

            # score_signal() уже учитывает cfg.MIN_CONFLUENCE, REQUIRE_BREAKOUT, FUNDING_FILTER
            if sig["direction"] not in ("LONG", "SHORT"):
                continue
            if sig["score"] < MIN_CONFLUENCE:
                continue

            price = sig["price"]
            atr   = sig["atr"]

            logger.info(
                "🔔 Signal: %s %s | score=%d | funding=%.4f%% | %s",
                sig["direction"], symbol, sig["score"],
                funding * 100, sig["signals"],
            )

            # ── Pre-trade checks ──
            available = get_available_balance()
            if available < MIN_NOTIONAL * 1.5:
                logger.warning("Insufficient available balance ($%.2f). Skipping.", available)
                continue

            info = get_instrument_info(symbol)
            qty  = calc_qty(price, atr, available, info)
            if qty is None:
                logger.warning("Skipping %s — cannot calc valid qty for $%.2f", symbol, available)
                continue

            # SL / TP
            if sig["direction"] == "LONG":
                sl = price - LIVE_SL_ATR * atr
                tp = price + LIVE_TP_ATR * atr
            else:
                sl = price + LIVE_SL_ATR * atr
                tp = price - LIVE_TP_ATR * atr

            # Validate SL/TP sanity
            if sl <= 0 or tp <= 0:
                logger.warning("Invalid SL=%.4f or TP=%.4f for %s. Skipping.", sl, tp, symbol)
                continue

            notional = qty * price
            logger.info(
                "📝 Preparing: %s %s | price=%.4f | qty=%.6f | $%.2f | SL=%.4f | TP=%.4f",
                sig["direction"], symbol, price, qty, notional, sl, tp,
            )

            # ── Set leverage & position mode ──
            ensure_one_way_mode(symbol)
            set_leverage(symbol, LIVE_LEVERAGE)
            time.sleep(0.3)

            # ── Place order ──
            order_id = place_order(symbol, sig["direction"], qty, sl, tp, info)
            if order_id:
                # Track locally
                tracker.register(LiveTrade(
                    symbol=symbol,
                    direction=sig["direction"],
                    entry_price=price,
                    qty=qty,
                    sl=sl,
                    tp=tp,
                    order_id=order_id,
                    score=sig["score"],
                    signals=",".join(sig["signals"]),
                ))
                break  # только 1 позиция за цикл

        except Exception as e:
            logger.warning("Error processing %s: %s", symbol, e, exc_info=True)

    time.sleep(CYCLE_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def _print_summary(tracker: TradeTracker, guard: RiskGuard):
    s = tracker.stats()
    balance = get_wallet_balance()
    print()
    print("=" * 55)
    print("  DEPOSIT BOOSTER — LIVE SESSION SUMMARY")
    print("=" * 55)
    print(f"  Balance:        ${balance:.4f} USDT")
    print(f"  Peak balance:   ${guard.peak_balance:.4f}")
    print(f"  Total trades:   {s['total']}")
    print(f"  Wins / Losses:  {s['wins']} / {s['total'] - s['wins']}")
    print(f"  Win rate:       {s['win_rate']:.1f}%")
    print(f"  Total PnL:      ${s['total_pnl']:+.4f}")
    print(f"  Profit factor:  {s['profit_factor']:.2f}")
    print(f"  Avg R:R:        {s['avg_rr']:.2f}")
    print(f"  Active pos:     {list(tracker.active.keys()) or 'none'}")
    print("=" * 55)
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _elapsed(start_str: str) -> str:
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        secs = int((datetime.now(timezone.utc) - start).total_seconds())
        h, m = divmod(secs // 60, 60)
        return f"{h}h {m}m"
    except Exception:
        return "?"


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run()