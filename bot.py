"""
Founder Voice Bot — Supabase Edition
Notion removed. All profile reads/writes go to Supabase REST API.

Changes from previous version:
  - Removed: notion_client, NOTION_TOKEN, NOTION_DATABASE_ID
  - save_profile_to_notion() → save_profile_to_supabase()
  - get_founder_profile_from_notion() → get_founder_profile_from_supabase()
  - save_approved_post_to_notion() → removed (web app handles post storage now)
  - auto_save_telegram_id() → removed (handled in save_profile_to_supabase)
  - trigger_heygen_pipeline() → now posts to Supabase webhook edge function
  - Everythiyng else (interview, post gen, news selection, draft editing) unchanged
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
from telegram import Update, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN      = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_API_KEY  = os.environ["OPENROUTER_API_KEY"]
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-5")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Supabase — replaces Notion
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

# Webhook secret — must match what Supabase edge function expects
# WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "")

ADMIN_DASHBOARD_URL = os.environ.get("ADMIN_DASHBOARD_URL", "")  # your Lovable app URL

NEWS_DIGEST_FILE = "news_digest.json"
PENDING_FILE     = "pending_drafts.json"

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

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _webhook_headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }


async def save_profile_to_supabase(name: str, profile: dict, transcript: list, uid: int) -> str:
    """
    Upsert founder profile into Supabase founders table.
    Calls the save-founder-profile edge function.
    Returns the founder ID or a status string.
    """
    arch     = profile.get("archetype_blend", {})
    business = profile.get("business", {})

    arch_line = (
        f"{arch.get('primary', '—')} ({int(float(arch.get('primary_weight', 0)) * 100)}%)"
        f" + {arch.get('secondary', '—')} ({int(float(arch.get('secondary_weight', 0)) * 100)}%)"
    )

    # Build plain text profile for Claude prompts (same format as before)
    dims     = profile.get("dimensions", {})
    tov      = profile.get("tov", {})
    dims_text = "\n".join(f"  {k.replace('_', ' ').title()}: {v}/100" for k, v in dims.items())
    tov_text  = (
        f"Hook: {tov.get('hook_style', '—')}\n"
        f"CTA: {tov.get('cta_style', '—')}\n"
        f"Sentence length: {tov.get('sentence_length', '—')}\n"
        f"Vocabulary: {tov.get('vocabulary', '—')}\n"
        f"Topics: {', '.join(tov.get('topics', []))}"
    )
    biz_text = (
        f"Company: {business.get('company_name', '—')}\n"
        f"What they do: {business.get('what_they_do', '—')}\n"
        f"Industry: {business.get('industry', '—')}\n"
        f"Target audience: {business.get('target_audience', '—')}\n"
        f"Unique angle: {business.get('unique_angle', '—')}"
    )
    profile_text = f"Business Information\n{biz_text}\n\nArchetype: {arch_line}\n\nPersonality Dimensions\n{dims_text}\n\nTone of Voice\n{tov_text}"

    payload = {
        "telegram_id":    str(uid),
        "name":           name,
        "profile_json":   profile,
        "profile_text":   profile_text,
        "archetype":      arch_line,
        "industry":       business.get("industry", ""),
        "target_audience": business.get("target_audience", ""),
        "unique_angle":   business.get("unique_angle", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{SUPABASE_URL}/functions/v1/save-founder-profile",
                json=payload,
                headers=_webhook_headers(),
            )
        if r.status_code in (200, 201):
            data = r.json()
            founder_id = data.get("founder_id", "")
            print(f"[Supabase] Profile saved for {name} — id: {founder_id}")
            return founder_id
        else:
            print(f"[Supabase] Profile save failed: {r.status_code} {r.text[:120]}")
            return ""
    except Exception as e:
        print(f"[Supabase] Profile save error: {e}")
        return ""


def get_founder_profile_from_supabase(uid: int) -> dict:
    """
    Read founder profile from Supabase by telegram_id.
    Returns { name, page_id, profile_text, tov_json }
    Synchronous wrapper — uses httpx sync client so it can be called from sync context.
    """
    try:
        import httpx as _httpx
        r = _httpx.get(
            f"{SUPABASE_URL}/rest/v1/founders",
            params={"telegram_id": f"eq.{uid}", "select": "*", "limit": "1"},
            headers=_sb_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[Supabase] Profile read failed: {r.status_code}")
            return {"name": "Founder", "page_id": None, "profile_text": "", "tov_json": {}}

        rows = r.json()
        if not rows:
            return {"name": "Founder", "page_id": None, "profile_text": "", "tov_json": {}}

        row = rows[0]
        profile_json = row.get("profile_json") or {}
        tov_json     = profile_json.get("tov", {})

        return {
            "name":         row.get("name", "Founder"),
            "page_id":      row.get("id"),          # uuid, used as reference
            "profile_text": row.get("profile_text", ""),
            "tov_json":     tov_json,
        }
    except Exception as e:
        print(f"[Supabase] Profile read error: {e}")
        return {"name": "Founder", "page_id": None, "profile_text": "", "tov_json": {}}


async def trigger_video_pipeline(payload: dict):
    """
    POST approved post to Supabase webhook edge function.
    The edge function saves the post and auto-triggers video generation.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{SUPABASE_URL}/functions/v1/webhook-post-approved",
                json=payload,
                headers=_webhook_headers(),
            )
        if r.status_code in (200, 201, 202):
            print(f"[Webhook] Post sent ✓ for {payload.get('founder_name')}")
        else:
            print(f"[Webhook] Failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[Webhook] Error: {e}")


