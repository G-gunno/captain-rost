import re
import argparse
from datetime import datetime, timedelta
import pandas as pd
import httpx
import plotly.graph_objects as go
from loguru import logger

# Если сервер работает в другой зоне, поменяй на нужную, чтобы логи совпали со свечами
LOG_TIMEZONE = "Europe/Moscow" 


class TradeVisualizer:
    def __init__(self, log_path: str, symbol: str, interval: int = 1):
        self.log_path = log_path
        self.symbol = symbol.upper()
        self.base_sym = self.symbol.replace("USDT", "")
        self.interval = interval
        
    def parse_logs(self) -> pd.DataFrame:
        """Парсит текстовые логи и возвращает DataFrame с событиями."""
        events = []
        # Ищем стандартный паттерн времени: 2026-09-07 05:51:36.802
        ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        
        try:
            with open(self.log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    # Фильтр для ускорения: ищем тикер целиком или его базу (для коротких записей)
                    if self.symbol not in line and f" {self.base_sym} " not in line and f"продан {self.base_sym}" not in line:
                        continue
                        
                    ts_match = ts_pattern.search(line)
                    if not ts_match:
                        continue
                        
                    # Парсим время как наивное (предполагаем, что оно в LOG_TIMEZONE)
                    dt = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
                    
                    # 1. Покупки
                    if "PAPER FILL BUY" in line and self.symbol in line:
                        m = re.search(rf"PAPER FILL BUY {self.symbol} [\d\.]+ @ ([\d\.]+)", line)
                        if m:
                            events.append({"time": dt, "type": "buy", "price": float(m.group(1))})
                            
                    # 2. Трейлинг SL
                    elif "SL поднят" in line and self.symbol in line:
                        m = re.search(rf"SL поднят {self.symbol} -> ([\d\.]+)", line)
                        if m:
                            events.append({"time": dt, "type": "sl", "price": float(m.group(1))})
                            
                    # 3. Продажа (Ротация или другая причина)
                    elif ("продан " + self.symbol in line) or ("продан " + self.base_sym in line):
                        events.append({"time": dt, "type": "sell", "price": None}) # Цену подтянем с графика
                        
                    # 4. Снятие / Отмена ордера
                    elif "снят" in line and (self.symbol in line or f" {self.base_sym} " in line):
                        events.append({"time": dt, "type": "cancel", "price": None})
                        
                    # 5. Пропуск сканером (токсичные новости и т.д.)
                    elif "пропущен" in line and (self.symbol in line or f" {self.base_sym} " in line):
                        events.append({"time": dt, "type": "skip", "price": None})

                    # 7. Выставление ордера
                    elif "ORDER PLACED" in line and self.symbol in line:
                        m = re.search(rf"ORDER PLACED {self.symbol} @ ([\d\.]+)", line)
                        if m:
                            events.append({"time": dt, "type": "order", "price": float(m.group(1))})
                        
        except FileNotFoundError:
            logger.error(f"Файл логов {self.log_path} не найден.")
            return pd.DataFrame()

        df = pd.DataFrame(events)
        if not df.empty:
            df = df.sort_values("time").reset_index(drop=True)
        return df

    def fetch_klines(self, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        """Загружает исторические свечи с Bybit за нужный период."""
        # Переводим локальное время логов в UTC для правильного запроса к Bybit
        start_utc = pd.Timestamp(start_dt).tz_localize(LOG_TIMEZONE).tz_convert("UTC")
        end_utc = pd.Timestamp(end_dt).tz_localize(LOG_TIMEZONE).tz_convert("UTC")
        
        start_ms = int(start_utc.timestamp() * 1000)
        curr_end_ms = int(end_utc.timestamp() * 1000)
        limit = 1000
        all_candles = []
        
        url = "https://api.bybit.com/v5/market/kline"
        logger.info(f"Загрузка свечей {self.symbol} с Bybit...")

        with httpx.Client(timeout=15) as client:
            while True:
                params = {
                    "category": "spot",
                    "symbol": self.symbol,
                    "interval": str(self.interval),
                    "start": start_ms,
                    "end": curr_end_ms,
                    "limit": limit
                }
                resp = client.get(url, params=params)
                if resp.status_code != 200:
                    logger.error(f"Ошибка API Bybit: {resp.status_code} {resp.text}")
                    break
                    
                data = resp.json()
                if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
                    break
                    
                batch = data["result"]["list"]
                all_candles.extend(batch)
                
                oldest_candle_ts = int(batch[-1][0])
                if len(batch) < limit or oldest_candle_ts <= start_ms:
                    break
                    
                curr_end_ms = oldest_candle_ts - 1

        if not all_candles:
            logger.warning("Свечи не найдены для заданного периода.")
            return pd.DataFrame()

        df = pd.DataFrame(all_candles, columns=["start_time", "open", "high", "low", "close", "volume", "turnover"])
        df["start_time"] = pd.to_numeric(df["start_time"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = df[col].astype(float)

        # Конвертируем UTC время Bybit обратно в локальное время логов для синхронизации на графике
        df["datetime"] = pd.to_datetime(df["start_time"], unit="ms", utc=True).dt.tz_convert(LOG_TIMEZONE).dt.tz_localize(None)
        
        df = df.sort_values("datetime").drop_duplicates(subset=["start_time"]).reset_index(drop=True)
        return df

    def _merge_prices(self, df_events: pd.DataFrame, df_klines: pd.DataFrame) -> pd.DataFrame:
        """Подставляет цену закрытия свечи для событий, где цена не была указана в логах."""
        
        # --- ИСПРАВЛЕНИЕ: Принудительно приводим ключи к одному типу времени (наносекунды) ---
        df_events['time'] = pd.to_datetime(df_events['time']).astype('datetime64[ns]')
        df_klines['datetime'] = pd.to_datetime(df_klines['datetime']).astype('datetime64[ns]')
        # -----------------------------------------------------------------------------------

        # Используем merge_asof для поиска ближайшей свечи по времени
        merged = pd.merge_asof(
            df_events, 
            df_klines[['datetime', 'close']], 
            left_on='time', 
            right_on='datetime', 
            direction='nearest'
        )
        # Заменяем пустые цены на цену закрытия ближайшей свечи
        merged['price'] = merged['price'].fillna(merged['close'])
        return merged

    def build_chart(self, show: bool = True):
        """Главный метод: парсит, загружает, клеит и рисует график."""
        df_events = self.parse_logs()
        if df_events.empty:
            logger.warning(f"В логах не найдено событий для монеты {self.symbol}.")
            return None
            
        # Определяем диапазон времени с запасом в 2 часа (до и после)
        start_dt = df_events['time'].min() - timedelta(hours=2)
        end_dt = df_events['time'].max() + timedelta(hours=2)
        
        df_klines = self.fetch_klines(start_dt, end_dt)
        if df_klines.empty:
            return None
            
        # Подклеиваем цены для отмен, пропусков и продаж
        df_events = self._merge_prices(df_events, df_klines)

        # Создаем фигуру Plotly
        fig = go.Figure()

        # 1. Японские свечи
        fig.add_trace(go.Candlestick(
            x=df_klines['datetime'],
            open=df_klines['open'],
            high=df_klines['high'],
            low=df_klines['low'],
            close=df_klines['close'],
            name='Market Price',
            increasing_line_color='rgba(38, 166, 154, 0.8)',
            decreasing_line_color='rgba(239, 83, 80, 0.8)'
        ))

        # 2. Покупки (Зеленые треугольники)
        buys = df_events[df_events['type'] == 'buy']
        if not buys.empty:
            fig.add_trace(go.Scatter(
                x=buys['time'], y=buys['price'],
                mode='markers',
                marker=dict(symbol='triangle-up', size=16, color='lime', line=dict(width=1, color='black')),
                name='Buy (Вход)'
            ))

        # 3. Трейлинг-Стопы (Оранжевая пунктирная линия с точками)
        sls = df_events[df_events['type'] == 'sl']
        if not sls.empty:
            fig.add_trace(go.Scatter(
                x=sls['time'], y=sls['price'],
                mode='lines+markers',
                marker=dict(symbol='circle', size=6, color='orange'),
                line=dict(color='orange', width=2, dash='dot'),
                name='Trailing SL'
            ))

        # 4. Продажи / Ротации (Красные кресты)
        sells = df_events[df_events['type'] == 'sell']
        if not sells.empty:
            fig.add_trace(go.Scatter(
                x=sells['time'], y=sells['price'],
                mode='markers',
                marker=dict(symbol='x', size=14, color='red', line=dict(width=2, color='white')),
                name='Sell (Продажа/Ротация)'
            ))

        # 5. Отмены ордеров (Серые ромбы)
        cancels = df_events[df_events['type'] == 'cancel']
        if not cancels.empty:
            fig.add_trace(go.Scatter(
                x=cancels['time'], y=cancels['price'],
                mode='markers',
                marker=dict(symbol='diamond-open', size=10, color='silver', line=dict(width=2)),
                name='Cancel (Ордер снят)'
            ))

        # 6. Пропуски сканером (Фиолетовые круги)
        skips = df_events[df_events['type'] == 'skip']
        if not skips.empty:
            fig.add_trace(go.Scatter(
                x=skips['time'], y=skips['price'],
                mode='markers',
                marker=dict(symbol='circle-open', size=10, color='fuchsia', line=dict(width=2)),
                name='Skip (Пропущен)'
            ))

        # 7. Ордера (Голубые черточки)
        orders = df_events[df_events['type'] == 'order']
        if not orders.empty:
            fig.add_trace(go.Scatter(
                x=orders['time'], y=orders['price'],
                mode='markers',
                marker=dict(symbol='line-ew', size=16, color='cyan', line=dict(width=3)),
                name='Order Placed (Ордер)'
            ))

        # Настройка визуального оформления
        fig.update_layout(
            title=dict(text=f"Анализ торгов: <b>{self.symbol}</b>", font=dict(size=24)),
            template="plotly_dark",
            xaxis_rangeslider_visible=False, # Убираем нижний слайдер
            yaxis_title="Price (USDT)",
            xaxis_title="Local Time",
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        # Убираем выходные дни / пустые зоны из графика, если нужно (оставим по умолчанию для крипты - она торгуется 24/7)
        fig.update_xaxes(showgrid=True, gridcolor='rgba(255,255,255,0.1)')
        fig.update_yaxes(showgrid=True, gridcolor='rgba(255,255,255,0.1)')

        if show:
            fig.show()
            
        return fig


# CLI-интерфейс для запуска как независимого скрипта
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Анализ и визуализация логов CaptainRost.")
    parser.add_argument("--log", type=str, default="logs/bot.log", help="Путь к файлу логов")
    parser.add_argument("--symbol", type=str, required=True, help="Тикер монеты (например, LINKUSDT)")
    parser.add_argument("--interval", type=int, default=1, help="Таймфрейм свечей (по умолчанию 1 минута)")
    args = parser.parse_args()

    viz = TradeVisualizer(log_path=args.log, symbol=args.symbol, interval=args.interval)
    viz.build_chart(show=True)
