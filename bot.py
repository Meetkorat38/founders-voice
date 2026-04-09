"""
Founder Voice Bot — FINAL (two-file version)
model.py is merged in here — no separate file needed.

Flow:
  1. content_engine.py sends 4-6 news items at 7AM
  2. Founder replies with a number (1-6) to pick a topic
  3. Founder sends voice note or text with their angle/idea
  4. Bot generates LinkedIn post using their Notion profile + their idea
  5. APPROVE / EDIT / NEXT / SHORTER / LONGER

Run: python bot.py
"""

import asyncio
import io
import json
import os
import random
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from notion_client import Client as NotionClient
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_API_KEY  = os.environ["OPENROUTER_API_KEY"]
NOTION_TOKEN        = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID  = os.environ["NOTION_DATABASE_ID"]
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-5")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# n8n webhook — fires after founder approves a post → triggers HeyGen video pipeline
N8N_HEYGEN_WEBHOOK_URL = os.environ.get("N8N_HEYGEN_WEBHOOK_URL", "")

# Optional: GIF/animation shown on /start to teach users how to send voice notes
# Set WELCOME_GIF in .env to a local file path OR a public URL of the GIF
WELCOME_GIF = os.environ.get("WELCOME_GIF", "")

NEWS_DIGEST_FILE = "news_digest.json"    # Written by content_engine.py
PENDING_FILE     = "pending_drafts.json" # Written by this bot

USE_VOICE = False
el        = None

if ELEVENLABS_API_KEY:
    try:
        from elevenlabs.client import ElevenLabs
        el        = ElevenLabs(api_key=ELEVENLABS_API_KEY)
        USE_VOICE = True
        print("ElevenLabs — voice ON")
    except Exception as e:
        print(f"ElevenLabs failed ({e}) — text only")

notion = NotionClient(auth=NOTION_TOKEN)

# ── State ─────────────────────────────────────────────────────────────────────

sessions:        dict[int, dict] = {}  # interview sessions
pending_drafts:  dict[int, dict] = {}  # drafts waiting for APPROVE/EDIT
news_selections: dict[int, dict] = {}  # selected news topic, waiting for idea

# ── Telegram command menu ─────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("start",    "Begin your content profile interview"),
    BotCommand("help",     "Show all commands"),
    BotCommand("status",   "Check your current status"),
    BotCommand("profile",  "View your personality profile"),
    BotCommand("draft",    "Show your current post draft"),
    BotCommand("approve",  "Save and approve the draft"),
    BotCommand("next",     "Get a different version"),
    BotCommand("shorter",  "Make the draft shorter"),
    BotCommand("longer",   "Expand the draft"),
    BotCommand("skip",     "Skip this topic"),
    BotCommand("reset",    "Restart your profile session"),
]

HELP_TEXT = """━━━━━━━━━━━━━━━━━━━━━━━━━
FOUNDER VOICE — COMMANDS
━━━━━━━━━━━━━━━━━━━━━━━━━

/start    — Begin content profile interview
/status   — What's happening right now
/profile  — See your personality & writing profile
/draft    — View draft or today's content ideas
/approve  — Save draft to Notion
/next     — Get a completely different version
/shorter  — Cut the draft down
/longer   — Add more depth
/skip     — Skip this topic
/reset    — Restart your profile interview

━━━━━━━━━━━━━━━━━━━━━━━━━
HOW IT WORKS EACH MORNING
━━━━━━━━━━━━━━━━━━━━━━━━━

📰 TRENDING (1–5)  — Top news in your sector
💡 YOUR IDEAS (6–8) — Based on your business

Pick a number → send your take (voice or text)
→ get your LinkedIn draft in seconds
→ APPROVE to save, or give feedback to refine"""

# ── File helpers ──────────────────────────────────────────────────────────────

def load_pending():
    global pending_drafts
    if Path(PENDING_FILE).exists():
        try:
            with open(PENDING_FILE, "r") as f:
                data = json.load(f)
            pending_drafts = {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"[Drafts] Load error: {e}")


def save_pending():
    try:
        with open(PENDING_FILE, "w") as f:
            json.dump({str(k): v for k, v in pending_drafts.items()}, f, indent=2)
    except Exception as e:
        print(f"[Drafts] Save error: {e}")


def get_draft(uid: int) -> dict | None:
    load_pending()
    return pending_drafts.get(uid)


def clear_draft(uid: int):
    pending_drafts.pop(uid, None)
    save_pending()


def get_today_digest(uid: int) -> dict:
    """Read full digest (trending news + business ideas) sent this morning."""
    if not Path(NEWS_DIGEST_FILE).exists():
        return {"news_items": [], "business_ideas": []}
    try:
        with open(NEWS_DIGEST_FILE, "r") as f:
            data = json.load(f)
        entry = data.get(str(uid), {})
        return {
            "news_items":     entry.get("news_items", []),
            "business_ideas": entry.get("business_ideas", []),
        }
    except Exception:
        return {"news_items": [], "business_ideas": []}


def get_today_news(uid: int) -> list[dict]:
    """Backwards-compat: return news_items only."""
    return get_today_digest(uid)["news_items"]


