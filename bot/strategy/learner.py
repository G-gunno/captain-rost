import json
import time
from pathlib import Path

from loguru import logger

from bot.core.remote_state import download_state, upload_state
from bot.core.state import bot_state

STATE_FILE = Path("storage/learner.json")
REMOTE_PATH = "learner.json"

KEYS = ["ema50", "ema21", "impulse", "rsi", "volume", "chg24h", "indep", "news_pos", "hype"]

# Защита каркасных трендовых весов от деградации и перекоса в сторону indep
WEIGHT_FLOORS = {
    "ema50": 0.88,
    "ema21": 0.85,
    "impulse": 0.85,
    "volume": 0.78,
    "rsi": 0.75,
    "chg24h": 0.75,
    "indep": 0.70,
    "news_pos": 0.75,
    "hype": 0.75,
}

WEIGHT_CAPS = {
    "ema50": 1.35,
    "ema21": 1.35,
    "impulse": 1.35,
    "volume": 1.30,
    "rsi": 1.25,
    "chg24h": 1.25,
    "indep": 1.15,
    "news_pos": 1.20,
    "hype": 1.20,
}

PATTERN_NAMES = {
    "pinbar": "🔨 Пин-бар выкупа (Spring)",
    "divergence": "🧲 Дивергенция RSI",
    "vcp": "📦 Сжатие волатильности (VCP)",
    "breakout": "🚀 Импульсный пробой",
    "engulfing": "⚡ Бычье поглощение",
}

TIERS = ["TOP20", "MID", "SMALL", "MICRO"]
MODE_LADDER = ["STRICT", "CAUTIOUS", "NORMAL", "AGGRESSIVE"]

BE_LOW_PCT = -0.25
BE_HIGH_PCT = 0.25


def is_neutral_be(pnl_pct: float) -> bool:
    return BE_LOW_PCT <= float(pnl_pct) <= BE_HIGH_PCT


def clamp_weight(key: str, val: float) -> float:
    lo = WEIGHT_FLOORS.get(key, 0.75)
    hi = WEIGHT_CAPS.get(key, 1.30)
    return round(min(hi, max(lo, float(val))), 3)


