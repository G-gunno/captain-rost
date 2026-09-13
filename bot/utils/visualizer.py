import pandas as pd
import httpx
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from loguru import logger
import json
import os
from pathlib import Path

LOG_TIMEZONE = "Europe/Moscow" 

class TradeVisualizer:
    def __init__(self, log_path: str, symbol: str, interval: str = "15"):
        self.state_path = Path(os.getenv("STORAGE_DIR", "storage")) / "paper_state.json"
        self.symbol = symbol.upper()
        self.base_sym = self.symbol.replace("USDT", "")
        self.interval = interval
        
    def fetch_klines(self, start_ts: int, end_ts: int) -> pd.DataFrame:
        url = "https://api.bybit.com/v5/market/kline"
        all_candles = []
        curr_end_ms = end_ts * 1000
        start_ms = start_ts * 1000

        with httpx.Client(timeout=15) as client:
            while True:
                resp = client.get(url, params={
                    "category": "spot", "symbol": self.symbol, 
                    "interval": str(self.interval), "start": start_ms, 
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

        # Считаем индикаторы для отрисовки
        df_kline['EMA21'] = df_kline['close'].ewm(span=21, adjust=False).mean()
        df_kline['EMA50'] = df_kline['close'].ewm(span=50, adjust=False).mean()
        
        # Расчет RSI
        delta = df_kline['close'].diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / loss
        df_kline['RSI'] = 100 - (100 / (1 + rs))

        # Цвета баров объема
        colors_vol = ['rgba(38, 166, 154, 0.5)' if row['close'] >= row['open'] else 'rgba(239, 83, 80, 0.5)' for _, row in df_kline.iterrows()]

        # Создаем мульти-график: 3 строки
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

        # === 2 СЛОЙ: Объем ===
        fig.add_trace(go.Bar(x=df_kline['datetime'], y=df_kline['volume'], name='Объем', marker_color=colors_vol), row=2, col=1)

        # === 3 СЛОЙ: RSI ===
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['RSI'], mode='lines', name='RSI', line=dict(color='rgba(156, 39, 176, 0.8)', width=1.5)), row=3, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="rgba(239, 83, 80, 0.5)", row=3, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="rgba(38, 166, 154, 0.5)", row=3, col=1)

        # Выставляем маркеры (Только на 1 графике)
        colors = {
            "order_placed": ("cyan", "line-ew", "Ордер"),
            "order_moved": ("blue", "diamond-open", "Сдвиг"),
            "buy": ("lime", "triangle-up", "Покупка"),
            "sell": ("red", "x", "Продажа"),
            "sl_moved": ("orange", "circle-open", "Трейлинг"),
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
        
        # Настройка осей
        fig.update_yaxes(title_text="Цена", row=1, col=1)
        fig.update_yaxes(title_text="Объем", row=2, col=1, showticklabels=False) # Скрываем цифры объемов (они мешают)
        fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1, tickvals=[30, 50, 70])
        
        if show: fig.show()
        return fig
