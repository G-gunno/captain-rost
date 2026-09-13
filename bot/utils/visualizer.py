import pandas as pd
import httpx
import plotly.graph_objects as go
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
        for col in ["open", "high", "low", "close"]:
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

        fig = go.Figure()
        
        # Свечи
        fig.add_trace(go.Candlestick(
            x=df_kline['datetime'], open=df_kline['open'], high=df_kline['high'], 
            low=df_kline['low'], close=df_kline['close'], name='Цена',
            increasing_line_color='rgba(38, 166, 154, 0.8)', decreasing_line_color='rgba(239, 83, 80, 0.8)'
        ))

        # Линии индикаторов
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['EMA21'], mode='lines', name='EMA 21', line=dict(color='rgba(255, 235, 59, 0.8)', width=1.5)))
        fig.add_trace(go.Scatter(x=df_kline['datetime'], y=df_kline['EMA50'], mode='lines', name='EMA 50', line=dict(color='rgba(255, 152, 0, 0.8)', width=1.5)))

        # Выставляем маркеры по типам событий
        colors = {
            "order_placed": ("cyan", "line-ew", "Ордер"),
            "order_moved": ("blue", "diamond-open", "Сдвиг ордера"),
            "buy": ("lime", "triangle-up", "Покупка"),
            "sell": ("red", "x", "Продажа"),
            "sl_moved": ("orange", "circle-open", "Трейлинг"),
            "cancel": ("silver", "x-open", "Отмена")
        }

        for ev_type, (color, symbol, name) in colors.items():
            mask = df_ev['type'] == ev_type
            if mask.any():
                subset = df_ev[mask]
                # Формируем красивый тултип. Если текст пустой, не рисуем пустую строку
                def format_hover(row):
                    title = f"<b>{row.get('mode', name) or name}</b>"
                    info = f"<br><span style='color: #aaa;'>{row.get('text', '')}</span>" if row.get('text') else ""
                    rsi = f"<br><i>RSI: {row.get('rsi')}</i>" if row.get('rsi') and not pd.isna(row.get('rsi')) else ""
                    return title + info + rsi
                
                hover_text = subset.apply(format_hover, axis=1)
                
                fig.add_trace(go.Scatter(
                    x=subset['datetime'], y=subset['price'], mode='markers',
                    marker=dict(symbol=symbol, size=13, color=color, line=dict(width=1, color='white')),
                    name=name, text=hover_text, hoverinfo="text+y"
                ))

        fig.update_layout(
            title=f"История торгов: {self.symbol}", template="plotly_dark", xaxis_rangeslider_visible=False,
            hovermode="x unified", hoverlabel=dict(bgcolor="rgba(20, 20, 20, 0.9)", font_size=13)
        )
        if show: fig.show()
        return fig
