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
    """Новостной фон по монете: (негатив, позитив, заголовки)."""
    neg = pos = 0
    heads = []
    keys_low = [k.lower() for k in keys if k]
    for it in items:
        text = (it["title"] + " " + it["desc"]).lower()
        if not any(k in text for k in keys_low):
            continue
        n = sum(w in text for w in NEG_WORDS)
        p = sum(w in text for w in POS_WORDS)
        if n > p:
            neg += 1
            heads.append("⚠️ " + it["title"])
        elif p > n:
            pos += 1
            heads.append("✅ " + it["title"])
    return neg, pos, heads[:2]
