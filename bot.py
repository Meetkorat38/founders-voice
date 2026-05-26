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
  - Everything else (interview, post gen, news selection, draft editing) unchanged
"""

import asyncio
import io
import json
import os
import random
import re
import uuid
from datetime import datetime, date, timezone

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
CLAUDE_MODEL        = os.environ.get("CLAUDE_MODEL", "anthropic/claude-sonnet-4-6")
ELEVENLABS_API_KEY  = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Supabase — For Lovable deployments, only anon key is available.
# Lovable manages Supabase directly. RLS is configured to allow anon read/write on bot tables.
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Optional webhook shared secret — the Supabase edge function can verify this header.
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET", "")

ADMIN_DASHBOARD_URL = os.environ.get("ADMIN_DASHBOARD_URL", "")  # your Lovable app URL

MAX_VOICE_BYTES     = 5 * 1024 * 1024   # 5 MB cap on voice-note downloads

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
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _webhook_headers() -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if WEBHOOK_SECRET:
        h["x-webhook-secret"] = WEBHOOK_SECRET
    return h


_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

async def _post_with_retry(
    client: httpx.AsyncClient, url: str, *, headers: dict, json_body: dict,
    attempts: int = 3, label: str = "http",
) -> httpx.Response:
    """POST with exponential backoff (1s, 2s, 4s) on transient failures."""
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
    return r  # last response with retryable status — let caller handle


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


async def get_founder_profile_from_supabase(uid: int) -> dict:
    """
    Read founder profile from Supabase by telegram_id.
    Returns { name, page_id, profile_text, tov_json }.
    Async so it never blocks the Telegram event loop.
    """
    empty = {"name": "Founder", "page_id": None, "profile_text": "", "tov_json": {}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/founders",
                params={"telegram_id": f"eq.{uid}", "select": "*", "limit": "1"},
                headers=_sb_headers(),
            )
        if r.status_code != 200:
            print(f"[Supabase] Profile read failed: {r.status_code}")
            return empty

        rows = r.json()
        if not rows:
            return empty

        row = rows[0]
        profile_json = row.get("profile_json") or {}
        tov_json     = profile_json.get("tov", {})

        return {
            "name":          row.get("name", "Founder"),
            "page_id":       row.get("id"),
            "profile_text":  row.get("profile_text", ""),
            "tov_json":      tov_json,
            "dimensions":    profile_json.get("dimensions", {}),
            "archetype":     profile_json.get("archetype_blend", {}),
            "profile_json":  profile_json,
            "example_posts": row.get("example_posts") or [],
        }
    except Exception as e:
        print(f"[Supabase] Profile read error: {e}")
        return empty


async def trigger_video_pipeline(payload: dict) -> bool:
    """
    POST approved post to Supabase webhook edge function.
    Returns True on success so the caller can decide whether to delete the draft.
    """
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await _post_with_retry(
                client,
                f"{SUPABASE_URL}/functions/v1/webhook-post-approved",
                headers=_webhook_headers(),
                json_body=payload,
                label="webhook-post-approved",
            )
        if r.status_code in (200, 201, 202):
            print(f"[Webhook] Post sent ✓ for {payload.get('founder_name')}")
            return True
        print(f"[Webhook] Failed: {r.status_code} {r.text[:120]}")
        return False
    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return False


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
# Drafts + digests live in Supabase (see supabase/migrations/0001_drafts_and_digests.sql).
# Sessions and news_selections stay in-memory — they're short-lived and lost on restart.

sessions:        dict[int, dict] = {}
news_selections: dict[int, dict] = {}
edit_pending:    dict[int, bool] = {}


async def get_draft(uid: int) -> dict | None:
    """Read the pending draft for this founder from Supabase, or None."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pending_drafts",
                params={"telegram_id": f"eq.{uid}", "select": "*", "limit": "1"},
                headers=_sb_headers(),
            )
        if r.status_code == 200:
            rows = r.json()
            return rows[0] if rows else None
        print(f"[Draft] Read failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[Draft] Read error: {e}")
    return None


