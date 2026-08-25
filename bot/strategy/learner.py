import json
from pathlib import Path

from loguru import logger

STATE_FILE = Path("storage/learner.json")

KEYS = ["ema50", "ema21", "impulse", "rsi", "volume", "chg24h", "news_pos", "hype"]


class Learner:
    """Самообучение: веса сигналов подстраиваются под результаты сделок."""

    def __init__(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []        # исходы последних сделок: 1 = TP, 0 = SL
        self.threshold_adj = 0   # 0 или +1 (строже вход при серии убытков)
        self._load()

    def _load(self):
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                self.weights.update(data.get("weights", {}))
                self.results = data.get("results", [])
                self.threshold_adj = data.get("threshold_adj", 0)
        except Exception as e:
            logger.error(f"learner load error: {e}")

    def save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({
                "weights": self.weights,
                "results": self.results[-200:],
                "threshold_adj": self.threshold_adj,
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"learner save error: {e}")

    def weight(self, key):
        return self.weights.get(key, 1.0)

    def record(self, keys, win):
        """Закрыта сделка: усиливаем/ослабляем веса причин, которые были в сигнале."""
        self.results.append(1 if win else 0)
        self.results = self.results[-200:]
        for k in keys:
            if k in self.weights:
                delta = 0.05 if win else -0.05
                self.weights[k] = round(min(1.7, max(0.3, self.weights[k] + delta)), 3)
        last = self.results[-20:]
        if len(last) >= 10:
            wr = sum(last) / len(last)
            if wr < 0.35:
                self.threshold_adj = 1     # серия убытков -> вход строже
            elif wr > 0.55:
                self.threshold_adj = 0
        self.save()
        logger.info(f"learner: win={win} keys={keys} thr_adj={self.threshold_adj}")

    def winrate(self):
        last = self.results[-20:]
        return (sum(last) / len(last), len(last)) if last else (0.0, 0)

    def summary(self):
        wr, n = self.winrate()
        top = sorted(self.weights.items(), key=lambda kv: kv[1], reverse=True)
        txt = ", ".join(f"{k} {v:.2f}" for k, v in top[:3])
        return f"winrate {wr:.0%} ({n} сделок) | топ-веса: {txt} | строгость +{self.threshold_adj}"


learner = Learner()
