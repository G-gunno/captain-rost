import os
import json
import time
from pathlib import Path

from loguru import logger
from bot.core.remote_state import download_state, upload_state
from bot.strategy.learner import learner, clamp_weight

STATE_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "shadow.json"
REMOTE_PATH = "shadow.json"

OBS_GAP = 2.5
H1 = 1800
H4 = 4 * 3600
PUMP_PCT = 3.2
MAX_EPISODES = 350
COOLDOWN = 3 * 3600


def _band(score):
    if score < 5:
        return "0-5"
    if score < 6:
        return "5-6"
    if score < 7:
        return "6-7"
    if score < 8:
        return "7-8"
    return "8+"


class Shadow:
    """
    Теневой журнал 4ч-волн + Детектор Упущенных Пампов (Missed Pump Tracker):
    - Отслеживает ВСЕ монеты пула (включая отсеянные через 1H_BAN и tactical_hold) на 4-часовом горизонте волны.
    - Если монета с хорошим потенциалом была пропущена и дала памп >= +3.2% за 4 часа — снижает порог входа (nudge),
      а не задирает его на падающем рынке.
    """

    def __init__(self):
        self.episodes = {}
        self.agg = {}
        self.missed_bans = {}
        self.cooldown = {}
        self.coin_dna = {}
        self.tuning = {
            "auto": True,
            "nudge_rocket": 0.0, "nudge_sniper": 0.0, "nudge_reversal": 0.0,
            "hunt": -0.0030, "near": -0.0018, "capture": +0.002,
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
            self.missed_bans = data.get("missed_bans", {})
            self.cooldown = data.get("cooldown", {})
            self.coin_dna = data.get("coin_dna", {})
            self.tuning.update(data.get("tuning", {}))
            # Сбрасываем старую завышенную накрутку порогов (> +0.20)
            for k_nudge in ("nudge_rocket", "nudge_sniper", "nudge_reversal"):
                self.tuning[k_nudge] = max(-0.25, min(0.20, float(self.tuning.get(k_nudge, 0.0))))
            h_val = float(self.tuning.get("hunt", -0.0030))
            if h_val < -0.0045 or h_val > -0.0020:
                self.tuning["hunt"] = -0.0030
            logger.info(
                f"shadow: загружен ({len(self.episodes)} эпиз., ДНК {len(self.coin_dna)} монет, hunt={self.tuning['hunt']})"
            )

    def save(self):
        if len(self.coin_dna) > 400:
            sorted_dna = sorted(self.coin_dna.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:350]
            self.coin_dna = dict(sorted_dna)

        payload = {
            "episodes": self.episodes,
            "agg": self.agg,
            "missed_bans": self.missed_bans,
            "cooldown": self.cooldown,
            "coin_dna": self.coin_dna,
            "tuning": self.tuning,
        }
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"shadow save error: {e}")
        if time.time() - self._last_upload > 3600:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def update_coin_dna(self, symbol: str, candles_15m: list, candles_1h: list, atr_pct: float, chg_48h: float, pos_in_48h: float):
        if not candles_1h or len(candles_1h) < 24:
            return

        recent_1h = candles_1h[-48:] if len(candles_1h) >= 48 else candles_1h
        swings = []
        chunk_size = 6
        for i in range(0, len(recent_1h) - 2, chunk_size):
            slc = recent_1h[i:i + chunk_size]
            lo = min(c["low"] for c in slc)
            hi = max(c["high"] for c in slc)
            if lo > 0:
                swings.append((hi - lo) / lo * 100.0)

        raw_wave_amp = (sum(swings) / len(swings)) if swings else max(2.2, atr_pct * 2.8)

        pullbacks = []
        if candles_15m and len(candles_15m) >= 30:
            for i in range(4, len(candles_15m) - 1, 4):
                w = candles_15m[i - 4:i + 1]
                hi_15 = max(c["high"] for c in w)
                lo_15 = min(c["low"] for c in w)
                if hi_15 > 0:
                    pullbacks.append((hi_15 - lo_15) / hi_15 * 100.0)
        avg_pullback = (sum(pullbacks) / len(pullbacks)) if pullbacks else max(0.5, atr_pct * 0.85)

        prev = self.coin_dna.get(symbol, {})
        hist_mfe = prev.get("hist_mfe", raw_wave_amp)
        prev_wave = prev.get("wave_amp_pct", raw_wave_amp)
        blended_wave = round(0.55 * raw_wave_amp + 0.25 * prev_wave + 0.20 * hist_mfe, 2)
        blended_atr = round(0.6 * atr_pct + 0.4 * prev.get("atr_pct", atr_pct), 3)

        if chg_48h >= 3.5 and pos_in_48h >= 0.45:
            med_trend = "bull"
        elif chg_48h <= -3.5 and pos_in_48h <= 0.45:
            med_trend = "bear"
        else:
            med_trend = "range"

        self.coin_dna[symbol] = {
            "wave_amp_pct": max(1.4, min(16.0, blended_wave)),
            "atr_pct": max(0.2, min(6.0, blended_atr)),
            "pullback_pct": round(max(0.28, min(3.2, avg_pullback)), 2),
            "chg_48h": round(chg_48h, 2),
            "pos_in_48h": round(pos_in_48h, 2),
            "med_trend": med_trend,
            "hist_mfe": round(hist_mfe, 2),
            "ts": int(time.time()),
        }

    def get_coin_dna(self, symbol: str) -> dict:
        dna = self.coin_dna.get(symbol) or {}
        wave_amp = float(dna.get("wave_amp_pct", 3.0))
        med_trend = dna.get("med_trend", "range")
        atr_pct = float(dna.get("atr_pct", 0.8))

        if wave_amp >= 4.0 or (wave_amp >= 3.2 and med_trend == "bull"):
            wave_class = "long_wave"
            tp1_ratio = 0.30
            wave_mult = 1.25
        elif wave_amp < 2.40 and med_trend != "bull":
            wave_class = "short_wave"
            tp1_ratio = 0.50
            wave_mult = 0.85
        else:
            wave_class = "mid_wave"
            tp1_ratio = 0.38
            wave_mult = 1.05

        return {
            "wave_amp_pct": wave_amp,
            "atr_pct": atr_pct,
            "pullback_pct": float(dna.get("pullback_pct", 0.75)),
            "med_trend": med_trend,
            "wave_class": wave_class,
            "tp1_ratio": tp1_ratio,
            "wave_mult": wave_mult,
        }

    def observe(self, all_analyzed, regime, thrs):
        """Записывает как рабочие сигналы, так и отсеянные (1H_BAN / tactical_hold) для контроля упущенных пампов."""
        from bot.exchange.paper_exchange import paper
        now = time.time()

        for c in all_analyzed:
            if not c:
                continue
            sym = c["symbol"]
            mode = c.get("entry_mode", "sniper")
            thr = thrs.get(mode, 6.0)
            obs_thr = thr - OBS_GAP
            eff_score = float(c.get("unbanned_score", c.get("score", 0.0)))

            if sym in self.episodes:
                ep = self.episodes[sym]
                if eff_score > ep["max_score"]:
                    ep["max_score"] = eff_score
                if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                    ep["traded"] = True
                continue

            if eff_score < obs_thr:
                continue
            if now < self.cooldown.get(sym, 0):
                continue
            if len(self.episodes) >= MAX_EPISODES:
                continue

            traded = (sym in paper.positions) or any(o["symbol"] == sym for o in paper.orders)

            self.episodes[sym] = {
                "ts": now, "price": c["last"], "last": c["last"],
                "max_score": eff_score,
                "regime": regime, "entry_mode": mode,
                "gate_1h": c.get("gate_1h", "1H_TREND_OK"),
                "reason_1h": c.get("reason_1h", ""),
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
            self.episodes[sym]["traded"] = True
            self.save()

    def tick(self, tickers):
        from bot.exchange.paper_exchange import paper
        now = time.time()
        for sym, ep in list(self.episodes.items()):
            t = tickers.get(sym)
            if not t:
                continue
            last = t["last"]
            ep["last"] = last
            ep["hi"] = max(ep["hi"], last)
            ep["lo"] = min(ep["lo"], last)
            if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                ep["traded"] = True

            if not ep.get("pumped"):
                ep["lo_before_pump"] = min(ep.get("lo_before_pump", last), last)
                if last >= ep["price"] * (1 + PUMP_PCT / 100):
                    ep["pumped"] = True

            age = now - ep["ts"]
            # Закрываем эпизод через 4 часа (реальный цикл внутридневной волны), чтобы видеть все 4 пампа GLMR/SKY/DATA!
            if age >= H4:
                ep["p4"] = last
                self._close(sym, ep, last)
        self.save()

    def _combined_agg(self) -> dict:
        merged = {}
        for k, v in self.agg.items():
            merged[k] = {
                "n": v.get("n", 0),
                "sum4": v.get("sum4", 0.0),
                "sum24": v.get("sum24", 0.0),
                "pumps4": v.get("pumps4", 0),
                "pumps24": v.get("pumps24", 0),
                "traded": v.get("traded", 0),
                "sum_mae": v.get("sum_mae", 0.0),
                "sum_mfe": v.get("sum_mfe", 0.0),
                "sum_pull": v.get("sum_pull", 0.0),
                "sum_pull_pump": v.get("sum_pull_pump", 0.0),
                "sum_mae_pump": v.get("sum_mae_pump", 0.0),
            }

        now = time.time()
        for sym, ep in self.episodes.items():
            age = now - ep.get("ts", now)
            dec = ep.get("price", 0.0)
            if age < H1 or dec <= 0:
                continue
            ref_last = ep.get("p4") or ep.get("last") or dec
            move_cur = (ref_last - dec) / dec * 100
            mae = (ep.get("lo", dec) - dec) / dec * 100
            mfe = (ep.get("hi", dec) - dec) / dec * 100
            pullback = (ep.get("lo_before_pump", ep.get("lo", dec)) - dec) / dec * 100
            did_pump = bool(ep.get("pumped") or mfe >= PUMP_PCT or move_cur >= PUMP_PCT)

            mode = ep.get("entry_mode", "sniper")
            key = f"{ep.get('regime', 'neutral')}|{mode}|{_band(ep.get('max_score', 6.0))}"
            a = merged.setdefault(key, {
                "n": 0, "sum4": 0.0, "sum24": 0.0, "pumps4": 0, "pumps24": 0,
                "traded": 0, "sum_mae": 0.0, "sum_mfe": 0.0, "sum_pull": 0.0,
                "sum_pull_pump": 0.0, "sum_mae_pump": 0.0,
            })
            a["n"] += 1
            a["sum4"] += move_cur
            a["sum24"] += move_cur
            a["sum_mae"] += mae
            a["sum_mfe"] += mfe
            a["sum_pull"] += mae
            if did_pump:
                a["pumps24"] += 1
                a["sum_pull_pump"] += pullback
                a["sum_mae_pump"] += mae
            if ep.get("success") or (ep.get("traded") and did_pump):
                a["traded"] += 1

        return merged

    def _close(self, sym, ep, last):
        dec = ep["price"]
        self.episodes.pop(sym, None)
        self.cooldown[sym] = time.time() + COOLDOWN
        if dec <= 0:
            return

        move4 = (last - dec) / dec * 100
        mae = (ep["lo"] - dec) / dec * 100
        mfe = (ep["hi"] - dec) / dec * 100
        pullback = (ep.get("lo_before_pump", ep["lo"]) - dec) / dec * 100

        if sym in self.coin_dna and mfe > 0.5:
            old_mfe = self.coin_dna[sym].get("hist_mfe", mfe)
            self.coin_dna[sym]["hist_mfe"] = round(0.7 * old_mfe + 0.3 * mfe, 2)

        did_pump = bool(ep.get("pumped") or mfe >= PUMP_PCT or move4 >= PUMP_PCT)
        if did_pump and not ep.get("traded") and ep.get("gate_1h") == "1H_BAN":
            r_tag = (ep.get("reason_1h") or "1H_BAN").split("(")[0].strip()
            mb = self.missed_bans.setdefault(r_tag, {"count": 0, "avg_mfe": 0.0})
            mb["avg_mfe"] = round((mb["avg_mfe"] * mb["count"] + mfe) / (mb["count"] + 1), 2)
            mb["count"] += 1

        mode = ep.get("entry_mode", "sniper")
        key = f"{ep['regime']}|{mode}|{_band(ep['max_score'])}"

        a = self.agg.setdefault(key, {
            "n": 0, "sum4": 0.0, "sum24": 0.0, "pumps4": 0, "pumps24": 0,
            "traded": 0, "sum_mae": v_mae if (v_mae := 0.0) else 0.0, "sum_mfe": 0.0, "sum_pull": 0.0,
            "sum_pull_pump": 0.0, "sum_mae_pump": 0.0,
            "keys_pump": {}, "keys_all": {},
        })
        a["n"] += 1
        a["sum4"] += move4
        a["sum24"] += move4
        a["sum_mae"] += mae
        a["sum_mfe"] += mfe
        a["sum_pull"] += mae

        if move4 >= PUMP_PCT:
            a["pumps4"] += 1

        if did_pump:
            a["pumps24"] += 1
            a["sum_pull_pump"] = a.get("sum_pull_pump", 0.0) + pullback
            a["sum_mae_pump"] = a.get("sum_mae_pump", 0.0) + mae

            for k in ep["keys"]:
                a["keys_pump"][k] = a["keys_pump"].get(k, 0) + 1
            sv = ep.get("signal_values", {})
            for k, v in sv.items():
                if isinstance(v, (int, float)):
                    a.setdefault("signal_values_pump", {}).setdefault(k, []).append(v)

        for k in ep["keys"]:
            a["keys_all"][k] = a["keys_all"].get(k, 0) + 1

        if ep.get("success") or (ep.get("traded") and did_pump):
            a["traded"] += 1

    def autotune(self):
        if not self.tuning["auto"]:
            return
        now = time.time()
        if now - self._last_tune < 1800:
            return
        self._last_tune = now

        active_agg = self._combined_agg()
        actionable_bands = {"5-6", "6-7", "7-8", "8+"}

        for mode in ["rocket", "sniper", "reversal"]:
            band_stats = {}
            for key, a in active_agg.items():
                parts = key.split("|")
                if len(parts) != 3:
                    continue
                _, m_name, b_name = parts
                if m_name != mode or b_name not in actionable_bands:
                    continue
                bs = band_stats.setdefault(b_name, {
                    "n": 0, "sum4": 0.0, "sum_mfe": 0.0, "sum_mae": 0.0, "pumps": 0, "traded": 0
                })
                bs["n"] += a["n"]
                bs["sum4"] += a.get("sum4", 0.0)
                bs["sum_mfe"] += a.get("sum_mfe", 0.0)
                bs["sum_mae"] += a.get("sum_mae", 0.0)
                bs["pumps"] += a.get("pumps24", 0)
                bs["traded"] += a.get("traded", 0)

            tot_n = sum(v["n"] for v in band_stats.values())
            tot_pumps = sum(v["pumps"] for v in band_stats.values())
            tot_traded = sum(v["traded"] for v in band_stats.values())
            missed_pumps = max(0, tot_pumps - tot_traded)

            nudge = 0.0
            if tot_n >= 4:
                avg_mfe = sum(v["sum_mfe"] for v in band_stats.values()) / tot_n
                avg_mae = abs(sum(v["sum_mae"] for v in band_stats.values()) / tot_n)

                # Если рынок даёт пампы (MFE >= 2.5%), но мы их пропускаем (missed_pumps >= 2) — СНИЖАЕМ порог!
                if missed_pumps >= 2 and avg_mfe >= 2.4:
                    nudge = -0.20
                elif avg_mfe < 1.3 and avg_mae > 2.2:
                    nudge = 0.15

            prev_nudge = float(self.tuning.get(f"nudge_{mode}", 0.0))
            smoothed_nudge = round(0.5 * prev_nudge + 0.5 * nudge, 2)
            # Никогда не задираем порог выше +0.20, чтобы не ослеплять сканер на дне!
            self.tuning[f"nudge_{mode}"] = max(-0.25, min(0.20, smoothed_nudge))

        total_pumps = 0
        sum_pull_pump = 0.0
        sum_mae_pump = 0.0

        for key, a in active_agg.items():
            parts = key.split("|")
            if len(parts) == 3 and parts[2] in actionable_bands:
                pumps = a.get("pumps24", 0)
                total_pumps += pumps
                sum_pull_pump += a.get("sum_pull_pump", 0.0)
                sum_mae_pump += a.get("sum_mae_pump", 0.0)

        if total_pumps >= 4:
            avg_pull = sum_pull_pump / total_pumps
            avg_mae = sum_mae_pump / total_pumps
            # Плотное взятие отката (-0.22% .. -0.36%), чтобы ордера (как на PENGU и INJ) не оставались неисполненными!
            self.tuning["hunt"] = round(max(-0.0036, min(-0.0022, avg_pull / 100 * 0.32)), 4)
            self.tuning["tp_mult"] = 1.0
            self.tuning["sl_mult"] = round(max(0.95, min(1.12, 1.0 + abs(avg_mae) / 100 * 0.12)), 2)

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
            if ka.get(k, 0) >= 12 and base_rate > 0:
                lift = (kp.get(k, 0) / ka.get(k, 0)) / base_rate
                if lift > 1.22:
                    learner.weights[k] = clamp_weight(k, learner.weights[k] + 0.02)
                elif lift < 0.78:
                    learner.weights[k] = clamp_weight(k, learner.weights[k] - 0.02)
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
        out = ["👁 <b>Теневой журнал волн (4ч) и ДНК монет</b>"]
        out.append(
            f"   автотюн: {'🟢 вкл' if self.tuning['auto'] else '🔴 выкл'} · "
            f"в наблюдении: {len(self.episodes)} · ДНК профилей: <b>{len(self.coin_dna)}</b>"
        )
        shown = False
        reg_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}

        active_agg = self._combined_agg()
        for key in sorted(active_agg):
            a = active_agg[key]
            if a["n"] < 2:
                continue

            parts = key.split("|")
            if len(parts) == 3:
                reg, mode, band = parts
            else:
                continue

            avg = a["sum4"] / a["n"]
            avg_mfe = a.get("sum_mfe", 0.0) / a["n"]
            em = reg_emoji.get(reg, "⚪")
            mode_icon = "🚀" if mode == "rocket" else "🧲" if mode == "reversal" else "🏹"
            pumps_cnt = max(a.get("pumps24", 0), a.get("traded", 0))

            out.append(
                f"   {em} {mode_icon} {band}: {a['n']} набл. · ср. {avg:+.1f}% (пик +{avg_mfe:.1f}%) · "
                f"🎯 {a.get('traded', 0)}/{pumps_cnt} пампов"
            )
            shown = True

        if not shown:
            out.append("   (накапливается статистика 4ч-волн)")

        if self.missed_bans:
            top_missed = sorted(self.missed_bans.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)[:3]
            m_str = ", ".join(f"{k}: {v['count']}x (пик +{v['avg_mfe']:.1f}%)" for k, v in top_missed if v.get("count", 0) > 0)
            if m_str:
                out.append(f"   🔭 Упущенные под 1H-баном: {m_str}")

        if getattr(learner, "coin_stats", None):
            trusted, penalized = [], []
            for sym, _ in learner.coin_stats.items():
                b = learner.coin_bias(sym)
                base_s = sym[:-4] if sym.endswith("USDT") else sym
                if b >= 0.20:
                    trusted.append((b, f"{base_s} ({b:+.2f})"))
                elif b <= -0.25:
                    penalized.append((b, f"{base_s} ({b:+.2f})"))
            trusted.sort(reverse=True)
            penalized.sort()
            if trusted:
                out.append(f"   🟢 Доверие (фавориты): {', '.join(x[1] for x in trusted[:5])}")
            if penalized:
                out.append(f"   🔴 Штраф доверия: {', '.join(x[1] for x in penalized[:5])}")

        t = self.tuning
        w = t.get("signal_windows", {})
        out.append(f"   ⚙️ тюнинг: 🚀 {t.get('nudge_rocket', 0):+.2f} | 🏹 {t.get('nudge_sniper', 0):+.2f} | 🧲 {t.get('nudge_reversal', 0):+.2f}")
        out.append(f"   📏 откат {t['hunt']*100:+.2f}% · SL ×{t['sl_mult']:.2f} · TP ×{t.get('tp_mult', 1.0):.2f}")
        out.append(f"   📏 RSI ≤{w.get('rsi_hi', 90)} · chg ≤{w.get('chg_hi', 30)}% · vol ≥{w.get('vol_lo', 1.3)}×")
        return out

    def stats_text(self):
        return "\n".join(self.learn_lines())

    def reset(self):
        self.episodes = {}
        self.agg = {}
        self.missed_bans = {}
        self.cooldown = {}
        self.tuning = {
            "auto": True,
            "nudge_rocket": 0.0, "nudge_sniper": 0.0, "nudge_reversal": 0.0,
            "hunt": -0.0030, "near": -0.0018, "capture": +0.002,
            "sl_mult": 1.0, "tp_mult": 1.0,
            "signal_windows": {"rsi_hi": 90, "chg_hi": 30, "vol_lo": 1.3},
        }
        self.save()
        logger.info("shadow: журнал автотюна полностью сброшен")


shadow = Shadow()
