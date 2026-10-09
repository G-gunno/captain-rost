import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import live_score, get_thresholds, _returns
from bot.strategy.indicators import atr, ema, rsi
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.sizing import entry_offset
from bot.utils.format import pair_html, usd, fmt_price, fmt_pct, corr_txt, funding_line, pnl_emoji
from bot.core.state import bot_state

TIER_SL_FLOOR = {
    "TOP20": 0.010,
    "MID":   0.015,
    "SMALL": 0.020,
    "MICRO": 0.025,
}

TIER_TP_FLOOR = {
    "TOP20": 0.012,
    "MID":   0.020,
    "SMALL": 0.030,
    "MICRO": 0.045,
}

class PositionManagerWorker:
    """Медленный I/O воркер: проверяет новости, инвалидацию скора, двигает ордера-снайперы за ценой."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.regime_queue = self.bus.subscribe("REGIME_UPDATED", maxsize=2)
        self.current_regime = "neutral"
        self.FEE_PCT = 0.10
        self.MIN_TP_PCT = 0.60
        self.MIN_SL_PCT = 0.35

    async def run(self):
        logger.info("🛡 Position Manager запущен: охрана позиций и реквоты ордеров...")
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
        if not tickers: return

        btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
        btc_ret = _returns([c["close"] for c in btc_candles])

        metrics_24h = paper.get_metrics(tickers, hours=24)
        
        # Уведомление о смене режима риска (ИИ)
        old_mode = getattr(learner, "current_risk_mode", "NORMAL")
        learner.update_threshold(metrics_24h["profit_factor"], metrics_24h["max_drawdown_pct"], metrics_24h["total_trades"])
        new_mode = getattr(learner, "current_risk_mode", "NORMAL")
        
        if old_mode != new_mode and metrics_24h["total_trades"] > 0:
            mode_icons = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}
            self._notify(f"🎚 <b>Режим риска (ИИ) изменен:</b> {mode_icons.get(new_mode, '⚪')} {new_mode}\n<i>(строгость {learner.threshold_adj:+.2f})</i>")

        news_items = await fetch_news_cache()
        regime = self.current_regime
        thrs = get_thresholds(regime)
        current_time = int(time.time())

        # 1. Проверка ОТКРЫТЫХ ПОЗИЦИЙ
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t: continue
            
            score_pos, candles = await live_score(sym, t, regime, btc_ret, news_items, deriv_t=deriv_tickers.get(sym), is_open_pos=True)
            if score_pos is None: continue
            
            closes = [c["close"] for c in candles]
            a = atr(candles)
            if a <= 0: continue
            
            last = t["last"]
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
            
            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            is_toxic = neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)

            if is_toxic:
                bot_state.set_cooldown(sym, 7200)
                if pnl_pct >= 1.0 or pnl_pct <= 0:
                    reason = "✂📰 Новости"
                    ex = paper._sell(sym, last, reason, regime_now=regime)
                    paper.log_event(sym, "sell", last, f"Новости {neg}/{mentions}")
                    self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason} {neg}/{mentions}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                    continue

            # === АЛМАЗНЫЕ РУКИ И ИММУНИТЕТ 15 МИНУТ ===
            entry_mode = pos.get("entry_mode", "sniper")
            is_sniper = entry_mode == "sniper" or "accumulation" in pos.get("reason_keys", [])
            trend_broken = (last < e50 and e21 < (e50 * 0.998))
            thr = thrs.get(entry_mode, 6.0)
            pos_age = current_time - pos.get("entry_time", current_time)

            if pos_age < 900 and not is_toxic:
                signal_weak = False  # Иммунитет первых 15 минут от рыночного шума!
            elif entry_mode == "reversal":
                signal_weak = False  # Ловца дна защищает жесткий SL/TP, не режем досрочно
            elif is_sniper:
                signal_weak = trend_broken
            else:
                signal_weak = score_pos <= (thr - 1.8)
            # ==========================================

            pos_corr = pos.get("corr", 0.5)
            regime_danger = (pos.get("regime_entry") == "bull" and pos_corr >= 0.45 and (regime == "bear" or (regime == "neutral" and score_pos < thr)))

            if signal_weak or regime_danger:
                if regime_danger and not signal_weak:
                    reason = "🔐📉"
                    bot_state.set_cooldown(sym, 420)
                elif pnl_pct > 0:
                    reason = "🪫"
                    bot_state.set_cooldown(sym, 180)
                else:
                    reason = "✂️📉"
                    bot_state.set_cooldown(sym, 420)

                ex = paper._sell(sym, last, reason, regime_now=regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, last, reason)
                if ex["pnl"] > 0: shadow.mark_success(sym)
                self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                continue

            if pos.get("tp1_done") and last > e21 > e50 and score_pos >= thr and last >= pos["tp"] - 0.3 * a:
                new_tp = max(pos["tp"], last + 1.5 * a)
                if new_tp > pos["tp"]:
                    pos["tp"] = round(new_tp, 10)
                    pos["sl"] = max(pos["sl"], pos["avg"] + 0.5 * a)
                    paper.save()
                    self._notify(f"🎯 <b>TP поднят</b> (раннер) · {pair_html(sym, pos)}\n🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])}")

        # 2. Проверка ОРДЕРОВ (реквоты и отмены)
        for order in list(paper.orders):
            sym = order["symbol"]
            t = tickers.get(sym)
            if not t: continue
            
            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            if neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Токсичные новости")
                bot_state.set_cooldown(sym, 7200)
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🤬📰 (⏸️ 2ч)")
                continue

            score_now, candles = await live_score(sym, t, regime, btc_ret, news_items)
            if score_now is None: continue
            
            closes = [c["close"] for c in candles]
            e21 = ema(closes, 21)[-1]
            e50 = ema(closes, 50)[-1]
            
            # === ИММУНИТЕТ ДЛЯ ОРДЕРОВ В СТАКАНЕ ===
            order_age = current_time - order["created"]
            entry_mode = order.get("entry_mode", "sniper")
            tier = order.get("tier") or "SMALL"
            is_sniper = entry_mode == "sniper"
            thr = thrs.get(entry_mode, 6.0)
            
            trend_broken_order = (t["last"] < e50 and e21 < (e50 * 0.998))
            flash_crash = t["last"] < (e50 * 0.985)
            
            order_amnesty = (is_sniper and order_age < 3600 and not trend_broken_order and not flash_crash) 
            
            if not order_amnesty:
                if score_now <= thr - 1.5:
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], "Сигнал умер (Слом тренда/Дамп)")
                    bot_state.set_cooldown(sym, 420)
                    self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️ (⏸️ 7м)")
                    continue
                    
                if score_now < thr - 0.5:
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], "Сигнал ослаб")
                    bot_state.set_cooldown(sym, 180)
                    self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🪫 (⏸️ 3м)")
                    continue
            # =======================================

            if (current_time - order["created"]) > 7200:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Тайм-аут 2ч")
                bot_state.set_cooldown(sym, 420)
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ⏳ (⏸️ 7м)")
                continue

            a = atr(candles)
            if a <= 0: continue
            
            atr_pct = a / t["last"] * 100

            # 1. ДЕТЕКТОР ПАДАЮЩЕГО НОЖА
            last_c = candles[-1] if candles else None
            if last_c and a > 0:
                c_body = last_c["open"] - last_c["close"]
                vols_past = [c["volume"] for c in candles[-21:-1]]
                avg_vol_past = (sum(vols_past) / len(vols_past)) if vols_past else 1.0
                vol_ratio_now = last_c["volume"] / avg_vol_past if avg_vol_past else 1.0
                if last_c["close"] < last_c["open"] and c_body >= 1.2 * a and vol_ratio_now >= 1.2:
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], "Падающий нож (дамп-свеча)")
                    bot_state.set_cooldown(sym, 300)
                    self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🔪 Водопад (защита от ножа)")
                    continue

            # Расчет отступа с учетом ликвидности актива
            off = entry_offset(score_now, thr, regime, atr_pct, entry_mode, tier=tier)
            ideal_price = t["last"] * (1 + off)
            old_price = order["price"]
            dev_pct = abs(ideal_price - old_price) / old_price * 100

            # 2. ЗАПРЕТ ПОГОНИ ЗА РАКЕТАМИ НА ХАЯХ
            if entry_mode == "rocket" and ideal_price > old_price:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Ракета улетела (не берем на хаях)")
                bot_state.set_cooldown(sym, 300)
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🚀💨 Улетела (не берем на хаях)")
                continue
            
            action_type = None

            if entry_mode in ("rocket", "reversal"):
                if ideal_price < old_price and dev_pct >= 0.2:
                    order["price"] = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"
            else:
                ideal_price = min(ideal_price, t.get("bid1", t["last"]))
                
                if t["last"] > old_price + 4.0 * a: 
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], "Улетела без нас")
                    bot_state.set_cooldown(sym, 420)
                    self._notify(f"⚠ Снят · {pair_html(sym, order)} · 🚀 (⏸️ 7м)")
                    continue
                
                if ideal_price < old_price and dev_pct >= 0.2:
                    order["price"] = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"

            # Реквот TP/SL по тировым планкам
            sl_floor = TIER_SL_FLOOR.get(tier, 0.020)
            tp_floor = TIER_TP_FLOOR.get(tier, 0.030)
            
            sl_dist = max(1.2 * a, order["price"] * sl_floor)
            tp_dist = max(2.0 * a, sl_dist * 1.5, order["price"] * tp_floor)
            order["tp"] = order["price"] + tp_dist
            order["sl"] = order["price"] - sl_dist
            
            if action_type:
                order["created"] = current_time  
                paper.save()
                paper.log_event(sym, "order_moved", ideal_price, f"Сдвиг: {action_type}")
                h_cnt = order.get('hunt_count', 0)
                msg_desc = f"попытка {h_cnt}" if action_type == "hunt" else f"сдвиг на {dev_pct:.2f}%"
                self._notify(f"📐 <b>Сдвиг {price_icon}</b> · {pair_html(sym, order)}\n📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · {msg_desc}")
            else:
                paper.save()
