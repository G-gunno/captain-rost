import json
import time
from pathlib import Path
from loguru import logger

PAUSED_FILE = Path("storage/paused_orders.json")


class BotState:
    def __init__(self):
        self.trading_enabled = True
        self.paused = False
        self.fomo_cooldowns = {}
        self.cooldown_meta = {}      # sym -> {"structural_reset": bool, "reason": str, "ts": int}
        self.consecutive_sl = {}     # sym -> count of consecutive stop-losses
        self.last_sl_price = {}      # sym -> price of last stop-loss
        self.current_regime = "neutral"

    def set_cooldown(self, sym: str, seconds: int, structural_reset: bool = False, reason: str = ""):
        until_ts = int(time.time()) + int(seconds)
        self.fomo_cooldowns[sym] = until_ts
        self.cooldown_meta[sym] = {
            "structural_reset": structural_reset,
            "reason": reason,
            "set_at": int(time.time()),
            "until": until_ts,
        }

    def record_trade_exit(self, sym: str, pnl_pct: float, exit_price: float, is_sl: bool = False):
        """
        Блок 4: Прогрессивный карантин монеты (Escalating Cooldown) со структурным сбросом:
        - Плюс или БУ (>= -0.15%) -> пауза 15 минут (900с), сброс серии стопов.
        - 1-й стоп-лосс -> пауза 45 минут (2700с), фиксация уровня стопа.
        - 2-й и более стоп подряд -> карантин до 4 часов (14400с) с правом досрочного
          структурного сброса при возврате цены над EMA 21 (кейс WAL).
        """
        if not is_sl or pnl_pct >= -0.15:
            self.consecutive_sl[sym] = 0
            self.last_sl_price.pop(sym, None)
            self.set_cooldown(sym, 900, structural_reset=True, reason="win_or_be")
            return

        streak = self.consecutive_sl.get(sym, 0) + 1
        self.consecutive_sl[sym] = streak
        self.last_sl_price[sym] = float(exit_price)

        if streak == 1:
            self.set_cooldown(sym, 2700, structural_reset=True, reason="sl_1")
            logger.info(f"Cooldown {sym}: 1-й стоп -> пауза 45 мин (или до пробоя EMA21 наверх)")
        else:
            self.set_cooldown(sym, 14400, structural_reset=True, reason=f"sl_{streak}")
            logger.info(f"Cooldown {sym}: {streak}-й стоп подряд -> карантин до 4ч (со структурным сбросом над EMA21)")

    def check_structural_reset(self, sym: str, last_price: float, ema21_15m: float, ema50_15m: float = 0.0) -> bool:
        """
        Если монета на карантине после стопов, но уже сформировала базу (минимум 15 мин прошло)
        и пробила EMA 21 наверх на 15m — карантин автоматически сгорает!
        """
        if sym not in self.fomo_cooldowns:
            return True

        now = time.time()
        if now > self.fomo_cooldowns[sym]:
            self.fomo_cooldowns.pop(sym, None)
            self.cooldown_meta.pop(sym, None)
            return True

        meta = self.cooldown_meta.get(sym, {})
        if not meta.get("structural_reset"):
            return False

        # Минимальная пауза 15 минут после стопа для формирования свежей базы
        elapsed = now - meta.get("set_at", 0)
        if elapsed < 900:
            return False

        if ema21_15m > 0 and last_price > ema21_15m * 1.0015:
            if ema50_15m <= 0 or last_price >= ema50_15m * 0.995:
                logger.info(f"🔓 Структурный сброс карантина {sym}: цена ${last_price:.4f} закрепилась над EMA21 ${ema21_15m:.4f}")
                self.fomo_cooldowns.pop(sym, None)
                self.cooldown_meta.pop(sym, None)
                return True

        return False

    def can_enter_reversal(self, sym: str, current_price: float, ema21_15m: float) -> tuple:
        """
        Блокировка повторной ловли ножа: запрет нового входа reversal,
        пока цена ниже уровня предыдущего стопа и не вернулась выше EMA 21 на 15m.
        """
        last_sl = self.last_sl_price.get(sym)
        if last_sl and current_price < last_sl:
            if ema21_15m <= 0 or current_price < ema21_15m:
                return False, f"цена ниже прошлого стопа (${last_sl:.4f}) и под EMA21"
        return True, "OK"

    def is_on_cooldown(self, sym: str) -> bool:
        if sym not in self.fomo_cooldowns:
            return False
        if time.time() > self.fomo_cooldowns[sym]:
            del self.fomo_cooldowns[sym]
            self.cooldown_meta.pop(sym, None)
            return False
        return True

    def pause(self, orders):
        self.paused = True
        try:
            PAUSED_FILE.parent.mkdir(parents=True, exist_ok=True)
            PAUSED_FILE.write_text(json.dumps(orders, ensure_ascii=False))
        except Exception as e:
            logger.error(f"pause save error: {e}")

    def resume(self):
        self.paused = False
        orders = []
        try:
            if PAUSED_FILE.exists():
                orders = json.loads(PAUSED_FILE.read_text())
                PAUSED_FILE.unlink()
        except Exception as e:
            logger.error(f"resume load error: {e}")
        return orders

    def fresh_start(self):
        self.paused = False
        self.trading_enabled = True
        self.fomo_cooldowns.clear()
        self.cooldown_meta.clear()
        self.consecutive_sl.clear()
        self.last_sl_price.clear()
        try:
            if PAUSED_FILE.exists():
                PAUSED_FILE.unlink()
        except Exception:
            pass


bot_state = BotState()
