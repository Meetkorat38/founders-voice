"""
content_engine.py — Daily News Digest Engine (Firecrawl Edition)

Structure sent each morning:
  1-2  🔥 WHAT'S VIRAL IN INDIA  — top 2 trending stories from Indian news sites + Reddit India
  3-4  🎯 IN YOUR WORLD          — 2 domain-specific news items (Firecrawl + domain Reddit)
  5    💡 YOUR NEXT POST         — 1 profile-based content idea (no URL)

How URLs work (three-pass system):
  Pass 1 — source collectors return title + description + metadata + URL per result.
            Sources include Firecrawl, Google Trends India, Reddit, HN, RSS, Product Hunt.
  Pass 2 — local scoring clusters related candidates before Claude sees them.
  Pass 3 — Claude reads numbered clusters and picks the best ones, returning
            {index, headline, summary}. URL is taken from items[index].url —
            Claude never invents a URL.

Social media blocking:
  All Firecrawl queries include -site: operators to exclude Instagram, Facebook, X/Twitter,
  TikTok, YouTube, and Threads. A post-fetch filter strips any that slip through.
  Niche news uses tech-native sources and domain-matched Reddit subreddits instead of
  the same generic India/news searches for every founder.
"""

import asyncio
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from firecrawl import Firecrawl

load_dotenv()

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
FIRECRAWL_API_KEY  = os.environ["FIRECRAWL_API_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL       = os.environ["SUPABASE_URL"]
# For Lovable deployments, only anon key is available. Lovable manages Supabase directly.
# RLS is configured to allow anon read/write on bot tables.
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-6")

# Cap concurrent founder processing so we don't storm Claude / Firecrawl with N parallel requests.
FOUNDER_CONCURRENCY = int(os.environ.get("FOUNDER_CONCURRENCY", "5"))

VIRAL_TARGET = 2
NICHE_TARGET = 2
IDEA_TARGET = 1
MIN_POOL_FOR_CLAUDE = 8

firecrawl = Firecrawl(api_key=FIRECRAWL_API_KEY)

# ── Social media domain blocking ──────────────────────────────────────────────

# Appended to every Firecrawl query so Google excludes social platforms from results.
# The Firecrawl Python SDK v2 has no native excludeDomains parameter; -site: operators work.
_SOCIAL_BLOCK = (
    " -site:instagram.com -site:facebook.com"
    " -site:twitter.com -site:x.com"
    " -site:tiktok.com -site:youtube.com -site:threads.net"
)

_SOCIAL_DOMAINS = frozenset({
    "instagram.com", "facebook.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "threads.net",
})

HIGH_AUTHORITY_DOMAINS = frozenset({
    "anthropic.com", "openai.com", "deepmind.google", "blog.google",
    "ai.google.dev", "microsoft.com", "nvidia.com", "perplexity.ai",
    "mistral.ai", "x.ai", "techcrunch.com", "theverge.com",
    "venturebeat.com", "technologyreview.com", "inc42.com",
    "yourstory.com", "entrackr.com", "moneycontrol.com",
    "economictimes.indiatimes.com", "livemint.com", "thehindu.com",
    "indianexpress.com", "hindustantimes.com", "timesofindia.indiatimes.com",
    "news.ycombinator.com", "producthunt.com",
})

LOW_QUALITY_MARKERS = (
    "explained", "everything you need to know", "what is", "list of",
    "top 10", "guide", "how to", "wiki", "coupon", "promo code",
)

TECH_WATCH_QUERIES = [
    "Anthropic Claude model release",
    "OpenAI model release",
    "Google Gemini model release",
    "AI coding agent launch",
    "LLM benchmark release",
    "AI startup funding",
    "SaaS AI product launch",
    "developer tools AI launch",
]

TECH_RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/feed/",
]

def _block_social(query: str) -> str:
    return query + _SOCIAL_BLOCK

def _filter_social_urls(items: list[dict]) -> list[dict]:
    """Strip any item whose URL belongs to a social media platform."""
    return [it for it in items if not any(d in it.get("url", "") for d in _SOCIAL_DOMAINS)]

def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host

def _authority_score(url: str) -> float:
    host = _domain(url)
    if not host:
        return 0.2
    if host in HIGH_AUTHORITY_DOMAINS or any(host.endswith("." + d) for d in HIGH_AUTHORITY_DOMAINS):
        return 1.0
    if host.endswith(".gov.in") or host.endswith(".edu") or host.endswith(".org"):
        return 0.8
    return 0.45

def _low_quality_score(title: str, description: str = "") -> float:
    haystack = f"{title} {description}".lower()
    return 0.25 if any(marker in haystack for marker in LOW_QUALITY_MARKERS) else 0.0

def _freshness_score(item: dict, max_age_hours: int) -> float:
    dt = _item_datetime(item)
    if not dt:
        return 0.55
    age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return 0.85
    if age_hours <= max_age_hours:
        return max(0.25, 1 - (age_hours / max_age_hours))
    return 0.0

def _engagement_score(item: dict) -> float:
    upvotes = float(item.get("upvotes") or 0)
    comments = float(item.get("comments") or 0)
    search_volume = float(item.get("search_volume") or 0)
    hn_points = float(item.get("hn_points") or 0)
    product_votes = float(item.get("product_votes") or 0)
    weighted = upvotes + comments * 3 + search_volume / 5000 + hn_points * 2 + product_votes * 2
    if weighted <= 0:
        return 0.25
    return min(1.0, weighted / 500)

def _tokenize_topic(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "today", "india",
        "indian", "news", "latest", "breaking", "launch", "launches", "new",
        "update", "after", "about", "into", "over", "amid", "says", "will",
    }
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.+-]{2,}", text.lower())
    return {w for w in words if w not in stop}

