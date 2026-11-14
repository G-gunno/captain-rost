import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import live_score, get_thresholds, _returns, is_post_pump_dump
from bot.strategy.indicators import atr, ema, find_sr_levels, get_all_sr_levels
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.strategy.risk import (
    calc_initial_sl_tp, calc_runner_target, update_position_sl,
    get_trade_policy, _round_px
)
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.sizing import entry_offset
from bot.utils.format import (
    pair_html, format_coin_compact, sl_emoji, grade_badge,
    usd, fmt_price, fmt_pct, pnl_emoji
)
from bot.core.state import bot_state


class PositionManagerWorker:
    """Управление позициями: частые ступеньки SL/TP на прокол, анти-зомби фильтр и ведение ордеров."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.regime_queue = self.bus.subscribe("REGIME_UPDATED", maxsize=2)
        self.current_regime = "neutral"

    async def run(self):
        logger.info("🛡 Position Manager запущен: Ступенчатый Коридор SL/TP (на прокол) и ведение ордеров...")
        asyncio.create_task(self._regime_updater())

        while True:
            if bot_state.paused or not bot_state.trading_enabled:
                await asyncio.sleep(10)
                continue

            try:
                await self._maintenance_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"PositionManager error: {e}")

            await asyncio.sleep(60)

    async def _regime_updater(self):
        while True:
            event = await self.regime_queue.get()
            self.current_regime = event.payload["regime"]
            self.regime_queue.task_done()

    def _notify(self, text: str, urgent: bool = False):
        self.bus.publish("NOTIFY", {"text": text, "urgent": urgent})

    async def _maintenance_cycle(self):
        tickers = await market_data.get_tickers()
        deriv_tickers = await market_data.get_derivatives_tickers()
        if not tickers:
            return

        btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
        btc_ret = _returns([c["close"] for c in btc_candles])

        m_24 = paper.get_metrics(tickers, hours=24)
        m_48 = paper.get_metrics(tickers, hours=48)
        pf_24 = m_24["profit_factor"] if m_24["profit_factor"] is not None else 1.0
        pf_48 = m_48["profit_factor"] if m_48["profit_factor"] is not None else pf_24
        if pf_24 == float("inf"):
            pf_24 = 2.2
        if pf_48 == float("inf"):
            pf_48 = 2.2
        blended_pf = (0.70 * pf_24 + 0.30 * pf_48) if m_48["total_trades"] > 0 else None
        blended_dd = max(m_24["max_drawdown_pct"], m_48["max_drawdown_pct"] * 0.85)

        old_mode = getattr(learner, "current_risk_mode", "NORMAL")
        learner.update_threshold(blended_pf, blended_dd, m_48["total_trades"])
        new_mode = getattr(learner, "current_risk_mode", "NORMAL")

        if old_mode != new_mode and m_48["total_trades"] > 0:
            mode_icons = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}
            self._notify(f"🎚 <b>Режим риска (ИИ) плавно изменен:</b> {mode_icons.get(new_mode, '⚪')} {new_mode}\n<i>(строгость {learner.threshold_adj:+.2f})</i>")

        news_items = await fetch_news_cache()
        regime = self.current_regime
        thrs = get_thresholds(regime)
        policy = get_trade_policy(new_mode)
        current_time = int(time.time())

        # =====================================================================
        # 1. ПРОВЕРКА ОТКРЫТЫХ ПОЗИЦИЙ И ЧАСТЫЕ СТУПЕНЬКИ SL / TP «НА ПРОКОЛ»
        # =====================================================================
        for sym in list(paper.positions.keys()):
            t = tickers.get(sym)
            if not t or sym not in paper.positions:
                continue

            pos_snapshot = paper.positions.get(sym, {})
            score_pos, candles = await live_score(
                sym, t, regime, btc_ret, news_items,
                deriv_t=deriv_tickers.get(sym),
                is_open_pos=True,
                saved_patterns=pos_snapshot.get("patterns"),
                saved_mode=pos_snapshot.get("entry_mode"),
                btc_candles=btc_candles,
                saved_gate_1h=pos_snapshot.get("gate_1h"),
            )
            pos = paper.positions.get(sym)
            if not pos or score_pos is None or not candles:
                continue
            pos["symbol"] = sym

            closes = [c["close"] for c in candles]
            a = atr(candles)
            if a <= 0:
                continue

            last = t["last"]
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            e21_series = ema(closes, 21)
            e21, e50 = e21_series[-1], ema(closes, 50)[-1]

            base = sym[:-4]
            name = await get_coin_name(base)
            pos = paper.positions.get(sym)
            if not pos:
                continue

            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            is_toxic = neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)

            if is_toxic:
                bot_state.set_cooldown(sym, 7200)
                if pnl_pct >= 0.8 or pnl_pct <= -0.3:
                    if sym not in paper.positions:
                        continue
                    reason = "✂📰 Новости"
                    ex = paper._sell(sym, last, reason, regime_now=regime)
                    paper.log_event(sym, "sell", last, f"Новости {neg}/{mentions}", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {reason} {neg}/{mentions}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                    continue

            entry_mode = pos.get("entry_mode", "sniper")
            e21_falling = len(e21_series) >= 4 and e21 < e21_series[-3] * 0.9995
            closed_2_below = (
                len(candles) >= 3 and
                candles[-2]["close"] < (e50 - 0.55 * a) and
                candles[-3]["close"] < (e50 - 0.55 * a)
            )
            trend_broken = (
                closed_2_below and
                last < (e50 - 0.55 * a) and
                (e21 < e50 * 0.997 or e21_falling)
            )
            pos_age = current_time - pos.get("entry_time", current_time)

            if pos_age >= 2400 and not is_toxic and pnl_pct >= 0.45 and entry_mode != "reversal" and trend_broken and sym in paper.positions:
                reason = "🪫 Слом тренда"
                ex = paper._sell(sym, last, reason, regime_now=regime)
                paper.log_event(sym, "sell_profit", last, reason, tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                bot_state.record_trade_exit(sym, ex["pnl_pct"], last, is_sl=False)
                shadow.mark_success(sym)
                self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {reason}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                continue

            is_stagnant = (not pos.get("tp1_done")) and pos_age >= 7200 and (last < e50 * 0.999 and e21 < e50 * 0.999)
            if is_stagnant and pnl_pct >= 0.25 and sym in paper.positions:
                reason = "⏱ Выход по времени (без импульса)"
                ex = paper._sell(sym, last, reason, regime_now=regime)
                paper.log_event(sym, "sell_profit", last, reason, tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                bot_state.record_trade_exit(sym, ex["pnl_pct"], last, is_sl=False)
                self._notify(f"⏱ <b>Разгрузка слота</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {reason}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                continue

            pos = paper.positions.get(sym)
            if not pos:
                continue

            max_p = max(pos.get("max_price", last), last)
            pos["max_price"] = max_p
            sl_updated, new_sl, new_state, tag_emoji = update_position_sl(pos, max_p, last_price=last, candles_15m=candles)
            if sl_updated and sym in paper.positions:
                old_sl_str = pos.get("_last_notified_sl_str") or fmt_price(pos.get("sl", 0.0))
                new_sl_str = fmt_price(new_sl)
                old_state = pos.get("_last_notified_sl_state") or pos.get("sl_state", "SHIELD")
                pos["sl"] = max(pos.get("sl", 0.0), new_sl)
                pos["sl_state"] = new_state
                pos["max_sl"] = pos["sl"]
                paper.save()
                paper.log_event(sym, "sl_moved", pos["sl"], tag_emoji, tp=pos.get("tp"), sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                last_notify_ts = float(pos.get("_last_sl_notify_ts", 0.0))
                if new_sl_str != old_sl_str and ((current_time - last_notify_ts >= 180) or new_state != old_state):
                    pos["_last_notified_sl_str"] = new_sl_str
                    pos["_last_notified_sl_state"] = new_state
                    pos["_last_sl_notify_ts"] = current_time
                    coin_compact = format_coin_compact(sym, pos)
                    self._notify(f"🛡 SL поднят по {coin_compact} до {new_sl_str} ({tag_emoji})")

            if pos.get("tp1_done"):
                candles_1h = await market_data.get_kline(sym, "60", 250)
                pos = paper.positions.get(sym)
                if not pos:
                    continue
                next_target, can_step, phase_reason = calc_runner_target(sym, last, candles, candles_1h, pos=pos)
                old_tp = float(pos.get("tp", 0.0))
                if can_step and old_tp > 0 and abs(next_target - old_tp) / old_tp >= 0.0018:
                    pos["tp"] = next_target
                    paper.save()
                    step_dir = "⬇️ под прокол" if next_target < old_tp else "⬆️ по волне"
                    paper.log_event(sym, "tp_runner_raised", pos["tp"], f"TP🏃🏻‍➡️ ступень {step_dir}", mode="TP🏃🏻‍➡️", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    if abs(next_target - old_tp) / old_tp * 100 >= 0.85:
                        sl_tag = sl_emoji(pos.get("sl_state", "TRAILING_RUNNER"))
                        self._notify(f"TP🏃🏻‍➡️ <b>Ступень тейка ({step_dir})</b> · {pair_html(sym, pos)}\n🎯 {fmt_price(pos['tp'])} · {sl_tag} {fmt_price(pos['sl'])}")
            else:
                peak_pct = (max_p - pos["avg"]) / pos["avg"] * 100 if pos["avg"] > 0 else 0.0
                pulled_back = (max_p - last) >= 0.25 * a
                if pos_age >= 1800 and peak_pct >= 0.70 and pulled_back and last >= pos["avg"] * 0.998:
                    step_down_tp1 = _round_px(max(pos["avg"] * 1.0075, last + 0.50 * a, e21 + 0.55 * a))
                    if step_down_tp1 < pos["tp"] * 0.9980:
                        pos["tp"] = step_down_tp1
                        paper.save()
                        paper.log_event(sym, "tp_runner_raised", pos["tp"], "🎯 TP1 снижен под прокол", mode="TP1", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

        # =====================================================================
        # 2. ПРОВЕРКА ОЖИДАЮЩИХ ОРДЕРОВ
        # =====================================================================
        for order in list(paper.orders):
            if order not in paper.orders:
                continue
            sym = order["symbol"]
            t = tickers.get(sym)
            if not t:
                continue

            base = sym[:-4]
            name = await get_coin_name(base)
            if order not in paper.orders:
                continue
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            if neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Токсичные новости", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 7200)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> токсичные новости ({neg}/{mentions})")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ (новости)")
                continue

            entry_mode = order.get("entry_mode", "sniper")
            patterns = order.get("patterns", [])
            ord_gate_1h = order.get("gate_1h", "")

            score_now, candles = await live_score(
                sym, t, regime, btc_ret, news_items,
                deriv_t=deriv_tickers.get(sym),
                is_open_pos=True,
                saved_patterns=patterns,
                saved_mode=entry_mode,
                btc_candles=btc_candles,
                saved_gate_1h=ord_gate_1h,
            )
            if order not in paper.orders or score_now is None or not candles:
                continue

            closes = [c["close"] for c in candles]
            e21_series = ema(closes, 21)
            e21, e50 = e21_series[-1], ema(closes, 50)[-1]
            a = atr(candles)
            if a <= 0:
                continue

            order_age = current_time - order["created"]
            tier = order.get("tier") or "SMALL"
            is_accum = "vcp" in patterns
            thr = thrs.get(entry_mode, 6.0)
            init_score = order.get("initial_score", order.get("score", thr))

            is_post_pump, pp_reason = is_post_pump_dump(candles)
            if is_post_pump:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Пост-памп слив ({pp_reason})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 1800)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ☠️⚰️ {pp_reason}")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ (пост-памп слив)")
                continue

            curr_c = candles[-1]
            closed_c = candles[-2]
            ord_sup = order.get("nearest_sup")
            ord_sl = order.get("sl", 0.0)
            sup_broken = bool(ord_sup and closed_c["close"] < ord_sup * 0.992 and t["last"] < ord_sup * 0.992)

            near_sl_danger = bool(ord_sl > 0 and t["last"] <= (ord_sl + 0.30 * a))
            is_dip_order = (ord_gate_1h == "1H_DIP_BOUNCE")

            # Для 1H_DIP_BOUNCE (вход на дне часовой просадки, где цена заведомо под 15m EMA50)
            # НЕ отменяем ордер из-за last < e50, отменяем только при пробое опоры или угрозе стопа!
            sniper_broken = (
                entry_mode != "reversal" and
                not is_dip_order and
                t["last"] < (e50 - 0.65 * a) and
                e21 < e50 * 0.996 and
                (near_sl_danger or score_now < thr - 1.0)
            )
            dip_or_rev_broken = (
                (entry_mode == "reversal" or is_dip_order) and
                (sup_broken or near_sl_danger or (curr_c["open"] - curr_c["close"]) > 0.95 * a)
            )

            if sniper_broken or dip_or_rev_broken:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Слом структуры ({init_score:.1f}-{score_now:.1f})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 900)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ☠️⚰️ слом структуры/нож ({init_score:.1f} -> {score_now:.1f})")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ ({init_score:.1f}-{score_now:.1f})")
                continue

            base_intact = (
                (not sup_broken and not near_sl_danger)
                if is_dip_order
                else (entry_mode != "reversal" and t["last"] >= e50 * 0.996 and e21 >= e50 * 0.997)
            )
            if order_age >= 1500 and not base_intact and score_now < (thr - 1.8):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Ослаб ({score_now:.1f})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 600)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> 🪫📉 ослаб сигнал ({score_now:.1f})")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🪫📉 ({score_now:.1f})")
                continue

            if t["last"] > order["price"] + 2.6 * a:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Улетела без нас", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 300)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> 🚀👋🏻 улетела без нас")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🚀👋🏻")
                continue

            max_order_age = 3600 if (entry_mode == "reversal" or is_dip_order) else 2700
            if order_age > max_order_age:
                if score_now >= (thr + 0.5) and order.get("extensions", 0) < 1 and base_intact:
                    order["extensions"] = order.get("extensions", 0) + 1
                    order["created"] = current_time - (max_order_age - 1800)
                    paper.save()
                    logger.info(f"PositionManager: ⏳ Ордер {sym} продлён на 30 мин (сильный сигнал {score_now:.1f}/{thr:g})")
                else:
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], f"Тайм-аут ({init_score:.1f}-{score_now:.1f})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                    bot_state.set_cooldown(sym, 300)
                    logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ⌛ тайм-аут ({init_score:.1f}-{score_now:.1f})")
                    self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ⌛ Тайм-аут ({init_score:.1f}-{score_now:.1f})")
                    continue

            last_move_ts = order.get("last_move_ts", order["created"])
            if (current_time - last_move_ts) < 480:
                continue

            atr_pct = a / t["last"] * 100
            off = entry_offset(score_now, thr, regime, atr_pct, entry_mode, tier=tier, is_accumulation=is_accum, symbol=sym)
            ideal_price = min(t["last"] * (1 + off), t.get("bid1", t["last"]) * 0.9992)
            old_price = order["price"]

            candles_1h = await market_data.get_kline(sym, "60", 250)
            if order not in paper.orders:
                continue
            nearest_res, res_touches, nearest_sup, sup_touches = find_sr_levels(candles, candles_1h)
            live_sup = nearest_sup if (nearest_sup and sup_touches >= 2) else order.get("nearest_sup")
            live_sup_touches = sup_touches if (nearest_sup and sup_touches >= 2) else order.get("sup_touches", 2 if order.get("nearest_sup") else 0)

            target_price = old_price
            action_type = None
            price_icon = ""
            dev_pct = abs(ideal_price - old_price) / old_price * 100
            in_freeze_zone = abs(t["last"] - old_price) <= (0.35 * a) or (abs(t["last"] - old_price) / old_price * 100) <= 0.28

            if ideal_price < old_price and dev_pct >= 0.40 and not in_freeze_zone:
                if not (live_sup and ideal_price < live_sup * 1.0015):
                    target_price = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"
            elif (
                regime == "bull" and
                ideal_price > old_price and
                order_age >= 720 and
                order.get("requotes_up", 0) < 1 and
                score_now >= thr + 1.2 and
                e21 > e50 and
                0.28 <= dev_pct <= 0.65
            ):
                target_price = ideal_price
                action_type = "pull_up"
                price_icon = "⬆️"

            if not action_type:
                continue

            all_res, all_sup = get_all_sr_levels(candles, candles_1h)
            res_above_target = [(r[0], r[1]) for r in all_res if r[0] > target_price * 1.0005]
            if res_above_target:
                res_above_target.sort(key=lambda x: x[0])
                entry_res, entry_res_touches = res_above_target[0]
            else:
                entry_res, entry_res_touches = None, 0

            sup_below_target = [(s[0], s[1]) for s in all_sup if s[0] <= target_price * 1.0010]
            if sup_below_target:
                sup_below_target.sort(key=lambda x: x[0], reverse=True)
                entry_sup, entry_sup_touches = sup_below_target[0]
            else:
                entry_sup, entry_sup_touches = live_sup, live_sup_touches

            min_rr = policy["min_rr_sniper"] if entry_mode == "sniper" else policy["min_rr_alt"]
            recent_low_6 = min(c["low"] for c in candles[-6:]) if len(candles) >= 6 else None

            closes_1h = [c["close"] for c in candles_1h] if candles_1h and len(candles_1h) >= 50 else []
            e21_1h_val = ema(closes_1h, 21)[-1] if closes_1h else order.get("e21_1h")
            e50_1h_val = ema(closes_1h, 50)[-1] if closes_1h else order.get("e50_1h")

            sl, tp, is_valid, _ = calc_initial_sl_tp(
                symbol=sym, entry=target_price, tier=tier, a=a,
                sup=entry_sup, sup_touches=entry_sup_touches,
                res=entry_res, res_touches=entry_res_touches,
                is_mom=(entry_mode == "rocket"), entry_mode=entry_mode,
                min_rr=min_rr,
                recent_low_6=recent_low_6,
                ema50=e50,
                e21_1h=e21_1h_val,
                e50_1h=e50_1h_val,
            )

            if is_valid and action_type and order in paper.orders:
                old_notional = order["qty"] * order["price"]
                order["price"] = target_price
                order["qty"] = old_notional / target_price if target_price > 0 else order["qty"]
                order["sl"] = sl
                order["tp"] = tp
                order["nearest_sup"] = entry_sup
                order["sup_touches"] = entry_sup_touches
                order["nearest_res"] = entry_res
                order["res_touches"] = entry_res_touches
                order["last_move_ts"] = current_time
                order["requotes"] = order.get("requotes", 0) + 1
                if action_type == "pull_up":
                    order["requotes_up"] = order.get("requotes_up", 0) + 1
                paper.save()

                msg_desc = f"{dev_pct:.2f}%"
                logger.info(f"PositionManager: 📐 Сдвиг ордера {sym} ({action_type}) -> new_entry=${fmt_price(target_price)}, SL=${fmt_price(sl)}")
                paper.log_event(sym, "order_moved", order["price"], f"Сдвиг: {action_type}", tp=order["tp"], sl=order["sl"], sup=entry_sup, res=entry_res)
                self._notify(f"📐 <b>Сдвиг {price_icon}</b> · {pair_html(sym, order)}\n📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · 🛡📉 {fmt_price(order['sl'])} · {msg_desc}")
