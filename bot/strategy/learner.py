import json
import time
from pathlib import Path

from loguru import logger

from bot.core.remote_state import download_state, upload_state

STATE_FILE = Path("storage/learner.json")
REMOTE_PATH = "learner.json"

KEYS = ["ema50", "ema21", "impulse", "rsi", "volume", "chg24h", "indep", "news_pos", "hype"]

PATTERN_NAMES = {
    "pinbar": "🔨 Пин-бар выкупа (Spring)",
    "divergence": "🧲 Дивергенция RSI",
    "vcp": "📦 Сжатие волатильности (VCP)",
    "breakout": "🚀 Импульсный пробой",
    "engulfing": "⚡ Бычье поглощение",
}

TIERS = ["TOP20", "MID", "SMALL", "MICRO"]

# Блок 5.1 и 6.1: Границы нейтральной зоны безубытка (🛡🔒 БУ)
BE_LOW_PCT = -0.15
BE_HIGH_PCT = 0.25


def is_neutral_be(pnl_pct: float) -> bool:
    return BE_LOW_PCT <= float(pnl_pct) <= BE_HIGH_PCT


class Learner:
    """Самообучение: веса контекста + нейтральная зона БУ + управление сетапами Grade A+ [💎]."""

    def __init__(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.be_count = 0
        self.threshold_adj = 0.0
        self.current_risk_mode = "NORMAL"
        self.sector_stats = {}
        self.tier_stats = {}
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
                self.weights[k] = loaded_weights.get(k, 1.0)

            self.results = data.get("results", [])
            self.be_count = int(data.get("be_count", 0))
            self.threshold_adj = float(data.get("threshold_adj", 0))
            self.sector_stats = data.get("sector_stats", {})
            self.tier_stats = data.get("tier_stats", {})
            self.exit_stats = data.get("exit_stats", {})
            self.entry_stats = data.get("entry_stats", {})
            self.pattern_stats = data.get("pattern_stats", {})
            self.grade_stats = data.get("grade_stats", {"A+": [], "B": []})
            self.grade_a_streak = int(data.get("grade_a_streak", 0))
            logger.info("learner: опыт загружен")

    def save(self):
        payload = {
            "weights": self.weights,
            "results": self.results[-200:],
            "be_count": self.be_count,
            "threshold_adj": self.threshold_adj,
            "sector_stats": {k: v[-50:] for k, v in self.sector_stats.items()},
            "tier_stats": {k: v[-50:] for k, v in self.tier_stats.items()},
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
        return self.weights.get(key, 1.0)

    def grade_a_trust_mult(self) -> float:
        """
        Блок 6.2: Адаптивный мультипликатор доверия к сетапам Grade A+ [💎]:
        - Серия стопов (streak <= -2) -> снижаем вес позиции до 0.80x
        - Серия побед (streak >= 2) -> повышаем вес до 1.15x
        """
        if self.grade_a_streak <= -3:
            return 0.70
        if self.grade_a_streak == -2:
            return 0.82
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
               entry_mode=None, patterns=None, grade="B"):
        """
        Блок 5.1: Сделки в нейтральной зоне БУ (-0.15% <= pnl_pct <= +0.25%)
        НЕ накручивают ложный win=True и НЕ искажают веса индикаторов!
        """
        g_key = "A+" if grade == "A+" else "B"
        gh = self.grade_stats.setdefault(g_key, [])
        gh.append(round(pnl_pct, 2))
        self.grade_stats[g_key] = gh[-100:]

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
            eh = self.exit_stats.setdefault(exit_type, [])
            eh.append(round(pnl_pct, 2))
            self.exit_stats[exit_type] = eh[-50:]
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
            delta = 0.10 if real_win else -0.10
        elif abs_pnl >= 1.0:
            delta = 0.05 if real_win else -0.05
        else:
            delta = 0.03 if real_win else -0.03
        if runner_bonus > 5.0:
            delta += 0.05
        if soft and not real_win:
            delta *= 0.5

        for k in keys:
            if k in self.weights:
                self.weights[k] = round(min(1.7, max(0.3, self.weights[k] + delta)), 3)

        target = float(len(self.weights))
        cur = sum(self.weights.values())
        if cur > 0:
            k = target / cur
            self.weights = {
                w: round(min(1.7, max(0.3, v * k)), 3)
                for w, v in self.weights.items()
            }

        self.save()
        logger.info(f"learner: win={real_win} pnl={pnl_pct:+.2f}% grade={g_key} delta={delta:+.3f} keys={keys}")

    def pattern_multiplier(self, pattern_key: str) -> float:
        hist = [p for p in (self.pattern_stats.get(pattern_key) or []) if not is_neutral_be(p)]
        if len(hist) < 3:
            return 1.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        mult = 1.0 + (wr - 0.5) * 0.5 + min(0.2, max(-0.2, avg * 0.05))
        return round(max(0.60, min(1.40, mult)), 2)

    def sector_bias(self, sector):
        hist = [p for p in (self.sector_stats.get(sector) or []) if not is_neutral_be(p)]
        if len(hist) < 3:
            return 0.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        bias = (wr - 0.5) + max(-0.5, min(0.5, avg * 0.25))
        return round(max(-1.0, min(1.0, bias)), 2)

    def tier_bias(self, tier):
        hist = [p for p in (self.tier_stats.get(tier) or []) if not is_neutral_be(p)]
        if len(hist) < 5:
            return 0.0
        wr = sum(1 for p in hist if p > 0) / len(hist)
        avg = sum(hist) / len(hist)
        bias = (wr - 0.5) + max(-0.25, min(0.25, avg * 0.25))
        return round(max(-0.5, min(0.5, bias)), 2)

    def reset(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.be_count = 0
        self.threshold_adj = 0.0
        self.current_risk_mode = "NORMAL"
        self.sector_stats = {}
        self.tier_stats = {}
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
        self.sector_stats = {}
        self.tier_stats = {}
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

    def risk_mode(self, profit_factor, max_dd_pct, total_trades=0):
        adj = 0.0
        enough = total_trades >= 3
        if enough and profit_factor is not None:
            if profit_factor < 0.6:
                adj += 1.5
            elif profit_factor < 1.0:
                adj += 0.5
            elif profit_factor > 1.5 and max_dd_pct and max_dd_pct < 8:
                adj -= 0.5
        if max_dd_pct and max_dd_pct > 12:
            adj += 1.0
        elif max_dd_pct and max_dd_pct > 8:
            adj += 0.5

        if adj >= 1.5:
            mode = "STRICT"
        elif adj >= 0.5:
            mode = "CAUTIOUS"
        elif adj <= -0.5:
            mode = "AGGRESSIVE"
        else:
            mode = "NORMAL"
        return mode, adj

    def update_threshold(self, profit_factor, max_dd_pct, total_trades=0):
        mode, dyn_adj = self.risk_mode(profit_factor, max_dd_pct, total_trades)
        self.current_risk_mode = mode

        wr_adj = 0.0
        if total_trades >= 5:
            last = self.results[-20:]
            if last:
                wr = sum(last) / len(last)
                if wr < 0.35:
                    wr_adj = 0.25

        raw_adj = dyn_adj + wr_adj
        self.threshold_adj = round(max(-0.25, min(0.25, raw_adj * 0.2)), 2)
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