def draft_keyboard() -> InlineKeyboardMarkup:
    """Inline action buttons shown below every generated draft."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve",  callback_data="APPROVE"),
            InlineKeyboardButton("⏭ Next",     callback_data="NEXT"),
        ],
        [
            InlineKeyboardButton("✂️ Shorter",  callback_data="SHORTER"),
            InlineKeyboardButton("📝 Longer",   callback_data="LONGER"),
            InlineKeyboardButton("⏩ Skip",     callback_data="SKIP"),
        ],
    ])

# ── ElevenLabs voice ──────────────────────────────────────────────────────────

async def tts(text: str) -> bytes | None:
    global USE_VOICE
    if not USE_VOICE or not el:
        return None
    try:
        audio = el.text_to_speech.convert(
            voice_id=ELEVENLABS_VOICE_ID,
            text=text,
            model_id="eleven_multilingual_v2",
        )
        buf = io.BytesIO()
        for chunk in audio:
            buf.write(chunk)
        return buf.getvalue()
    except Exception as e:
        err = str(e)
        if "voice_not_found" in err or "404" in err:
            print(f"[TTS] Voice ID not found — disabling voice.")
            print(f"[TTS] Fix in .env: ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM")
            USE_VOICE = False
        else:
            print(f"[TTS] {err[:100]}")
        return None


async def stt(ogg: bytes) -> str | None:
    if not USE_VOICE or not el:
        return None
    try:
        buf      = io.BytesIO(ogg)
        buf.name = "audio.ogg"
        return el.speech_to_text.convert(file=buf, model_id="scribe_v1").text.strip()
    except Exception as e:
        print(f"[STT] {e}")
        return None


async def send_reply(update: Update, text: str):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        await update.message.reply_text(chunk)

# ── Claude API call ───────────────────────────────────────────────────────────

async def call_claude(messages: list, system: str = None, max_tokens: int = 1500) -> str:
    msgs = [{"role": "system", "content": system}, *messages] if system else messages
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://founderbot.app",
                "X-Title": "Founder Bot",
            },
            json={"model": CLAUDE_MODEL, "max_tokens": max_tokens, "messages": msgs},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

# ── Notion helpers ────────────────────────────────────────────────────────────

def _txt(c: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": c[:1900]}}]}}

def _h2(c: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": c}}]}}

def _h3(c: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": c}}]}}

def _div() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def auto_save_telegram_id(name: str, uid: int):
    """Find founder by name in Notion, save TelegramID if missing."""
    try:
        pages = notion.databases.query(database_id=NOTION_DATABASE_ID).get("results", [])
        for page in pages:
            title = page["properties"].get("Name", {}).get("title", [])
            pname = title[0]["text"]["content"] if title else ""
            if pname.lower() == name.lower():
                tg = page["properties"].get("TelegramID", {}).get("rich_text", [])
                if tg:
                    return
                notion.pages.update(
                    page_id=page["id"],
                    properties={"TelegramID": {"rich_text": [{"text": {"content": str(uid)}}]}}
                )
                print(f"[Notion] TelegramID {uid} saved for {name}")
                return
    except Exception as e:
        print(f"[Notion] TelegramID save error: {e}")


def get_founder_profile_from_notion(uid: int) -> dict:
    """
    Read full founder profile from Notion by TelegramID.
    Returns { name, page_id, profile_text, tov_json }
    """
    try:
        pages = notion.databases.query(database_id=NOTION_DATABASE_ID).get("results", [])
        for page in pages:
            tg = page["properties"].get("TelegramID", {}).get("rich_text", [])
            if not tg or tg[0]["text"]["content"] != str(uid):
                continue

            title = page["properties"].get("Name", {}).get("title", [])
            name  = title[0]["text"]["content"] if title else "Founder"

            blocks = notion.blocks.children.list(block_id=page["id"]).get("results", [])
            lines, tov_json = [], {}

            for b in blocks:
                bt = b.get("type", "")
                if bt not in ("paragraph", "heading_2", "heading_3", "quote"):
                    continue
                for r in b[bt].get("rich_text", []):
                    text = r.get("text", {}).get("content", "").strip()
                    if not text:
                        continue
                    lines.append(text)
                    # Extract TOV JSON if stored
                    if text.startswith('{"avg_sentence') or text.startswith('{"uses_emojis'):
                        try:
                            tov_json = json.loads(text)
                        except Exception:
                            pass

            return {
                "name":         name,
                "page_id":      page["id"],
                "profile_text": "\n".join(lines),
                "tov_json":     tov_json,
            }
    except Exception as e:
        print(f"[Notion] Profile read error: {e}")

    return {"name": "Founder", "page_id": None, "profile_text": "", "tov_json": {}}


def save_profile_to_notion(name: str, profile: dict, transcript: list, uid: int) -> str:
    dims     = profile.get("dimensions", {})
    arch     = profile.get("archetype_blend", {})
    tov      = profile.get("tov", {})
    business = profile.get("business", {})

    arch_line = (
        f"{arch.get('primary','—')} ({int(float(arch.get('primary_weight',0))*100)}%) + "
        f"{arch.get('secondary','—')} ({int(float(arch.get('secondary_weight',0))*100)}%)"
    )
    dims_text = "\n".join(f"  {k.replace('_',' ').title()}: {v}/100" for k, v in dims.items())
    tov_text  = (
        f"Hook: {tov.get('hook_style','—')}\n"
        f"CTA: {tov.get('cta_style','—')}\n"
        f"Sentence length: {tov.get('sentence_length','—')}\n"
        f"Vocabulary: {tov.get('vocabulary','—')}\n"
        f"Topics: {', '.join(tov.get('topics', []))}"
    )
    biz_text = (
        f"Company: {business.get('company_name','—')}\n"
        f"What they do: {business.get('what_they_do','—')}\n"
        f"Industry: {business.get('industry','—')}\n"
        f"Target audience: {business.get('target_audience','—')}\n"
        f"Unique angle: {business.get('unique_angle','—')}"
    ) if business else "Not captured"
    tx_text = "\n".join(
        f"{'Founder' if m['role']=='user' else 'Bot'}: {m['content']}"
        for m in transcript
    )

    children = [
        _h2("Business Information"), _txt(biz_text),    _div(),
        _h2("Archetype blend"),      _txt(arch_line),   _div(),
        _h2("Personality dimensions"), _txt(dims_text), _div(),
        _h2("Tone of voice"),        _txt(tov_text),    _div(),
        _h2("Full transcript"),      _txt(tx_text[:1900]),
    ]

    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "Name":       {"title": [{"text": {"content": name}}]},
            "TelegramID": {"rich_text": [{"text": {"content": str(uid)}}]},
        },
        children=children
    )
    url = f"https://notion.so/{page['id'].replace('-','')}"
    print(f"[Notion] Profile saved → {url}")
    return url


def save_approved_post_to_notion(page_id: str, news_topic: str,
                                  founder_idea: str, post_text: str):
    today = datetime.utcnow().strftime("%B %d, %Y")
    notion.blocks.children.append(
        block_id=page_id,
        children=[
            _div(),
            _h3(f"Approved post — {today}"),
            _txt(f"News hook: {news_topic}"),
            _txt(f"Founder idea: {founder_idea[:300]}"),
            {"object": "block", "type": "quote",
             "quote": {"rich_text": [{"type": "text", "text": {"content": post_text[:1900]}}]}},
        ]
    )
    print(f"[Notion] Post saved to {page_id[:8]}...")


# ── HeyGen pipeline webhook ───────────────────────────────────────────────────

async def trigger_heygen_pipeline(payload: dict):
    """
    Fire-and-forget POST to n8n webhook.
    n8n receives this and runs: HeyGen → ElevenLabs → merge → subtitles → Sheet.

    Payload keys (all sent in JSON body — n8n reads them via {{ $json.key }}):
      founder_name   — e.g. "Rahul"
      founder_id     — Telegram user ID (int)
      post_text      — the approved LinkedIn post text
      news_topic     — the headline/topic that was picked
      notion_page_id — Notion page ID where the post was saved
      approved_at    — ISO timestamp of approval
    """
    if not N8N_HEYGEN_WEBHOOK_URL:
        print("[HeyGen] N8N_HEYGEN_WEBHOOK_URL not set — skipping webhook")
        return

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                N8N_HEYGEN_WEBHOOK_URL,
                json=payload,                          # sent as JSON body
                headers={"Content-Type": "application/json"},
            )
        if r.status_code in (200, 202):
            print(f"[HeyGen] Webhook sent ✓ (HTTP {r.status_code}) for {payload.get('founder_name')}")
        else:
            print(f"[HeyGen] Webhook returned {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[HeyGen] Webhook failed: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# POST GENERATION (was model.py — merged here)
# ════════════════════════════════════════════════════════════════════════════════

def _build_tov_instruction(tov: dict) -> str:
    """Build writing style instruction from TOV JSON."""
    if not tov:
        return ""
    return (
        f"\nMATCH THEIR EXACT WRITING STYLE:\n"
        f"- Avg sentence: ~{tov.get('avg_sentence_words', tov.get('sentence_length', 'short'))} words\n"
        f"- Post length: ~{tov.get('avg_post_words', 130)} words\n"
        f"- Emojis: {'yes sparingly' if tov.get('uses_emojis') else 'NO emojis'}\n"
        f"- Hashtags: {'yes at end' if tov.get('uses_hashtags') else 'NO hashtags'}\n"
        f"- Bullets: {'yes' if tov.get('uses_bullet_points') or tov.get('uses_bullets') else 'NO bullets'}\n"
        f"- Line breaks between sentences: yes\n"
        f"- Power words: {', '.join(tov.get('power_words', [])[:5])}\n"
        f"- Never: {', '.join(tov.get('what_they_never_do', [])[:3])}\n"
        f"- Vocabulary: {tov.get('vocabulary_level', tov.get('vocabulary', 'conversational'))}\n"
    )


async def generate_post(
    uid: int,
    news_topic: str,
    founder_idea: str,
    existing_draft: str = "",
    edit_instruction: str = "",
    is_business_idea: bool = False,
) -> tuple[str, dict]:
    """
    Generate or regenerate a LinkedIn post.
    Returns (post_text, founder_profile).
    """
    profile = get_founder_profile_from_notion(uid)
    name    = profile["name"]
    tov_ins = _build_tov_instruction(profile["tov_json"])

    print(f"[Generate] {name} | topic: {news_topic[:50]} | idea: {founder_idea[:60]}")

    if existing_draft and edit_instruction:
        prompt = (
            f"Ghostwrite a LinkedIn post for {name}.\n\n"
            f"PROFILE:\n{profile['profile_text'][:2500]}\n{tov_ins}\n"
            f"TOPIC: {news_topic}\n"
            f"THEIR IDEA: {founder_idea}\n\n"
            f"CURRENT DRAFT:\n{existing_draft}\n\n"
            f"EDIT: {edit_instruction}\n\n"
            f"Rewrite following the edit. Keep their exact voice.\n"
            f"Output ONLY the post text."
        )
    elif is_business_idea:
        prompt = (
            f"Ghostwrite a LinkedIn post for {name}.\n\n"
            f"THEIR PROFILE (personality, archetype, tone of voice):\n"
            f"{profile['profile_text'][:2500]}\n{tov_ins}\n"
            f"CONTENT IDEA: {news_topic}\n\n"
            f"FOUNDER'S ANGLE:\n\"{founder_idea}\"\n\n"
            f"Write a LinkedIn post that:\n"
            f"- Draws from their own business experience and expertise\n"
            f"- Expresses the founder's angle in their own authentic voice\n"
            f"- Sounds like THEM — not generic AI\n"
            f"- Provides real value to their specific audience\n\n"
            f"Rules:\n"
            f"- Strong hook on line 1\n"
            f"- Short paragraphs, white space\n"
            f"- End with a thought or question — not a hard sell\n"
            f"- Output ONLY the post text, ready to copy-paste"
        )
    else:
        prompt = (
            f"Ghostwrite a LinkedIn post for {name}.\n\n"
            f"THEIR PROFILE (personality, archetype, tone of voice):\n"
            f"{profile['profile_text'][:2500]}\n{tov_ins}\n"
            f"TODAY'S NEWS HOOK: {news_topic}\n\n"
            f"FOUNDER'S IDEA / ANGLE:\n\"{founder_idea}\"\n\n"
            f"Write a LinkedIn post that:\n"
            f"- Uses the news as the hook or context\n"
            f"- Expresses the founder's idea in their own voice\n"
            f"- Sounds like THEM — not generic AI\n"
            f"- Makes their audience stop and read\n\n"
            f"Rules:\n"
            f"- Strong hook on line 1\n"
            f"- Short paragraphs, white space\n"
            f"- End with a thought or question — not a hard sell\n"
            f"- Output ONLY the post text, ready to copy-paste"
        )

    post = await call_claude([{"role": "user", "content": prompt}], max_tokens=600)
    print(f"[Generate] Done — {len(post)} chars")
    return post, profile

# ════════════════════════════════════════════════════════════════════════════════
# INTERVIEW LOGIC
# ════════════════════════════════════════════════════════════════════════════════

INTERVIEW_SYSTEM = """\
You are conducting a friendly interview to build a founder's content personality profile.
RULES:
- Ask ONE question at a time. Max 2 sentences.
- Adapt each question based on their previous answer.
- Be warm and conversational.
- Naturally gather: what their company does, who they serve, what their unique angle is.
- Ask questions until you have enough information for a complete founder profile. Do NOT stop early.
- You may ask up to 15 questions maximum. Do NOT exceed 15 founder responses.
- Output the completion marker as soon as the profile is complete, even if fewer than 15 questions were asked.
- If you have reached 15 founder responses, output the completion marker regardless.

