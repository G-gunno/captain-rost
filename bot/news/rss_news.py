import time

import httpx
import xml.etree.ElementTree as ET
from loguru import logger

FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
]

NEG_WORDS = {"hack", "exploit", "scam", "rug", "fraud", "lawsuit", "sues", "sec",
             "ban", "delist", "crash", "plunge", "sanction", "breach", "steal",
             "stolen", "vulnerability", "attack", "investigation", "fine", "collapse"}
POS_WORDS = {"partnership", "integration", "listing", "listed", "approval",
             "approved", "adopt", "adoption", "upgrade", "launch", "ath",
             "record", "surge", "rally", "breakout", "institutional", "etf", "inflow"}

_cache = {"items": None, "ts": 0}


def _parse(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            items.append({
                "title": item.findtext("title") or "",
                "desc": (item.findtext("description") or "")[:300],
            })
    except Exception as e:
        logger.error(f"RSS parse error: {e}")
    return items


async def fetch_news_cache():
    """Свежие заголовки из RSS (кэш 15 минут)."""
    now = time.time()
    if _cache["items"] is not None and now - _cache["ts"] < 900:
        return _cache["items"]
    all_items = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for url in FEEDS:
            try:
                r = await c.get(url)
                all_items += _parse(r.text)
            except Exception as e:
                logger.error(f"RSS fetch error {url}: {e}")
    _cache["items"], _cache["ts"] = all_items, now
    return all_items


def check_sentiment(items, keys):
    """Новостной фон по монете: (негатив, позитив, упоминания, заголовки)."""
    neg = pos = mentions = 0
    heads = []
    keys_low = [k.lower() for k in keys if k]
    for it in items:
        text = (it["title"] + " " + it["desc"]).lower()
        if not any(k in text for k in keys_low):
            continue
        mentions += 1
        n = sum(w in text for w in NEG_WORDS)
        p = sum(w in text for w in POS_WORDS)
        if n > p:
            neg += 1
            heads.append("⚠️ " + it["title"])
        elif p > n:
            pos += 1
            heads.append("✅ " + it["title"])
    return neg, pos, mentions, heads[:2]


def get_stats():
    """Статистика RSS-лент для мониторинга."""
    now = time.time()
    cache_age = now - _cache["ts"] if _cache["ts"] else None
    items = _cache["items"] or []

    neg_examples = []
    pos_examples = []
    for item in items[:20]:
        text = (item["title"] + " " + item.get("desc", "")).lower()
        neg_count = sum(w in text for w in NEG_WORDS)
        pos_count = sum(w in text for w in POS_WORDS)
        if neg_count > 0:
            neg_examples.append(item["title"][:80])
        if pos_count > 0:
            pos_examples.append(item["title"][:80])

    return {
        "cache_age_min": round(cache_age / 60, 1) if cache_age else None,
        "items_count": len(items),
        "feeds_working": len(items) > 0,
        "neg_examples": neg_examples[:3],
        "pos_examples": pos_examples[:3],
    }
