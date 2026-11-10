import json
import time
from pathlib import Path
from loguru import logger
from bot.utils.format import fmt_price

PAUSED_FILE = Path("storage/paused_orders.json")


class BotState:
    def __init__(self):
        self.trading_enabled = True
        self.paused = False
        self.fomo_cooldowns = {}
        self.cooldown_meta = {}
        self.consecutive_sl = {}
        self.last_sl_price = {}
        self.last_sl_ts = {}
        self.last_win_price = {}
        self.last_win_ts = {}
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
        Умный карантин:
        - Выходы от -0.25% до +0.25% считаются нейтральным БУ (пауза 15 мин, серия стопов не копится).
        - Профит (> +0.25%) -> пауза 15 мин (1 свеча 15m) от мгновенного перезахода в ту же свечу.
        - 1-й реальный стоп (< -0.25%) -> карантин 90 мин.
        - 2-й реальный стоп подряд -> бан 6 часов.
        """
        now_ts = int(time.time())
        if not is_sl or pnl_pct >= -0.25:
            self.consecutive_sl[sym] = 0
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
            if pnl_pct > 0.25:
                self.last_win_price[sym] = float(exit_price)
                self.last_win_ts[sym] = now_ts
                self.set_cooldown(sym, 900, structural_reset=False, reason="post_peak_win")
                logger.info(f"Cooldown {sym}: профитный выход ({pnl_pct:+.2f}%) -> пауза 15 мин")
            else:
                self.set_cooldown(sym, 900, structural_reset=False, reason="neutral_be")
            return

        streak = self.consecutive_sl.get(sym, 0) + 1
        self.consecutive_sl[sym] = streak
        self.last_sl_price[sym] = float(exit_price)
        self.last_sl_ts[sym] = now_ts

        if streak == 1:
            self.set_cooldown(sym, 5400, structural_reset=True, reason="sl_1")
            logger.info(f"Cooldown {sym}: 1-й стоп ({pnl_pct:+.2f}%) -> карантин 90 мин")
        else:
            self.set_cooldown(sym, 21600, structural_reset=False, reason=f"sl_{streak}")
            logger.info(f"Cooldown {sym}: {streak}-й стоп подряд -> БАН 6ч")

    def _cleanup_expired_sl(self, sym: str):
        sl_ts = self.last_sl_ts.get(sym, 0)
        if sl_ts and (time.time() - sl_ts) > 7200 and sym not in self.fomo_cooldowns:
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)

    def check_structural_reset(self, sym: str, last_price: float, ema21_15m: float, ema50_15m: float = 0.0) -> bool:
        if sym not in self.fomo_cooldowns:
            self._cleanup_expired_sl(sym)
            return True

        now = time.time()
        if now > self.fomo_cooldowns[sym]:
            self.fomo_cooldowns.pop(sym, None)
            self.cooldown_meta.pop(sym, None)
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
            return True

        meta = self.cooldown_meta.get(sym, {})
        if not meta.get("structural_reset"):
            return False

        elapsed = now - meta.get("set_at", 0)
        if elapsed < 2700:
            return False

        last_sl = self.last_sl_price.get(sym, 0.0)
        if last_sl > 0 and last_price < last_sl * 1.002:
            return False

        if ema21_15m > 0 and ema50_15m > 0 and last_price > max(ema21_15m, ema50_15m) * 1.001 and ema21_15m >= ema50_15m:
            logger.info(f"🔓 Структурный сброс карантина {sym}: цена ${fmt_price(last_price)} закрепилась над EMA21/50")
            self.fomo_cooldowns.pop(sym, None)
            self.cooldown_meta.pop(sym, None)
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
            return True

        return False

    def can_enter_sniper(
        self, sym: str, current_price: float, patterns: list = None,
        ema21_15m: float = 0.0, ema50_15m: float = 0.0, atr_15m: float = 0.0,
        score: float = 0.0, gate_1h: str = ""
    ) -> tuple:
        self._cleanup_expired_sl(sym)
        last_sl = self.last_sl_price.get(sym)
        if last_sl and current_price < last_sl * 1.001:
            return False, f"цена ниже предыдущего стоп-лосса (${fmt_price(last_sl)})"

        win_p = self.last_win_price.get(sym)
        win_ts = self.last_win_ts.get(sym, 0)
        if win_p and (time.time() - win_ts) < 3600:
            pats = patterns or []
            has_brk_vcp = any(p in pats for p in ("vcp", "breakout", "engulfing"))
            # Разрешаем перезаход по тренду (фикс INITUSDT 8.8), если цена откатила к растущей EMA21
            is_healthy_trend_pullback = (
                ema21_15m > ema50_15m > 0 and
                current_price >= ema50_15m * 0.998 and
                ((atr_15m > 0 and abs(current_price - ema21_15m) <= 1.05 * atr_15m) or score >= 8.2 or gate_1h == "1H_SUPER_TREND")
            )
            if current_price >= win_p * 0.994 and not (has_brk_vcp or is_healthy_trend_pullback):
                return False, f"защита от покупки хая выше точки выхода (${fmt_price(win_p)}) без отката к EMA21/VCP"
        return True, "OK"

    def can_enter_reversal(self, sym: str, current_price: float, ema21_15m: float) -> tuple:
        self._cleanup_expired_sl(sym)
        last_sl = self.last_sl_price.get(sym)
        if last_sl and current_price < last_sl * 1.001:
            if ema21_15m <= 0 or current_price < ema21_15m * 0.998:
                return False, f"цена ниже прошлого стопа (${fmt_price(last_sl)}) и под EMA21"
        return True, "OK"

    def is_on_cooldown(self, sym: str) -> bool:
        if sym not in self.fomo_cooldowns:
            self._cleanup_expired_sl(sym)
            return False
        if time.time() > self.fomo_cooldowns[sym]:
            del self.fomo_cooldowns[sym]
            self.cooldown_meta.pop(sym, None)
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
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
        self.last_sl_ts.clear()
        self.last_win_price.clear()
        self.last_win_ts.clear()
        try:
            if PAUSED_FILE.exists():
                PAUSED_FILE.unlink()
        except Exception:
            pass


bot_state = BotState()