After profile is complete (or at 15 responses max), output EXACTLY this on its own line:
===INTERVIEW_COMPLETE===
Then on the next line output ONLY this JSON (no backticks, no markdown):
{"dimensions":{"philosophical":0,"hustle":0,"data_driven":0,"storytelling":0,"mission":0,"social_empathy":0},"archetype_blend":{"primary":"Naval Ravikant","primary_weight":0.7,"secondary":"Simon Sinek","secondary_weight":0.3},"tov":{"sentence_length":"short - avg 8 words","vocabulary":"clear and direct","hook_style":"bold statement","cta_style":"soft invitation","topics":["topic1","topic2","topic3"]},"business":{"company_name":"","what_they_do":"one line description","industry":"","target_audience":"","unique_angle":"what makes them different from competition"}}
Replace ALL values with real data extracted from the conversation. Dimension scores 0-100.
Archetypes: Naval Ravikant, Gary Vaynerchuk, Simon Sinek, Brene Brown, Balaji Srinivasan, Oprah Winfrey.
"""

OPENING = "Hey {name}! I'll ask you a few questions to understand how you think and communicate. No right or wrong answers — just be real. Ready?"
FIRST_Q = "Tell me the real reason you started your company — not the pitch, the actual reason."


def parse_profile(text: str) -> dict | None:
    if "===INTERVIEW_COMPLETE===" not in text:
        return None
    after = text.split("===INTERVIEW_COMPLETE===", 1)[1].strip()
    m = re.search(r'\{.*\}', after, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def build_summary(name: str, p: dict) -> str:
    arch     = p.get("archetype_blend", {})
    dims     = p.get("dimensions", {})
    tov      = p.get("tov", {})
    business = p.get("business", {})
    top3     = sorted(dims.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_str = ", ".join(f"{k.replace('_',' ')} ({v})" for k, v in top3)

    biz_line = ""
    if business.get("company_name"):
        biz_line = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR BUSINESS\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Company : {business.get('company_name','—')}\n"
            f"You do  : {business.get('what_they_do','—')}\n"
            f"Audience: {business.get('target_audience','—')}\n"
            f"Edge    : {business.get('unique_angle','—')}\n"
        )

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"PROFILE READY, {name.upper()}!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Archetype : {arch.get('primary','—')} ({int(float(arch.get('primary_weight',0))*100)}%)"
        f" + {arch.get('secondary','—')} ({int(float(arch.get('secondary_weight',0))*100)}%)\n"
        f"Strengths : {top3_str}\n"
        f"Hook style: {tov.get('hook_style','—')}\n"
        f"Topics    : {', '.join(tov.get('topics', []))}"
        f"{biz_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Morning digest arrives at 7AM.\n"
        f"You'll get trending news + 3 ideas based on your business.\n"
        f"Pick a number, send your take — I'll write your LinkedIn post."
    )


async def process_interview(update: Update, uid: int, text: str):
    session = sessions[uid]
    name    = session["name"]

    session["messages"].append({"role": "user", "content": text})
    session["transcript"].append({"role": "user", "content": text})
    await update.message.reply_chat_action("typing")

    user_turns = len([m for m in session["messages"] if m["role"] == "user"])
    if user_turns >= 15:
        session["messages"].append({
            "role": "user",
            "content": "[SYSTEM: Maximum questions reached. Output ===INTERVIEW_COMPLETE=== and the JSON profile now.]"
        })

    try:
        response = await call_claude(session["messages"], system=INTERVIEW_SYSTEM)
    except Exception as e:
        await update.message.reply_text(f"Error — try again. ({str(e)[:80]})")
        return

    session["messages"].append({"role": "assistant", "content": response})
    session["transcript"].append({"role": "assistant", "content": response})

    if "===INTERVIEW_COMPLETE===" in response:
        session["done"] = True
        await update.message.reply_text("Building your profile...")

        profile = parse_profile(response)
        if not profile:
            await update.message.reply_text("Send /reset and try again.")
            return

        try:
            url = save_profile_to_notion(name, profile, session["transcript"], uid)
            await send_reply(update, build_summary(name, profile))
            await update.message.reply_text(f"Profile saved: {url}")
        except Exception as e:
            await send_reply(update, build_summary(name, profile))
            await update.message.reply_text(
                f"Profile built! Notion save failed: {str(e)[:100]}\n"
                "Check your Notion integration is connected."
            )
        return

    await send_reply(update, response.strip())

# ════════════════════════════════════════════════════════════════════════════════
# NEWS + POST INTERACTION
# ════════════════════════════════════════════════════════════════════════════════

async def handle_news_selection(update: Update, uid: int, text: str) -> bool:
    """Detect number reply (1-9), store selected topic (trending or business idea)."""
    if not re.match(r'^[1-9]$', text.strip()):
        return False

    digest     = get_today_digest(uid)
    news_items = digest["news_items"]
    biz_ideas  = digest["business_ideas"]
    total      = len(news_items) + len(biz_ideas)

    if not news_items and not biz_ideas:
        return False

    num = int(text.strip())

    if num > total:
        await update.message.reply_text(
            f"I sent {total} topics today (1–{total}).\n"
            f"Reply with a number in that range."
        )
        return True

    # Determine if trending or business idea
    if num <= len(news_items):
        selected = next((n for n in news_items if n.get("number") == num), None)
        if not selected:
            selected = news_items[num - 1]
        is_business = False
        label       = selected.get("sector", "Trending")
        prompt_hint = "What's your take on this? Your angle, opinion, or a story from your experience."
    else:
        biz_idx  = num - len(news_items) - 1
        selected = biz_ideas[biz_idx] if biz_idx < len(biz_ideas) else None
        if not selected:
            await update.message.reply_text(f"Couldn't find topic {num}. Try again.")
            return True
        is_business = True
        label       = selected.get("category", "Your Idea")
        prompt_hint = "What's your personal take on this? Share your experience, lesson, or story."

    news_selections[uid] = {
        "headline":         selected.get("headline", ""),
        "sector":           label,
        "is_business_idea": is_business,
        "waiting_for_idea": True,
    }

    print(f"[Bot] {uid} selected #{num}: {selected.get('headline','')[:50]} (business={is_business})")

    type_tag = "YOUR IDEA" if is_business else "TRENDING"
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"TOPIC SELECTED [{type_tag}]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{selected.get('headline', '')}\n\n"
        f"{prompt_hint}\n\n"
        f"Hold the mic button to record a voice note\n"
        f"or just type your thoughts below."
    )
    return True


async def handle_founder_idea(update: Update, uid: int, idea_text: str) -> bool:
    """Generate post once founder sends their idea."""
    sel = news_selections.get(uid)
    if not sel or not sel.get("waiting_for_idea"):
        return False

    news_topic       = sel["headline"]
    is_business_idea = sel.get("is_business_idea", False)
    print(f"[Bot] Idea received from {uid}: {idea_text[:80]}")

    await update.message.reply_text("Writing your post...")
    await update.message.reply_chat_action("typing")

    try:
        post_text, profile = await generate_post(
            uid=uid,
            news_topic=news_topic,
            founder_idea=idea_text,
            is_business_idea=is_business_idea,
        )
    except Exception as e:
        print(f"[Generate] Failed: {e}")
        await update.message.reply_text(f"Generation failed: {str(e)[:100]}\nTry again.")
        return True

    pending_drafts[uid] = {
        "founder_name":    profile.get("name", ""),
        "page_id":         profile.get("page_id"),
        "news_topic":      news_topic,
        "founder_idea":    idea_text,
        "current_draft":   post_text,
        "is_business_idea": is_business_idea,
        "version":         1,
    }
    save_pending()
    news_selections.pop(uid, None)

    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"YOUR LINKEDIN POST  v1\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{post_text}",
        reply_markup=draft_keyboard(),
    )
    return True


async def handle_draft_reply(update: Update, uid: int, text: str) -> bool:
    """Handle APPROVE/SKIP/NEXT/SHORTER/LONGER and free text edits."""
    draft = get_draft(uid)
    if not draft:
        return False

    cmd = text.strip().upper()

    def _draft_msg(label: str, post: str, ver: int) -> str:
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR LINKEDIN POST  v{ver}  [{label}]\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{post}"
        )

    is_biz = draft.get("is_business_idea", False)

    # APPROVE
    if cmd in ("APPROVE", "/APPROVE"):
        await update.message.reply_text("Saving to Notion...")
        try:
            if draft.get("page_id"):
                save_approved_post_to_notion(
                    draft["page_id"], draft.get("news_topic", ""),
                    draft.get("founder_idea", ""), draft.get("current_draft", "")
                )
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "POST APPROVED ✅\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Saved to Notion.\n"
                "🎬 Sending to video pipeline..."
            )
        except Exception as e:
            await update.message.reply_text(f"Approved! (Notion error: {str(e)[:80]})")

        # Fire webhook to n8n — non-blocking, runs in background
        asyncio.create_task(trigger_heygen_pipeline({
            "founder_name":   draft.get("founder_name", ""),
            "founder_id":     uid,
            "post_text":      draft.get("current_draft", ""),
            "news_topic":     draft.get("news_topic", ""),
            "notion_page_id": draft.get("page_id", ""),
            "approved_at":    datetime.utcnow().isoformat(),
        }))

        clear_draft(uid)
        return True

    # SKIP
    if cmd in ("SKIP", "/SKIP"):
        clear_draft(uid)
        await update.message.reply_text(
            "Skipped.\nReply with a different topic number or wait for tomorrow's digest."
        )
        return True

    # NEXT
    if cmd in ("NEXT", "/NEXT"):
        await update.message.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Write a completely different version — different hook, different angle, same topic and idea.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:80]}")
            return True
        draft["current_draft"] = new_post
        draft["version"]       = draft.get("version", 1) + 1
        pending_drafts[uid]    = draft
        save_pending()
        await update.message.reply_text(_draft_msg("NEW VERSION", new_post, draft["version"]), reply_markup=draft_keyboard())
        return True

    # SHORTER
    if cmd in ("SHORTER", "/SHORTER"):
        await update.message.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Make it shorter — cut at least 30% of words. Keep the core message and voice.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:80]}")
            return True
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await update.message.reply_text(_draft_msg("SHORTER", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())
        return True

    # LONGER
    if cmd in ("LONGER", "/LONGER"):
        await update.message.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Expand it — add a personal story or example, more depth. Keep their voice.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:80]}")
            return True
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await update.message.reply_text(_draft_msg("EXPANDED", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())
        return True

    # Free text edit feedback
    if len(text.strip()) > 3 and not text.startswith("/"):
        await update.message.reply_chat_action("typing")
        await update.message.reply_text("Rewriting with your feedback...")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"], text, is_business_idea=is_biz,
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:80]}")
            return True
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await update.message.reply_text(_draft_msg("UPDATED", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())
        return True

    return False

# ── Inline keyboard callback handler ─────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard button taps (APPROVE / NEXT / SHORTER / LONGER / SKIP)."""
    query = update.callback_query
    await query.answer()          # clears the tap-loading spinner immediately
    uid   = query.from_user.id
    cmd   = query.data            # "APPROVE" | "NEXT" | "SHORTER" | "LONGER" | "SKIP"
    msg   = query.message

    draft = get_draft(uid)
    if not draft:
        await msg.reply_text("No active draft. Pick a topic from today's digest.")
        return

    is_biz = draft.get("is_business_idea", False)

    def _dmsg(label: str, post: str, ver: int) -> str:
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR LINKEDIN POST  v{ver}  [{label}]\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{post}"
        )

    # ── APPROVE ───────────────────────────────────────────────────────────────
    if cmd == "APPROVE":
        await msg.reply_text("Saving to Notion...")
        try:
            if draft.get("page_id"):
                save_approved_post_to_notion(
                    draft["page_id"], draft.get("news_topic", ""),
                    draft.get("founder_idea", ""), draft.get("current_draft", "")
                )
            await msg.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "POST APPROVED ✅\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Saved to Notion.\n"
                "🎬 Sending to video pipeline..."
            )
        except Exception as e:
            await msg.reply_text(f"Approved! (Notion error: {str(e)[:80]})")
        asyncio.create_task(trigger_heygen_pipeline({
            "founder_name":   draft.get("founder_name", ""),
            "founder_id":     uid,
            "post_text":      draft.get("current_draft", ""),
            "news_topic":     draft.get("news_topic", ""),
            "notion_page_id": draft.get("page_id", ""),
            "approved_at":    datetime.utcnow().isoformat(),
        }))
        clear_draft(uid)

    # ── SKIP ──────────────────────────────────────────────────────────────────
    elif cmd == "SKIP":
        clear_draft(uid)
        await msg.reply_text("Skipped.\nReply with a different topic number or wait for tomorrow's digest.")

    # ── NEXT ──────────────────────────────────────────────────────────────────
    elif cmd == "NEXT":
        await msg.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Write a completely different version — different hook, different angle, same topic and idea.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await msg.reply_text(f"Error: {str(e)[:80]}")
            return
        draft["current_draft"] = new_post
        draft["version"]       = draft.get("version", 1) + 1
        pending_drafts[uid]    = draft
        save_pending()
        await msg.reply_text(_dmsg("NEW VERSION", new_post, draft["version"]), reply_markup=draft_keyboard())

    # ── SHORTER ───────────────────────────────────────────────────────────────
    elif cmd == "SHORTER":
        await msg.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Make it shorter — cut at least 30% of words. Keep the core message and voice.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await msg.reply_text(f"Error: {str(e)[:80]}")
            return
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await msg.reply_text(_dmsg("SHORTER", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())

    # ── LONGER ────────────────────────────────────────────────────────────────
    elif cmd == "LONGER":
        await msg.reply_chat_action("typing")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                "Expand it — add a personal story or example, more depth. Keep their voice.",
                is_business_idea=is_biz,
            )
        except Exception as e:
            await msg.reply_text(f"Error: {str(e)[:80]}")
            return
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await msg.reply_text(_dmsg("EXPANDED", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())


# ════════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMAND HANDLERS
# ════════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or "there"
    auto_save_telegram_id(name, uid)
    clear_draft(uid)
    news_selections.pop(uid, None)
    sessions[uid] = {"name": name, "messages": [], "transcript": [], "done": False}

    # Send onboarding GIF/animation if configured, with voice note instructions
    if WELCOME_GIF:
        voice_tip = (
            "HOW TO SEND A VOICE NOTE\n\n"
            "1. Hold the microphone button (bottom right)\n"
            "2. Speak your idea naturally\n"
            "3. Release to send\n\n"
            "You can also just type if you prefer."
        )
        try:
            if WELCOME_GIF.startswith("http"):
                await update.message.reply_animation(animation=WELCOME_GIF, caption=voice_tip)
            else:
                with open(WELCOME_GIF, "rb") as f:
                    await update.message.reply_animation(animation=f, caption=voice_tip)
        except Exception as e:
            print(f"[Start] GIF send failed: {e}")
        await asyncio.sleep(0.5)

    opening = OPENING.format(name=name)
    sessions[uid]["messages"] = [{"role": "assistant", "content": opening + " " + FIRST_Q}]
    await send_reply(update, opening)
    await asyncio.sleep(0.5)
    await send_reply(update, FIRST_Q)
    print(f"[Start] {name} ({uid})")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    draft = get_draft(uid)
    sel   = news_selections.get(uid)
    sess  = sessions.get(uid)
    news  = get_today_news(uid)

    digest    = get_today_digest(uid)
    all_topics = len(digest["news_items"]) + len(digest["business_ideas"])

    if draft:
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: DRAFT READY  v{draft.get('version',1)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic','?')}\n\n"
            f"Use /draft to view it.\n"
            f"APPROVE · NEXT · SHORTER · LONGER · SKIP"
        )
    elif sel and sel.get("waiting_for_idea"):
        type_tag = "YOUR IDEA" if sel.get("is_business_idea") else "TRENDING"
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: WAITING FOR YOUR TAKE [{type_tag}]\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{sel.get('headline','?')}\n\n"
            f"Send a voice note or text with your angle."
        )
    elif sess and not sess.get("done"):
        count = len([m for m in sess["messages"] if m["role"] == "user"])
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: PROFILE INTERVIEW\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Progress: {count}/15 questions answered.\n"
            f"Answer the question above to continue."
        )
    elif all_topics > 0:
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: TOPICS READY\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{len(digest['news_items'])} trending  +  {len(digest['business_ideas'])} your ideas\n\n"
            f"Reply with a number (1–{all_topics}) to pick a topic.\n"
            f"Use /draft to see the full list."
        )
    else:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "STATUS: WAITING\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Morning digest arrives at 7AM.\n"
            "Use /start if you haven't done your profile interview yet."
        )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_chat_action("typing")
    try:
        profile = get_founder_profile_from_notion(uid)
        if not profile["profile_text"]:
            await update.message.reply_text("No profile found. Send /start to create yours.")
            return
        await update.message.reply_text(
            f"Your content profile:\n\n{profile['profile_text'][:1500]}"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:80]}")


