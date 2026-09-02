import os
import json
import time
from pathlib import Path

from loguru import logger

from bot.core.remote_state import download_state, upload_state
from bot.strategy.learner import learner

STATE_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "shadow.json"
REMOTE_PATH = "shadow.json"

OBS_GAP = 2.0            # порог наблюдения = thr - 2
H4 = 4 * 3600
H24 = 24 * 3600
PUMP_PCT = 3.0
MAX_EPISODES = 300
COOLDOWN = 24 * 3600


def _band(score):
    if score < 5:
        return "0-5"
    if score < 6:
        return "5-6"
    if score < 7:
        return "6-7"
    return "7+"


class Shadow:
    """Теневой журнал: наблюдает монеты (в т.ч. упущенные), копит агрегаты,
    мягко тюнит порог/веса/охоту/SL. Хранит копейки данных."""

    def __init__(self):
        self.episodes = {}
        self.agg = {}
        self.cooldown = {}
        self.tuning = {
            "auto": True,
            "thr_nudge": 0.0,
            "hunt": -0.004, "near": -0.0015, "capture": +0.002,
            "sl_mult": 1.0, "tp_mult": 1.0,
        }
        self._last_upload = 0.0
        self._last_tune = 0.0
        self._load()

    def _load(self):
        data = None
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
        except Exception as e:
            logger.error(f"shadow load error: {e}")
        if data is None:
            data = download_state(REMOTE_PATH)
        if data:
            self.episodes = data.get("episodes", {})
            self.agg = data.get("agg", {})
            self.cooldown = data.get("cooldown", {})
            self.tuning.update(data.get("tuning", {}))
            logger.info(f"shadow: загружен ({len(self.episodes)} эпизодов)")

    def save(self):
        payload = {"episodes": self.episodes, "agg": self.agg,
                   "cooldown": self.cooldown, "tuning": self.tuning}
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"shadow save error: {e}")
        if time.time() - self._last_upload > 60:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    # ---------- наблюдение ----------
    def observe(self, scored, regime, thr):
        from bot.exchange.paper_exchange import paper
        now = time.time()
        obs_thr = thr - OBS_GAP
        for c in scored:
            sym = c["symbol"]
            if sym in self.episodes:
                ep = self.episodes[sym]
                if c["score"] > ep["max_score"]:
                    ep["max_score"] = c["score"]
                continue
            if c["score"] < obs_thr:
                continue
            if now < self.cooldown.get(sym, 0):
                continue
            if len(self.episodes) >= MAX_EPISODES:
                continue
            traded = (sym in paper.positions or
                      any(o["symbol"] == sym for o in paper.orders))
            self.episodes[sym] = {
                "ts": now, "price": c["last"], "max_score": c["score"],
                "regime": regime, "sector": c.get("sector"), "tier": c.get("tier"),
                "keys": c.get("reason_keys", []), "traded": bool(traded),
                "hi": c["last"], "lo": c["last"], "p4": None,
            }

    def tick(self, tickers):
        now = time.time()
        for sym, ep in list(self.episodes.items()):
            t = tickers.get(sym)
            if not t:
                continue
            last = t["last"]
            ep["hi"] = max(ep["hi"], last)
            ep["lo"] = min(ep["lo"], last)
            age = now - ep["ts"]
            if ep["p4"] is None and age >= H4:
                ep["p4"] = last
            if age >= H24:
                self._close(sym, ep, last)
        self.save()

    def _close(self, sym, ep, last):
        dec = ep["price"]
        self.episodes.pop(sym, None)
        self.cooldown[sym] = time.time() + COOLDOWN
        if dec <= 0:
            return
        move24 = (last - dec) / dec * 100
        move4 = ((ep["p4"] or last) - dec) / dec * 100
        mae = (ep["lo"] - dec) / dec * 100
        mfe = (ep["hi"] - dec) / dec * 100
        key = f"{ep['regime']}|{_band(ep['max_score'])}"
        a = self.agg.setdefault(key, {
            "n": 0, "sum4": 0.0, "sum24": 0.0, "pumps4": 0, "pumps24": 0,
            "traded": 0, "sum_mae": 0.0, "sum_mfe": 0.0, "sum_pull": 0.0,
            "keys_pump": {}, "keys_all": {},
        })
        a["n"] += 1
        a["sum4"] += move4
        a["sum24"] += move24
        if move4 >= PUMP_PCT:
            a["pumps4"] += 1
        if move24 >= PUMP_PCT:
            a["pumps24"] += 1
            for k in ep["keys"]:
                a["keys_pump"][k] = a["keys_pump"].get(k, 0) + 1
        for k in ep["keys"]:
            a["keys_all"][k] = a["keys_all"].get(k, 0) + 1
        if ep["traded"]:
            a["traded"] += 1
        a["sum_mae"] += mae
        a["sum_mfe"] += mfe
        a["sum_pull"] += mae   # pullback ≈ макс. просадка после сигнала

    # ---------- автотюн (раз в час) ----------
    def autotune(self):
        if not self.tuning["auto"]:
            return
        now = time.time()
        if now - self._last_tune < 3600:
            return
        self._last_tune = now

        # Калибровка порога: если под порогом копятся пампы — мягко ослабляем.
        nudge = 0.0
        base = {"bull": 5.0, "neutral": 6.5, "bear": 8.0}
        for reg, b in base.items():
            a = self.agg.get(f"{reg}|{_band(b - 1.0)}")
            if a and a["n"] >= 10:
                missed = a["n"] - a["traded"]
                if missed >= 5:
                    pump_rate = a["pumps24"] / a["n"]
                    avg = a["sum24"] / a["n"]
                    if pump_rate >= 0.3 and avg >= 2.0:
                        nudge -= 0.25
        self.tuning["thr_nudge"] = round(max(-0.5, min(0.5, nudge)), 2)

        # Охота: глубина = ~60% типичного отката.
        pull = self._avg("sum_pull")
        if pull is not None:
            self.tuning["hunt"] = round(max(-0.01, min(-0.002, pull / 100 * 0.6)), 4)

        # SL: если победители терпят просадку — чуть расширяем.
        sm = self._avg("sum_mae")
        if sm is not None:
            self.tuning["sl_mult"] = round(max(0.8, min(1.5, 1.0 + abs(sm) / 100 * 0.3)), 2)

        self._apply_weight_lift()
        self.save()
        logger.info(f"shadow autotune: {self.tuning}")

    def _apply_weight_lift(self):
        kp, ka = {}, {}
        for a in self.agg.values():
            for k, v in a["keys_pump"].items():
                kp[k] = kp.get(k, 0) + v
            for k, v in a["keys_all"].items():
                ka[k] = ka.get(k, 0) + v
        total_pump = sum(kp.values()) or 1
        total_all = sum(ka.values()) or 1
        base_rate = total_pump / total_all
        for k in list(learner.weights):
            if ka.get(k, 0) >= 10 and base_rate > 0:
                lift = (kp.get(k, 0) / ka.get(k, 0)) / base_rate
                if lift > 1.2:
                    learner.weights[k] = round(min(1.7, learner.weights[k] + 0.05), 3)
                elif lift < 0.8:
                    learner.weights[k] = round(max(0.3, learner.weights[k] - 0.05), 3)
        learner.save()

    def _avg(self, field, min_n=10):
        tn = sum(a["n"] for a in self.agg.values())
        ts = sum(a[field] for a in self.agg.values())
        return ts / tn if tn >= min_n else None

    # ---------- геттеры ----------
    def threshold_nudge(self):
        return self.tuning["thr_nudge"] if self.tuning["auto"] else 0.0

    def hunt(self):
        return self.tuning["hunt"]

    def near(self):
        return self.tuning["near"]

    def capture(self):
        return self.tuning["capture"]

    def sl_mult(self):
        return self.tuning["sl_mult"] if self.tuning["auto"] else 1.0

    def tp_mult(self):
        return self.tuning["tp_mult"] if self.tuning["auto"] else 1.0

    def set_auto(self, on):
        self.tuning["auto"] = bool(on)
        self.save()
        return self.tuning["auto"]

    # ---------- вывод ----------
    def learn_lines(self):
        out = ["👁 <b>Теневой журнал</b> · упущенные возможности (24ч)"]
        out.append(f"   автотюн: {'🟢 вкл' if self.tuning['auto'] else '🔴 выкл'} · "
                   f"эпизодов: {len(self.episodes)}")
        shown = False
        for key in sorted(self.agg):
            a = self.agg[key]
            if a["n"] < 3:
                continue
            reg, band = key.split("|")
            avg = a["sum24"] / a["n"]
            out.append(f"   {reg} · {band}: {a['n']} набл. · ср. {avg:+.1f}% · "
                       f"пампов {a['pumps24']} · взяли {a['traded']}")
            shown = True
        if not shown:
            out.append("   (накапливается)")
        t = self.tuning
        out.append(f"   тюнинг: порог {t['thr_nudge']:+.2f} · охота {t['hunt']*100:+.2f}% · "
                   f"SL ×{t['sl_mult']:.2f}")
        return out

    def stats_text(self):
        return "\n".join(self.learn_lines())


shadow = Shadow()
