---
name: seisen-discord-bot
description: Seisen Hub Discord bot (moderation-heavy), FastAPI config API, Next.js dashboard, JSON-backed settings, and bot↔API IPC.
---

# Seisen Hub — Discord bot & dashboard

Use this skill when editing **this repository**: the Seisen Discord bot, its **FastAPI** surface, or the **Next.js** dashboard under `seisen/`.

## Mandatory: refresh this skill and the project rule together

After you **add or change** anything structural or operational, update **both** files in the same change so agents stay aligned:

- **This file:** `.cursor/skills/seisen-discord-bot/SKILL.md` — refresh the **Stack** table, **Feature map**, env/deployment notes, and **How to add** steps if they are affected.
- **Project rule:** `.cursor/rules/seisen-discord-bot.mdc` — add or tighten bullets for new areas (moderation, persistence, API, dashboard) and new constraints.

**Triggers (non-exhaustive):** new/removed `modules/*.py` or `register_all_modules()` entry; `api.py` routes/models/CORS; `modules/ipc.py` or port env vars; `.env.example`; new root `*.json` configs; `seisen/` pages or env; README run/deploy instructions; notable moderation or automation behavior.

**Split of labor:** keep the **rule** short (policies + “where things live”); keep **tables, checklists, and procedures** in this **skill**. Never update only one of the two if the other would become wrong or stale.

## Stack

| Layer | Location | Notes |
|--------|-----------|--------|
| Bot entry | `main.py` | Intents, `commands.Bot`, `register_all_modules()`, `on_ready` syncs slash commands and starts IPC + embedded API |
| Cogs / features | `modules/*.py` | Each feature area is a module with `register(bot)` |
| Standalone API | `api.py` | FastAPI app, CORS (`CORS_ORIGINS` env), Pydantic models, JSON I/O; **dashboard routes require Authorization: Bearer (Discord user token)** + Manage Server (or Admin) per guild; auth checks now short-cache manageable guild IDs and only report `session invalid` for Discord 401/403 (429/5xx return transient API errors); `verify_member_web` trigger only needs guild membership + self `user_id` |
| In-process API + IPC | `modules/ipc.py` | Port selection (`SERVER_PORT`, `PORT`, `API_PORT`, `IPC_PORT`), bridge to bot |
| Dashboard UI | `seisen/` | Next.js 16; `src/proxy.ts` (replaces middleware); `/api/bot/[[...path]]` proxies to Python and injects Bearer from httpOnly `session_token`; proxy/auth routes clear `session_token` + `user_id` cookies automatically on upstream/Discord `401` so expired sessions self-logout; see `seisen/.env.local.example` |
| Secrets | `.env` | Copy from `.env.example`; never commit real tokens |

## Feature map (high level)

- **Moderation**: `modules/moderation.py`, `modules/automod.py`
- **Access / owner tooling**: `modules/access_control.py`, `command_access.json`
- **Tickets / onboarding / roles**: `modules/tickets.py`, `modules/onboarding.py`, `modules/reaction_roles.py`, `modules/select_menu_roles.py`
- **Engagement**: `modules/activity_rewards.py` (chat-activity tracking + RNG key rewards via webhooks), `modules/giveaways.py`, `modules/polls.py`, `modules/sticky.py`, `modules/boost.py`, `modules/fun.py` (optional import; `fun_config.json` supports per-guild keys under `guilds` plus legacy flat keys)
- **Monitoring / integrations**: `modules/social_monitor.py`, `modules/roblox_monitor.py`, `modules/ai_help.py` (OpenRouter)
- **Dashboard glue**: `modules/dashboard_handlers.py`, routes in `api.py`
- **Activity rewards dashboard**: `seisen/src/app/dashboard/[guildId]/activity-rewards/page.tsx` with API routes `/api/guilds/{guild_id}/activity_rewards`, `/api/guilds/{guild_id}/activity_rewards/status`, `/api/guilds/{guild_id}/activity_rewards/leaderboard`, and `/api/guilds/{guild_id}/activity_rewards/test_webhook`; manual reroll/revoke actions are triggered through `/api/trigger/*` IPC actions.

## How to add a new bot module

1. Create `modules/newfeature.py` with `def register(bot: commands.Bot):` and register commands/listeners/tasks there.
2. In `main.py`: `import modules.newfeature as newfeature` and call `newfeature.register(bot)` inside `register_all_modules()`.
3. If settings are JSON-backed, add a root JSON file only if consistent with other features; implement atomic read/write like sibling modules.
4. If the web dashboard must edit it, add FastAPI endpoints in `api.py` and wire the frontend in `seisen/`; avoid `api.py` ↔ `modules.ipc` import cycles (use lazy/local imports where `api.py` already does).
5. **Update** `.cursor/skills/seisen-discord-bot/SKILL.md` (feature map / stack if needed) and `.cursor/rules/seisen-discord-bot.mdc` (if new area or policy) per **Mandatory: refresh this skill and the project rule together**.

## Debugging checklist

- **Slash commands missing**: bot syncs per-guild and global in `on_ready`; check logs for sync errors and Discord app permissions.
- **Dashboard cannot save**: confirm proxy target (`BOT_API_URL` / `API_PROXY_TARGET`) reaches Python; ensure `CORS_ORIGINS` includes your dashboard origin; 401/403 usually mean missing Bearer (proxy cookie) or missing Manage Server in that guild. A `503`/`502` from auth checks is usually transient Discord upstream/rate-limit behavior.
- **Auth diagnostics in logs**: `api.py` emits safe auth markers (`auth_missing_bearer`, `auth_discord_invalid_session`, `auth_discord_rate_limited`, `auth_guild_forbidden`) with method/path/ip and short token hash prefix only (never full token).
- **Bot and API out of sync**: two writers to the same JSON without locking can race; follow existing save patterns and consider ordering (API vs bot).
- **Giveaway key tickets**: winner key channels now auto-close 5 minutes after successful **Key Claimed** confirmation; key delivery/claim logs remain in the configured giveaway log channel.
- **Activity rewards not delivering keys**: verify `ACTIVITY_REWARDS_ENABLED=1`, both webhook envs (`ACTIVITY_REWARDS_WEBHOOK_WEEKLY`, `ACTIVITY_REWARDS_WEBHOOK_5D`), and HMAC vars (`ACTIVITY_REWARDS_WEBHOOK_HMAC_SECRET`, `ACTIVITY_REWARDS_WEBHOOK_HMAC_HEADER`) are set in `.env`; confirm active users exceed configured thresholds.
- **AI Help storage**: logical key `ai_help_global` is stored in `database/ai_help_config.json`. See `modules/utils.py` for the local JSON loader/saver.
- No external database env vars are required for the current backend; persistence is file-based under `database/`.

## Quality expectations

- Preserve existing naming, error handling style, and JSON schemas unless the task explicitly migrates data.
- Do not log or embed secrets (tokens, webhook URLs, HMAC keys).
- Prefer small, reviewable diffs over broad refactors.

## Git workflow note

- The bot root and `seisen/` can be separate repositories.
- Keep commits scoped to the repo being changed, and if both repos are touched, commit/push each repo independently.
