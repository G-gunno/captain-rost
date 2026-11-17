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
        - Выходы от -0.25% до +0.25% — нейтральный БУ (пауза 10 мин).
        - Профит (> +0.25%) — пауза 10 мин от мгновенного перезахода в ту же свечу.
        - 1-й стоп (< -0.25%) — карантин 60 мин (с досрочным снятием через 20 мин при развороте над EMA21).
        - 2-й стоп подряд — карантин 2.5 часа (со снятием через 40 мин при выходе в 1H_TREND_OK / 1H_EMA21_RECLAIM).
        """
        now_ts = int(time.time())
        if not is_sl or pnl_pct >= -0.25:
            self.consecutive_sl[sym] = 0
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
            if pnl_pct > 0.25:
                self.last_win_price[sym] = float(exit_price)
                self.last_win_ts[sym] = now_ts
                self.set_cooldown(sym, 600, structural_reset=True, reason="post_peak_win")
                logger.info(f"Cooldown {sym}: профитный выход ({pnl_pct:+.2f}%) -> пауза 10 мин")
            else:
                self.set_cooldown(sym, 600, structural_reset=True, reason="neutral_be")
            return

        streak = self.consecutive_sl.get(sym, 0) + 1
        self.consecutive_sl[sym] = streak
        self.last_sl_price[sym] = float(exit_price)
        self.last_sl_ts[sym] = now_ts

        if streak == 1:
            self.set_cooldown(sym, 3600, structural_reset=True, reason="sl_1")
            logger.info(f"Cooldown {sym}: 1-й стоп ({pnl_pct:+.2f}%) -> карантин 60 мин (structural reset вкл)")
        else:
            self.set_cooldown(sym, 9000, structural_reset=True, reason=f"sl_{streak}")
            logger.info(f"Cooldown {sym}: {streak}-й стоп подряд -> карантин 150 мин")

    def _cleanup_expired_sl(self, sym: str):
        sl_ts = self.last_sl_ts.get(sym, 0)
        if sl_ts and (time.time() - sl_ts) > 5400 and sym not in self.fomo_cooldowns:
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)

    def check_structural_reset(
        self, sym: str, last_price: float, ema21_15m: float,
        ema50_15m: float = 0.0, gate_1h: str = "", patterns: list = None
    ) -> bool:
        """
        Разблокирует монету после стопа (фикс ICNT, PUMP, PEPE), как только она нашла дно
        и вернулась над 15m EMA21 или вышла в 1H_EMA21_RECLAIM / 1H_TREND_OK!
        """
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
        streak = self.consecutive_sl.get(sym, 0)
        min_wait = 1200 if streak <= 1 else 2400
        if elapsed < min_wait:
            return False

        pats = patterns or []
        has_reversal_pat = any(p in pats for p in ("pinbar", "engulfing", "divergence", "breakout", "vcp"))
        reclaimed_15m = (ema21_15m > 0 and last_price > ema21_15m * 1.001 and (ema21_15m >= ema50_15m * 0.998 or has_reversal_pat))
        reclaimed_1h = gate_1h in ("1H_TREND_OK", "1H_SUPER_TREND", "1H_EMA21_RECLAIM")

        if reclaimed_15m or reclaimed_1h:
            logger.info(f"🔓 Структурный сброс карантина {sym}: цена ${fmt_price(last_price)} развернулась ({gate_1h})")
            self.fomo_cooldowns.pop(sym, None)
            self.cooldown_meta.pop(sym, None)
            self.last_sl_price.pop(sym, None)
            self.last_sl_ts.pop(sym, None)
            self.consecutive_sl[sym] = 0
            return True

        return False

    def can_enter_sniper(
        self, sym: str, current_price: float, patterns: list = None,
        ema21_15m: float = 0.0, ema50_15m: float = 0.0, atr_15m: float = 0.0,
        score: float = 0.0, gate_1h: str = ""
    ) -> tuple:
        """
        Защита от повторного входа в падающий нож (но БЕЗ блокировки более низкого разворота дна, как было на ICNT!),
        и разрешение перезахода на откате к EMA21 после профита (фикс GLMR и DATA).
        """
        self._cleanup_expired_sl(sym)
        pats = patterns or []

        # Если был стоп меньше 45 минут назад и цена всё ещё летит вниз ПОД 15m EMA21 без паттерна разворота — не ловим нож!
        last_sl = self.last_sl_price.get(sym)
        sl_ts = self.last_sl_ts.get(sym, 0)
        if last_sl and (time.time() - sl_ts) < 2700:
            if ema21_15m > 0 and current_price < ema21_15m * 0.999 and gate_1h not in ("1H_DIP_BOUNCE", "1H_EMA21_RECLAIM", "1H_TREND_OK"):
                if not any(p in pats for p in ("pinbar", "engulfing", "divergence")):
                    return False, "цена ещё не вернулась над 15m EMA21 после стопа"

        win_p = self.last_win_price.get(sym)
        win_ts = self.last_win_ts.get(sym, 0)
        if win_p and (time.time() - win_ts) < 2400:
            has_brk_vcp = any(p in pats for p in ("vcp", "breakout", "engulfing", "pinbar"))
            is_healthy_pullback_or_wave = (
                gate_1h in ("1H_TREND_OK", "1H_SUPER_TREND", "1H_EMA21_RECLAIM", "1H_DIP_BOUNCE") and
                ((ema21_15m > 0 and atr_15m > 0 and (current_price - ema21_15m) <= 0.85 * atr_15m) or score >= 7.6)
            )
            if current_price >= win_p * 1.008 and not (has_brk_vcp or is_healthy_pullback_or_wave):
                return False, f"ждём откат к EMA21 после выхода (${fmt_price(win_p)})"
        return True, "OK"

    def can_enter_reversal(self, sym: str, current_price: float, ema21_15m: float) -> tuple:
        self._cleanup_expired_sl(sym)
        sl_ts = self.last_sl_ts.get(sym, 0)
        if sl_ts and (time.time() - sl_ts) < 1200:
            return False, "пауза 20 мин после стопа для подтверждения дна"
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
