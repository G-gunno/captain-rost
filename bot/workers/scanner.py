import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.strategy.scanner import get_regime, scan
from bot.core.state import bot_state

class ScannerWorker:
    """Тяжелый I/O воркер: собирает метрики, анализирует RSI/EMA, генерирует сигналы."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus

    async def run(self):
        logger.info("🔎 Scanner Worker запущен: сканирование рынка каждые 60с...")
        while True:
            if bot_state.paused or not bot_state.trading_enabled:
                await asyncio.sleep(10)
                continue
                
            try:
                tickers = await market_data.get_tickers()
                deriv_tickers = await market_data.get_derivatives_tickers()
                
                if tickers:
                    regime, info = await get_regime()
                    # Уведомляем другие воркеры о текущем режиме
                    self.bus.publish("REGIME_UPDATED", {"regime": regime, "info": info})
                    
                    # Тяжеловесная функция сканирования
                    candidates = await scan(regime, tickers, deriv_tickers, limit=20)
                    
                    if candidates:
                        self.bus.publish("SIGNALS_READY", {
                            "candidates": candidates,
                            "tickers": tickers,
                            "regime": regime
                        })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"ScannerWorker error: {e}")
                
            await asyncio.sleep(60) # Тот самый цикл 60 секунд