async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    draft = get_draft(uid)

    if draft:
        ver = draft.get("version", 1)
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR LINKEDIN POST  v{ver}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic','')}\n\n"
            f"{draft.get('current_draft','')}",
            reply_markup=draft_keyboard(),
        )
        return

    digest    = get_today_digest(uid)
    news      = digest["news_items"]
    biz_ideas = digest["business_ideas"]

    if news or biz_ideas:
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                 "TODAY'S CONTENT IDEAS\n"
                 "━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        if news:
            lines.append("📰 TRENDING\n")
            for item in news:
                lines.append(f"{item.get('number','?')}. {item.get('headline','')}")
        if biz_ideas:
            lines.append("\n💡 YOUR BUSINESS IDEAS\n")
            for item in biz_ideas:
                lines.append(f"{item.get('number','?')}. {item.get('headline','')}")
        lines.append(f"\n─────────────────────────────")
        lines.append(f"Reply with a number (1–{len(news)+len(biz_ideas)}) to pick one.")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("No content ideas yet.\nMorning digest arrives at 7AM.")


async def cmd_approve(update: Update, ctx): await handle_draft_reply(update, update.effective_user.id, "APPROVE")
async def cmd_skip(update: Update, ctx):    await handle_draft_reply(update, update.effective_user.id, "SKIP")
async def cmd_next(update: Update, ctx):    await handle_draft_reply(update, update.effective_user.id, "NEXT")
async def cmd_shorter(update: Update, ctx): await handle_draft_reply(update, update.effective_user.id, "SHORTER")
async def cmd_longer(update: Update, ctx):  await handle_draft_reply(update, update.effective_user.id, "LONGER")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    clear_draft(uid)
    news_selections.pop(uid, None)
    await update.message.reply_text("All cleared. Send /start to begin your profile interview.")

