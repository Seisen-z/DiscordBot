# Wispbyte + Vercel Dashboard Runbook

Use this guide when the bot is online but the dashboard fails with BOT_API_UNAVAILABLE.

## Typical Symptoms

- Dashboard login works, but config pages fail.
- Vercel response shows:
	- error: BOT_API_UNAVAILABLE
	- detail: This operation was aborted
- Wisp logs show API is running, but Vercel cannot consistently reach custom host ports.

## Root Cause (Most Common)

Vercel serverless proxy cannot reliably connect to a raw IP + custom port path.
Direct calls may work from your machine, but Vercel calls still timeout.

## Code Fixes Already Applied in This Repo

- member counter legacy ID compatibility and safer sync handling
- bot startup retry/backoff for Discord login 429/1015
- IPC default port logic for single-port hosts
- bot proxy timeout/region/protocol handling in frontend API route

## Fast Recovery (Use Your Wisp Host)

Use the Wisp host directly.

### 1) Wisp Startup Command

Copy this as the startup command:

```bash
if [[ -d .git ]] && [[ "${AUTO_UPDATE}" == "1" ]]; then git pull; fi; if [[ -n "discord.py aiohttp python-dotenv openai certifi fastapi uvicorn[standard] pydantic" ]]; then pip install -U --prefix .local discord.py aiohttp python-dotenv openai certifi fastapi uvicorn[standard] pydantic; fi; if [[ -f /home/container/${REQUIREMENTS_FILE} ]]; then pip install -U --prefix .local -r ${REQUIREMENTS_FILE}; fi; /usr/local/bin/python /home/container/main.py
```

### 2) Verify the Wisp URL

Use this public API URL from your new host:

- `http://fi13.bot-hosting.cloud:20934`

### 3) Set Vercel Environment Variables

Set both to the exact Wisp URL:

- BOT_API_URL=http://fi13.bot-hosting.cloud:20934
- API_PROXY_TARGET=http://fi13.bot-hosting.cloud:20934

Then redeploy Vercel.

### 4) Verify

Check:

- https://seisenbot.vercel.app/api/bot/guilds

If this returns JSON, dashboard pages should load.

## Direct Host Notes

- If your Wisp host URL changes, update Vercel env values and redeploy again.

## Wisp Variables Checklist

- API_PORT=20934
- SERVER_PORT=20934
- IPC_PORT optional; if unset, code now aligns IPC with API port by default on single-port hosts
- VERIFICATION_INTERNAL_SECRET= A random, secure string (must exactly match the `.env` on your Vercel Dashboard for verification tokens to work)

## Local JSON Database Setup

The bot and API now store their shared state in the local `database/*.json` files, so there is no external database to configure.

### Required Files
- Keep the `database/` folder next to `api.py` and `main.py`.
- Make sure the process running the bot/API can read and write those JSON files.
- If you are migrating from another storage setup, copy your exported JSON data into this folder before starting the bot.

### Notes
1. The JSON files are loaded and saved through `modules.utils.load_json()` / `save_json()`.
2. If a file is missing, the bot falls back to the feature's default in-memory structure and creates the file on the next save.
3. Keep the `database/` folder writable by the bot and API process.

## Known Log Messages

- Invalid HTTP request received
	- Usually internet scanners or HTTPS probes against HTTP endpoint
	- Typically harmless noise

- Discord login temporarily rate-limited
	- Startup retry/backoff handles this automatically
	- Avoid manual restart loops

## Security Reminder

If tokens or tunnel secrets are shown in screenshots/logs, rotate them immediately.