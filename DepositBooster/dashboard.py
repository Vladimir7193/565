"""
dashboard.py — DepositBooster paper trading dashboard.
Run: streamlit run dashboard.py
"""
from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import config as cfg
from market_data import fetch_klines, get_ticker, get_funding_rate
from indicators import score_signal, compute_indicators

st.set_page_config(
    page_title="DepositBooster",
    page_icon="🚀",
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
#  DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=cfg.REFRESH_SEC)
def load_trades() -> pd.DataFrame:
    if not os.path.exists(cfg.TRADES_CSV):
        return pd.DataFrame()
    try:
        df = pd.read_csv(cfg.TRADES_CSV)
        if df.empty:
            return df
        df["time"] = pd.to_datetime(df["time"], format="%Y-%m-%d %H:%M:%S")
        return df.sort_values("time")
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=cfg.REFRESH_SEC)
def load_equity() -> pd.DataFrame:
    if not os.path.exists(cfg.EQUITY_CSV):
        return pd.DataFrame()
    try:
        df = pd.read_csv(cfg.EQUITY_CSV, names=["time", "equity", "balance"])
        df["time"] = pd.to_datetime(df["time"], format="%Y-%m-%d %H:%M:%S")
        return df.sort_values("time")
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=30)
def scan_signals() -> list[dict]:
    results = []
    for symbol in cfg.SYMBOLS:
        try:
            df5    = fetch_klines(symbol, cfg.TF_PRIMARY,  limit=cfg.LOOKBACK + 10)
            df15   = fetch_klines(symbol, cfg.TF_CONFIRM,  limit=60)
            df1h   = fetch_klines(symbol, cfg.TF_TREND,    limit=60)
            ticker = get_ticker(symbol)
            funding = ticker.get("funding_rate", 0.0)
            if df5 is None or len(df5) < 30:
                continue
            sig = score_signal(df5, df15, df1h, funding_rate=funding)
            fr_status = "🔴 HOT" if funding > cfg.FUNDING_MAX_LONG else ("🔵 COLD" if funding < cfg.FUNDING_MAX_SHORT else "🟢 OK")
            results.append({
                "symbol":     symbol,
                "direction":  sig["direction"],
                "score":      sig["score"],
                "signals":    ", ".join(sig["signals"]),
                "price":      sig["price"],
                "atr":        round(sig["atr"], 4),
                "funding%":   round(funding * 100, 4),
                "fr_status":  fr_status,
                "24h%":       round(ticker.get("price_change_pct", 0), 2),
            })
        except Exception:
            pass
    return sorted(results, key=lambda x: x["score"], reverse=True)