async def save_draft(uid: int, draft: dict):
    """Upsert the draft row keyed by telegram_id."""
    payload = {
        "telegram_id":      str(uid),
        "founder_name":     draft.get("founder_name", ""),
        "page_id":          draft.get("page_id"),
        "news_topic":       draft.get("news_topic", ""),
        "founder_idea":     draft.get("founder_idea", ""),
        "current_draft":    draft.get("current_draft", ""),
        "is_business_idea": draft.get("is_business_idea", False),
        "version":          draft.get("version", 1),
    }
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/pending_drafts",
                json=payload,
                headers=headers,
            )
        if r.status_code not in (200, 201):
            print(f"[Draft] Save failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[Draft] Save error: {e}")


async def delete_draft(uid: int):
    """Delete the draft row for this founder (after APPROVE or SKIP)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(
                f"{SUPABASE_URL}/rest/v1/pending_drafts",
                params={"telegram_id": f"eq.{uid}"},
                headers=_sb_headers(),
            )
        if r.status_code not in (200, 204):
            print(f"[Draft] Delete failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"[Draft] Delete error: {e}")


async def get_today_digest(uid: int) -> dict:
    """Today's digest row only — returns {news_items, business_ideas}."""
    today = date.today().isoformat()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/daily_digests",
                params={
                    "telegram_id": f"eq.{uid}",
                    "sent_date":   f"eq.{today}",
                    "select":      "news_items,business_ideas",
                    "limit":       "1",
                },
                headers=_sb_headers(),
            )
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            return {
                "news_items":     row.get("news_items") or [],
                "business_ideas": row.get("business_ideas") or [],
            }
    except Exception as e:
        print(f"[Digest] Read error: {e}")
    return {"news_items": [], "business_ideas": []}

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
/approve  — Save draft and trigger video
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

def draft_keyboard(version: int = 1) -> InlineKeyboardMarkup:
    """Each button encodes the draft version it was shown for, so taps on
    stale messages can be rejected in handle_callback (H5)."""
    def cb(action: str) -> str:
        return f"{action}:v{version}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve",  callback_data=cb("APPROVE")),
            InlineKeyboardButton("⏭ Next",     callback_data=cb("NEXT")),
        ],
        [
            InlineKeyboardButton("✂️ Shorter",  callback_data=cb("SHORTER")),
            InlineKeyboardButton("📝 Longer",   callback_data=cb("LONGER")),
            InlineKeyboardButton("⏩ Skip",     callback_data=cb("SKIP")),
        ],
        [
            InlineKeyboardButton("✏️ Edit",     callback_data=cb("EDIT")),
        ],
    ])

# ── ElevenLabs voice ──────────────────────────────────────────────────────────

def _tts_sync(text: str) -> bytes:
    audio = el.text_to_speech.convert(
        voice_id=ELEVENLABS_VOICE_ID,
        text=text,
        model_id="eleven_multilingual_v2",
    )
    buf = io.BytesIO()
    for chunk in audio:
        buf.write(chunk)
    return buf.getvalue()


async def tts(text: str) -> bytes | None:
    global USE_VOICE
    if not USE_VOICE or not el:
        return None
    try:
        return await asyncio.to_thread(_tts_sync, text)
    except Exception as e:
        err = str(e)
        if "voice_not_found" in err or "404" in err:
            print(f"[TTS] Voice ID not found — disabling voice.")
            USE_VOICE = False
        else:
            print(f"[TTS] {err[:100]}")
        return None


def _stt_sync(ogg: bytes) -> str:
    buf      = io.BytesIO(ogg)
    buf.name = "audio.ogg"
    return el.speech_to_text.convert(file=buf, model_id="scribe_v1").text.strip()


async def stt(ogg: bytes) -> str | None:
    if not USE_VOICE or not el:
        return None
    try:
        return await asyncio.to_thread(_stt_sync, ogg)
    except Exception as e:
        print(f"[STT] {e}")
        return None

# ── Claude API call ───────────────────────────────────────────────────────────

