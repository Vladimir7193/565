"""
DepositBooster — config.py
Aggressive paper trading bot for $100 deposit growth on Bybit Futures.
"""
import os

# ── Exchange ──────────────────────────────────────────────────────────────────
API_KEY    = os.getenv("BYBIT_API_KEY",    "YOUR_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET", "YOUR_API_SECRET")
TESTNET    = False  # False = mainnet prices (paper trade, no real orders)

# ── Starting deposit ──────────────────────────────────────────────────────────
INITIAL_BALANCE = 100.0   # USDT

# ── Pairs to scan (high-volatility, good for breakouts) ──────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "ARBUSDT",
    "APTUSDT", "INJUSDT", "SUIUSDT", "OPUSDT", "FETUSDT",
]

CATEGORY = "linear"   # USDT-margined perpetuals

# ── Timeframes ────────────────────────────────────────────────────────────────
TF_PRIMARY   = "5"    # 5m — entry signals
TF_CONFIRM   = "15"   # 15m — trend confirmation
TF_TREND     = "60"   # 1h — macro trend filter

# ── Leverage & sizing ─────────────────────────────────────────────────────────
LEVERAGE         = 20          # 20x leverage for aggressive growth
RISK_PCT         = 0.03        # 3% of equity per trade
MAX_POSITIONS    = 3           # max simultaneous open positions
MAX_DAILY_LOSS   = 0.10        # halt if daily loss > 10%
MAX_DRAWDOWN     = 0.20        # halt if drawdown from peak > 20%

# ── SL / TP ───────────────────────────────────────────────────────────────────
SL_ATR_MULT          = 1.5    # wider SL to avoid noise wipeouts
TP_ATR_MULT          = 4.5    # R:R = 3.0
TRAIL_ACTIVATE_ATR   = 2.0    # activate trailing after 2×ATR profit
TRAIL_STEP_ATR       = 1.2    # trailing step (wider than original SL to avoid premature exits)

# ── Signal thresholds ─────────────────────────────────────────────────────────
MIN_CONFLUENCE       = 4       # need 4 signals minimum
REQUIRE_BREAKOUT     = True   # must have BREAKOUT or SWEEP — no EMA-only entries
BREAKOUT_ATR_MULT    = 0.8    # breakout must exceed swing level by at least 0.8×ATR
BREAKOUT_VOL_Z       = 1.5    # volume Z-score min for breakout confirmation
SWING_LOOKBACK       = 20     # bars for swing high/low detection

# ── Funding rate filter ───────────────────────────────────────────────────────
FUNDING_FILTER       = True   # skip trades against overheated funding
FUNDING_MAX_LONG     = 0.0010  # skip LONG if funding > +0.10% (longs paying)
FUNDING_MAX_SHORT    = -0.0010 # skip SHORT if funding < -0.10% (shorts paying)

# ── Indicators ────────────────────────────────────────────────────────────────
ATR_PERIOD   = 14
EMA_FAST     = 9
EMA_SLOW     = 21
EMA_TREND    = 50
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
BB_PERIOD    = 20
BB_STD       = 2.0
LOOKBACK     = 100   # candles needed

# ── Fees ──────────────────────────────────────────────────────────────────────
TAKER_FEE       = 0.00055
ROUND_TRIP_FEE  = TAKER_FEE * 2

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR         = "logs"
TRADES_CSV      = "logs/trades.csv"
SIGNALS_CSV     = "logs/signals.csv"
EQUITY_CSV      = "logs/equity.csv"

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_PORT  = 8501
REFRESH_SEC     = 5    # dashboard auto-refresh interval
