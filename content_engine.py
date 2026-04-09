"""
content_engine.py — Daily News Digest Engine

What it does (and ONLY this):
  1. Fetches last 24h trending news from the internet
  2. Filters news relevant to each founder's sector/industry
  3. Sends 4-6 news items to each founder via Telegram
  4. Writes news_digest.json so bot.py knows what was sent

Run daily at 7AM:
  Windows Task Scheduler: python content_engine.py
  Railway / cron:         0 7 * * * python content_engine.py

What it does NOT do:
  - No scoring
  - No post generation
  - No draft writing
  All of that is handled by bot.py + model.py after the founder responds
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from notion_client import Client as NotionClient

load_dotenv()

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-5")
SEARCH_MODEL       = os.environ.get("SEARCH_MODEL", "perplexity/sonar")

NEWS_DIGEST_FILE   = "news_digest.json"  # Shared with bot.py

notion = NotionClient(auth=NOTION_TOKEN)


# ── AI call ───────────────────────────────────────────────────────────────────

async def call_ai(prompt: str, model: str = None, max_tokens: int = 2000) -> str:
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://founderbot.app",
                "X-Title": "Founder News Engine",
            },
            json={
                "model": model or CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# ── Load all founders from Notion ─────────────────────────────────────────────

def get_all_founders() -> list[dict]:
    print("[Notion] Loading founders...")
    pages = notion.databases.query(database_id=NOTION_DATABASE_ID).get("results", [])
    founders = []

    for page in pages:
        name_prop   = page["properties"].get("Name", {}).get("title", [])
        name        = name_prop[0]["text"]["content"] if name_prop else None
        if not name:
            continue

        tg_prop     = page["properties"].get("TelegramID", {}).get("rich_text", [])
        telegram_id = tg_prop[0]["text"]["content"] if tg_prop else None

        # Read profile to extract sector/industry context
        page_id = page["id"]
        blocks  = notion.blocks.children.list(block_id=page_id).get("results", [])
        lines   = []
        for b in blocks:
            bt = b.get("type", "")
            if bt in ("paragraph", "heading_2", "heading_3"):
                for r in b[bt].get("rich_text", []):
                    t = r.get("text", {}).get("content", "").strip()
                    if t and len(t) > 10:
                        lines.append(t)

        profile_text = "\n".join(lines[:40])  # First 40 lines = enough context

        founders.append({
            "name":         name,
            "telegram_id":  telegram_id,
            "page_id":      page_id,
            "profile_text": profile_text,
        })
        status = "✓" if telegram_id else "no TelegramID"
        print(f"  {name} — {status}")

    return founders


# ── Fetch trending news ───────────────────────────────────────────────────────

async def fetch_trending_news() -> str:
    """Get real 24h trending news using web search model."""
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"[News] Fetching trending news for {today}...")

    prompt = (
        f"Today is {today}. Search the internet and find the top 20 trending news stories "
        f"from the LAST 24 HOURS across these sectors:\n"
        f"- Business and entrepreneurship\n"
        f"- AI and technology\n"
        f"- Marketing and growth\n"
        f"- Startups and funding\n"
        f"- Indian business ecosystem\n"
        f"- Leadership and management\n"
        f"- Social media and content\n"
        f"- Finance and economy\n\n"
        f"For each story give:\n"
        f"- Headline (clear and specific)\n"
        f"- Sector (which category above)\n"
        f"- 1-sentence summary\n\n"
        f"Use REAL headlines from today. Be specific — not generic topics."
    )

    try:
        news = await call_ai(prompt, model=SEARCH_MODEL, max_tokens=3000)
        print(f"[News] Got {len(news)} chars from {SEARCH_MODEL}")
        return news
    except Exception as e:
        print(f"[News] Search model failed ({e}) — using Claude fallback")
        fallback = (
            f"Today is {today}. List 20 specific trending news stories that "
            f"entrepreneurs and founders are discussing this week. Cover: AI, startups, "
            f"marketing, Indian business, leadership, social media. Be as current and "
            f"specific as possible with real headlines."
        )
        return await call_ai(fallback, max_tokens=2000)


# ── Filter news for a specific founder ───────────────────────────────────────

async def filter_news_for_founder(founder: dict, all_news: str) -> list[dict]:
    """
    From the full news list, select 5-6 most relevant items for this founder
    based on their profile and sector.
    Returns a list of {number, headline, sector, summary} dicts.
    """
    name    = founder["name"]
    profile = founder["profile_text"]

    print(f"[Filter] Selecting news for {name}...")

    prompt = (
        f"You are selecting news for a specific founder.\n\n"
        f"FOUNDER PROFILE:\n{profile[:1500]}\n\n"
        f"ALL TODAY'S NEWS:\n{all_news}\n\n"
        f"Select the 5 to 6 news items that are MOST relevant to this founder "
        f"based on their industry, expertise, audience, and what they talk about.\n\n"
        f"Also include 1-2 wildcard items — news from adjacent sectors that a "
        f"smart founder in their space should know about.\n\n"
        f"Return ONLY a JSON array, no markdown, no backticks:\n"
        f'[{{"headline": "...", "sector": "...", "summary": "one sentence"}}]'
    )

    result = await call_ai(prompt, max_tokens=1000)
    match  = re.search(r'\[.*\]', result, re.DOTALL)
    if not match:
        # Fallback: return raw news split into items
        print(f"[Filter] JSON parse failed — using raw news")
        return []

    try:
        items = json.loads(match.group())
        # Number them
        for i, item in enumerate(items, 1):
            item["number"] = i
        print(f"[Filter] Selected {len(items)} items for {name}")
        return items[:6]
    except json.JSONDecodeError:
        return []


# ── Generate business content ideas ──────────────────────────────────────────

async def generate_business_ideas(founder: dict, start_num: int, num: int = 3) -> list[dict]:
    """
    Generate content ideas based on the founder's business and profile.
    Returns list of {number, headline, category, why} dicts.
    """
    name    = founder["name"]
    profile = founder["profile_text"]

    print(f"[Ideas] Generating {num} business ideas for {name}...")

    prompt = (
        f"You are a LinkedIn content strategist.\n\n"
        f"FOUNDER PROFILE:\n{profile[:2000]}\n\n"
        f"Generate {num} specific LinkedIn post IDEAS for this founder based on their "
        f"business, expertise, and what their audience needs right now.\n\n"
        f"Each idea should:\n"
        f"- Be specific to their business/industry (not generic)\n"
        f"- Be something they have unique authority to write about\n"
        f"- Have a compelling angle that resonates with their audience\n\n"
        f"Return ONLY a JSON array, no markdown, no backticks:\n"
        f'[{{"headline": "Compelling content prompt as a statement or question", '
        f'"category": "Lesson/Story/Insight/Contrarian/How-To", '
        f'"why": "one line — why this resonates for their audience now"}}]'
    )

    result = await call_ai(prompt, max_tokens=800)
    match  = re.search(r'\[.*\]', result, re.DOTALL)
    if not match:
        print(f"[Ideas] JSON parse failed for {name}")
        return []

    try:
        items = json.loads(match.group())
        for i, item in enumerate(items[:num], start_num):
            item["number"] = i
        print(f"[Ideas] Generated {len(items[:num])} ideas for {name}")
        return items[:num]
    except json.JSONDecodeError:
        return []


# ── Format digest message ─────────────────────────────────────────────────────

def format_digest(founder_name: str, news_items: list[dict],
                  business_ideas: list[dict]) -> str:
    today     = datetime.now(timezone.utc).strftime("%A, %B %d")
    total     = len(news_items) + len(business_ideas)
    lines     = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Good morning {founder_name}!",
        f"{today}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
    ]

    if news_items:
        lines.append("📰 TRENDING TODAY\n")
        for item in news_items:
            n        = item.get("number", "")
            headline = item.get("headline", "")
            sector   = item.get("sector", "")
            summary  = item.get("summary", "")
            lines.append(f"{n}. {headline}")
            tag = f"[{sector}] " if sector else ""
            if summary:
                lines.append(f"   {tag}{summary}")
            lines.append("")

    if business_ideas:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 YOUR BUSINESS IDEAS\n")
        for item in business_ideas:
            n        = item.get("number", "")
            headline = item.get("headline", "")
            category = item.get("category", "")
            why      = item.get("why", "")
            lines.append(f"{n}. {headline}")
            tag = f"[{category}] " if category else ""
            if why:
                lines.append(f"   {tag}{why}")
            lines.append("")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Reply with a number (1–{total}) to pick a topic.",
        "Then send a voice note or text with your angle.",
        "━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


# ── Send Telegram message ─────────────────────────────────────────────────────

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


# ── Write news_digest.json for bot.py ────────────────────────────────────────

def write_news_digest(telegram_id: str, founder_name: str,
                      news_items: list[dict], business_ideas: list[dict] = None):
    """
    Write sent items to news_digest.json.
    bot.py reads this when a founder replies with a number.
    """
    try:
        existing = {}
        if Path(NEWS_DIGEST_FILE).exists():
            with open(NEWS_DIGEST_FILE, "r") as f:
                existing = json.load(f)
    except Exception:
        existing = {}

    existing[str(telegram_id)] = {
        "founder_name":   founder_name,
        "sent_at":        datetime.now(timezone.utc).isoformat(),
        "news_items":     news_items,                      # {number, headline, sector, summary}
        "business_ideas": business_ideas or [],            # {number, headline, category, why}
    }

    with open(NEWS_DIGEST_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"[Digest] Written news_digest.json for {founder_name} "
          f"({len(news_items)} news + {len(business_ideas or [])} ideas)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    print("=" * 50)
    print("FOUNDER NEWS DIGEST ENGINE")
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 50)

    founders = get_all_founders()
    if not founders:
        print("No founders found in Notion DB.")
        return

    # Fetch all news once — filter per founder
    all_news = await fetch_trending_news()
    print(f"\n[News] Fetched. Sample:\n{all_news[:300]}...\n")

    for founder in founders:
        name = founder["name"]
        print(f"\n{'─' * 40}")
        print(f"Processing: {name}")
        print(f"{'─' * 40}")

        if not founder["telegram_id"]:
            print(f"[Skip] No TelegramID for {name}")
            continue

        # Filter trending news for this founder's sector
        news_items = await filter_news_for_founder(founder, all_news)

        if not news_items:
            # Fallback: raw news items numbered manually
            lines    = all_news.strip().split("\n")
            numbered = [l for l in lines if l.strip() and len(l) > 20][:5]
            news_items = [
                {"number": i+1, "headline": l.strip(), "sector": "", "summary": ""}
                for i, l in enumerate(numbered)
            ]

        # Generate business content ideas (numbered after news)
        start_biz  = len(news_items) + 1
        biz_ideas  = await generate_business_ideas(founder, start_num=start_biz, num=3)

        # Format and send
        digest_msg = format_digest(name, news_items, biz_ideas)
        await send_telegram(founder["telegram_id"], digest_msg)

        # Write to news_digest.json for bot.py
        write_news_digest(founder["telegram_id"], name, news_items, biz_ideas)

        await asyncio.sleep(2)

    print(f"\n{'=' * 50}")
    print("DIGEST SENT TO ALL FOUNDERS")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    asyncio.run(run())