async def call_claude(messages: list, system: str = None, max_tokens: int = 1500) -> str:
    msgs = [{"role": "system", "content": system}, *messages] if system else messages
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://founderbot.app",
        "X-Title": "Founder Bot",
    }
    body = {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "messages": msgs}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await _post_with_retry(
            c, "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json_body=body, label="call_claude",
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

# ── Post generation ───────────────────────────────────────────────────────────

def _build_tov_instruction(tov: dict) -> str:
    if not tov:
        return ""
    return (
        f"\nMATCH THEIR EXACT WRITING STYLE:\n"
        f"- Hook style: {tov.get('hook_style', 'natural observation')}\n"
        f"- CTA style: {tov.get('cta_style', 'open-ended thought')}\n"
        f"- Avg sentence: ~{tov.get('avg_sentence_words', tov.get('sentence_length', 'short'))} words\n"
        f"- NO emojis — this becomes a spoken video script\n"
        f"- NO hashtags\n"
        f"- Bullets: {'yes' if tov.get('uses_bullet_points') or tov.get('uses_bullets') else 'NO bullets'}\n"
        f"- Line breaks between sentences: yes\n"
        f"- Power words to use: {', '.join(tov.get('power_words', [])[:5])}\n"
        f"- Never: {', '.join(tov.get('what_they_never_do', [])[:3])}\n"
        f"- Vocabulary: {tov.get('vocabulary_level', tov.get('vocabulary', 'conversational'))}\n"
        f"- Preferred topics: {', '.join(tov.get('topics', [])[:4])}\n"
    )


_ARCHETYPE_TRAITS = {
    "Naval Ravikant":    "Aphorisms only. One insight per sentence. No filler. Philosophical depth in 8 words. Never explains — lets the idea land.",
    "Gary Vaynerchuk":   "Raw, punchy, zero corporate polish. Calls out excuses. Street energy. Challenges the reader directly. 'The truth is...'",
    "Simon Sinek":       "Always starts with WHY. Empathy before data. Builds slowly to an insight that feels earned. Inspires, never instructs.",
    "Brene Brown":       "Opens with personal vulnerability. One real story, one universal truth. Warm and disarming. Never preachy.",
    "Balaji Srinivasan": "Data and specifics first. Thesis-driven. Future-focused. References trends with precision. Dense but never academic.",
    "Oprah Winfrey":     "Conversational and human. Asks questions that make the reader feel seen. Emotional resonance over argument.",
    "Paul Graham":       "Short paragraphs. Counterintuitive observations. Strips away assumptions. Each sentence earns the next.",
    "Alex Hormozi":      "Numbered frameworks. 'Here's what nobody tells you.' Contrarian takes backed by specifics. High information density. Actionable above all.",
    "Sahil Bloom":       "Storytelling-first. Opens with a moment, not a statement. Threads a lesson through narrative. Warm but sharp.",
    "Justin Welsh":      "Personal, specific, repeatable. Heavy on one-line punches. Always ties back to one clear lesson. Never vague.",
}


def _build_persona_system_prompt(profile: dict) -> str:
    name     = profile.get("name", "Founder")
    arch     = profile.get("archetype", {})
    dims     = profile.get("dimensions", {})
    biz      = profile.get("profile_json", {}).get("business", {})
    examples = profile.get("example_posts", [])

    primary         = arch.get("primary", "")
    secondary       = arch.get("secondary", "")
    p_weight        = int(float(arch.get("primary_weight", 0)) * 100)
    s_weight        = int(float(arch.get("secondary_weight", 0)) * 100)
    primary_trait   = _ARCHETYPE_TRAITS.get(primary, "")
    secondary_trait = _ARCHETYPE_TRAITS.get(secondary, "")

    top_dims = sorted(dims.items(), key=lambda x: x[1], reverse=True)[:3]
    dims_str = ", ".join(f"{k.replace('_', ' ')} ({v}/100)" for k, v in top_dims)

    example_block = ""
    if examples:
        formatted = "\n\n---\n".join(f'"{p}"' for p in examples[:3])
        example_block = f"\n\nEXAMPLE POSTS IN THEIR VOICE (study these — match this style exactly):\n{formatted}"

    return (
        f"You are ghostwriting a LinkedIn post for {name}, founder in {biz.get('industry', 'tech')}.\n"
        f"They build: {biz.get('what_they_do', '')}.\n"
        f"Audience: {biz.get('target_audience', '')}.\n"
        f"Their edge: {biz.get('unique_angle', '')}.\n\n"
        f"VOICE BLEND:\n"
        f"  {p_weight}% {primary} — {primary_trait}\n"
        f"  {s_weight}% {secondary} — {secondary_trait}\n\n"
        f"PERSONALITY: {dims_str}\n\n"
        f"IMPORTANT: This post becomes a spoken video script. Write for the ear.\n"
        f"No emojis. No hashtags. Short sentences with natural spoken rhythm.\n"
        f"A reader who knows {name} must immediately recognise this as their voice.{example_block}"
    )


async def generate_post(
    uid: int,
    news_topic: str,
    founder_idea: str,
    existing_draft: str = "",
    edit_instruction: str = "",
    is_business_idea: bool = False,
) -> tuple[str, dict]:
    profile = await get_founder_profile_from_supabase(uid)
    name    = profile["name"]
    tov_ins = _build_tov_instruction(profile["tov_json"])
    system_prompt = _build_persona_system_prompt(profile)

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
            f"TONE OF VOICE RULES:\n{tov_ins}\n"
            f"TOPIC: {news_topic}\n"
            f"THEIR IDEA: {founder_idea}\n\n"
            f"CURRENT DRAFT:\n{existing_draft}\n\n"
            f"EDIT: {edit_instruction}\n\n"
            f"Rewrite following the edit. Keep their exact voice.\n"
            f"HARD LENGTH RULE: 80-90 words. This is a spoken video script — 25-30 seconds at natural pace.\n"
            f"Output ONLY the post text."
        )
    elif is_business_idea:
        prompt = (
            f"Ghostwrite a LinkedIn post for {name}.\n\n"
            f"TONE OF VOICE RULES:\n{tov_ins}\n"
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
            f"- HARD LENGTH RULE: 80-90 words. This is a spoken video script — 25-30 seconds at natural pace.\n"
            f"- Output ONLY the post text, ready to copy-paste"
        )
    else:
        prompt = (
            f"Ghostwrite a LinkedIn post for {name}.\n\n"
            f"TONE OF VOICE RULES:\n{tov_ins}\n"
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
            f"- HARD LENGTH RULE: 80-90 words. This is a spoken video script — 25-30 seconds at natural pace.\n"
            f"- Output ONLY the post text, ready to copy-paste"
        )

    post = await call_claude(
        [{"role": "user", "content": prompt}],
        system=system_prompt,
        max_tokens=700,
    )
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

    digest     = await get_today_digest(uid)
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

    draft = {
        "founder_name":     profile.get("name", ""),
        "page_id":          profile.get("page_id"),
        "news_topic":       news_topic,
        "founder_idea":     idea_text,
        "current_draft":    post_text,
        "is_business_idea": is_business_idea,
        "version":          1,
    }
    await save_draft(uid, draft)
    news_selections.pop(uid, None)

    await _send_with_retry(
        update.message.reply_text,
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"YOUR LINKEDIN POST  v1\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{post_text}",
        reply_markup=draft_keyboard(version=1),
        label="founder_idea post",
    )
    return True