def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def _rank_and_cluster_candidates(
    items: list[dict],
    *,
    label: str,
    max_age_hours: int,
    founder_context: str = "",
    cluster_limit: int = 12,
) -> list[dict]:
    scored: list[dict] = []
    context_tokens = _tokenize_topic(founder_context)
    for item in items:
        title = item.get("title", "")
        desc = item.get("description", "")
        relevance = _similarity(_tokenize_topic(f"{title} {desc}"), context_tokens) if context_tokens else 0.5
        score = (
            _freshness_score(item, max_age_hours) * 0.25
            + _authority_score(item.get("url", "")) * 0.20
            + _engagement_score(item) * 0.25
            + relevance * 0.15
            + (1.0 if item.get("source") in ("google_trends", "hn", "product_hunt", "reddit") else 0.45) * 0.15
            - _low_quality_score(title, desc)
        )
        enriched = {**item, "_score": round(max(score, 0.0), 4), "_tokens": _tokenize_topic(f"{title} {desc}")}
        scored.append(enriched)

    scored.sort(key=lambda x: x["_score"], reverse=True)
    clusters: list[dict] = []
    for item in scored:
        placed = False
        for cluster in clusters:
            if _similarity(item["_tokens"], cluster["_tokens"]) >= 0.45:
                cluster["items"].append(item)
                cluster["_tokens"] |= item["_tokens"]
                cluster["score"] = round(cluster["score"] + item["_score"] * 0.35, 4)
                cluster["source_count"] = len({_domain(i.get("url", "")) or i.get("source", "") for i in cluster["items"]})
                placed = True
                break
        if not placed:
            clusters.append({
                "title": item.get("title", ""),
                "items": [item],
                "_tokens": set(item["_tokens"]),
                "score": item["_score"],
                "source_count": 1,
            })

    clusters.sort(key=lambda c: (c["score"] + min(c["source_count"], 4) * 0.12), reverse=True)
    print(f"[{label}] Ranked {len(items)} candidates into {len(clusters)} clusters")
    for i, cluster in enumerate(clusters[:8], 1):
        best = cluster["items"][0]
        print(
            f"[{label}] Cluster {i}: score={cluster['score']:.2f} "
            f"sources={cluster['source_count']} title={best.get('title', '')[:100]}"
        )

    out: list[dict] = []
    for cluster in clusters[:cluster_limit]:
        best = cluster["items"][0]
        other_sources = sorted({_domain(i.get("url", "")) or i.get("source", "") for i in cluster["items"]})
        out.append({
            "title": best.get("title", ""),
            "description": (
                f"Cluster score {cluster['score']:.2f}; {cluster['source_count']} source(s): "
                f"{', '.join(s for s in other_sources if s)[:180]}. "
                f"{best.get('description', '')}"
            ),
            "url": best.get("url", ""),
            "markdown": best.get("markdown", "")[:800],
            "source": best.get("source", "cluster"),
            "published_at": best.get("published_at"),
            "created_utc": best.get("created_utc"),
            "score": cluster["score"],
            "source_count": cluster["source_count"],
        })
    return out

def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # Reddit created_utc and some APIs use epoch seconds.
        if value > 10_000_000_000:
            value = value / 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
        "%d %b %Y",
        "%B %d, %Y",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def _item_datetime(item: dict) -> datetime | None:
    for key in ("published_at", "publishedDate", "date", "created_utc"):
        dt = _parse_datetime(item.get(key))
        if dt:
            return dt.astimezone(timezone.utc)
    return None

def _filter_by_age(items: list[dict], *, max_age_hours: int, label: str) -> list[dict]:
    """
    Keep unknown-date items because Firecrawl does not always expose dates, but
    reject candidates whose metadata proves they are too old.
    """
    now = datetime.now(timezone.utc)
    kept: list[dict] = []
    stale = 0
    unknown = 0
    for item in items:
        dt = _item_datetime(item)
        if not dt:
            unknown += 1
            kept.append(item)
            continue
        if now - dt <= timedelta(hours=max_age_hours):
            kept.append(item)
        else:
            stale += 1
            print(
                f"[{label}] Dropped stale candidate dated {dt.date().isoformat()}: "
                f"{item.get('title', '')[:90]} | {item.get('url', '')}"
            )
    print(f"[{label}] Freshness filter kept {len(kept)}/{len(items)} ({stale} stale, {unknown} unknown-date)")
    return kept

def _dedupe_extend(target: list[dict], seen_urls: set[str], items: list[dict]) -> int:
    added = 0
    for item in items:
        url = item.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        target.append(item)
        added += 1
    return added

def _fallback_from_raw(
    picks: list[dict],
    raw_items: list[dict],
    *,
    target_count: int,
    label: str,
) -> list[dict]:
    picked_urls = {p.get("source_url") for p in picks}
    for item in raw_items:
        if len(picks) >= target_count:
            break
        url = item.get("url")
        if not url or url in picked_urls:
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        desc = (item.get("description") or "").strip()
        picks.append({
            "headline":   title,
            "summary":    desc[:260],
            "source_url": url,
        })
        picked_urls.add(url)
    if len(picks) < target_count:
        print(f"[{label}] Final fallback still short: {len(picks)}/{target_count}")
    elif len(picked_urls) > 0:
        print(f"[{label}] Final fallback filled to {len(picks)}/{target_count}")
    return picks

def _founder_business_context(founder: dict) -> dict:
    biz = (founder.get("profile_json") or {}).get("business", {}) or {}
    return {
        "industry": (
            founder.get("industry")
            or biz.get("industry")
            or "business"
        ).strip(),
        "target_audience": (
            founder.get("target_audience")
            or biz.get("target_audience")
            or ""
        ).strip(),
        "unique_angle": (
            founder.get("unique_angle")
            or biz.get("unique_angle")
            or ""
        ).strip(),
    }

# ── Per-founder domain Reddit subreddits ─────────────────────────────────────

NICHE_SUBREDDITS: dict[str, list[str]] = {
    "ai":                     ["MachineLearning", "artificial", "OpenAI", "LocalLLaMA", "AIStartups"],
    "artificial intelligence": ["MachineLearning", "artificial", "OpenAI", "LocalLLaMA"],
    "machine learning":       ["MachineLearning", "datascience", "learnmachinelearning"],
    "saas":                   ["SaaS", "startups", "Entrepreneur", "b2bmarketing"],
    "finance":                ["IndiaInvestments", "investing", "personalfinance", "FinancialIndependence"],
    "fintech":                ["fintech", "IndiaInvestments", "startups"],
    "ecommerce":              ["ecommerce", "shopify", "Entrepreneur", "IndianStartups"],
    "health":                 ["health", "HealthcareIT", "medicine"],
    "healthcare":             ["health", "HealthcareIT", "medicine", "IndianHealthcare"],
    "edtech":                 ["edtech", "education", "IndianStartups"],
    "education":              ["edtech", "education", "IndianStartups"],
    "marketing":              ["marketing", "digital_marketing", "SEO", "content_marketing"],
    "crypto":                 ["CryptoCurrency", "IndiaInvestments", "DeFi"],
    "blockchain":             ["ethereum", "CryptoCurrency", "web3"],
    "real estate":            ["RealEstate", "realestateinvesting", "IndiaInvestments"],
    "logistics":              ["logistics", "supplychain", "IndianStartups"],
    "hr":                     ["humanresources", "recruiting", "IndianStartups"],
    "legal":                  ["LegalAdviceIndia", "legaladvice", "IndianStartups"],
    "default":                ["IndianStartups", "startups", "Entrepreneur", "india"],
}

# ── Supabase ──────────────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