async def log_to_supabase(telegram_id: str, event_type: str, message: str, payload: dict = None):
    """Fire-and-forget log to bot_logs table."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/bot_logs",
                json={
                    "telegram_id": str(telegram_id),
                    "event_type":  event_type,
                    "message":     message,
                    "payload":     payload or {},
                },
                headers=_sb_headers(),
            )
    except Exception:
        pass  # logs are best-effort, never block main flow

# ── State ─────────────────────────────────────────────────────────────────────

sessions:        dict[int, dict] = {}
pending_drafts:  dict[int, dict] = {}
news_selections: dict[int, dict] = {}

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
    return pending_drafts.get(uid)


def clear_draft(uid: int):
    pending_drafts.pop(uid, None)
    save_pending()


def get_today_digest(uid: int) -> dict:
    if not Path(NEWS_DIGEST_FILE).exists():
        return {"news_items": [], "business_ideas": []}
    try:
        with open(NEWS_DIGEST_FILE, "r") as f:
            data = json.load(f)
        entry = data.get(str(uid), {})
        # Merge viral (1-3) + niche (4-8) into a single flat list
        # so all existing callers (handle_news_selection, cmd_draft, etc.) work unchanged
        viral    = entry.get("viral_items", [])
        niche    = entry.get("news_items", [])
        combined = viral + niche
        return {
            "news_items":     combined,
            "business_ideas": entry.get("business_ideas", []),
        }
    except Exception as e:
        print(f"[Digest] Failed to parse {NEWS_DIGEST_FILE}: {e}")
        return {"news_items": [], "business_ideas": []}


def get_today_news(uid: int) -> list[dict]:
    return get_today_digest(uid)["news_items"]


def draft_keyboard() -> InlineKeyboardMarkup:
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

# ── Post generation ───────────────────────────────────────────────────────────

def _build_tov_instruction(tov: dict) -> str:
    if not tov:
        return ""
    return (
        f"\nMATCH THEIR EXACT WRITING STYLE:\n"
        f"- Avg sentence: ~{tov.get('avg_sentence_words', tov.get('sentence_length', 'short'))} words\n"
        f"- Post length: 50-60 words (strict)\n"
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
    # ← CHANGED: reads from Supabase instead of Notion
    profile = get_founder_profile_from_supabase(uid)
    name    = profile["name"]
    tov_ins = _build_tov_instruction(profile["tov_json"])

    if not profile["profile_text"]:
        try:
            async with httpx.AsyncClient(timeout=10) as _c:
                await _c.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": uid,
                        "text": "⚠️ I don't have your profile yet — run /start to set it up. I'll generate a basic post for now.",
                    },
                )
        except Exception:
            pass

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
            f"HARD LENGTH RULE: The final post MUST be between 50 and 60 words total. Not more, not less. Count carefully.\n"
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
            f"- HARD LENGTH RULE: The post MUST be between 50 and 60 words total. Not more, not less. Count carefully before outputting.\n"
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
            f"- HARD LENGTH RULE: The post MUST be between 50 and 60 words total. Not more, not less. Count carefully before outputting.\n"
            f"- Output ONLY the post text, ready to copy-paste"
        )

    post = await call_claude([{"role": "user", "content": prompt}], max_tokens=600)
    print(f"[Generate] Done — {len(post)} chars")
    return post, profile

# ── Interview logic ───────────────────────────────────────────────────────────

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

ONBOARDING_MSG = """\
Welcome to Founder Voice! Here's how this works 👇

