import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper
from bot.strategy.sizing import buy_size, portfolio_limits, tier_limits, entry_offset
from bot.strategy.scanner import get_thresholds
from bot.strategy.learner import learner
from bot.strategy.risk import calc_initial_sl_tp
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.utils.format import pair_html, corr_txt, usd, fmt_price, fmt_pct
from bot.core.state import bot_state

class OrderManagerWorker:
    """Управляет портфелем: предварительная фильтрация новостей, единый риск и сайзинг."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.signal_queue = self.bus.subscribe("SIGNALS_READY", maxsize=2)

    async def run(self):
        logger.info("💼 Order Manager запущен: предварительная фильтрация новостей и контроль структуры...")
        while True:
            try:
                event = await self.signal_queue.get()
                await self._process_signals(event.payload)
                self.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"OrderManager error: {e}")

    def _notify(self, text: str, urgent: bool = False):
        self.bus.publish("NOTIFY", {"text": text, "urgent": urgent})

    async def _process_signals(self, payload: dict):
        candidates = payload["candidates"]
        tickers = payload["tickers"]
        regime = payload["regime"]

        equity = paper.equity(tickers)
        thrs = get_thresholds(regime)
        sec_lim, other_lim = portfolio_limits(equity)
        sat_limit = learner.satellite_limit()
        news_items = await fetch_news_cache()

        logger.info(f"💼 OrderManager: получено кандидатов: {len(candidates)} {[c['symbol'] for c in candidates]}")

        for cand in candidates:
            sym = cand["symbol"]
            base = sym[:-4]

            # ⚡ 1. ПРЕДВАРИТЕЛЬНЫЙ НОВОСТНОЙ ФИЛЬТР (ДО ЛЮБЫХ ДЕЙСТВИЙ)
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            if neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                logger.info(f"OrderManager: {sym} отклонен ДО выставления -> токсичные новости ({neg}/{mentions})")
                bot_state.set_cooldown(sym, 7200)
                continue

            is_accum = "accumulation" in cand.get("reason_keys", [])
            if bot_state.is_on_cooldown(sym) and not is_accum:
                logger.debug(f"OrderManager: {sym} пропущен (кулдаун)")
                continue

            if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                logger.debug(f"OrderManager: {sym} уже в работе")
                continue

            kind = cand.get("kind", "core")
            sector = cand.get("sector", "Other")
            tier = cand.get("tier") or "SMALL"
            entry_mode = cand.get("entry_mode", "rocket" if cand.get("is_momentum") else "sniper")
            is_mom = cand.get("is_momentum", False)

            thr = thrs.get(entry_mode, 6.0)
            off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], entry_mode, tier=tier, is_accumulation=is_accum)

            t_data = tickers.get(sym, {})
            bid1 = t_data.get("bid1", cand["last"])
            ideal_entry = cand["last"] * (1 + off)
            entry = ideal_entry if is_mom else min(ideal_entry, bid1)

            # Правило пола поддержки
            sup = cand.get("nearest_sup")
            sup_touches = cand.get("sup_touches", 0)
            if sup and sup_touches >= 2 and not is_mom:
                retest_floor = sup * 1.0015
                if cand["last"] >= retest_floor:
                    entry = max(entry, retest_floor)
                    entry = min(entry, bid1)

            a = cand["atr"]
            if a <= 0: continue

            # ⚡ 2. ЕДИНЫЙ РАСЧЕТ SL И TP ЧЕРЕЗ МОДУЛЬ RISK
            res = cand.get("nearest_res")
            res_touches = cand.get("res_touches", 0)

            sl, tp, is_valid, rej_reason = calc_initial_sl_tp(
                symbol=sym, entry=entry, tier=tier, a=a,
                sup=sup, sup_touches=sup_touches,
                res=res, res_touches=res_touches,
                is_mom=is_mom, entry_mode=entry_mode
            )

            if not is_valid:
                logger.info(f"OrderManager: {sym} отклонён -> {rej_reason}")
                continue

            size = buy_size(equity, cand["score"], thr, cand["liquidity"], paper.usdt,
                            kind=kind, entry_mode=entry_mode)

            if size < 10:
                logger.debug(f"OrderManager: {sym} пропущен (размер {size} < 10 USDT)")
                continue

            if kind == "satellite":
                sat_exp = sum(p["qty"] * tickers.get(s, {}).get("last", 0) for s, p in paper.positions.items() if p.get("kind") == "satellite")
                sat_exp += sum(o["qty"] * o["price"] for o in paper.orders if o.get("kind") == "satellite")
                if sat_exp >= equity * sat_limit / 100:
                    logger.debug(f"OrderManager: {sym} пропущен (лимит сателлитов {sat_limit}%)")
                    continue

            # Проверка лимитов секторов и ротация
            planned_sells, planned_cancels = [], []
            lim = other_lim if sector == "Other" else sec_lim
            sector_count = sum(1 for p in paper.positions.values() if (p.get("sector") or "Other") == sector)

            if sector_count >= lim:
                if is_mom: 
                    sector_positions = [(s, p) for s, p in paper.positions.items() if (p.get("sector") or "Other") == sector]
                    if sector_positions:
                        w_sym, w_pos = min(sector_positions, key=lambda kv: kv[1].get("score", 0))
                        t_w = tickers.get(w_sym)
                        if t_w:
                            weak_pnl = (t_w["last"] - w_pos["avg"]) / w_pos["avg"] * 100
                            can_rotate = (cand["score"] >= w_pos.get("score", 0) + 1.0 and not w_pos.get("tp1_done") and weak_pnl >= -2.0)
                            if can_rotate:
                                planned_sells.append((w_sym, w_pos, t_w["last"], weak_pnl, "РОТАЦИЯ СЕКТОРА 🔄"))
                            else:
                                continue
                else:
                    logger.debug(f"OrderManager: {sym} пропущен (лимит сектора {sector})")
                    continue

            proj_pending = sum(o["qty"] * o["price"] for o in paper.orders)
            proj_usdt = paper.usdt + sum(w_pos["qty"] * last for _, w_pos, last, _, _ in planned_sells)

            if proj_pending + size > proj_usdt:
                available_orders = [o for o in paper.orders if o not in planned_cancels]
                if available_orders:
                    w_o = min(available_orders, key=lambda o: o.get("score", 0))
                    if cand["score"] >= w_o.get("score", 0) + 1.0:
                        planned_cancels.append(w_o)
                        proj_pending -= w_o["qty"] * w_o["price"]

                if is_mom and (proj_pending + size > proj_usdt):
                    planned_syms = [s[0] for s in planned_sells]
                    available_pos = [(s, p) for s, p in paper.positions.items() if s not in planned_syms]
                    if available_pos:
                        w_sym, w_p = min(available_pos, key=lambda kv: kv[1].get("score", 0))
                        t_w = tickers.get(w_sym)
                        if t_w:
                            pnl = (t_w["last"] - w_p["avg"]) / w_p["avg"] * 100
                            if cand["score"] >= w_p.get("score", 0) + 1.5 and not w_p.get("tp1_done") and pnl >= -2.0:
                                planned_sells.append((w_sym, w_p, t_w["last"], pnl, "ОБЩАЯ РОТАЦИЯ 🔄"))
                                proj_usdt += w_p["qty"] * t_w["last"]

            if proj_pending + size > proj_usdt:
                logger.debug(f"OrderManager: {sym} пропущен (недостаточно свободных USDT)")
                continue 

            for w_o in planned_cancels:
                paper.cancel_order(w_o["id"])
                self._notify(f"🔄 <b>Ротация ордера</b> · <b>{w_o['symbol'][:-4]}</b> снят\nМесто для <b>{sym[:-4]}</b>")

            for w_sym, w_pos, last, pnl, reason in planned_sells:
                ex = paper._sell(w_sym, last, reason, regime_now=regime)
                self._notify(f"🔄 <b>{reason}</b> · <b>{w_sym[:-4]}</b> → <b>{sym[:-4]}</b>\n💵 Освобождено {usd(ex['pnl'])}")

            qty = size / entry
            order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl, score=cand["score"], reason_keys=cand.get("reason_keys", []))
            
            order.update({
                "kind": kind, "sector": sector, "tier": tier, "corr": cand.get("corr"),
                "regime": regime, "is_momentum": is_mom, "entry_mode": entry_mode,
                "nearest_res": res,
                "nearest_sup": sup,
                "sl_state": "SHIELD"
            })
            paper.save()

            reasons_html = "<br>".join([f"• {r}" for r in cand.get("reasons", [])])
            paper.log_event(sym, "order_placed", entry, text=reasons_html, mode=entry_mode, tp=tp, sl=sl, sup=sup, res=res)

            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            new_tag = "· 🆕 " if cand.get("is_new") else ""

            logger.info(f"OrderManager: 🎯 {sym} ВЫСТАВЛЕН ОРДЕР {entry_mode}: entry=${fmt_price(entry)}, TP=${fmt_price(tp)}, SL=${fmt_price(sl)}")

            self._notify(
                f"📋 <b>Ордер</b> {new_tag}· {pair_html(sym, order)}\n"
                f"💵 {usd(size)} · 📥 {fmt_price(entry)} ({off * 100:+.2f}%){corr_txt(cand)}\n"
                f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
                f"⭐ {cand['score']:.1f} · 🧠 {'; '.join(cand['reasons'][:3])}"
            )
