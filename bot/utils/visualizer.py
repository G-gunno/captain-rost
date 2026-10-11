import pandas as pd
import httpx
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from loguru import logger
import json
import os
from pathlib import Path
from bot.utils.format import fmt_price
from bot.strategy.indicators import find_sr_levels

LOG_TIMEZONE = "Europe/Moscow" 

class TradeVisualizer:
    def __init__(self, log_path: str, symbol: str, interval: str = "15"):
        self.state_path = Path(os.getenv("STORAGE_DIR", "storage")) / "paper_state.json"
        self.symbol = symbol.upper()
        self.base_sym = self.symbol.replace("USDT", "")
        self.interval = interval
        
    def fetch_klines(self, start_ts: int, end_ts: int, interval_override: str = None) -> pd.DataFrame:
        url = "https://api.bybit.com/v5/market/kline"
        all_candles = []
        curr_end_ms = end_ts * 1000
        start_ms = start_ts * 1000
        active_interval = str(interval_override or self.interval)

        with httpx.Client(timeout=15) as client:
            while True:
                resp = client.get(url, params={
                    "category": "spot", "symbol": self.symbol, 
                    "interval": active_interval, "start": start_ms, 
                    "end": curr_end_ms, "limit": 1000
                })
                data = resp.json()
                if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
                    break
                batch = data["result"]["list"]
                all_candles.extend(batch)
                oldest = int(batch[-1][0])
                if len(batch) < 1000 or oldest <= start_ms:
                    break
                curr_end_ms = oldest - 1

        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame(all_candles, columns=["start_time", "open", "high", "low", "close", "volume", "turnover"])
        df["start_time"] = pd.to_numeric(df["start_time"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["datetime"] = pd.to_datetime(df["start_time"], unit="ms", utc=True).dt.tz_convert(LOG_TIMEZONE).dt.tz_localize(None)
        return df.sort_values("datetime").reset_index(drop=True)

    def build_chart(self, show: bool = True):
        if not self.state_path.exists():
            return None
            
        data = json.loads(self.state_path.read_text())
        events = [e for e in data.get("chart_events", []) if e.get("sym") == self.symbol]
        
        if not events:
            return None
            
        df_ev = pd.DataFrame(events)
        df_ev['datetime'] = pd.to_datetime(df_ev['ts'], unit='s', utc=True).dt.tz_convert(LOG_TIMEZONE).dt.tz_localize(None)
        
        start_ts = df_ev['ts'].min() - 7200
        end_ts = df_ev['ts'].max() + 3600
        
        df_kline = self.fetch_klines(start_ts, end_ts)
        if df_kline.empty:
            return None

        df_kline['EMA21'] = df_kline['close'].ewm(span=21, adjust=False).mean()
        df_kline['EMA50'] = df_kline['close'].ewm(span=50, adjust=False).mean()
        
        delta = df_kline['close'].diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df_kline['RSI'] = 100 - (100 / (1 + rs))

        df_kline = df_kline.tail(200).reset_index(drop=True)
        min_time = df_kline['datetime'].min()
        max_time = df_kline['datetime'].max()
        df_ev = df_ev[df_ev['datetime'] >= min_time]

        colors_vol = ['rgba(38, 166, 154, 0.5)' if row['close'] >= row['open'] else 'rgba(239, 83, 80, 0.5)' for _, row in df_kline.iterrows()]

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, 
                            vertical_spacing=0.03, row_heights=[0.6, 0.2, 0.2])

        # === 1 СЛОЙ: Свечи и EMA ===
        fig.add_trace(go.Candlestick(
            x=df_kline['datetime'], open=df_kline['open'], high=df_kline['high'], 
            low=df_kline['low'], close=df_kline['close'], name='Цена',
            increasing_line_color='rgba(38, 166, 154, 0.8)', decreasing_line_color='rgba(239, 83, 80, 0.8)'
        ), row=1, col=1)

        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['EMA21'], mode='lines', name='EMA 21', line=dict(color='rgba(255, 235, 59, 0.8)', width=1.5)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['EMA50'], mode='lines', name='EMA 50', line=dict(color='rgba(255, 152, 0, 0.8)', width=1.5)), row=1, col=1)

        # ⚡ РАСЧЕТ S/R ВСЕГДА ПО 15M (СИНХРОНИЗАЦИЯ С МОЗГОМ БОТА)
        if self.interval == "15":
            df_sr = df_kline
        else:
            df_sr = self.fetch_klines(start_ts, end_ts, interval_override="15")
            if df_sr.empty:
                df_sr = df_kline

        candles_sr = df_sr.to_dict('records')
        nearest_res, res_touches, nearest_sup, sup_touches = find_sr_levels(candles_sr)
        
        if nearest_res:
            fig.add_trace(go.Scatter(
                x=[min_time, max_time], y=[nearest_res, nearest_res],
                mode="lines+text", name=f"Сопротивление ({res_touches}x)",
                line=dict(color="rgba(233, 30, 99, 0.85)", width=1.5, dash="dot"),
                text=["", f" 🧱 Сопротивление ${fmt_price(nearest_res)} ({res_touches}x)"],
                textposition="middle left", textfont=dict(color="rgba(233, 30, 99, 0.9)", size=11)
            ), row=1, col=1)

        if nearest_sup:
            fig.add_trace(go.Scatter(
                x=[min_time, max_time], y=[nearest_sup, nearest_sup],
                mode="lines+text", name=f"Поддержка ({sup_touches}x)",
                line=dict(color="rgba(33, 150, 243, 0.85)", width=1.5, dash="dot"),
                text=["", f" 🛡 Поддержка ${fmt_price(nearest_sup)} ({sup_touches}x)"],
                textposition="middle left", textfont=dict(color="rgba(33, 150, 243, 0.9)", size=11)
            ), row=1, col=1)

        # === 2 СЛОЙ: Объем ===
        fig.add_trace(go.Bar(x=df_kline['datetime'], y=df_kline['volume'], name='Объем', marker_color=colors_vol), row=2, col=1)

        # === 3 СЛОЙ: RSI ===
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['RSI'], mode='lines', name='RSI', line=dict(color='rgba(156, 39, 176, 0.8)', width=1.5)), row=3, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="rgba(239, 83, 80, 0.5)", row=3, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="rgba(38, 166, 154, 0.5)", row=3, col=1)

        # Режимы рынка
        market_hist = data.get("market_history", [])
        if market_hist:
            reg_colors = {
                "bull": "rgba(38, 166, 154, 0.08)",
                "neutral": "rgba(255, 235, 59, 0.04)",
                "bear": "rgba(239, 83, 80, 0.08)"
            }
            sorted_hist = sorted(market_hist, key=lambda x: x.get("ts", 0))
            
            for i, h in enumerate(sorted_hist):
                t_start = pd.to_datetime(h.get("ts", 0), unit='s', utc=True).tz_convert(LOG_TIMEZONE).tz_localize(None)
                if i + 1 < len(sorted_hist):
                    t_end = pd.to_datetime(sorted_hist[i+1].get("ts", 0), unit='s', utc=True).tz_convert(LOG_TIMEZONE).tz_localize(None)
                else:
                    t_end = max_time
                
                if t_end < min_time or t_start > max_time:
                    continue
                v_x0 = max(min_time, t_start)
                v_x1 = min(max_time, t_end)
                
                if v_x1 > v_x0:
                    reg = h.get("regime", "neutral")
                    c = reg_colors.get(reg, reg_colors["neutral"])
                    fig.add_vrect(x0=v_x0, x1=v_x1, fillcolor=c, line_width=0, layer="below")

        # TP / SL уровни открытой позиции
        pos = data.get("positions", {}).get(self.symbol)
        if pos:
            entry_ts = pos.get("entry_time", start_ts)
            t_start = max(min_time, pd.to_datetime(entry_ts, unit='s', utc=True).tz_convert(LOG_TIMEZONE).tz_localize(None))
            t_end = max_time
            
            if pos.get("tp"):
                tp_val = pos["tp"]
                tp_name = "🎯 TP Runner" if pos.get("tp1_done") else "🎯 TP1"
                tp_col = "rgba(0, 229, 255, 0.9)" if pos.get("tp1_done") else "rgba(0, 230, 118, 0.9)"
                fig.add_trace(go.Scatter(
                    x=[t_start, t_end], y=[tp_val, tp_val],
                    mode="lines+text", name=tp_name,
                    line=dict(color=tp_col, width=1.5, dash="dash"),
                    text=["", f" {tp_name} ({fmt_price(tp_val)})"],
                    textposition="middle right", textfont=dict(color=tp_col, size=11)
                ), row=1, col=1)
                
            if pos.get("sl"):
                sl_val = pos["sl"]
                is_be = sl_val >= pos.get("avg", 0)
                sl_name = "🔒 Безубыток" if is_be else "🛡 Стоп (SL)"
                sl_col = "rgba(255, 152, 0, 0.9)" if is_be else "rgba(255, 23, 68, 0.9)"
                fig.add_trace(go.Scatter(
                    x=[t_start, t_end], y=[sl_val, sl_val],
                    mode="lines+text", name=sl_name,
                    line=dict(color=sl_col, width=1.5, dash="dash"),
                    text=["", f" {sl_name} ({fmt_price(sl_val)})"],
                    textposition="middle right", textfont=dict(color=sl_col, size=11)
                ), row=1, col=1)

        # Исторические уровни TP1 / SL
        buys = df_ev[df_ev['type'] == 'buy']
        for _, b_row in buys.iterrows():
            t_b = b_row['datetime']
            tp_b = b_row.get('tp')
            sl_b = b_row.get('sl')
            exits = df_ev[(df_ev['datetime'] > t_b) & (df_ev['type'].isin(['sell_profit', 'sell_loss', 'sell', 'tp_runner']))]
            t_ex = exits['datetime'].min() if not exits.empty else max_time
            
            if pd.notna(tp_b) and tp_b > 0:
                fig.add_trace(go.Scatter(
                    x=[t_b, t_ex], y=[tp_b, tp_b],
                    mode="lines", name="Цель TP1 (исходная)",
                    line=dict(color="rgba(0, 230, 118, 0.4)", width=1.2, dash="dash"),
                    hoverinfo="skip", showlegend=False
                ), row=1, col=1)
            if pd.notna(sl_b) and sl_b > 0:
                fig.add_trace(go.Scatter(
                    x=[t_b, t_ex], y=[sl_b, sl_b],
                    mode="lines", name="Стоп SL (исходный)",
                    line=dict(color="rgba(255, 23, 68, 0.4)", width=1.2, dash="dash"),
                    hoverinfo="skip", showlegend=False
                ), row=1, col=1)

        colors = {
            "order_placed": ("cyan", "line-ew", "Ордер"),
            "order_moved": ("blue", "diamond-open", "Сдвиг"),
            "buy": ("lime", "triangle-up", "Покупка"),
            "tp1": ("#00e676", "star", "TP1 🎯"),
            "tp_runner": ("#00e5ff", "diamond", "TP Runner 🚀"),
            "sell_profit": ("#00e676", "x", "Профит"),
            "sell_loss": ("#ff1744", "x", "Убыток"),
            "sell": ("red", "x", "Продажа"),
            "sl_moved": ("orange", "circle-open", "Трейлинг"),
            "tp_runner_raised": ("#00e5ff", "triangle-up-open", "TP поднят"),
            "cancel": ("silver", "x-open", "Отмена")
        }

        for ev_type, (color, symbol, name) in colors.items():
            mask = df_ev['type'] == ev_type
            if mask.any():
                subset = df_ev[mask]
                def format_hover(row):
                    title = f"<b>{row.get('mode', name) or name}</b>"
                    info = f"<br><span style='color: #aaa;'>{row.get('text', '')}</span>" if row.get('text') else ""
                    return title + info
                
                hover_text = subset.apply(format_hover, axis=1)
                
                fig.add_trace(go.Scatter(
                    x=subset['datetime'], y=subset['price'], mode='markers',
                    marker=dict(symbol=symbol, size=13, color=color, line=dict(width=1, color='white')),
                    name=name, text=hover_text, hoverinfo="text+y"
                ), row=1, col=1)

        fig.update_layout(
            title=f"История торгов: {self.symbol} | Капитан Рост", template="plotly_dark", xaxis_rangeslider_visible=False,
            hovermode="x unified", hoverlabel=dict(bgcolor="rgba(20, 20, 20, 0.9)", font_size=13),
            height=850, margin=dict(t=60, b=40, l=40, r=40)
        )
        
        fig.update_yaxes(title_text="Цена", row=1, col=1)
        fig.update_yaxes(title_text="Объем", row=2, col=1, showticklabels=False)
        fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1, tickvals=[30, 50, 70])
        
        if show: fig.show()
        return fig