def _is_off_topic(text: str) -> bool:
    """Return True if text looks like a general-purpose LLM query, not a post-edit instruction.
    Scoped to the first 30 chars so phrases like 'explain my angle more' (legit edit)
    don't get rejected as off-topic."""
    lower = text.lower()[:30]
    return any(re.search(pat, lower) for pat in OFF_TOPIC_PATTERNS)


async def handle_draft_reply(update: Update, uid: int, text: str) -> bool:
    draft = await get_draft(uid)
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

    # APPROVE — blocks on the webhook so we only delete the draft once the post is saved.
    if cmd in ("APPROVE", "/APPROVE"):
        await update.message.reply_text("Saving your post...")
        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "founder_name":    draft.get("founder_name", ""),
            "founder_id":      uid,              # telegram_id
            "post_text":       draft.get("current_draft", ""),
            "news_topic":      draft.get("news_topic", ""),
            "founder_page_id": draft.get("page_id", ""),
            "approved_at":     datetime.now(timezone.utc).isoformat(),
        }
        ok = await trigger_video_pipeline(payload)
        if not ok:
            await update.message.reply_text(
                "Couldn't save your post right now — the draft is still here. Tap ✅ Approve again in a moment."
            )
            return True

        approval_msg = (
            "Your post is saved! 🎉\n"
            "We're creating your video — you'll get a notification when it's ready.\n"
            "Keep crushing it. 💪"
        )
        if ADMIN_DASHBOARD_URL:
            approval_msg += f"\n\nDashboard: {ADMIN_DASHBOARD_URL}"
        await update.message.reply_text(approval_msg)
        await delete_draft(uid)
        return True

    # SKIP
    if cmd in ("SKIP", "/SKIP"):
        await delete_draft(uid)
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
        await save_draft(uid, draft)
        await update.message.reply_text(_draft_msg("NEW VERSION", new_post, draft["version"]), reply_markup=draft_keyboard(draft["version"]))
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
        draft["version"]       = draft.get("version", 1) + 1
        await save_draft(uid, draft)
        await update.message.reply_text(_draft_msg("SHORTER", new_post, draft["version"]), reply_markup=draft_keyboard(draft["version"]))
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
        draft["version"]       = draft.get("version", 1) + 1
        await save_draft(uid, draft)
        await update.message.reply_text(_draft_msg("EXPANDED", new_post, draft["version"]), reply_markup=draft_keyboard(draft["version"]))
        return True

    # Free text edit
    if len(text.strip()) > 3 and not text.startswith("/"):
        in_edit_mode = edit_pending.pop(uid, False)
        if not in_edit_mode and _is_off_topic(text):
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
        draft["version"]       = draft.get("version", 1) + 1
        await save_draft(uid, draft)
        await update.message.reply_text(_draft_msg("EDITED", new_post, draft["version"]), reply_markup=draft_keyboard(draft["version"]))
        return True

    return False


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data or ""

    # callback_data is "ACTION:vN" — parse and reject if the stored draft has moved on.
    action, _, vtag = data.partition(":")
    clicked_version = int(vtag[1:]) if vtag.startswith("v") and vtag[1:].isdigit() else 0

    if clicked_version:
        draft = await get_draft(uid)
        current_version = (draft or {}).get("version", 0)
        if current_version and current_version != clicked_version:
            await query.message.reply_text(
                "That's an older version — scroll down to the latest draft and use those buttons."
            )
            return

    if action == "EDIT":
        edit_pending[uid] = True
        await query.message.reply_text(
            "What would you like to change? Type your edit instruction\n"
            "(e.g. 'make the opening line stronger' or 'add a personal story'):"
        )
        return

    class _FakeUpdate:
        message = query.message
        effective_user = query.from_user

    await handle_draft_reply(_FakeUpdate(), uid, action)

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
    profile = await get_founder_profile_from_supabase(uid)
    if profile["profile_text"]:
        await update.message.reply_text(RETURNING_WELCOME.format(name=profile['name']))
        return

    existing = sessions.get(uid)
    if existing and not existing.get("done"):
        await update.message.reply_text(
            "You've got an interview in progress — keep answering, or send /reset to start fresh."
        )
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
    draft  = await get_draft(uid)
    sel    = news_selections.get(uid)
    sess   = sessions.get(uid)
    digest = await get_today_digest(uid)
    all_topics = len(digest["news_items"]) + len(digest["business_ideas"])

    if draft:
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"STATUS: DRAFT READY v{draft.get('version', 1)}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic', '')[:60]}\n\n"
            f"APPROVE / NEXT / SHORTER / LONGER\nor send feedback as text.",
            reply_markup=draft_keyboard(draft.get("version", 1)),
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
    profile = await get_founder_profile_from_supabase(uid)
    if not profile["profile_text"]:
        await update.message.reply_text("No profile found. Send /start to create yours.")
        return
    await update.message.reply_text(
        f"Your content profile:\n\n{profile['profile_text'][:1500]}"
    )


