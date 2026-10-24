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
        self.cooldown_meta = {}      # sym -> {"structural_reset": bool, "reason": str, "set_at": int, "until": int}
        self.consecutive_sl = {}     # sym -> count of consecutive stop-losses
        self.last_sl_price = {}      # sym -> price of last stop-loss
        self.last_win_price = {}     # sym -> exit price of last profitable trade
        self.last_win_ts = {}        # sym -> timestamp of last profitable trade
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
        Прогрессивный карантин монеты (Escalating & Post-Peak Cooldown):
        - Реальный профит (> +0.25%) -> пауза 45 минут (2700с) для защиты от входа в остывающий памп (кейс SENT #2).
        - Нейтральный БУ (-0.15% .. +0.25%) -> пауза 15 минут (900с).
        - 1-й стоп-лосс -> пауза 45 минут (2700с), фиксация уровня стопа.
        - 2-й и более стоп подряд -> карантин 4 часа (14400с).
        """
        now_ts = int(time.time())
        if not is_sl or pnl_pct >= -0.15:
            self.consecutive_sl[sym] = 0
            self.last_sl_price.pop(sym, None)
            if pnl_pct > 0.25:
                self.last_win_price[sym] = float(exit_price)
                self.last_win_ts[sym] = now_ts
                self.set_cooldown(sym, 2700, structural_reset=False, reason="post_peak_win")
                logger.info(f"Cooldown {sym}: профитный выход ({pnl_pct:+.2f}%) -> пауза 45 мин (защита от остывания с пика)")
            else:
                self.set_cooldown(sym, 900, structural_reset=True, reason="neutral_be")
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

    def can_enter_sniper(self, sym: str, current_price: float, patterns: list = None) -> tuple:
        """
        Блокирует повторный трендовый вход sniper в течение 2 часов после профитного выхода,
        если цена сползает ниже точки нашего выхода и не сформировала свежий VCP или Breakout (кейс SENT #2).
        """
        win_p = self.last_win_price.get(sym)
        win_ts = self.last_win_ts.get(sym, 0)
        if win_p and (time.time() - win_ts) < 7200:
            pats = patterns or []
            if current_price < win_p * 0.998 and not any(p in pats for p in ("vcp", "breakout")):
                return False, f"остывание ниже пика выхода (${win_p:.4f}) без паттерна VCP/Breakout"
        return True, "OK"

    def can_enter_reversal(self, sym: str, current_price: float, ema21_15m: float) -> tuple:
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
        self.last_win_price.clear()
        self.last_win_ts.clear()
        try:
            if PAUSED_FILE.exists():
                PAUSED_FILE.unlink()
        except Exception:
            pass


bot_state = BotState()