def read_open_positions() -> list[dict]:
    """Read current open positions from JSON written by paper_engine."""
    import json
    path = f"{cfg.LOG_DIR}/positions.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🚀 DepositBooster")
    st.caption(f"Paper Trade | Bybit {'Testnet' if cfg.TESTNET else 'Mainnet'}")
    st.divider()

    equity_df = load_equity()
    trades_df = load_trades()

    if not equity_df.empty:
        cur_eq  = equity_df["equity"].iloc[-1]
        cur_bal = equity_df["balance"].iloc[-1]
        growth  = (cur_eq - cfg.INITIAL_BALANCE) / cfg.INITIAL_BALANCE * 100
        peak    = equity_df["equity"].max()
        dd      = (peak - cur_eq) / peak * 100
    else:
        cur_eq = cur_bal = cfg.INITIAL_BALANCE
        growth = dd = 0.0

    st.metric("💰 Equity",  f"${cur_eq:.2f}",  delta=f"{growth:+.1f}%")
    st.metric("🏦 Balance", f"${cur_bal:.2f}")
    st.metric("📈 Growth",  f"{growth:+.1f}%")
    st.metric("📉 Drawdown", f"{dd:.1f}%")

    if not trades_df.empty:
        wins = (trades_df["pnl"] > 0).sum()
        total = len(trades_df)
        st.metric("🎯 Win Rate", f"{wins/total*100:.0f}%", delta=f"{total} trades")

    st.divider()
    st.caption("Strategy: Momentum Breakout + SMC")
    st.caption(f"R:R = {cfg.TP_ATR_MULT/cfg.SL_ATR_MULT:.1f} | SL={cfg.SL_ATR_MULT}×ATR | TP={cfg.TP_ATR_MULT}×ATR")
    st.caption(f"Funding filter: {'ON ✅' if cfg.FUNDING_FILTER else 'OFF'}")
    st.divider()

    page = st.radio("Navigation", ["📊 Overview", "🔍 Scanner", "📈 Trades", "📉 Chart", "⚙️ Config"])

    st.divider()
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
#  PAGES
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
if page == "📊 Overview":
    st.title("📊 Portfolio Overview")

    # KPI row
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("💰 Equity",   f"${cur_eq:.2f}",  delta=f"{growth:+.1f}%")
    c2.metric("🏦 Balance",  f"${cur_bal:.2f}")
    c3.metric("📈 Growth",   f"{growth:+.1f}%",
              delta="🏆 Done!" if growth >= 100 else f"Target: +100%")
    if not trades_df.empty:
        wins  = trades_df[trades_df["pnl"] > 0]
        losses = trades_df[trades_df["pnl"] <= 0]
        wr    = len(wins) / len(trades_df) * 100
        gp    = wins["pnl"].sum()
        gl    = abs(losses["pnl"].sum())
        c4.metric("🎯 Win Rate", f"{wr:.0f}%",  delta=f"{len(trades_df)} trades")
        c5.metric("⚖️ PF",       f"{gp/(gl+1e-9):.2f}")
    else:
        c4.metric("🎯 Win Rate", "—")
        c5.metric("⚖️ PF", "—")
    c6.metric("📉 Drawdown", f"{dd:.1f}%", delta_color="inverse")

    st.divider()

    # Open positions
    st.subheader("🔓 Open Positions")
    open_pos = read_open_positions()
    if open_pos:
        for pos in open_pos:
            side = pos.get("side", "")
            sym  = pos.get("symbol", "")
            entry = pos.get("entry_price", 0)
            sl    = pos.get("sl", 0)
            tp    = pos.get("tp", 0)
            score = pos.get("score", 0)
            line  = f"{side} {sym} | entry={entry:.4f} | SL={sl:.4f} | TP={tp:.4f} | score={score}"
            if side == "LONG":
                st.success(f"🟢 {line}")
            else:
                st.error(f"🔴 {line}")
    else:
        st.info("No open positions yet. Bot is scanning...")

    st.divider()

    # Equity curve
    st.subheader("📈 Equity Curve")
    if not equity_df.empty and len(equity_df) > 1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=equity_df["time"], y=equity_df["equity"],
            mode="lines", name="Equity",
            line=dict(color="#00ff88", width=2),
            fill="tozeroy", fillcolor="rgba(0,255,136,0.05)",
        ))
        fig.add_trace(go.Scatter(
            x=equity_df["time"], y=equity_df["balance"],
            mode="lines", name="Balance",
            line=dict(color="#4488ff", width=1, dash="dot"),
        ))
        fig.add_hline(y=cfg.INITIAL_BALANCE, line_dash="dash",
                      line_color="gray", annotation_text="Start $100")
        fig.update_layout(
            xaxis_title="Time", yaxis_title="USDT",
            template="plotly_dark", height=350,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Equity curve will appear after the first position closes.")

    # Recent trades
    if not trades_df.empty:
        st.subheader("🕐 Recent Trades")
        recent = trades_df.tail(10).sort_values("time", ascending=False).copy()
        recent["pnl"]     = recent["pnl"].apply(lambda x: f"{x:+.4f}")
        recent["pnl_pct"] = recent["pnl_pct"].apply(lambda x: f"{x:+.3f}%")
        st.dataframe(
            recent[["time","symbol","side","entry","exit","pnl","pnl_pct","reason","rr","score"]],
            use_container_width=True, hide_index=True,
        )

# ══════════════════════════════════════════════════════════════════════════════
elif page == "🔍 Scanner":
    st.title("🔍 Live Signal Scanner")
    st.caption("Scans all pairs for Momentum Breakout + SMC confluence.")

    col1, col2 = st.columns([2, 1])
    with col1:
        dir_filter = st.multiselect("Direction", ["LONG", "SHORT", "HOLD"], default=["LONG", "SHORT"])
    with col2:
        min_score = st.slider("Min score", 0, 9, cfg.MIN_CONFLUENCE)

    with st.spinner("Scanning markets..."):
        signals = scan_signals()

    longs  = sum(1 for s in signals if s["direction"] == "LONG")
    shorts = sum(1 for s in signals if s["direction"] == "SHORT")
    holds  = sum(1 for s in signals if s["direction"] == "HOLD")

    c1, c2, c3 = st.columns(3)
    c1.metric("🟢 LONG",  longs)
    c2.metric("🔴 SHORT", shorts)
    c3.metric("⚪ HOLD",  holds)

    st.divider()

    filtered = [s for s in signals if s["direction"] in dir_filter and s["score"] >= min_score]
    if filtered:
        df_sig = pd.DataFrame(filtered)
        df_sig["price"]   = df_sig["price"].apply(lambda x: f"{x:.4f}")
        df_sig["atr"]     = df_sig["atr"].apply(lambda x: f"{x:.4f}")
        df_sig["24h%"]    = df_sig["24h%"].apply(lambda x: f"{x:+.2f}%")
        df_sig["funding%"] = df_sig["funding%"].apply(lambda x: f"{x:+.4f}%")
        st.dataframe(
            df_sig[["symbol","direction","score","price","atr","funding%","fr_status","24h%","signals"]],
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No signals matching filters.")

# ══════════════════════════════════════════════════════════════════════════════
elif page == "📈 Trades":
    st.title("📈 Trade History")

    trades_df = load_trades()
    if trades_df.empty:
        st.info("No trades yet. Bot is running and scanning...")
    else:
        wins   = trades_df[trades_df["pnl"] > 0]
        losses = trades_df[trades_df["pnl"] <= 0]
        total_pnl = trades_df["pnl"].sum()
        wr  = len(wins) / len(trades_df) * 100
        gp  = wins["pnl"].sum()
        gl  = abs(losses["pnl"].sum())
        pf  = gp / (gl + 1e-9)

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Total PnL",     f"${total_pnl:+.4f}")
        c2.metric("Win Rate",      f"{wr:.1f}%")
        c3.metric("Profit Factor", f"{pf:.2f}")
        c4.metric("Avg R:R",       f"{trades_df['rr'].mean():.2f}")
        c5.metric("Best Trade",    f"${trades_df['pnl'].max():+.4f}")
        c6.metric("Worst Trade",   f"${trades_df['pnl'].min():+.4f}")

        st.divider()

        # Cumulative PnL chart
        trades_df["cum_equity"] = cfg.INITIAL_BALANCE + trades_df["pnl"].cumsum()
        colors = ["#00ff88" if v >= 0 else "#ff4444" for v in trades_df["pnl"]]
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=trades_df["time"], y=trades_df["pnl"],
            name="Trade PnL", marker_color=colors, opacity=0.7,
        ))
        fig.add_trace(go.Scatter(
            x=trades_df["time"], y=trades_df["cum_equity"],
            name="Equity", yaxis="y2",
            line=dict(color="#ffaa00", width=2),
        ))
        fig.update_layout(
            yaxis=dict(title="PnL (USDT)"),
            yaxis2=dict(title="Equity", overlaying="y", side="right"),
            template="plotly_dark", height=350,
            legend=dict(orientation="h"),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            rc = trades_df["reason"].value_counts().reset_index()
            rc.columns = ["reason", "count"]
            fig2 = px.pie(rc, values="count", names="reason", title="Exit Reasons",
                          template="plotly_dark",
                          color_discrete_map={"TP": "#00ff88", "SL": "#ff4444"})
            st.plotly_chart(fig2, use_container_width=True)
        with col_b:
            by_sym = trades_df.groupby("symbol")["pnl"].sum().reset_index().sort_values("pnl")
            bar_colors = ["#ff4444" if v < 0 else "#00ff88" for v in by_sym["pnl"]]
            fig3 = go.Figure(go.Bar(
                x=by_sym["pnl"], y=by_sym["symbol"],
                orientation="h", marker_color=bar_colors,
            ))
            fig3.update_layout(title="PnL by Symbol", template="plotly_dark",
                               height=300, margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig3, use_container_width=True)

        st.divider()
        st.subheader("All Trades")
        display = trades_df.sort_values("time", ascending=False).copy()
        display["pnl"]     = display["pnl"].apply(lambda x: f"{x:+.4f}")
        display["pnl_pct"] = display["pnl_pct"].apply(lambda x: f"{x:+.3f}%")
        st.dataframe(
            display[["time","symbol","side","entry","exit","pnl","pnl_pct","reason","rr","score","signals"]],
            use_container_width=True, hide_index=True,
        )

# ══════════════════════════════════════════════════════════════════════════════
elif page == "📉 Chart":
    st.title("📉 Price Chart")

    col1, col2 = st.columns([2, 1])
    with col1:
        sym = st.selectbox("Symbol", cfg.SYMBOLS)
    with col2:
        tf  = st.selectbox("Timeframe", ["1", "5", "15", "60", "240"], index=1)

    with st.spinner(f"Loading {sym} {tf}m..."):
        cdf = fetch_klines(sym, tf, limit=100)

    if cdf is not None and len(cdf) > 20:
        cdf = compute_indicators(cdf)
        x   = cdf.index if "timestamp" not in cdf.columns else cdf["timestamp"]

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=x, open=cdf["open"], high=cdf["high"],
            low=cdf["low"], close=cdf["close"], name="Price",
            increasing_line_color="#00ff88",
            decreasing_line_color="#ff4444",
        ))
        fig.add_trace(go.Scatter(x=x, y=cdf["ema_fast"],  name=f"EMA{cfg.EMA_FAST}",  line=dict(color="#ffaa00", width=1)))
        fig.add_trace(go.Scatter(x=x, y=cdf["ema_slow"],  name=f"EMA{cfg.EMA_SLOW}",  line=dict(color="#4488ff", width=1)))
        fig.add_trace(go.Scatter(x=x, y=cdf["ema_trend"], name=f"EMA{cfg.EMA_TREND}", line=dict(color="#ff88ff", width=1, dash="dot")))
        fig.add_trace(go.Scatter(x=x, y=cdf["bb_upper"],  name="BB Upper", line=dict(color="rgba(100,100,255,0.4)", width=1)))
        fig.add_trace(go.Scatter(x=x, y=cdf["bb_lower"],  name="BB Lower", line=dict(color="rgba(100,100,255,0.4)", width=1),
                                  fill="tonexty", fillcolor="rgba(100,100,255,0.04)"))
        fig.update_layout(
            xaxis_rangeslider_visible=False,
            template="plotly_dark", height=500,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)

        # MACD
        fig2 = go.Figure()
        macd_colors = ["#00ff88" if v >= 0 else "#ff4444" for v in cdf["macd_hist"]]
        fig2.add_trace(go.Bar(x=x, y=cdf["macd_hist"], name="MACD Hist", marker_color=macd_colors))
        fig2.add_trace(go.Scatter(x=x, y=cdf["macd"],     name="MACD",   line=dict(color="#ffaa00", width=1)))
        fig2.add_trace(go.Scatter(x=x, y=cdf["macd_sig"], name="Signal", line=dict(color="#4488ff", width=1)))
        fig2.update_layout(template="plotly_dark", height=180,
                           showlegend=True, margin=dict(l=0, r=0, t=5, b=0))
        st.plotly_chart(fig2, use_container_width=True)

        # Current signal
        df5  = fetch_klines(sym, "5",  limit=cfg.LOOKBACK + 10)
        df15 = fetch_klines(sym, "15", limit=60)
        df1h = fetch_klines(sym, "60", limit=60)
        if df5 is not None:
            ticker  = get_ticker(sym)
            funding = ticker.get("funding_rate", 0.0)
            sig = score_signal(df5, df15, df1h, funding_rate=funding)
            icon = {"LONG": "🟢", "SHORT": "🔴", "HOLD": "⚪"}.get(sig["direction"], "⚪")
            st.info(
                f"{icon} **{sig['direction']}** | Score: **{sig['score']}** | "
                f"Funding: {funding*100:+.4f}% | "
                f"Signals: {', '.join(sig['signals']) if sig['signals'] else 'none'}"
            )
    else:
        st.warning("Could not load chart data.")