async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    draft = await get_draft(uid)

    if draft:
        ver = draft.get("version", 1)
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"YOUR LINKEDIN POST  v{ver}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Topic: {draft.get('news_topic', '')}\n\n"
            f"{draft.get('current_draft', '')}",
            reply_markup=draft_keyboard(ver),
        )
        return

    digest    = await get_today_digest(uid)
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
    news_selections.pop(uid, None)
    await delete_draft(uid)
    await update.message.reply_text("All cleared. Send /start to begin your profile interview.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_chat_action("typing")

    if not USE_VOICE:
        await update.message.reply_text("🎙 Voice notes require ElevenLabs. Please type your message.")
        return

    voice = update.message.voice
    if voice and voice.file_size and voice.file_size > MAX_VOICE_BYTES:
        await update.message.reply_text(
            "🎙 That voice note is too long — please keep it under 5 MB (~3 min) or send text."
        )
        return

    ogg  = bytes(await (await voice.get_file()).download_as_bytearray())
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

    digest    = await get_today_digest(uid)
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
    print("=" * 48)
    print("  FOUNDER VOICE BOT — SUPABASE EDITION")
    print("=" * 48)
    print(f"  Model     : {CLAUDE_MODEL}")
    print(f"  Voice     : {'ON' if USE_VOICE else 'OFF'}")
    print(f"  Supabase  : {SUPABASE_URL[:40]}...")
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