# ── Retry helpers ─────────────────────────────────────────────────────────────

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

async def _post_with_retry(
    client: httpx.AsyncClient, url: str, *, headers: dict, json_body: dict,
    attempts: int = 3, label: str = "http",
) -> httpx.Response:
    delay = 1.0
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            r = await client.post(url, headers=headers, json=json_body)
            if r.status_code not in _RETRYABLE_STATUS:
                return r
            print(f"[retry] {label} attempt {i}/{attempts} got {r.status_code} — retrying in {delay}s")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            print(f"[retry] {label} attempt {i}/{attempts} failed ({e.__class__.__name__}) — retrying in {delay}s")
        if i < attempts:
            await asyncio.sleep(delay)
            delay *= 2
    if last_exc:
        raise last_exc
    return r

def get_all_founders() -> list[dict]:
    print(f"[Supabase] Loading founders...")
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/founders",
            params={"select": "id,name,telegram_id,profile_text,profile_json,industry,target_audience,unique_angle"},
            headers=_sb_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[Supabase] Failed: {r.status_code} {r.text[:200]}")
            return []
        rows = r.json()
        if not rows:
            print(f"[Supabase] Table returned 0 rows. Check RLS policies allow anon SELECT on founders table.")
        founders = []
        for row in rows:
            name        = row.get("name")
            telegram_id = row.get("telegram_id")
            if not name or not telegram_id:
                continue
            founders.append({
                "name":         name,
                "telegram_id":  telegram_id,
                "page_id":      row.get("id"),
                "profile_text": row.get("profile_text", ""),
                "profile_json": row.get("profile_json") or {},
                "industry":     row.get("industry", ""),
                "target_audience": row.get("target_audience", ""),
                "unique_angle":    row.get("unique_angle", ""),
            })
            print(f"  {name} — {telegram_id}")
        print(f"[Supabase] {len(founders)} founders loaded")
        return founders
    except Exception as e:
        print(f"[Supabase] Error: {e}")
        return []

async def log_digest_sent(telegram_id: str, founder_name: str, item_count: int):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/bot_logs",
                json={
                    "telegram_id": str(telegram_id),
                    "event_type":  "digest_sent",
                    "message":     f"Digest sent to {founder_name} — {item_count} items",
                    "payload":     {"item_count": item_count},
                },
                headers=_sb_headers(),
            )
    except Exception:
        pass

# ── Core AI calls ─────────────────────────────────────────────────────────────

def _firecrawl_search(
    query: str,
    sources: list[str],
    tbs: str,
    limit: int = 10,
    location: str | None = None,
    country: str | None = None,
) -> list[dict]:
    """
    Pass 1: Firecrawl /search. Returns flat list of {title, description, url, markdown, source}.
    Each result already carries a verified URL — no positional citation merge needed.
    Retries once on any exception (Firecrawl 5xxs are routine).
    """
    import time as _time
    print(f"[Firecrawl] query={query[:80]!r} sources={sources} tbs={tbs} limit={limit}")
    resp = None
    for attempt in (1, 2):
        try:
            kwargs = {
                "query": query,
                "sources": sources,
                "tbs": tbs,
                "limit": limit,
                "location": location,
                "scrape_options": {
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "parsers": [],
                    "proxy": "basic",
                },
            }
            if country:
                kwargs["country"] = country
            resp = firecrawl.search(**kwargs)
            break
        except TypeError as e:
            if country:
                print(f"[Firecrawl] country parameter unsupported, retrying without it: {e}")
                country = None
                continue
            print(f"[Firecrawl] attempt {attempt}/2 failed: {e}")
            if attempt == 2:
                return []
            _time.sleep(3)
        except Exception as e:
            print(f"[Firecrawl] attempt {attempt}/2 failed: {e}")
            if attempt == 2:
                return []
            _time.sleep(3)
    if resp is None:
        return []

    items: list[dict] = []
    for src in sources:
        bucket = getattr(resp, src, None)
        if bucket is None and isinstance(resp, dict):
            bucket = resp.get(src)
        if not bucket:
            continue
        for r in bucket:
            d = r if isinstance(r, dict) else getattr(r, "__dict__", {})
            url = d.get("url") or ""
            if not url:
                continue
            items.append({
                "title":       d.get("title") or "",
                "description": d.get("description") or d.get("snippet") or "",
                "url":         url,
                "markdown":    (d.get("markdown") or "")[:800],
                "source":      src,
                "published_at": (
                    d.get("publishedDate")
                    or d.get("published_date")
                    or d.get("date")
                    or d.get("publishedAt")
                    or d.get("createdAt")
                ),
            })

    print(f"[Firecrawl] → {len(items)} items with URLs")
    return items


async def _claude_pick(
    items: list[dict],
    system_prompt: str,
    max_items: int,
    max_tokens: int = 1000,
) -> list[dict]:
    """
    Pass 2: Claude reads numbered Firecrawl results and picks the best ones.
    Returns [{headline, summary, source_url}] where source_url is taken from
    items[index].url — Claude never invents a URL.

    Internally accepts up to (max_items + 2) picks from Claude's response so that
    URL-dedup and invalid-index drops don't leave us short of max_items. The caller's
    prompt is expected to ask for "top N picks" (over-request) — we still cap output
    at max_items unique entries.
    """
    if not items:
        return []

    payload = "\n\n".join(
        f"[{i}] SOURCE: {it.get('source', 'web').upper()}\n"
        f"    TITLE: {it['title']}\n"
        f"    DESC: {it['description']}\n"
        f"    SNIPPET: {it['markdown'][:500]}"
        for i, it in enumerate(items)
    )

    async with httpx.AsyncClient(timeout=60) as client:
        r = await _post_with_retry(
            client,
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://founderbot.app",
                "X-Title":      "Founder News Engine",
            },
            json_body={
                "model":      CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": payload},
                ],
            },
            label="claude_pick",
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()

    match = re.search(r'\[.*\]', content, re.DOTALL)
    if not match:
        print(f"[Claude] No JSON array found in response. Raw: {content[:500]!r}")
        return []
    try:
        picks = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[Claude] JSON parse error: {e}. Raw: {content[:500]!r}")
        return []

    # Over-pick buffer: walk through up to max_items+2 picks so dedup/invalid drops
    # don't leave us short. We still cap unique output at max_items.
    out: list[dict] = []
    seen_urls: set[str] = set()
    for p in picks[: max_items + 2]:
        if len(out) >= max_items:
            break
        idx = p.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(items):
            continue
        url = items[idx]["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "headline":   (p.get("headline") or items[idx]["title"]).strip(),
            "summary":    (p.get("summary") or "").strip(),
            "source_url": url,
        })

    if len(out) < max_items:
        print(
            f"[Claude] Got {len(out)}/{max_items} picks from {len(items)} input items. "
            f"Raw response: {content[:500]!r}"
        )
    return out