class Learner:
    """Самообучение: защищённый каркас весов тренда + персональный рейтинг доверия к монетам (Coin Trust)."""

    def __init__(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.be_count = 0
        self.threshold_adj = 0.0
        self.current_risk_mode = "NORMAL"
        self._last_mode_change_ts = 0.0
        self.sector_stats = {}
        self.tier_stats = {}
        self.coin_stats = {}
        self.exit_stats = {}
        self.entry_stats = {}
        self.pattern_stats = {}
        self.grade_stats = {"A+": [], "B": []}
        self.grade_a_streak = 0
        self._last_upload = 0.0
        self._load()

    def _load(self):
        data = None
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
        except Exception as e:
            logger.error(f"learner load error: {e}")
        if data is None:
            data = download_state(REMOTE_PATH)
        if data:
            loaded_weights = data.get("weights", {})
            for k in KEYS:
                w_val = float(loaded_weights.get(k, 1.0))
                self.weights[k] = clamp_weight(k, w_val)

            self.results = data.get("results", [])
            self.be_count = int(data.get("be_count", 0))
            self.threshold_adj = float(data.get("threshold_adj", 0))
            self.current_risk_mode = data.get("current_risk_mode", "NORMAL")
            self._last_mode_change_ts = float(data.get("last_mode_change_ts", 0.0))
            self.sector_stats = data.get("sector_stats", {})
            self.tier_stats = data.get("tier_stats", {})
            self.coin_stats = data.get("coin_stats", {})
            self.exit_stats = data.get("exit_stats", {})
            if "🛡🔒" in self.exit_stats:
                old_be = self.exit_stats.pop("🛡🔒")
                for p in old_be:
                    target_k = "🛡💼" if p > BE_HIGH_PCT else "🛡🔒 БУ"
                    self.exit_stats.setdefault(target_k, []).append(p)
            self.entry_stats = data.get("entry_stats", {})
            self.pattern_stats = data.get("pattern_stats", {})
            self.grade_stats = data.get("grade_stats", {"A+": [], "B": []})
            self.grade_a_streak = int(data.get("grade_a_streak", 0))
            logger.info("learner: опыт загружен, веса тренда защищены (Structural Anchor)")

    def save(self):
        payload = {
            "weights": self.weights,
            "results": self.results[-200:],
            "be_count": self.be_count,
            "threshold_adj": self.threshold_adj,
            "current_risk_mode": self.current_risk_mode,
            "last_mode_change_ts": self._last_mode_change_ts,
            "sector_stats": {k: v[-50:] for k, v in self.sector_stats.items()},
            "tier_stats": {k: v[-50:] for k, v in self.tier_stats.items()},
            "coin_stats": {k: v[-30:] for k, v in self.coin_stats.items()},
            "exit_stats": {k: v[-50:] for k, v in self.exit_stats.items()},
            "entry_stats": {k: v[-50:] for k, v in self.entry_stats.items()},
            "pattern_stats": {k: v[-50:] for k, v in self.pattern_stats.items()},
            "grade_stats": {k: v[-100:] for k, v in self.grade_stats.items()},
            "grade_a_streak": self.grade_a_streak,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"learner save error: {e}")
        if time.time() - self._last_upload > 3600:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def weight(self, key):
        return clamp_weight(key, self.weights.get(key, 1.0))

    def grade_a_trust_mult(self) -> float:
        if self.grade_a_streak <= -3:
            return 0.75
        if self.grade_a_streak == -2:
            return 0.85
        if self.grade_a_streak >= 3:
            return 1.15
        if self.grade_a_streak == 2:
            return 1.08
        return 1.0

    def get_grade_a_stats(self) -> dict:
        hist = self.grade_stats.get("A+", [])
        decisive = [p for p in hist if not is_neutral_be(p)]
        be_cnt = len(hist) - len(decisive)
        wins = [p for p in decisive if p > 0]
        wr = (len(wins) / len(decisive)) if decisive else 0.0
        avg = (sum(decisive) / len(decisive)) if decisive else 0.0
        return {
            "total": len(hist),
            "decisive": len(decisive),
            "be": be_cnt,
            "wr": wr,
            "avg": avg,
            "streak": self.grade_a_streak,
            "mult": self.grade_a_trust_mult(),
        }

    def record(self, keys, win, pnl_pct=0.0, sector=None, tier=None,
               exit_type=None, runner_bonus=0.0, soft=False,
               entry_mode=None, patterns=None, grade="B", symbol=None):
        g_key = "A+" if grade == "A+" else "B"
        gh = self.grade_stats.setdefault(g_key, [])
        gh.append(round(pnl_pct, 2))
        self.grade_stats[g_key] = gh[-100:]

        if symbol:
            ch = self.coin_stats.setdefault(symbol, [])
            ch.append(round(pnl_pct, 2))
            self.coin_stats[symbol] = ch[-30:]

        if is_neutral_be(pnl_pct):
            self.be_count += 1
            eh = self.exit_stats.setdefault("🛡🔒 БУ", [])
            eh.append(round(pnl_pct, 2))
            self.exit_stats["🛡🔒 БУ"] = eh[-50:]
            self.save()
            logger.info(f"learner: нейтральный выход 🛡🔒 БУ ({pnl_pct:+.2f}%) — веса и Winrate не затронуты")
            return

        real_win = pnl_pct > BE_HIGH_PCT
        self.results.append(1 if real_win else 0)
        self.results = self.results[-200:]

        if g_key == "A+":
            if real_win:
                self.grade_a_streak = max(1, self.grade_a_streak + 1)
            else:
                self.grade_a_streak = min(-1, self.grade_a_streak - 1)

        if sector:
            hist = self.sector_stats.setdefault(sector, [])
            hist.append(round(pnl_pct, 2))
            self.sector_stats[sector] = hist[-50:]
        if tier:
            hist = self.tier_stats.setdefault(tier, [])
            hist.append(round(pnl_pct, 2))
            self.tier_stats[tier] = hist[-50:]
        if exit_type:
            norm_exit = "🛡💼" if (exit_type == "🛡🔒" and real_win) else ("🛡📉" if (exit_type == "🛡🔒" and not real_win) else exit_type)
            eh = self.exit_stats.setdefault(norm_exit, [])
            eh.append(round(pnl_pct, 2))
            self.exit_stats[norm_exit] = eh[-50:]
        if entry_mode:
            em_h = self.entry_stats.setdefault(entry_mode, [])
            em_h.append(round(pnl_pct, 2))
            self.entry_stats[entry_mode] = em_h[-50:]

        if patterns:
            for p in patterns:
                ph = self.pattern_stats.setdefault(p, [])
                ph.append(round(pnl_pct, 2))
                self.pattern_stats[p] = ph[-50:]

        abs_pnl = abs(pnl_pct)
        if abs_pnl >= 3.0:
            delta = 0.045 if real_win else -0.040
        elif abs_pnl >= 1.0:
            delta = 0.025 if real_win else -0.025
        else:
            delta = 0.015 if real_win else -0.015
        if runner_bonus > 5.0:
            delta += 0.025
        if soft and not real_win:
            delta *= 0.5

        for k in KEYS:
            w_cur = self.weights.get(k, 1.0)
            if k in keys:
                # Базовые индикаторы тренда штрафуются в 2 раза мягче при стопе, чтобы не ослеплять сканер
                eff_delta = delta * 0.5 if (not real_win and k in ("ema50", "ema21", "impulse")) else delta
                w_cur += eff_delta
            w_reverted = w_cur * 0.96 + 1.0 * 0.04
            self.weights[k] = clamp_weight(k, w_reverted)

        self.save()
        logger.info(f"learner: win={real_win} pnl={pnl_pct:+.2f}% grade={g_key} delta={delta:+.3f} keys={keys}")

    def coin_bias(self, symbol: str) -> float:
        """
        Персональный рейтинг доверия к монете (от -1.50 до +0.60 баллов к скору):
        - 2 стопа подряд -> штраф -1.50 (защита от повторных сливов на ENA, TRUMP, PYTH).
        - Стабильные профиты -> бонус до +0.60 (приоритет для AVAX, NXPC, BSB, ARX).
        """
        raw_hist = self.coin_stats.get(symbol) or []
        decisive = [p for p in raw_hist if not is_neutral_be(p)]
        if not decisive:
            return 0.0

        if len(decisive) >= 2 and decisive[-1] < 0 and decisive[-2] < 0:
            return -1.50
        if len(decisive) == 1:
            return 0.20 if decisive[0] > 0.5 else (-0.45 if decisive[0] < -0.5 else 0.0)

        recent = decisive[-6:]
        wr = sum(1 for p in recent if p > 0) / len(recent)
        avg = sum(recent) / len(recent)
        bias = (wr - 0.5) * 0.9 + max(-0.6, min(0.4, avg * 0.22))
        if recent[-1] < 0:
            bias -= 0.25
        return round(max(-1.50, min(0.60, bias)), 2)

    def coin_trust_mult(self, symbol: str) -> float:
        """Множитель размера позиции по доверию к конкретной монете (0.65x .. 1.15x)."""
        if not symbol:
            return 1.0
        b = self.coin_bias(symbol)
        if b <= -1.0:
            return 0.65
        if b < -0.3:
            return 0.80
        if b >= 0.35:
            return 1.15
        if b >= 0.15:
            return 1.08
        return 1.0

    def pattern_multiplier(self, pattern_key: str) -> float:
        hist = [p for p in (self.pattern_stats.get(pattern_key) or []) if not is_neutral_be(p)]
        if len(hist) < 3:
            return 1.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        mult = 1.0 + (wr - 0.5) * 0.5 + min(0.2, max(-0.2, avg * 0.05))
        return round(max(0.75, min(1.30, mult)), 2)

    def sector_bias(self, sector):
        hist = [p for p in (self.sector_stats.get(sector) or []) if not is_neutral_be(p)]
        if len(hist) < 3:
            return 0.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        bias = (wr - 0.5) + max(-0.4, min(0.4, avg * 0.20))
        return round(max(-0.65, min(0.65, bias)), 2)

    def tier_bias(self, tier):
        hist = [p for p in (self.tier_stats.get(tier) or []) if not is_neutral_be(p)]
        if len(hist) < 5:
            return 0.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        bias = (wr - 0.5) + max(-0.25, min(0.25, avg * 0.25))
        return round(max(-0.45, min(0.45, bias)), 2)

    def reset(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.be_count = 0
        self.threshold_adj = 0.0
        self.current_risk_mode = "NORMAL"
        self._last_mode_change_ts = 0.0
        self.sector_stats = {}
        self.tier_stats = {}
        self.coin_stats = {}
        self.exit_stats = {}
        self.entry_stats = {}
        self.pattern_stats = {}
        self.grade_stats = {"A+": [], "B": []}
        self.grade_a_streak = 0
        self.save()
        logger.info("learner: опыт сброшен")

    def reset_stats(self):
        self.results = []
        self.be_count = 0
        self.threshold_adj = 0.0
        self.current_risk_mode = "NORMAL"
        self._last_mode_change_ts = 0.0
        self.sector_stats = {}
        self.tier_stats = {}
        self.coin_stats = {}
        self.exit_stats = {}
        self.entry_stats = {}
        self.pattern_stats = {}
        self.grade_stats = {"A+": [], "B": []}
        self.grade_a_streak = 0
        self.save()
        logger.info("learner: статистика сброшена")

    def winrate(self):
        last = self.results[-20:]
        return (sum(last) / len(last), len(last)) if last else (0.0, 0)

    def _raw_target_mode(self, profit_factor, max_dd_pct, total_trades=0) -> tuple:
        adj = 0.0
        enough = total_trades >= 4
        if enough and profit_factor is not None:
            if profit_factor < 0.60:
                adj += 1.5
            elif profit_factor < 1.02:
                adj += 0.5
            elif profit_factor > 1.55 and (max_dd_pct or 0) < 6.0 and total_trades >= 6:
                adj -= 0.5
        if max_dd_pct and max_dd_pct > 12:
            adj += 1.0
        elif max_dd_pct and max_dd_pct > 8:
            adj += 0.5

        if adj >= 1.5:
            target = "STRICT"
        elif adj >= 0.5:
            target = "CAUTIOUS"
        elif adj <= -0.5:
            target = "AGGRESSIVE"
        else:
            target = "NORMAL"

        if getattr(bot_state, "current_regime", "neutral") == "bear" and target == "AGGRESSIVE":
            target = "NORMAL"
            adj = max(0.0, adj)

        return target, adj

    def risk_mode(self, profit_factor, max_dd_pct, total_trades=0):
        target_mode, _ = self._raw_target_mode(profit_factor, max_dd_pct, total_trades)
        cur_mode = self.current_risk_mode if self.current_risk_mode in MODE_LADDER else "NORMAL"
        cur_idx = MODE_LADDER.index(cur_mode)
        tgt_idx = MODE_LADDER.index(target_mode)

        now = time.time()
        if tgt_idx != cur_idx and (now - self._last_mode_change_ts >= 3600 or self._last_mode_change_ts == 0.0):
            step = 1 if tgt_idx > cur_idx else -1
            cur_idx = max(0, min(len(MODE_LADDER) - 1, cur_idx + step))
            cur_mode = MODE_LADDER[cur_idx]

        adj_map = {"STRICT": 1.2, "CAUTIOUS": 0.5, "NORMAL": 0.0, "AGGRESSIVE": -0.4}
        return cur_mode, adj_map.get(cur_mode, 0.0)

    def update_threshold(self, profit_factor, max_dd_pct, total_trades=0):
        old_mode = self.current_risk_mode if self.current_risk_mode in MODE_LADDER else "NORMAL"
        new_mode, dyn_adj = self.risk_mode(profit_factor, max_dd_pct, total_trades)
        if new_mode != old_mode:
            self.current_risk_mode = new_mode
            self._last_mode_change_ts = time.time()
            logger.info(f"learner: плавный переход режима риска {old_mode} -> {new_mode}")

        wr_adj = 0.0
        if total_trades >= 5:
            last = self.results[-20:]
            if last:
                wr = sum(last) / len(last)
                if wr < 0.35:
                    wr_adj = 0.20

        raw_adj = dyn_adj + wr_adj
        target_thr_adj = max(-0.20, min(0.20, raw_adj * 0.15))
        self.threshold_adj = round(0.6 * self.threshold_adj + 0.4 * target_thr_adj, 2)
        self.save()
        return self.threshold_adj

    def summary(self):
        wr, n = self.winrate()
        top = sorted(self.weights.items(), key=lambda kv: kv[1], reverse=True)[:3]
        txt = ", ".join(f"{k} {v:.2f}" for k, v in top[:3])
        return f"wr {wr:.0%} ({n}) · 🛡🔒 БУ: {self.be_count} · топ: {txt} · режим: {self.current_risk_mode}"

    def kelly_multiplier(self, mode):
        hist = [p for p in (self.entry_stats.get(mode) or []) if not is_neutral_be(p)]
        if len(hist) < 10:
            return None
        wins = [p for p in hist if p > 0]
        losses = [p for p in hist if p <= 0]
        if not wins or not losses:
            return None
        p = len(wins) / len(hist)
        avg_win = sum(wins) / len(wins)
        avg_loss = abs(sum(losses) / len(losses))
        b = avg_win / avg_loss if avg_loss else 0
        if b <= 0:
            return 0.0
        kelly = (b * p - (1 - p)) / b
        return round(max(0.0, min(kelly * 0.5, 1.0)), 3)


learner = Learner()