Every morning at 7AM, you'll get:
• 3 viral topics trending in India right now
• 5 news stories from your niche
• 2 content ideas based on your business

To create a LinkedIn post:
1️⃣ Reply with a number (like "3") to pick a topic
2️⃣ Share your angle — as a voice note OR text
3️⃣ Get a ghostwritten LinkedIn post in your voice

📱 How to send a voice note on Telegram:
Hold the 🎙 mic button → speak → release to send.

Once your draft is ready, you can:
• ✅ Approve — saves it and starts your video
• ⏭ Next — see a different version
• ✂️ Shorter / 📝 Longer — adjust the length
• Or just type feedback: "make it more personal" / "add a hook"

First, let's build your content profile (5 min). This is what makes every post sound like YOU.\
"""

RETURNING_WELCOME = """\
Hey {name}, welcome back! 👋

Your morning digest arrives at 7AM — trending news + content ideas built around your business.

Reply with a number to pick a topic, share your angle, and get your LinkedIn draft in seconds.

Use /draft to see today's topics, or /help for all commands.\
"""

OFF_TOPIC_PATTERNS = (
    r'\bwhat (is|are|was|were)\b',
    r'\bwho (is|are|was|were)\b',
    r'\bwhen (is|was|will)\b',
    r'\bhow (do|does|did|can|could|would|should)\b',
    r'\bwhy (is|are|was|did)\b',
    r'\bwhat.*(date|time|day|year)\b',
    r'\btell me (about|a joke|something)\b',
    r'\bexplain\b',
    r'\bsearch for\b',
    r'\bjoke\b',
    r'\bweather\b',
    r'\btranslate\b',
    r'\bcalculate\b',
    r'\bwrite (a poem|an essay|a story|code|a song)\b',
)

OFF_TOPIC_REPLY = (
    "I'm your LinkedIn content assistant — I help you turn ideas into posts.\n\n"
    "To edit your draft, reply with:\n"
    "• ✂️ Shorter / 📝 Longer / ⏭ Next\n"
    "• Or describe the change: \"make it more personal\" / \"add a hook at the start\""
)


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
    top3_str = ", ".join(f"{k.replace('_', ' ')} ({v})" for k, v in top3)

    biz_line = ""
    if business.get("company_name"):
        biz_line = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR BUSINESS\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Company : {business.get('company_name', '—')}\n"
            f"You do  : {business.get('what_they_do', '—')}\n"
            f"Audience: {business.get('target_audience', '—')}\n"
            f"Edge    : {business.get('unique_angle', '—')}\n"
        )

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"PROFILE READY, {name.upper()}!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Archetype : {arch.get('primary', '—')} ({int(float(arch.get('primary_weight', 0)) * 100)}%)"
        f" + {arch.get('secondary', '—')} ({int(float(arch.get('secondary_weight', 0)) * 100)}%)\n"
        f"Strengths : {top3_str}\n"
        f"Hook style: {tov.get('hook_style', '—')}\n"
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
            # ← CHANGED: saves to Supabase instead of Notion
            founder_id = await save_profile_to_supabase(name, profile, session["transcript"], uid)
            await send_reply(update, build_summary(name, profile))
            if founder_id:
                await update.message.reply_text("✅ Profile saved!")
            else:
                await update.message.reply_text("Profile built! (Save may have failed — admin will check.)")
            # Log the event
            asyncio.create_task(log_to_supabase(str(uid), "interview_complete", f"Profile saved for {name}", {"founder_id": founder_id}))
        except Exception as e:
            await send_reply(update, build_summary(name, profile))
            await update.message.reply_text(f"Profile built! Save failed: {str(e)[:100]}")
        return

    await send_reply(update, response.strip())

# ── News selection + post generation ─────────────────────────────────────────
# (unchanged from original — only generate_post() reads from Supabase now)

