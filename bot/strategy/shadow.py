import os
import json
import time
from pathlib import Path

from loguru import logger
from bot.core.remote_state import download_state, upload_state
from bot.strategy.learner import learner

STATE_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "shadow.json"
REMOTE_PATH = "shadow.json"

OBS_GAP = 2.0            # порог наблюдения
H4 = 4 * 3600
H24 = 24 * 3600
PUMP_PCT = 3.0
MAX_EPISODES = 300
COOLDOWN = 24 * 3600


def _band(score):
    if score < 5: return "0-5"
    if score < 6: return "5-6"
    if score < 7: return "6-7"
    if score < 8: return "7-8"
    return "8+"


class Shadow:
    """Теневой журнал: наблюдает монеты, копит агрегаты и независимо тюнит стратегии."""

    def __init__(self):
        self.episodes = {}
        self.agg = {}
        self.cooldown = {}
        self.tuning = {
            "auto": True,
            "nudge_rocket": 0.0, "nudge_sniper": 0.0, "nudge_reversal": 0.0,
            "hunt": -0.004, "near": -0.0015, "capture": +0.002,
            "sl_mult": 1.0, "tp_mult": 1.0,
            "signal_windows": {"rsi_hi": 90, "chg_hi": 30, "vol_lo": 1.3},
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
        if time.time() - self._last_upload > 3600:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def observe(self, scored, regime, thrs):
        from bot.exchange.paper_exchange import paper
        now = time.time()
        
        for c in scored:
            sym = c["symbol"]
            mode = c.get("entry_mode", "sniper")
            thr = thrs.get(mode, 6.0)
            obs_thr = thr - OBS_GAP
            
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
            
            traded = (sym in paper.positions or any(o["symbol"] == sym for o in paper.orders))
            
            self.episodes[sym] = {
                "ts": now, "price": c["last"], "max_score": c["score"],
                "regime": regime, "entry_mode": mode,
                "sector": c.get("sector"), "tier": c.get("tier"),
                "keys": c.get("reason_keys", []), "traded": bool(traded),
                "success": False, 
                "hi": c["last"], "lo": c["last"], "p4": None,
                "lo_before_pump": c["last"], "pumped": False,  
                "signal_values": c.get("signal_values", {}),
            }

    def mark_success(self, sym):
        if sym in self.episodes:
            self.episodes[sym]["success"] = True
            self.save()

    def tick(self, tickers):
        now = time.time()
        for sym, ep in list(self.episodes.items()):
            t = tickers.get(sym)
            if not t:
                continue
            last = t["last"]
            ep["hi"] = max(ep["hi"], last)
            ep["lo"] = min(ep["lo"], last)
            
            if not ep.get("pumped"):
                ep["lo_before_pump"] = min(ep.get("lo_before_pump", last), last)
                if last >= ep["price"] * (1 + PUMP_PCT / 100):
                    ep["pumped"] = True
            
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
        pullback = (ep.get("lo_before_pump", ep["lo"]) - dec) / dec * 100
        
        # === Ключ агрегации теперь содержит тип стратегии ===
        mode = ep.get("entry_mode", "sniper")
        key = f"{ep['regime']}|{mode}|{_band(ep['max_score'])}"
        
        a = self.agg.setdefault(key, {
            "n": 0, "sum4": 0.0, "sum24": 0.0, "pumps4": 0, "pumps24": 0,
            "traded": 0, "sum_mae": 0.0, "sum_mfe": 0.0, "sum_pull": 0.0,
            "sum_pull_pump": 0.0, "sum_mae_pump": 0.0, 
            "keys_pump": {}, "keys_all": {},
        })
        a["n"] += 1
        a["sum4"] += move4
        a["sum24"] += move24
        a["sum_mae"] += mae
        a["sum_mfe"] += mfe
        a["sum_pull"] += mae   
        
        if move4 >= PUMP_PCT:
            a["pumps4"] += 1
            
        if move24 >= PUMP_PCT:
            a["pumps24"] += 1
            a["sum_pull_pump"] = a.get("sum_pull_pump", 0.0) + pullback
            a["sum_mae_pump"] = a.get("sum_mae_pump", 0.0) + mae
            
            for k in ep["keys"]:
                a["keys_pump"][k] = a["keys_pump"].get(k, 0) + 1
            sv = ep.get("signal_values", {})
            for k, v in sv.items():
                a.setdefault("signal_values_pump", {}).setdefault(k, []).append(v)
                
        for k in ep["keys"]:
            a["keys_all"][k] = a["keys_all"].get(k, 0) + 1
            
        if ep.get("success"):
            a["traded"] += 1

    def autotune(self):
        if not self.tuning["auto"]:
            return
        now = time.time()
        if now - self._last_tune < 3600:
            return
        self._last_tune = now

        # === ИНДИВИДУАЛЬНАЯ КАЛИБРОВКА ПО СТРАТЕГИЯМ ===
        for mode in ["rocket", "sniper", "reversal"]:
            nudge = 0.0
            mode_pumps = 0
            mode_traded = 0
            mode_n = 0
            
            for key, a in self.agg.items():
                if f"|{mode}|" in key:
                    mode_n += a["n"]
                    mode_pumps += a["pumps24"]
                    mode_traded += a["traded"]
            
            if mode_n >= 5:
                pump_rate = mode_pumps / mode_n
                missed = mode_pumps - mode_traded
                
                # Если упускаем пампы -> Смягчаем порог
                if missed >= 3 and pump_rate >= 0.3:
                    nudge -= 0.25
                # Если ловим фейкауты -> Ужесточаем порог
                elif pump_rate < 0.15 and mode_traded >= 1:
                    nudge += 0.25
                    
            self.tuning[f"nudge_{mode}"] = round(max(-0.5, min(0.5, nudge)), 2)

        # Общие агрегаты (SL и Hunt) по успешным пампам
        total_pumps = 0
        total_missed_pumps = 0
        sum_pull_pump = 0.0
        sum_mae_pump = 0.0
        
        for a in self.agg.values():
            pumps = a.get("pumps24", 0)
            total_pumps += pumps
            total_missed_pumps += (pumps - a.get("traded", 0))
            sum_pull_pump += a.get("sum_pull_pump", 0.0)
            sum_mae_pump += a.get("sum_mae_pump", 0.0)

        if total_pumps >= 5:
            avg_pull = sum_pull_pump / total_pumps
            avg_mae = sum_mae_pump / total_pumps

            if total_missed_pumps > 10:
                self.tuning["hunt"] = round(max(-0.01, min(-0.001, avg_pull / 100 * 0.3)), 4)
            else:
                self.tuning["hunt"] = round(max(-0.01, min(-0.002, avg_pull / 100 * 0.6)), 4)

            self.tuning["sl_mult"] = round(max(0.8, min(1.5, 1.0 + abs(avg_mae) / 100 * 0.3)), 2)

        self._apply_weight_lift()
        self._calibrate_signal_windows()
        self.save()
        logger.info(f"shadow autotune: {self.tuning}")

    def _apply_weight_lift(self):
        kp, ka = {}, {}
        for a in self.agg.values():
            for k, v in a.get("keys_pump", {}).items():
                kp[k] = kp.get(k, 0) + v
            for k, v in a.get("keys_all", {}).items():
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

    def _calibrate_signal_windows(self):
        pump_values = {"rsi": [], "chg24h": [], "volume": []}
        for a in self.agg.values():
            sv = a.get("signal_values_pump", {})
            for k in pump_values:
                pump_values[k].extend(sv.get(k, []))
        if len(pump_values["rsi"]) < 50:
            return
        
        w = self.tuning["signal_windows"]
        rsi_sorted = sorted(pump_values["rsi"])
        rsi_p90 = rsi_sorted[int(len(rsi_sorted) * 0.9)]
        w["rsi_hi"] = int(round(w["rsi_hi"] * 0.8 + max(80, min(95, round(rsi_p90))) * 0.2))
        
        chg_sorted = sorted(pump_values["chg24h"])
        chg_p90 = chg_sorted[int(len(chg_sorted) * 0.9)]
        w["chg_hi"] = int(round(w["chg_hi"] * 0.8 + max(20, min(50, round(chg_p90))) * 0.2))
        
        vol_sorted = sorted(pump_values["volume"])
        vol_p10 = vol_sorted[int(len(vol_sorted) * 0.1)]
        w["vol_lo"] = round(w["vol_lo"] * 0.8 + max(1.0, min(3.0, round(vol_p10, 1))) * 0.2, 1)

    def signal_windows(self):
        return self.tuning.get("signal_windows", {"rsi_hi": 90, "chg_hi": 30, "vol_lo": 1.3})

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

    def learn_lines(self):
        out = ["👁 <b>Теневой журнал</b> · упущенные возможности (24ч)"]
        out.append(f"   автотюн: {'🟢 вкл' if self.tuning['auto'] else '🔴 выкл'} · "
                   f"эпизодов: {len(self.episodes)}")
        shown = False
        reg_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}
        
        for key in sorted(self.agg):
            a = self.agg[key]
            if a["n"] < 3: continue
            
            # Парсим новый 3-составной ключ (Режим|Стратегия|Скор)
            parts = key.split("|")
            if len(parts) == 3:
                reg, mode, band = parts
            else:
                continue
                
            avg = a["sum24"] / a["n"]
            em = reg_emoji.get(reg, "⚪")
            mode_icon = "🚀" if mode == "rocket" else "🧲" if mode == "reversal" else "🏹"
            
            out.append(f"   {em} {mode_icon} {band}: {a['n']} набл. · ср. {avg:+.1f}% · "
                       f"🎯 {a.get('traded', 0)}/{a['pumps24']} пампов")
            shown = True
            
        if not shown:
            out.append("   (накапливается)")
            
        t = self.tuning
        w = t.get("signal_windows", {})
        out.append(f"   ⚙️ тюнинг: 🚀 {t.get('nudge_rocket', 0):+.2f} | 🏹 {t.get('nudge_sniper', 0):+.2f} | 🧲 {t.get('nudge_reversal', 0):+.2f}")
        out.append(f"   📏 откат {t['hunt']*100:+.2f}% · SL ×{t['sl_mult']:.2f}")
        out.append(f"   📏 RSI ≤{w.get('rsi_hi', 90)} · chg ≤{w.get('chg_hi', 30)}% · vol ≥{w.get('vol_lo', 1.3)}×")
        return out

    def stats_text(self):
        return "\n".join(self.learn_lines())

    def reset(self):
        self.episodes = {}
        self.agg = {}
        self.cooldown = {}
        self.tuning = {
            "auto": True,
            "nudge_rocket": 0.0, "nudge_sniper": 0.0, "nudge_reversal": 0.0,
            "hunt": -0.004, "near": -0.0015, "capture": +0.002,
            "sl_mult": 1.0, "tp_mult": 1.0,
            "signal_windows": {"rsi_hi": 90, "chg_hi": 30, "vol_lo": 1.3},
        }
        self.save()
        logger.info("shadow: журнал автотюна полностью сброшен")

shadow = Shadow()
