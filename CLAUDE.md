# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A two-script Telegram bot that helps founders create LinkedIn content:

1. **`content_engine.py`** — runs daily at 7AM, fetches trending news via OpenRouter (Perplexity Sonar Pro for web search), filters it per founder using their Supabase profile, generates a digest (viral Indian social media stories + niche-specific items + profile-based ideas), and sends it to each founder via Telegram. Writes `news_digest.json`.

2. **`bot.py`** — the persistent Telegram bot. Founders reply with a number to pick a topic, then send a voice note or text with their angle. The bot transcribes (ElevenLabs STT), generates a LinkedIn ghostwritten post (Claude via OpenRouter), and presents it with inline keyboard buttons: ✅ Approve / ⏭ Next / ✂️ Shorter / 📝 Longer / ⏩ Skip. On approval, POSTs to a Supabase edge function (`webhook-post-approved`) which saves the post and triggers video generation.

## Running the project

```bash
# Activate venv
source .venv/Scripts/activate   # Windows bash

# Install dependencies
pip install -r requirements.txt

# Run the daily digest (normally scheduled via Task Scheduler or cron at 7AM)
python content_engine.py

# Run the Telegram bot (long-polling, stays alive)
python bot.py
```

## Required environment variables (`.env`)

```
TELEGRAM_TOKEN
OPENROUTER_API_KEY
FIRECRAWL_API_KEY
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY   # used server-side by bot.py & content_engine.py — bypasses RLS
SUPABASE_ANON_KEY           # fallback for local dev; also used by the admin dashboard

# Optional
CLAUDE_MODEL=anthropic/claude-sonnet-4-6    # default
SEARCH_MODEL=perplexity/sonar-pro           # default
ELEVENLABS_API_KEY
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM   # default
WEBHOOK_SECRET                              # sent as x-webhook-secret if set
ADMIN_DASHBOARD_URL                         # shown to founder after approval
FOUNDER_CONCURRENCY=5                       # content_engine parallelism
WELCOME_GIF
```

Notion vars (`NOTION_TOKEN`, `NOTION_DATABASE_ID`) and `N8N_HEYGEN_WEBHOOK_URL` have been removed.

## Railway deployment

- [Procfile](Procfile) declares `worker: python bot.py`. [railway.json](railway.json) pins `numReplicas: 1`.
- `bot.py` uses Telegram long-polling, so the worker service MUST stay at **1 replica**. Two replicas both poll, one wins, the other silently 409s.
- `content_engine.py` is a **separate Railway service** (same repo), start command `python content_engine.py`, with Railway cron schedule `30 1 * * *` (7 AM IST = 01:30 UTC).
- Python version is pinned by [runtime.txt](runtime.txt) (`python-3.11.9`). Bump it there, not in Nixpacks.
- Both services must be on a paid Railway plan — Hobby sleeps projects and the 7 AM cron won't fire.

## Architecture — data flow

```
content_engine.py  →  news_digest.json  →  bot.py reads on founder reply
                    ↓
              Supabase (founders table: name, telegram_id, profile_text, profile_json, industry)
                    ↓
              Telegram (digest sent, responses handled)
                    ↓
              OpenRouter / Claude (post generation)
                    ↓
              ElevenLabs (STT for voice notes, optional TTS for replies)
                    ↓
              Supabase edge function: webhook-post-approved (saves post + triggers video)
                    ↓
              Supabase edge function: save-founder-profile (upserts founder on /start)
```

## Supabase data model

**`founders` table** (read by both scripts):
- `id` (uuid) — used as `page_id` reference in draft state
- `name`, `telegram_id` (string)
- `profile_text` (plain text for Claude prompts)
- `profile_json` (structured data with `tov`, `dimensions`, `archetype_blend`, `business` keys)
- `industry`, `target_audience`, `unique_angle`, `archetype`

**`bot_logs` table**: fire-and-forget event logs written by `log_to_supabase()` in bot.py.

**Edge functions** (called via `{SUPABASE_URL}/functions/v1/`):
- `save-founder-profile` — upserts founder row on `/start` interview completion
- `webhook-post-approved` — saves approved post and auto-triggers HeyGen video pipeline

`bot.py` & `content_engine.py` use `SUPABASE_SERVICE_ROLE_KEY` for all calls (bypasses RLS). RLS is enabled with no anon policies on `pending_drafts` / `daily_digests`, so a leaked anon key can't read or wipe bot state.

## Persistent state (Supabase tables)

- **`pending_drafts`** — one row per active draft, keyed by `telegram_id`. Managed by `get_draft` / `save_draft` / `delete_draft` in bot.py.
- **`daily_digests`** — one row per founder per day, keyed by `(telegram_id, sent_date)`. Written by `content_engine.py`, read by `bot.py` via `get_today_digest`. Idempotent per day — re-running the cron overwrites the same row.
- **`approved_posts`** — populated by the `webhook-post-approved` edge function when the founder taps ✅ Approve.
- **`founders`** — onboarding profiles. Written by the `save-founder-profile` edge function on `/start` interview completion.
- **`bot_logs`** — fire-and-forget event log.

RLS lockdown: [supabase/migrations/0001_lock_rls_to_service_role.sql](supabase/migrations/0001_lock_rls_to_service_role.sql). Run once in the Supabase SQL editor — enables RLS on all five tables with no anon policies.

`news_digest.json` and `pending_drafts.json` on disk are legacy artefacts and should be deleted; they're already in [.gitignore](.gitignore).

## In-memory state in bot.py (ephemeral)

- **`sessions[uid]`** — active interview session: `{name, messages, transcript, done}`. Set by `/start`, cleared when interview completes. Lost on restart — pragmatic trade-off; the user gets nudged to `/reset`.
- **`news_selections[uid]`** — topic selected from digest, awaiting the founder's angle. Set in `handle_news_selection()`, cleared in `handle_founder_idea()`.

Message routing in `route_message()` checks: draft reply → founder idea → news selection → interview → fallback.

## Key design decisions

- All AI calls go through OpenRouter (`https://openrouter.ai/api/v1/chat/completions`) — not directly to Anthropic or Perplexity APIs. `call_ai()` in `content_engine.py` and `call_claude()` in `bot.py` are the two wrappers.
- Post generation in `generate_post()` has three branches: edit+instruction (refinement), `is_business_idea=True` (profile-based), and default (news hook). Each uses a different prompt template.
- Draft actions (APPROVE/NEXT/SHORTER/LONGER/SKIP) are handled in two places: `handle_callback()` for inline keyboard taps, and `handle_draft_reply()` for text commands. Both must stay in sync.
- `content_engine.py` fetches broad Indian viral news once (shared pool) and then calls `get_viral_items_for_founder()` per founder — not a separate search per founder.
- `bot.py` merges what was previously `model.py` — there is intentionally no third file.
- ElevenLabs is optional: `USE_VOICE` is `False` if `ELEVENLABS_API_KEY` is unset or the import fails. STT (voice notes → text) is the primary use; TTS is available but gated by the same flag.
- Post approval no longer saves to Notion — `trigger_video_pipeline()` replaces the old `save_approved_post_to_notion()` + `trigger_heygen_pipeline()` combo and POSTs to a single Supabase edge function.