async def handle_news_selection(update: Update, uid: int, text: str) -> bool:
    if not re.match(r'^\d+$', text.strip()):
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
            f"I sent {total} topics today (1–{total}).\nReply with a number in that range."
        )
        return True

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
    sel = news_selections.get(uid)
    if not sel or not sel.get("waiting_for_idea"):
        return False

    news_topic       = sel["headline"]
    is_business_idea = sel.get("is_business_idea", False)

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
        "founder_name":     profile.get("name", ""),
        "page_id":          profile.get("page_id"),
        "news_topic":       news_topic,
        "founder_idea":     idea_text,
        "current_draft":    post_text,
        "is_business_idea": is_business_idea,
        "version":          1,
    }
    save_pending()
    news_selections.pop(uid, None)

    await _send_with_retry(
        update.message.reply_text,
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"YOUR LINKEDIN POST  v1\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{post_text}",
        reply_markup=draft_keyboard(),
        label="founder_idea post",
    )
    return True


def _is_off_topic(text: str) -> bool:
    """Return True if text looks like a general-purpose LLM query, not a post-edit instruction."""
    lower = text.lower()
    return any(re.search(pat, lower) for pat in OFF_TOPIC_PATTERNS)


async def handle_draft_reply(update: Update, uid: int, text: str) -> bool:
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

    # APPROVE — ← CHANGED: fires to Supabase webhook instead of Notion + n8n
    if cmd in ("APPROVE", "/APPROVE"):
        try:
            asyncio.create_task(trigger_video_pipeline({
                "founder_name":   draft.get("founder_name", ""),
                "founder_id":     uid,              # telegram_id
                "post_text":      draft.get("current_draft", ""),
                "news_topic":     draft.get("news_topic", ""),
                "notion_page_id": draft.get("page_id", ""),  # kept for reference
                "approved_at":    datetime.utcnow().isoformat(),
            }))
            approval_msg = (
                "Your post is saved! 🎉\n"
                "We're creating your video — you'll get a notification when it's ready.\n"
                "Keep crushing it. 💪"
            )
            if ADMIN_DASHBOARD_URL:
                approval_msg += f"\n\nSent you video soon"
            await update.message.reply_text(approval_msg)
        except Exception as e:
            await update.message.reply_text(f"Approved! (Error: {str(e)[:80]})")

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

    # Free text edit
    if len(text.strip()) > 3 and not text.startswith("/"):
        if _is_off_topic(text):
            await update.message.reply_text(OFF_TOPIC_REPLY)
            return True
        await update.message.reply_chat_action("typing")
        await update.message.reply_text("Rewriting with your feedback...")
        try:
            new_post, _ = await generate_post(
                uid, draft["news_topic"], draft["founder_idea"],
                draft["current_draft"],
                text.strip(),
                is_business_idea=is_biz,
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:80]}")
            return True
        draft["current_draft"] = new_post
        pending_drafts[uid]    = draft
        save_pending()
        await update.message.reply_text(_draft_msg("EDITED", new_post, draft.get("version", 1)), reply_markup=draft_keyboard())
        return True

    return False


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    class _FakeUpdate:
        message = query.message
        effective_user = query.from_user

    await handle_draft_reply(_FakeUpdate(), uid, data)

# ── Commands ──────────────────────────────────────────────────────────────────

async def _send_with_retry(send_fn, *args, label: str = "", **kwargs):
    """Retry a Telegram send call up to 3 times on TimedOut/NetworkError."""
    delays = [1, 2, 4]
    last_exc = None
    for attempt, delay in enumerate(delays, 1):
        try:
            await send_fn(*args, **kwargs)
            return
        except (TimedOut, NetworkError) as e:
            last_exc = e
            print(f"[send_retry] {label} attempt {attempt} failed ({e.__class__.__name__}) — retrying in {delay}s")
            await asyncio.sleep(delay)
        except Exception as e:
            print(f"[send_retry] {label} non-retryable error: {e}")
            return
    print(f"[send_retry] {label} gave up after {len(delays)} attempts: {last_exc}")


