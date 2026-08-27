import json
import time
from pathlib import Path

from loguru import logger

from bot.core.remote_state import download_state, upload_state

STATE_FILE = Path("storage/learner.json")
REMOTE_PATH = "learner.json"

KEYS = ["ema50", "ema21", "impulse", "rsi", "volume", "chg24h", "news_pos", "hype", "indep"]


class Learner:
    """Самообучение: взвешенное по силе сделок + адаптивная стратегия по PF и Max Drawdown."""

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
            data = download_state(REMOTE_PATH)
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

    def record(self, keys, win, pnl_pct=0.0):
        """Взвешенное обучение: сильные сделки влияют на веса больше."""
        self.results.append(1 if win else 0)
        self.results = self.results[-200:]
        abs_pnl = abs(pnl_pct)
        if abs_pnl >= 3.0:
            delta = 0.10 if win else -0.10
        elif abs_pnl >= 1.0:
            delta = 0.05 if win else -0.05
        else:
            delta = 0.03 if win else -0.03
        for k in keys:
            if k in self.weights:
                self.weights[k] = round(min(1.7, max(0.3, self.weights[k] + delta)), 3)
        self.save()
        logger.info(f"learner: win={win} pnl={pnl_pct:+.2f}% delta={delta:+.2f} keys={keys}")

    def reset(self):
        """Полный сброс: веса + статистика + порог."""
        self.weights = {k: 1.0 for k in KEYS}
        self.results = []
        self.threshold_adj = 0
        self.save()
        logger.info("learner: опыт сброшен")

    def reset_stats(self):
        """Сбросить только статистику (results, порог), веса-знания сохранить."""
        self.results = []
        self.threshold_adj = 0
        self.save()
        logger.info("learner: статистика сброшена (веса сохранены)")

    def winrate(self):
        last = self.results[-20:]
        return (sum(last) / len(last), len(last)) if last else (0.0, 0)

    # --- АДАПТИВНАЯ СТРАТЕГИЯ НА ОСНОВЕ PF И MAX DD (СМЯГЧЁННАЯ) ---
    def risk_mode(self, profit_factor, max_dd_pct, total_trades=0):
        """Возвращает (mode_name, динамический сдвиг порога).

        Мягкая логика без мёртвой петли:
        - штрафы по PF только после 5+ сделок (раньше — шум);
        - PF < 0.5   -> STRICT (+2);
        - PF 0.5–1.0 -> CAUTIOUS (+1), а не STRICT как раньше;
        - DD > 15%   -> +1 (защита капитала, всегда);
        - PF > 1.5 и DD < 8% -> -1 (AGGRESSIVE).
        """
        adj = 0
        enough = total_trades >= 5
        if enough and profit_factor is not None:
            if profit_factor < 0.5:
                adj += 2
            elif profit_factor < 1.0:
                adj += 1
            elif profit_factor > 1.5 and max_dd_pct and max_dd_pct < 8:
                adj -= 1
        if max_dd_pct and max_dd_pct > 15:
            adj += 1
        if adj >= 2:
            mode = "STRICT"
        elif adj == 1:
            mode = "CAUTIOUS"
        elif adj == -1:
            mode = "AGGRESSIVE"
        else:
            mode = "NORMAL"
        return mode, adj

    def update_threshold(self, profit_factor, max_dd_pct, total_trades=0):
        """Обновляет динамическую корректировку порога на основе метрик."""
        _, dyn_adj = self.risk_mode(profit_factor, max_dd_pct, total_trades)
        wr_adj = 0
        if total_trades >= 10:
            last = self.results[-20:]
            if last:
                wr = sum(last) / len(last)
                if wr < 0.35:
                    wr_adj = 1
        self.threshold_adj = dyn_adj + wr_adj
        self.save()
        return self.threshold_adj

    def summary(self):
        wr, n = self.winrate()
        top = sorted(self.weights.items(), key=lambda kv: kv[1], reverse=True)
        txt = ", ".join(f"{k} {v:.2f}" for k, v in top[:3])
        return f"winrate {wr:.0%} ({n}) | топ-веса: {txt} | строгость +{self.threshold_adj}"


learner = Learner()