def _number_items(items: list[dict], start_num: int, item_type: str) -> list[dict]:
    return [
        {
            "number":     start_num + i,
            "headline":   it["headline"],
            "summary":    it["summary"],
            "source_url": it["source_url"],
            "type":       item_type,
        }
        for i, it in enumerate(items)
    ]

# ── Reddit trending helper ────────────────────────────────────────────────────

async def fetch_reddit_trending() -> list[dict]:
    """
    Fetch hot posts from Indian subreddits via Reddit's public JSON API.
    Returns items in the same {title, description, url, markdown, source} format
    as _firecrawl_search so they can be pooled together.
    Only external URLs are kept (self-posts and reddit.com links are skipped).
    """
    subs = ["india", "bollywood", "Cricket", "IndiaInvestments", "startups"]
    items: list[dict] = []
    seen_urls: set[str] = set()
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "FoundersVoiceBot/1.0"},
        follow_redirects=True,
    ) as client:
        for sub in subs:
            try:
                r = await client.get(f"https://www.reddit.com/r/{sub}/hot.json?limit=5")
                if r.status_code != 200:
                    continue
                posts = r.json().get("data", {}).get("children", [])
                for p in posts:
                    d = p.get("data", {})
                    url = d.get("url", "")
                    if not url or url in seen_urls or "reddit.com" in url:
                        continue
                    seen_urls.add(url)
                    items.append({
                        "title":       d.get("title", ""),
                        "description": (
                            f"r/{sub} · {d.get('score', 0):,} upvotes · "
                            f"{d.get('num_comments', 0):,} comments"
                        ),
                        "url":      url,
                        "markdown": d.get("selftext", "")[:300],
                        "source":   "reddit",
                        "created_utc": d.get("created_utc"),
                        "upvotes": d.get("score", 0),
                        "comments": d.get("num_comments", 0),
                    })
            except Exception as e:
                print(f"[Reddit] r/{sub} failed: {e}")
    print(f"[Reddit] → {len(items)} posts with external URLs")
    return items


async def fetch_google_trends_india() -> list[dict]:
    """Fetch Google Daily Trends RSS for India as a trend-first signal."""
    url = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=IN"
    items: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "FoundersVoiceBot/1.0"})
        if r.status_code != 200:
            print(f"[GoogleTrends] Failed: {r.status_code}")
            return []
        root = ET.fromstring(r.text)
    except Exception as e:
        print(f"[GoogleTrends] RSS failed: {e}")
        return []

    ns = {"ht": "https://trends.google.com/trends/trendingsearches/daily"}
    for node in root.findall("./channel/item")[:20]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = node.findtext("pubDate")
        traffic = node.findtext("ht:approx_traffic", namespaces=ns) or ""
        news_url = ""
        news_title = ""
        for news in node.findall("ht:news_item", namespaces=ns):
            candidate_url = (news.findtext("ht:news_item_url", namespaces=ns) or "").strip()
            if candidate_url:
                news_url = candidate_url
                news_title = (news.findtext("ht:news_item_title", namespaces=ns) or "").strip()
                break
        volume_match = re.search(r"[\d,]+", traffic)
        search_volume = int(volume_match.group(0).replace(",", "")) if volume_match else 0
        if not title:
            continue
        items.append({
            "title": news_title or title,
            "description": f"Google Trends India: {title}; approx traffic {traffic}".strip(),
            "url": news_url or link or f"https://trends.google.com/trends/explore?geo=IN&q={title}",
            "markdown": "",
            "source": "google_trends",
            "published_at": pub_date,
            "search_volume": search_volume,
        })
    print(f"[GoogleTrends] -> {len(items)} India trend candidates")
    return items


async def fetch_reddit_niche(founder: dict) -> list[dict]:
    """
    Fetch hot posts from domain-matched subreddits for this specific founder.
    Maps the founder's industry to relevant subreddits so an AI founder gets
    r/MachineLearning instead of r/india.
    Only external URLs kept; uses t=day (last 24h) for freshness.
    """
    industry = _founder_business_context(founder)["industry"].lower()
    subs = NICHE_SUBREDDITS["default"]
    for key, sub_list in NICHE_SUBREDDITS.items():
        if key == "default":
            continue
        if key in industry or industry in key:
            subs = sub_list
            break

    # Pick top 2 subreddits from the matched list to avoid over-fetching
    target_subs = subs[:2]
    items: list[dict] = []
    seen_urls: set[str] = set()
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "FoundersVoiceBot/1.0"},
        follow_redirects=True,
    ) as client:
        for sub in target_subs:
            try:
                r = await client.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=8")
                if r.status_code != 200:
                    continue
                posts = r.json().get("data", {}).get("children", [])
                for p in posts:
                    d = p.get("data", {})
                    url = d.get("url", "")
                    if not url or url in seen_urls or "reddit.com" in url:
                        continue
                    seen_urls.add(url)
                    items.append({
                        "title":       d.get("title", ""),
                        "description": (
                            f"r/{sub} · {d.get('score', 0):,} upvotes · "
                            f"{d.get('num_comments', 0):,} comments"
                        ),
                        "url":      url,
                        "markdown": d.get("selftext", "")[:300],
                        "source":   "reddit",
                        "created_utc": d.get("created_utc"),
                        "upvotes": d.get("score", 0),
                        "comments": d.get("num_comments", 0),
                    })
            except Exception as e:
                print(f"[Reddit/niche] r/{sub} failed: {e}")
    print(f"[Reddit/niche] {founder['name']} ({industry}) → {target_subs} → {len(items)} posts")
    return items


async def fetch_hn_tech_trends(founder: dict | None = None) -> list[dict]:
    """Fetch recent HN stories for AI/SaaS/startup topics through Algolia."""
    context = _founder_business_context(founder or {})
    query_terms = [
        "anthropic claude",
        "openai model",
        "llm ai",
        "startup saas",
        "developer tools product launch",
    ]
    if context["industry"] and context["industry"].lower() not in ("business", "tech"):
        query_terms.append(context["industry"])

    created_after = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
    items: list[dict] = []
    seen_urls: set[str] = set()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for query in query_terms:
            try:
                r = await client.get(
                    "https://hn.algolia.com/api/v1/search_by_date",
                    params={
                        "query": query,
                        "tags": "story",
                        "numericFilters": f"created_at_i>{created_after}",
                        "hitsPerPage": 20,
                    },
                )
                if r.status_code != 200:
                    print(f"[HN] query failed {r.status_code}: {query}")
                    continue
                hits = r.json().get("hits", [])
            except Exception as e:
                print(f"[HN] query failed: {query} ({e})")
                continue

            for hit in hits:
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                if not url or url in seen_urls:
                    continue
                title = hit.get("title") or hit.get("story_title") or ""
                if not title:
                    continue
                seen_urls.add(url)
                points = hit.get("points") or 0
                comments = hit.get("num_comments") or 0
                items.append({
                    "title": title,
                    "description": f"Hacker News: {points} points, {comments} comments",
                    "url": url,
                    "markdown": "",
                    "source": "hn",
                    "published_at": hit.get("created_at"),
                    "hn_points": points,
                    "comments": comments,
                })
    print(f"[HN] -> {len(items)} tech candidates")
    return items