# ── Voice + text handlers ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_chat_action("typing")

    ogg  = bytes(await (await update.message.voice.get_file()).download_as_bytearray())
    text = await stt(ogg)

    if not text:
        sel = news_selections.get(uid)
        if sel and sel.get("waiting_for_idea"):
            await update.message.reply_text(
                "Couldn't transcribe. Please type your idea instead."
            )
        elif get_draft(uid):
            await update.message.reply_text(
                "Couldn't transcribe. Type your feedback, e.g. 'make it shorter'."
            )
        else:
            await update.message.reply_text("Couldn't transcribe. Please type your message.")
        return

    await update.message.reply_text(random.choice([
        "Got it...", "On it...", "Processing...", "Noted...",
        "Thinking...", "Cooking...", "On it...", "Capturing...",
        "Got your take...", "Working on it...", "Crafting...",
    ]))
    await route_message(update, uid, text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    await route_message(update, uid, text)


async def route_message(update: Update, uid: int, text: str):
    """Route every message through handlers in priority order."""

    # 1. Draft edit/approval
    if await handle_draft_reply(update, uid, text):
        return

    # 2. Founder sending their idea (after selecting news)
    if await handle_founder_idea(update, uid, text):
        return

    # 3. News topic number selection
    if await handle_news_selection(update, uid, text):
        return

    # 4. Active interview
    if uid in sessions and not sessions[uid].get("done"):
        await process_interview(update, uid, text)
        return

    # 5. Check for stale draft after bot restart
    load_pending()
    if uid in pending_drafts:
        await handle_draft_reply(update, uid, text)
        return

    # 6. Fallback
    digest    = get_today_digest(uid)
    all_count = len(digest["news_items"]) + len(digest["business_ideas"])
    if all_count > 0:
        await update.message.reply_text(
            f"Today's content ideas are ready.\n"
            f"Reply with a number (1–{all_count}) to pick a topic.\n"
            f"Use /draft to see the full list."
        )
    else:
        await update.message.reply_text(
            "Morning digest arrives at 7AM.\n"
            "Use /start if you haven't done your profile yet.\n"
            "Use /help to see all commands."
        )

# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands(BOT_COMMANDS)
    print("[Bot] Command menu set in Telegram")


def main():
    load_pending()

    print("=" * 48)
    print("  FOUNDER VOICE BOT — FINAL")
    print("=" * 48)
    print(f"  Model  : {CLAUDE_MODEL}")
    print(f"  Voice  : {'ON' if USE_VOICE else 'OFF (text only)'}")
    print(f"  Notion : {NOTION_DATABASE_ID[:8]}...")
    print(f"  Drafts : {len(pending_drafts)} pending")
    print("  Flow   : 7AM news → pick topic → voice idea → post")
    print("=" * 48)

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("draft",   cmd_draft))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("skip",    cmd_skip))
    app.add_handler(CommandHandler("next",    cmd_next))
    app.add_handler(CommandHandler("shorter", cmd_shorter))
    app.add_handler(CommandHandler("longer",  cmd_longer))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot running — waiting for messages...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()