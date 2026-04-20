"""
content_engine.py — Daily News Digest Engine (Firecrawl Edition)

Structure sent each morning:
  1-3  🔥 WHAT'S VIRAL IN INDIA  — top 3 trending on Indian social media (24-72h)
  4-8  🎯 IN YOUR WORLD          — 5 niche-specific news for this founder
  9-10 💡 YOUR NEXT POST         — 2 profile-based content ideas (no URL)

How URLs work (two-pass system):
  Pass 1 — Firecrawl /search returns title + description + markdown + URL per result.
            Each result already carries its own verified URL.
  Pass 2 — Claude reads the numbered results and picks the best ones, returning
            {index, headline, summary}. URL is taken from items[index].url —
            Claude never invents a URL.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from firecrawl import Firecrawl

load_dotenv()

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
FIRECRAWL_API_KEY  = os.environ["FIRECRAWL_API_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
SUPABASE_URL       = os.environ["SUPABASE_URL"]
# Prefer service_role server-side so RLS can stay locked. Falls back to anon for local dev.
SUPABASE_KEY       = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_ANON_KEY"]
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-6")

# Cap concurrent founder processing so we don't storm Claude / Firecrawl with N parallel requests.
FOUNDER_CONCURRENCY = int(os.environ.get("FOUNDER_CONCURRENCY", "5"))

firecrawl = Firecrawl(api_key=FIRECRAWL_API_KEY)

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
    print("[Supabase] Loading founders...")
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/founders",
            params={"select": "id,name,telegram_id,profile_text,profile_json,industry"},
            headers=_sb_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[Supabase] Failed: {r.status_code} {r.text[:120]}")
            return []
        rows     = r.json()
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
            resp = firecrawl.search(
                query=query,
                sources=sources,
                tbs=tbs,
                limit=limit,
                location=location,
                scrape_options={
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "parsers": [],
                    "proxy": "basic",
                },
            )
            break
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
    """
    if not items:
        return []

    payload = "\n\n".join(
        f"[{i}] TITLE: {it['title']}\n"
        f"    DESC: {it['description']}\n"
        f"    SNIPPET: {it['markdown'][:300]}"
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
        print(f"[Claude] No JSON array found in response")
        return []
    try:
        picks = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[Claude] JSON parse error: {e}")
        return []

    out = []
    seen_urls = set()
    for p in picks[:max_items]:
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

# ── Step 1: Fetch 3 Indian viral stories ─────────────────────────────────────

async def fetch_indian_viral_news() -> list[dict]:
    """
    Two-pass fetch for top 3 stories viral across India (24-72h).
    """
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"\n[Viral] Fetching Indian viral news — {today}")

    # Pass 1: cast a WIDE net — run several broad queries and pool results.
    # We don't bias to any specific category (cricket/bollywood/etc). Claude filters.
    queries = [
        "top trending news India today",
        "what Indians are talking about today social media",
        "most discussed story India past 24 hours",
        "India trending twitter X today",
    ]
    raw_items: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries:
        for item in _firecrawl_search(
            query=q,
            sources=["news", "web"],
            tbs="qdr:d",
            limit=8,
            location="in",
        ):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            raw_items.append(item)

    if not raw_items:
        print(f"[Viral] Empty Firecrawl result")
        return []
    print(f"[Viral] Pooled {len(raw_items)} unique results across {len(queries)} queries")

    # Pass 2: Claude picks the 3 topics actually driving engagement right now.
    # No category hints — let the data speak.
    system_prompt = (
        f"Today is {today}. You are given numbered web/news search results from India "
        "covering the past 24-48 hours.\n\n"
        "Your job: identify the TOP 3 TOPICS currently driving the MOST conversation "
        "and engagement across Indian social media (Twitter/X India, Instagram, YouTube, "
        "WhatsApp) right now.\n\n"
        "A topic qualifies only if ALL are true:\n"
        "1. It broke or peaked in the last 24-48 hours\n"
        "2. Ordinary Indians (not just one industry) are reacting to it\n"
        "3. Posting an opinion about it would get real engagement today\n\n"
        "Do NOT assume categories. The 3 winners could be anything — policy, sports, "
        "tech layoffs, a celebrity, a startup scandal, a weather event, an economic shift, "
        "a viral video, whatever is actually dominating feeds. Pick what the DATA shows, "
        "not what you'd expect.\n\n"
        "Reject: evergreen explainers, listicles, pure promo, week-old stories, niche "
        "industry news that only insiders discuss.\n\n"
        "If two results cover the same topic, pick the one with the strongest source and "
        "merge context — don't return duplicates.\n\n"
        "Return ONLY a JSON array (no markdown, no backticks):\n"
        '[{"index": <int from input>, '
        '"headline": "the specific topic as a headline", '
        '"summary": "two sentences — what happened + why India is reacting / what the debate is"}]'
    )

    try:
        picks = await _claude_pick(raw_items, system_prompt, max_items=3, max_tokens=700)
    except Exception as e:
        print(f"[Viral] Claude picking failed: {e}")
        return []

    items = _number_items(picks, start_num=1, item_type="viral")
    print(f"[Viral] Done — {len(items)} items, {len([i for i in items if i['source_url']])} with URLs")
    return items