async def fetch_rss_items(feed_urls: list[str], label: str, limit_per_feed: int = 12) -> list[dict]:
    items: list[dict] = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for feed_url in feed_urls:
            try:
                r = await client.get(feed_url, headers={"User-Agent": "FoundersVoiceBot/1.0"})
                if r.status_code != 200:
                    print(f"[{label}/RSS] Failed {r.status_code}: {feed_url}")
                    continue
                root = ET.fromstring(r.text)
            except Exception as e:
                print(f"[{label}/RSS] Failed: {feed_url} ({e})")
                continue

            channel_items = root.findall("./channel/item")
            atom_items = root.findall("{http://www.w3.org/2005/Atom}entry")
            for node in channel_items[:limit_per_feed]:
                title = (node.findtext("title") or "").strip()
                link = (node.findtext("link") or "").strip()
                desc = re.sub(r"<[^>]+>", "", node.findtext("description") or "").strip()
                if title and link:
                    items.append({
                        "title": title,
                        "description": desc[:300],
                        "url": link,
                        "markdown": desc[:800],
                        "source": label.lower(),
                        "published_at": node.findtext("pubDate"),
                    })
            for node in atom_items[:limit_per_feed]:
                title = (node.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                link_node = node.find("{http://www.w3.org/2005/Atom}link")
                link = link_node.attrib.get("href", "") if link_node is not None else ""
                desc = (node.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
                if title and link:
                    items.append({
                        "title": title,
                        "description": re.sub(r"<[^>]+>", "", desc)[:300],
                        "url": link,
                        "markdown": desc[:800],
                        "source": label.lower(),
                        "published_at": node.findtext("{http://www.w3.org/2005/Atom}updated"),
                    })
    print(f"[{label}/RSS] -> {len(items)} candidates")
    return items


async def fetch_product_hunt_trends() -> list[dict]:
    """Fetch Product Hunt launches when PRODUCTHUNT_TOKEN is configured."""
    token = os.environ.get("PRODUCTHUNT_TOKEN")
    if not token:
        print("[ProductHunt] Skipped - PRODUCTHUNT_TOKEN not configured")
        return []
    query = """
    query {
      posts(first: 20) {
        edges {
          node {
            id
            name
            tagline
            url
            votesCount
            commentsCount
            createdAt
          }
        }
      }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.producthunt.com/v2/api/graphql",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": query},
            )
        if r.status_code != 200:
            print(f"[ProductHunt] Failed: {r.status_code} {r.text[:120]}")
            return []
        edges = r.json().get("data", {}).get("posts", {}).get("edges", [])
    except Exception as e:
        print(f"[ProductHunt] Failed: {e}")
        return []

    items = []
    for edge in edges:
        node = edge.get("node", {})
        name = node.get("name") or ""
        if not name:
            continue
        items.append({
            "title": f"{name}: {node.get('tagline', '')}".strip(),
            "description": f"Product Hunt launch: {node.get('votesCount', 0)} votes, {node.get('commentsCount', 0)} comments",
            "url": node.get("url") or f"https://www.producthunt.com/posts/{node.get('id')}",
            "markdown": node.get("tagline") or "",
            "source": "product_hunt",
            "published_at": node.get("createdAt"),
            "product_votes": node.get("votesCount") or 0,
            "comments": node.get("commentsCount") or 0,
        })
    print(f"[ProductHunt] -> {len(items)} candidates")
    return items


async def _get_yesterday_viral_headlines() -> list[str]:
    """
    Fetch the viral headline text from yesterday's digest (any founder row).
    Used to tell Claude which stories were already sent so it avoids repeating them.
    """
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/daily_digests",
                params={
                    "sent_date": f"eq.{yesterday}",
                    "select":    "news_items",
                    "limit":     "1",
                },
                headers=_sb_headers(),
            )
        if r.status_code != 200 or not r.json():
            return []
        headlines = []
        for row in r.json():
            for item in (row.get("news_items") or []):
                if item.get("type") == "viral" and item.get("headline"):
                    headlines.append(item["headline"])
        return headlines
    except Exception:
        return []


# ── Step 1: Fetch 2 Indian viral stories ─────────────────────────────────────

async def fetch_indian_viral_news() -> list[dict]:
    """
    Two-pass fetch for top 2 stories viral across India.
    Sources: Firecrawl (news/web, 6-24h windows) + Reddit India hot posts.
    Cross-day deduplication: yesterday's viral topics are excluded via Claude prompt.
    """
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    today_iso = datetime.now(timezone.utc).date().isoformat()
    print(f"\n[Viral] Fetching Indian viral news — {today}")

    # Pass 1A: Firecrawl — clean queries that target news/articles covering social chatter.
    # _block_social() keeps final sources off social platforms, while query text still asks
    # for reporting about what is being discussed on those platforms.
    queries = [
        ("India trending news breaking story today", "qdr:6h"),
        ("India most discussed news story social media reaction today", "qdr:d"),
        ("India controversy debate news Twitter Instagram reaction today", "qdr:d"),
        ("India viral story news coverage Reddit YouTube today", "qdr:d"),
        ("India cricket entertainment politics startup viral news today", "qdr:d"),
        ("India policy consumer internet culture viral debate today", "qdr:d"),
    ]
    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    for q, tbs in queries:
        for item in _firecrawl_search(
            query=_block_social(q),
            sources=["news", "web"],
            tbs=tbs,
            limit=8,
            location="India",
            country="IN",
        ):
            _dedupe_extend(raw_items, seen_urls, [item])
    after_firecrawl = len(raw_items)
    print(f"[Viral] After Firecrawl queries: {after_firecrawl} items")

    # Pass 1B: Google Trends India — search attention signal, then Reddit India.
    trend_items = await fetch_google_trends_india()
    _dedupe_extend(raw_items, seen_urls, trend_items)
    print(f"[Viral] After Google Trends India: {len(raw_items)} items (was {after_firecrawl})")

    # Pass 1C: Reddit India — real discussion signal with verified article URLs.
    reddit_items = await fetch_reddit_trending()
    _dedupe_extend(raw_items, seen_urls, reddit_items)
    print(f"[Viral] After Reddit India: {len(raw_items)} items")

    if len(raw_items) < MIN_POOL_FOR_CLAUDE:
        fallback_queries = [
            ("India latest viral news today", "qdr:d"),
            ("India breaking news live updates trending today", "qdr:d"),
        ]
        before_fallback = len(raw_items)
        for q, tbs in fallback_queries:
            fallback_items = _firecrawl_search(
                query=_block_social(q),
                sources=["news", "web"],
                tbs=tbs,
                limit=10,
                location="India",
                country="IN",
            )
            _dedupe_extend(raw_items, seen_urls, fallback_items)
        print(f"[Viral] After fallback searches: {len(raw_items)} items (was {before_fallback})")

    # Safety net: strip any social media URLs that slipped through
    before_social_filter = len(raw_items)
    raw_items = _filter_social_urls(raw_items)
    if len(raw_items) != before_social_filter:
        print(f"[Viral] Social-URL filter dropped {before_social_filter - len(raw_items)} items")
    raw_items = _filter_by_age(raw_items, max_age_hours=48, label="Viral")

    if not raw_items:
        print(f"[Viral] Empty result from all sources")
        return []
    print(f"[Viral] Pooled {len(raw_items)} unique results across {len(queries)} queries + Reddit")
    ranked_items = _rank_and_cluster_candidates(
        raw_items,
        label="Viral",
        max_age_hours=48,
        founder_context="India viral internet social media politics cricket Bollywood startups economy",
        cluster_limit=14,
    )

    # Cross-day dedup: fetch yesterday's viral headlines so Claude avoids repeating them.
    yesterday_headlines = await _get_yesterday_viral_headlines()
    if yesterday_headlines:
        already_sent = "\n".join(f"- {h}" for h in yesterday_headlines)
        exclusion_clause = (
            f"\n\nIMPORTANT — these topics were already sent YESTERDAY. "
            f"Do NOT pick them again unless something genuinely new broke today:\n{already_sent}\n"
        )
        print(f"[Viral] Excluding {len(yesterday_headlines)} yesterday topics: {yesterday_headlines}")
    else:
        print(f"[Viral] No yesterday topics to exclude")
        exclusion_clause = ""

    # Pass 2: Claude picks the topics actually driving engagement right now.
    # Over-request (max_items=4) handled in _claude_pick — we take the first 2 unique below.
    system_prompt = (
        f"Today is {today} ({today_iso}). You are given pre-ranked topic clusters from "
        "Google Trends India, Indian news/web results, and Reddit India discussions from the past 48 hours.\n\n"
        "Your job: return your TOP picks for topics currently driving the MOST conversation "
        "and engagement across Indian social media (Twitter/X India, Instagram, YouTube, "
        "WhatsApp, Reddit India) right now.\n\n"
        "HARD FRESHNESS RULE: reject stale or evergreen items. Prefer items from today "
        f"({today_iso}); use yesterday only if the story is still visibly peaking today. "
        "Articles summarizing days-old movements do NOT qualify unless there is a fresh update.\n\n"
        "A topic qualifies only if ALL are true:\n"
        "1. It broke or peaked in the last 24-48 hours\n"
        "2. Ordinary Indians (not just one industry) are reacting to it\n"
        "3. Posting an opinion about it would get real engagement today\n\n"
        "Prefer sources with high Reddit engagement (upvotes + comments) as they signal "
        "genuine discussion, not just publisher promotion.\n\n"
        "Do NOT assume categories. Winners could be anything — policy, sports, "
        "tech layoffs, a celebrity, a startup scandal, a weather event, an economic shift, "
        "a viral video, whatever is actually dominating feeds. Pick what the DATA shows.\n\n"
        "Reject: evergreen explainers, listicles, pure promo, day-old or week-old stories, "
        "niche industry news that only insiders discuss.\n\n"
        "The input is already clustered and scored. Prefer high-score clusters with multiple sources "
        "or strong Google Trends/Reddit engagement. Do not return duplicate topics."
        + exclusion_clause
        + f"\n\nReturn ONLY a JSON array of your TOP {VIRAL_TARGET + 2} picks "
        "(no markdown, no backticks). Return at least 2 if at least 2 usable candidates exist:\n"
        '[{"index": <int from input>, '
        '"headline": "the specific topic as a headline", '
        '"summary": "two sentences — what happened + why India is reacting / what the debate is"}]'
    )

    try:
        picks = await _claude_pick(ranked_items, system_prompt, max_items=VIRAL_TARGET, max_tokens=900)
    except Exception as e:
        print(f"[Viral] Claude picking failed: {e}")
        picks = []

    if len(picks) < VIRAL_TARGET:
        picks = _fallback_from_raw(picks, ranked_items or raw_items, target_count=VIRAL_TARGET, label="Viral")

    items = _number_items(picks, start_num=1, item_type="viral")
    print(f"[Viral] Done — {len(items)} items, {len([i for i in items if i['source_url']])} with URLs")
    return items

# ── Step 2: Fetch 2 niche news items for founder ─────────────────────────────

async def fetch_niche_news(founder: dict, start_num: int) -> list[dict]:
    """
    Two-pass fetch for 2 domain-specific news items for this founder.

    Fetch stages (each logs its raw count):
      1. Firecrawl industry and audience queries — qdr:d
      2. Firecrawl market/report/platform queries — qdr:d/qdr:w
      3. Domain Reddit (kept aside for possible backfill)
      4. Fallback Firecrawl query — only fires if pool < 8 items
      5. Claude picks top 2 from the merged pool
      6. Raw-candidate backfill — if Claude returns short, pad from the best pool

    The section should almost never be empty after this.
    """
    name          = founder["name"]
    profile       = founder["profile_text"][:1200]
    biz_context   = _founder_business_context(founder)
    industry      = biz_context["industry"]
    unique_angle  = biz_context["unique_angle"]
    target_aud    = biz_context["target_audience"]
    today         = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"\n[Niche] Fetching for {name} ({industry})")

    # Stage 1+2: Firecrawl — industry-level + founder-specific angle/audience.
    # _block_social() appends -site: operators so results come from news sites, not social media.
    audience_query = f"{target_aud} India business trends" if target_aud else f"{industry} India customer trends"
    angle_query = f"{unique_angle} India news trends" if unique_angle else f"{industry} India startup news"
    queries = [
        (f"{industry} news India today", "qdr:d", 10, "in"),
        (f"{industry} market shift report India today", "qdr:d", 10, "in"),
        (f"{industry} startup funding policy platform update India", "qdr:w", 10, "in"),
        (audience_query, "qdr:w", 10, "in"),
        (angle_query, "qdr:w", 10, "in"),
    ]
    if any(term in industry.lower() for term in ("ai", "tech", "software", "saas", "ecommerce", "startup")):
        for watch_query in TECH_WATCH_QUERIES:
            queries.append((watch_query, "qdr:w", 8, None))

    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    for q, tbs, limit, location in queries:
        found = _firecrawl_search(
            query=_block_social(q),
            sources=["news", "web"],
            tbs=tbs,
            limit=limit,
            location="India" if location == "in" else location,
            country="IN" if location == "in" else None,
        )
        added = _dedupe_extend(raw_items, seen_urls, found)
        print(f"[Niche] Query added {added}: {q[:80]!r}")
    after_firecrawl = len(raw_items)
    print(f"[Niche] After Firecrawl queries: {after_firecrawl} items")

    # Stage 3: Tech-native attention sources plus domain-matched Reddit.
    hn_items, rss_items, product_items = await asyncio.gather(
        fetch_hn_tech_trends(founder),
        fetch_rss_items(TECH_RSS_FEEDS, "Tech"),
        fetch_product_hunt_trends(),
    )
    _dedupe_extend(raw_items, seen_urls, hn_items)
    _dedupe_extend(raw_items, seen_urls, rss_items)
    _dedupe_extend(raw_items, seen_urls, product_items)
    print(
        f"[Niche] After tech-native sources: {len(raw_items)} items "
        f"(HN {len(hn_items)}, RSS {len(rss_items)}, ProductHunt {len(product_items)})"
    )

    # Stage 4: Domain-matched Reddit subreddits.
    reddit_pool = await fetch_reddit_niche(founder)
    _dedupe_extend(raw_items, seen_urls, reddit_pool)
    after_reddit = len(raw_items)
    print(f"[Niche] After Reddit niche: {after_reddit} items ({len(reddit_pool)} from Reddit pool)")

    # Stage 5: Broader fallback query — only fires when the pool is thin.
    # Uses qdr:w and no location filter to widen the net intentionally.
    if after_reddit < MIN_POOL_FOR_CLAUDE:
        fallback_queries = [
            f"{industry} business news",
            f"{industry} global news startup founders",
            f"{industry} latest research report market data",
        ]
        before_fallback = len(raw_items)
        for fallback_query in fallback_queries:
            fallback_items = _firecrawl_search(
                query=_block_social(fallback_query),
                sources=["news", "web"],
                tbs="qdr:w",
                limit=10,
                location=None,
            )
            _dedupe_extend(raw_items, seen_urls, fallback_items)
        print(f"[Niche] After fallback broader query: {len(raw_items)} items (was {after_reddit})")

    # Safety net: strip any social media URLs that slipped through
    raw_items = _filter_social_urls(raw_items)
    raw_items = _filter_by_age(raw_items, max_age_hours=168, label="Niche")

    if not raw_items:
        print(f"[Niche] Empty result for {name} — all stages produced nothing")
        return []
    founder_context = f"{profile} {industry} {target_aud} {unique_angle}"
    ranked_items = _rank_and_cluster_candidates(
        raw_items,
        label="Niche",
        max_age_hours=168,
        founder_context=founder_context,
        cluster_limit=16,
    )

    # Stage 6: Claude picks the 2 most relevant for THIS founder.
    # Loosened prompt — when nothing is ideal, pick the BEST AVAILABLE rather than skip.
    system_prompt = (
        f"Today is {today}.\n\n"
        f"FOUNDER CONTEXT:\n{profile}\n\n"
        "You are given numbered, pre-ranked topic clusters from web/news, official tech sources, "
        "Hacker News, Product Hunt, RSS feeds, and Reddit. Pick your TOP picks "
        "(at least 2 if any are usable) most relevant to THIS founder's industry, "
        "audience, and expertise. Prefer:\n"
        "- Breaking news or new developments in their sector, especially official AI/model/product launches\n"
        "- Industry data, reports, or research published recently\n"
        "- Competitor moves or market shifts they should know\n"
        "- Platform changes relevant to their work\n"
        "- Trends their target audience is actively discussing\n\n"
        "Prefer fresh, specific items from the last 7 days. For AI/model/company launches, "
        "prefer official company posts, Hacker News traction, or multiple credible tech outlets. "
        "BUT — if nothing is ideal, pick the 2 BEST "
        "AVAILABLE rather than returning fewer. An OK item beats an empty section.\n\n"
        f"Return ONLY a JSON array of your TOP {NICHE_TARGET + 2} picks "
        "(no markdown, no backticks, nothing else). Return at least 2 if at least 2 usable candidates exist:\n"
        '[{"index": <int from input>, "headline": "specific real headline", '
        '"summary": "one sentence why this matters for this founder"}]'
    )

    try:
        picks = await _claude_pick(ranked_items, system_prompt, max_items=NICHE_TARGET, max_tokens=900)
    except Exception as e:
        print(f"[Niche] Claude picking failed for {name}: {e}")
        picks = []

    # Stage 7: Raw-candidate backfill — if Claude returned short, pad from the candidate pool.
    if len(picks) < NICHE_TARGET:
        picks = _fallback_from_raw(picks, ranked_items or raw_items, target_count=NICHE_TARGET, label="Niche")

    items = _number_items(picks, start_num=start_num, item_type="niche")
    print(f"[Niche] Done — {len(items)} items, {len([i for i in items if i['source_url']])} with URLs")
    return items

# ── Step 3: Generate 1 content idea (Claude only, no search needed) ──────────

async def generate_content_ideas(founder: dict, start_num: int) -> list[dict]:
    """
    Claude generates 1 strategic LinkedIn post idea based on founder's profile.
    No URLs needed — these are generated ideas, not news items.
    """
    name    = founder["name"]
    profile = founder["profile_text"][:2000]
    print(f"\n[Ideas] Generating for {name}")

    prompt = (
        f"You are a LinkedIn content strategist.\n\n"
        f"FOUNDER PROFILE:\n{profile}\n\n"
        f"Generate {IDEA_TARGET + 1} specific, high-quality LinkedIn post IDEAS for this founder.\n\n"
        f"Each idea must:\n"
        f"- Be specific to their business and personal story — not generic\n"
        f"- Be something only THIS founder has the authority and experience to write\n"
        f"- Have a compelling hook that stops their specific audience while scrolling\n"
        f"- Draw from a real tension, insight, or lesson in their work\n\n"
        f"Return ONLY a JSON array, no markdown, no backticks:\n"
        f'[{{"headline": "post idea as a bold statement or question", '
        f'"category": "Story/Insight/Contrarian/Lesson/How-To", '
        f'"why": "one line — why this resonates for their audience"}}]'
    )

    async with httpx.AsyncClient(timeout=60) as client:
        r = await _post_with_retry(
            client,
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://founderbot.app",
                "X-Title":      "Founder News Engine",
            },
            json_body={
                "model":      CLAUDE_MODEL,
                "max_tokens": 600,
                "messages":   [{"role": "user", "content": prompt}],
            },
            label="generate_ideas",
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()

    match = re.search(r'\[.*\]', content, re.DOTALL)
    if not match:
        print(f"[Ideas] No JSON found for {name}")
        return []

    try:
        items = json.loads(match.group())
    except json.JSONDecodeError:
        print(f"[Ideas] JSON parse error for {name}")
        return []

    result = []
    for item in items[:IDEA_TARGET]:
        result.append({
            "number":   start_num + len(result),
            "headline": item.get("headline", "").strip(),
            "category": item.get("category", ""),
            "why":      item.get("why", ""),
            # No source_url — these are generated ideas
        })

    print(f"[Ideas] Done — {len(result)} ideas for {name}")
    return result

# ── Format digest ─────────────────────────────────────────────────────────────

def format_digest(founder_name: str, viral_items: list[dict],
                  niche_items: list[dict], ideas: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d")
    total = len(viral_items) + len(niche_items) + len(ideas)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Good morning {founder_name}! ☀️",
        today,
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if viral_items:
        lines.append("🔥 WHAT'S VIRAL IN INDIA\n")
        for item in viral_items:
            lines.append(f"{item['number']}. {item['headline']}")
            if item.get("summary"):
                lines.append(f"   → {item['summary']}")
            if item.get("source_url"):
                lines.append(f"   🔗 {item['source_url']}")
            lines.append("")

    if niche_items:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🎯 IN YOUR WORLD\n")
        for item in niche_items:
            lines.append(f"{item['number']}. {item['headline']}")
            if item.get("summary"):
                lines.append(f"   → {item['summary']}")
            if item.get("source_url"):
                lines.append(f"   🔗 {item['source_url']}")
            lines.append("")

    if ideas:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 YOUR NEXT POST\n")
        for item in ideas:
            tag = f"[{item['category']}] " if item.get("category") else ""
            lines.append(f"{item['number']}. {item['headline']}")
            if item.get("why"):
                lines.append(f"   {tag}→ {item['why']}")
            lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Reply with a number (1–{total}) to pick a topic.",
        "Then send a voice note or text with your angle.",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)

# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_telegram(chat_id: str, text: str):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    async with httpx.AsyncClient(timeout=30) as client:
        for i, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            # Attach the "Write your own script" button only to the last chunk
            if i == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [[
                        {"text": "✍️ Write your own script", "callback_data": "CUSTOM_SCRIPT"}
                    ]]
                }
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
            )
            ok = r.status_code == 200
            print(f"[Telegram] → {chat_id}: {'OK' if ok else r.text[:80]}")
            await asyncio.sleep(0.3)

# ── Write news_digest.json ────────────────────────────────────────────────────

def write_news_digest(telegram_id: str, founder_name: str,
                      news_items: list[dict], business_ideas: list[dict]):
    """
    Upsert today's digest into Supabase `daily_digests` table.
    bot.py reads the latest row when a founder picks a topic number.
    Re-running the cron for the same day overwrites that day's row (idempotent per day).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    payload = {
        "telegram_id":    str(telegram_id),
        "sent_date":      today,
        "founder_name":   founder_name,
        "news_items":     news_items,
        "business_ideas": business_ideas,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates"}

    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/daily_digests",
            json=payload,
            headers=headers,
            timeout=15,
        )
        if r.status_code not in (200, 201):
            print(f"[Digest] Save failed for {founder_name}: {r.status_code} {r.text[:120]}")
            return
    except Exception as e:
        print(f"[Digest] Save error for {founder_name}: {e}")
        return

    viral_count = len([n for n in news_items if n.get("type") == "viral"])
    niche_count = len([n for n in news_items if n.get("type") == "niche"])
    url_count   = len([n for n in news_items if n.get("source_url")])
    print(f"[Digest] Saved for {founder_name}: "
          f"{viral_count} viral + {niche_count} niche ({url_count} with URLs) + {len(business_ideas)} ideas")

# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    print("=" * 52)
    print("  FOUNDER DIGEST ENGINE — FIRECRAWL EDITION")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 52)

    founders = get_all_founders()
    if not founders:
        print("No founders found in Supabase.")
        return

    # Optional: pass a telegram_id as CLI arg to test for one founder only
    # Usage: python content_engine.py 5198041379
    if len(sys.argv) > 1:
        only_id = sys.argv[1]
        founders = [f for f in founders if str(f["telegram_id"]) == only_id]
        if not founders:
            print(f"[Test] No founder found with telegram_id={only_id}")
            return
        print(f"[Test] Running for single founder: {founders[0]['name']}")

    # Fetch Indian viral news once — same viral items for ALL founders
    viral_items = await fetch_indian_viral_news()
    if viral_items:
        print(f"\n[Viral] Pool ready: {len(viral_items)} items")
    else:
        print("\n[Viral] No viral items — section will be skipped for all founders")

    sem = asyncio.Semaphore(FOUNDER_CONCURRENCY)

    async def _process_founder(founder: dict):
        async with sem:
            name = founder["name"]
            if not founder["telegram_id"]:
                print(f"[Skip] No telegram_id for {name}")
                return
            print(f"\n{'─' * 48}\n  Processing: {name}\n{'─' * 48}")
            try:
                niche_start = len(viral_items) + 1
                niche_items = await fetch_niche_news(founder, start_num=niche_start)

                ideas_start = len(viral_items) + len(niche_items) + 1
                ideas       = await generate_content_ideas(founder, start_num=ideas_start)

                digest_msg = format_digest(name, viral_items, niche_items, ideas)
                await send_telegram(founder["telegram_id"], digest_msg)

                all_news_items = viral_items + niche_items
                write_news_digest(founder["telegram_id"], name, all_news_items, ideas)

                asyncio.create_task(log_digest_sent(
                    founder["telegram_id"], name, len(all_news_items) + len(ideas)
                ))
            except Exception as e:
                print(f"[Founder] {name} failed: {e}")

    await asyncio.gather(*(_process_founder(f) for f in founders), return_exceptions=False)

    print(f"\n{'=' * 52}")
    print("  DIGEST SENT TO ALL FOUNDERS")
    print(f"{'=' * 52}")


if __name__ == "__main__":
    asyncio.run(run())