# ══════════════════════════════════════════════════════════════════════════════
elif page == "⚙️ Config":
    st.title("⚙️ Configuration")

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Exchange")
        st.code(f"Mode:     {'Testnet (Paper)' if cfg.TESTNET else 'Mainnet'}\nCategory: {cfg.CATEGORY}")

        st.subheader("Risk Management")
        st.code(
            f"Initial Balance: ${cfg.INITIAL_BALANCE}\n"
            f"Leverage:        {cfg.LEVERAGE}x\n"
            f"Risk/Trade:      {cfg.RISK_PCT*100:.0f}%\n"
            f"Max Positions:   {cfg.MAX_POSITIONS}\n"
            f"Max Daily Loss:  {cfg.MAX_DAILY_LOSS*100:.0f}%\n"
            f"Max Drawdown:    {cfg.MAX_DRAWDOWN*100:.0f}%"
        )

    with col_b:
        st.subheader("Strategy")
        st.code(
            f"Strategy:        Momentum Breakout + SMC\n"
            f"Min Confluence:  {cfg.MIN_CONFLUENCE} pts\n"
            f"Breakout ATR:    {cfg.BREAKOUT_ATR_MULT}×\n"
            f"Breakout Vol Z:  ≥{cfg.BREAKOUT_VOL_Z}\n"
            f"Swing Lookback:  {cfg.SWING_LOOKBACK} bars\n"
            f"Funding Filter:  {'ON' if cfg.FUNDING_FILTER else 'OFF'}"
        )

        st.subheader("SL / TP")
        st.code(
            f"SL:    {cfg.SL_ATR_MULT}×ATR\n"
            f"TP:    {cfg.TP_ATR_MULT}×ATR\n"
            f"R:R:   {cfg.TP_ATR_MULT/cfg.SL_ATR_MULT:.1f}\n"
            f"Trail: activates at {cfg.TRAIL_ACTIVATE_ATR}×ATR profit"
        )

    st.divider()
    st.subheader("How to run")
    st.code(
        "# Terminal 1 — trading bot\npython bot.py\n\n"
        "# Terminal 2 — dashboard\nstreamlit run dashboard.py",
        language="bash",
    )