# ── Step 2: Fetch 5 niche news items for founder ─────────────────────────────

async def fetch_niche_news(founder: dict, start_num: int) -> list[dict]:
    """
    Two-pass fetch for 5 industry-specific news items for this founder.
    """
    name     = founder["name"]
    profile  = founder["profile_text"][:1200]
    industry = founder.get("industry", "") or "business"
    today    = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"\n[Niche] Fetching for {name} ({industry})")

    # Pass 1: Firecrawl, biased to industry + India + last week
    query = f"{industry} news India founders trends report this week"
    raw_items = _firecrawl_search(
        query=query,
        sources=["news", "web"],
        tbs="qdr:w",
        limit=15,
        location="in",
    )
    if not raw_items:
        print(f"[Niche] Empty Firecrawl result for {name}")
        return []

    # Pass 2: Claude picks the 5 most relevant for THIS founder
    system_prompt = (
        f"Today is {today}.\n\n"
        f"FOUNDER CONTEXT:\n{profile}\n\n"
        "You are given numbered web/news search results. Pick the 5 items most relevant "
        "to THIS founder's industry, audience, and expertise. Prefer a mix of:\n"
        "- Breaking news or new developments in their sector\n"
        "- Industry data, reports, or research published recently\n"
        "- Competitor moves or market shifts they should know\n"
        "- Platform changes relevant to their work\n"
        "- Trends their target audience is actively discussing\n\n"
        "Skip generic / promotional / evergreen results.\n\n"
        "Return ONLY a JSON array (no markdown, no backticks, nothing else):\n"
        '[{"index": <int from input>, "headline": "specific real headline", '
        '"summary": "one sentence why this matters for this founder"}]'
    )

    try:
        picks = await _claude_pick(raw_items, system_prompt, max_items=5, max_tokens=900)
    except Exception as e:
        print(f"[Niche] Claude picking failed for {name}: {e}")
        return []

    items = _number_items(picks, start_num=start_num, item_type="niche")
    print(f"[Niche] Done — {len(items)} items, {len([i for i in items if i['source_url']])} with URLs")
    return items

# ── Step 3: Generate 2 content ideas (Claude only, no search needed) ─────────

async def generate_content_ideas(founder: dict, start_num: int) -> list[dict]:
    """
    Claude generates 2 strategic LinkedIn post ideas based on founder's profile.
    No URLs needed — these are generated ideas, not news items.
    """
    name    = founder["name"]
    profile = founder["profile_text"][:2000]
    print(f"\n[Ideas] Generating for {name}")

    prompt = (
        f"You are a LinkedIn content strategist.\n\n"
        f"FOUNDER PROFILE:\n{profile}\n\n"
        f"Generate 2 specific, high-quality LinkedIn post IDEAS for this founder.\n\n"
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
    for item in items[:2]:
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
        for chunk in chunks:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": chunk}
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

    # Fetch Indian viral news once — same 3 items for ALL founders
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