async def send_reply(update: Update, text: str):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        await _send_with_retry(update.message.reply_text, chunk, label=f"chunk {i+1}/{len(chunks)}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or "Founder"

    # Check if profile already exists in Supabase
    profile = get_founder_profile_from_supabase(uid)
    if profile["profile_text"]:
        await update.message.reply_text(RETURNING_WELCOME.format(name=profile['name']))
        return

    sessions[uid] = {
        "name":      name,
        "messages":  [],
        "transcript": [],
        "done":      False,
    }
    await update.message.reply_text(ONBOARDING_MSG)
    await asyncio.sleep(1)
    await update.message.reply_text(OPENING.format(name=name))
    await asyncio.sleep(0.5)
    await update.message.reply_text(FIRST_Q)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    draft  = get_draft(uid)
    sel    = news_selections.get(uid)
    sess   = sessions.get(uid)
    digest = get_today_digest(uid)
    all_topics = len(digest["news_items"]) + len(digest["business_ideas"])

    if draft:
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: DRAFT READY v{draft.get('version', 1)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic', '')[:60]}\n\n"
            f"APPROVE / NEXT / SHORTER / LONGER\nor send feedback as text.",
            reply_markup=draft_keyboard(),
        )
    elif sel and sel.get("waiting_for_idea"):
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: WAITING FOR YOUR TAKE\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{sel.get('headline', '?')}\n\n"
            f"Send a voice note or text with your angle."
        )
    elif sess and not sess.get("done"):
        count = len([m for m in sess["messages"] if m["role"] == "user"])
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: PROFILE INTERVIEW\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Progress: {count}/15 questions answered."
        )
    elif all_topics > 0:
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: TOPICS READY\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{len(digest['news_items'])} trending  +  {len(digest['business_ideas'])} your ideas\n\n"
            f"Reply with a number (1–{all_topics}) to pick a topic."
        )
    else:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "STATUS: WAITING\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Morning digest arrives at 7AM."
        )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_chat_action("typing")
    # ← CHANGED: reads from Supabase
    profile = get_founder_profile_from_supabase(uid)
    if not profile["profile_text"]:
        await update.message.reply_text("No profile found. Send /start to create yours.")
        return
    await update.message.reply_text(
        f"Your content profile:\n\n{profile['profile_text'][:1500]}"
    )


async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    draft = get_draft(uid)

    if draft:
        ver = draft.get("version", 1)
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR LINKEDIN POST  v{ver}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic', '')}\n\n"
            f"{draft.get('current_draft', '')}",
            reply_markup=draft_keyboard(),
        )
        return

    digest    = get_today_digest(uid)
    news      = digest["news_items"]
    biz_ideas = digest["business_ideas"]

    if news or biz_ideas:
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━\nTODAY'S CONTENT IDEAS\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        if news:
            lines.append("📰 TRENDING\n")
            for item in news:
                lines.append(f"{item.get('number', '?')}. {item.get('headline', '')}")
        if biz_ideas:
            lines.append("\n💡 YOUR BUSINESS IDEAS\n")
            for item in biz_ideas:
                lines.append(f"{item.get('number', '?')}. {item.get('headline', '')}")
        lines.append(f"\nReply with a number (1–{len(news)+len(biz_ideas)}) to pick one.")
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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_chat_action("typing")

    if not USE_VOICE:
        await update.message.reply_text("🎙 Voice notes require ElevenLabs. Please type your message.")
        return

    ogg  = bytes(await (await update.message.voice.get_file()).download_as_bytearray())
    text = await stt(ogg)

    if not text:
        await update.message.reply_text("🎙 Couldn't transcribe. Please type instead.")
        return

    await update.message.reply_text(random.choice([
        "Got it...", "On it...", "Processing...", "Noted...",
        "Thinking...", "Cooking...", "Capturing...", "Working on it...",
    ]))
    await route_message(update, uid, text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    await route_message(update, uid, text)


async def route_message(update: Update, uid: int, text: str):
    if await handle_draft_reply(update, uid, text):
        return
    if await handle_founder_idea(update, uid, text):
        return
    if await handle_news_selection(update, uid, text):
        return
    if uid in sessions and not sessions[uid].get("done"):
        await process_interview(update, uid, text)
        return
    if uid in pending_drafts:
        await handle_draft_reply(update, uid, text)
        return

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
            "Use /start if you haven't done your profile yet."
        )

# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands(BOT_COMMANDS)
    print("[Bot] Command menu set")


def main():
    load_pending()

    print("=" * 48)
    print("  FOUNDER VOICE BOT — SUPABASE EDITION")
    print("=" * 48)
    print(f"  Model     : {CLAUDE_MODEL}")
    print(f"  Voice     : {'ON' if USE_VOICE else 'OFF'}")
    print(f"  Supabase  : {SUPABASE_URL[:40]}...")
    print(f"  Drafts    : {len(pending_drafts)} pending")
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