import asyncio
import time
from loguru import logger
from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper

class ExecutionRiskWorker:
    """Критический воркер ведения открытых позиций (Трейлинг-стопы, Риск-менеджмент)."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        # Очередь цен делаем короткой: нам нужны только актуальные цены
        self.price_queue = self.bus.subscribe("PRICE_UPDATED", maxsize=5)
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP", maxsize=1)
        self._last_btc_price = 0.0

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: мониторинг рисков и трейлинг-стопов")
        # Запускаем слушателей параллельно внутри воркера
        await asyncio.gather(
            self._price_loop(),
            self._emergency_loop()
        )

    async def _price_loop(self) -> None:
        while True:
            try:
                event = await self.price_queue.get()
                tickers = event.payload
                self._process_tick(tickers)  # Синхронно, чтобы не блокировать Event Loop
                self.price_queue.task_done()
            except Exception as e:
                logger.error(f"Execution _price_loop error: {e}")

    async def _emergency_loop(self) -> None:
        while True:
            try:
                event = await self.emergency_queue.get()
                tickers = event.payload
                logger.critical("🚨 Execution Worker: ЭКСТРЕННЫЙ ВЫХОД! Дамп рынка.")
                results = paper.sell_all(tickers)
                
                # Публикуем события уведомлений (асинхронно, чтобы не ждать отправку в TG)
                for ex in results:
                    self.bus.publish("NOTIFY_URGENT", f"🚨 Паника {ex['symbol']}: {ex['pnl_pct']:.2f}%")
                
                self.emergency_queue.task_done()
            except Exception as e:
                logger.error(f"Execution _emergency_loop error: {e}")

    def _process_tick(self, tickers: dict) -> None:
        """Мгновенная проверка позиций в памяти. Zero-delay."""
        # 1. Проверка глобального риска (Дамп BTC)
        btc = tickers.get("BTCUSDT")
        if btc:
            last_btc = btc["last"]
            if self._last_btc_price > 0:
                drop_pct = (last_btc - self._last_btc_price) / self._last_btc_price
                if drop_pct <= -0.03:  # Падение > 3% между тиками
                    self.bus.publish("EMERGENCY_DUMP", tickers)
                    return
            self._last_btc_price = last_btc

        # 2. Быстрый проход по позициям
        state_changed = False
        
        # Оборачиваем в list(), чтобы безопасно менять словарь во время итерации
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t:
                continue
                
            last = t["last"]
            
            # Проверка выходов
            if last >= pos["tp"]:
                if not pos.get("tp1_done"):
                    # Логику частичной продажи (TP1) передаем Order Manager'у или делаем тут
                    self.bus.publish("EXECUTE_TRADE", {"sym": sym, "type": "partial_tp", "price": last})
                else:
                    self.bus.publish("EXECUTE_TRADE", {"sym": sym, "type": "full_tp", "price": last})
                continue
                
            if last <= pos["sl"]:
                self.bus.publish("EXECUTE_TRADE", {"sym": sym, "type": "sl", "price": last})
                continue

            # Трейлинг-стоп (Сдвиг SL при росте цены)
            max_p = max(pos.get("max_price", last), last)
            if max_p > pos.get("max_price", 0.0):
                pos["max_price"] = max_p
                state_changed = self._update_trailing_stop(sym, pos, last, max_p) or state_changed

        if state_changed:
            paper.save()

    def _update_trailing_stop(self, sym: str, pos: dict, last: float, max_p: float) -> bool:
        """Перерасчет стопа. Возвращает True, если стоп был сдвинут."""
        # Здесь мы берем статический ATR, который Scanner Worker рассчитывает раз в минуту
        # и кладет в параметры позиции, чтобы Execution Worker не считал его сам.
        atr_val = pos.get("cached_atr", last * 0.02)  
        breakeven = pos["avg"] * 1.002  # Учет двойной комиссии
        
        new_sl = pos["sl"]
        if max_p >= pos["avg"] + 1.0 * atr_val:
            new_sl = max(new_sl, breakeven)
        if max_p >= pos["avg"] + 1.5 * atr_val:
            new_sl = max(new_sl, max_p - 1.0 * atr_val)
        if max_p >= pos["avg"] + 2.5 * atr_val:
            new_sl = max(new_sl, max_p - 0.5 * atr_val)

        if new_sl > pos["sl"]:
            pos["sl"] = round(new_sl, 8)
            self.bus.publish("NOTIFY", f"🛡 SL поднят по {sym} до {pos['sl']}")
            return True
            
        return False
