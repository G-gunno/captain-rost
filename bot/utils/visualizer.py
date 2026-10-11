import pandas as pd
import httpx
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from loguru import logger
import json
import os
from pathlib import Path
from bot.utils.format import fmt_price
from bot.strategy.indicators import get_all_sr_levels

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
        
        # ⚡ Защита от KeyError: гарантируем наличие всех аналитических колонок
        for col in ['tp', 'sl', 'sup', 'res']:
            if col not in df_ev.columns:
                df_ev[col] = None

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

        df_kline = df_kline.tail(500).reset_index(drop=True)
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

        # ⚡ ОТРИСОВКА ВСЕХ ПОДТВЕРЖДЕННЫХ УРОВНЕЙ S/R ПО 15M (ПОЛНАЯ ВИДИМОСТЬ СЕТКИ)
        df_sr_15m = df_kline if self.interval == "15" else self.fetch_klines(start_ts, end_ts, interval_override="15")
        if df_sr_15m.empty: df_sr_15m = df_kline
        
        all_res, all_sup = get_all_sr_levels(df_sr_15m.to_dict('records'))
        
        for idx, (r_val, r_tch) in enumerate(all_res):
            fig.add_trace(go.Scatter(
                x=[min_time, max_time], y=[r_val, r_val],
                mode="lines", name="Сопротивление (15m)",
                line=dict(color="rgba(233, 30, 99, 0.45)", width=1.0, dash="dot"),
                hoverinfo="name+y", showlegend=(idx == 0)
            ), row=1, col=1)

        for idx, (s_val, s_tch) in enumerate(all_sup):
            fig.add_trace(go.Scatter(
                x=[min_time, max_time], y=[s_val, s_val],
                mode="lines", name="Поддержка (15m)",
                line=dict(color="rgba(33, 150, 243, 0.45)", width=1.0, dash="dot"),
                hoverinfo="name+y", showlegend=(idx == 0)
            ), row=1, col=1)

        # ⚡ ПОТРЕЙДОВЫЙ АНАЛИЗ: Траектория SL, TP и опорные уровни для КАЖДОЙ сделки
        trade_groups = []
        current_trade = []
        for _, ev in df_ev.sort_values('ts').iterrows():
            current_trade.append(ev)
            if ev['type'] in ['sell', 'sell_loss', 'sell_profit', 'tp_runner', 'cancel']:
                trade_groups.append(pd.DataFrame(current_trade))
                current_trade = []
        if current_trade:
            trade_groups.append(pd.DataFrame(current_trade))

        pos = data.get("positions", {}).get(self.symbol)

        for idx, tr_df in enumerate(trade_groups):
            t_start = tr_df['datetime'].min()
            t_end = tr_df['datetime'].max()
            
            is_active_trade = (idx == len(trade_groups) - 1) and (pos is not None)
            if is_active_trade:
                t_end = max_time
            elif t_start == t_end:
                t_end = t_start + pd.Timedelta(minutes=15)

            is_real_trade = 'buy' in tr_df['type'].values
            label_tag = f"Сделка {idx+1}" if is_real_trade else f"Ордер {idx+1}"

            # 1. Траектория Стоп-лосса (SL)
            sl_points = tr_df[tr_df['sl'].notna() & (tr_df['sl'] > 0)]
            if not sl_points.empty:
                sl_x = list(sl_points['datetime'])
                sl_y = list(sl_points['sl'])
                if is_active_trade and pos.get('sl'):
                    sl_x.append(max_time)
                    sl_y.append(pos['sl'])

                fig.add_trace(go.Scatter(
                    x=sl_x, y=sl_y,
                    mode="lines+markers", line=dict(color="rgba(255, 23, 68, 0.8)", width=2.0, shape="hv"),
                    marker=dict(size=4, color="red"), name=f"Стоп ({label_tag})",
                    hoverinfo="text+y", text=[f"🛡 Стоп: {fmt_price(p)}" for p in sl_y]
                ), row=1, col=1)

            # 2. Траектория Тейк-профита (TP)
            tp_points = tr_df[tr_df['tp'].notna() & (tr_df['tp'] > 0)]
            if not tp_points.empty:
                tp_x = list(tp_points['datetime'])
                tp_y = list(tp_points['tp'])
                if is_active_trade and pos.get('tp'):
                    tp_x.append(max_time)
                    tp_y.append(pos['tp'])

                fig.add_trace(go.Scatter(
                    x=tp_x, y=tp_y,
                    mode="lines+markers", line=dict(color="rgba(0, 230, 118, 0.8)", width=2.0, shape="hv"),
                    marker=dict(size=4, color="green"), name=f"Тейк ({label_tag})",
                    hoverinfo="text+y", text=[f"🎯 Тейк: {fmt_price(p)}" for p in tp_y]
                ), row=1, col=1)

            # 3. Активные уровни Поддержки и Сопротивления этой конкретной сделки
            tr_sup = tr_df['sup'].dropna()
            if not tr_sup.empty and tr_sup.iloc[0] > 0:
                s_lvl = tr_sup.iloc[0]
                fig.add_trace(go.Scatter(
                    x=[t_start, t_end], y=[s_lvl, s_lvl],
                    mode="lines", line=dict(color="rgba(33, 150, 243, 0.85)", width=2.0, dash="dash"),
                    name=f"Опора поддержки ${fmt_price(s_lvl)} ({label_tag})", hoverinfo="name+y"
                ), row=1, col=1)

            tr_res = tr_df['res'].dropna()
            if not tr_res.empty and tr_res.iloc[0] > 0:
                r_lvl = tr_res.iloc[0]
                fig.add_trace(go.Scatter(
                    x=[t_start, t_end], y=[r_lvl, r_lvl],
                    mode="lines", line=dict(color="rgba(233, 30, 99, 0.85)", width=2.0, dash="dash"),
                    name=f"Стена сопротивления ${fmt_price(r_lvl)} ({label_tag})", hoverinfo="name+y"
                ), row=1, col=1)

        # === 2 СЛОЙ: Объем ===
        fig.add_trace(go.Bar(x=df_kline['datetime'], y=df_kline['volume'], name='Объем', marker_color=colors_vol), row=2, col=1)

        # === 3 СЛОЙ: RSI ===
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['RSI'], mode='lines', name='RSI', line=dict(color='rgba(156, 39, 176, 0.8)', width=1.5)), row=3, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="rgba(239, 83, 80, 0.5)", row=3, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="rgba(38, 166, 154, 0.5)", row=3, col=1)

        # Маркеры событий
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
                    marker=dict(symbol=symbol, size=12, color=color, line=dict(width=1, color='white')),
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
