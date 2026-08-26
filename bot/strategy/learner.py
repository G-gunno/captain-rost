import json
import time
from pathlib import Path

from loguru import logger

from bot.core.remote_state import download_state, upload_state

STATE_FILE = Path("storage/learner.json")
REMOTE_PATH = "learner.json"

KEYS = ["ema50", "ema21", "impulse", "rsi", "volume", "chg24h", "news_pos", "hype", "indep"]


class Learner:
    """Самообучение: веса сигналов подстраиваются под результаты сделок.
    Опыт НЕ сбрасывается командами /exitall и /pause — только /resetlearn."""

    def __init__(self):
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.threshold_adj = 0
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
            data = download_state(REMOTE_PATH)  # опыт из GitHub (если настроен)
        if data:
            self.weights.update(data.get("weights", {}))
            self.results = data.get("results", [])
            self.threshold_adj = data.get("threshold_adj", 0)
            logger.info("learner: опыт загружен")

    def save(self):
        payload = {
            "weights": self.weights,
            "results": self.results[-200:],
            "threshold_adj": self.threshold_adj,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"learner save error: {e}")
        if time.time() - self._last_upload > 60:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def weight(self, key):
        return self.weights.get(key, 1.0)

    def record(self, keys, win):
        """Закрыта сделка: усиливаем/ослабляем веса причин сигнала."""
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
                self.threshold_adj = 1
            elif wr > 0.55:
                self.threshold_adj = 0
        self.save()
        logger.info(f"learner: win={win} keys={keys} thr_adj={self.threshold_adj}")

    def reset(self):
        """Полный сброс опыта — только по команде /resetlearn."""
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.threshold_adj = 0
        self.save()
        logger.info("learner: опыт сброшен")

    def winrate(self):
        last = self.results[-20:]
        return (sum(last) / len(last), len(last)) if last else (0.0, 0)

    def summary(self):
        wr, n = self.winrate()
        top = sorted(self.weights.items(), key=lambda kv: kv[1], reverse=True)
        txt = ", ".join(f"{k} {v:.2f}" for k, v in top[:3])
        return f"winrate {wr:.0%} ({n} сделок) | топ-веса: {txt} | строгость +{self.threshold_adj}"


learner = Learner()
