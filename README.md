# Seisen Hub Dashboard

A pristine Next.js (App Router) + Tailwind CSS dashboard proxying directly to the internal Seisen Python Discord bot configuration files.

## Local Setup

### 1. Requirements
Ensure you have Node.js (v20+) and Python 3.10+ installed.

### 2. Environment Variables
In the `seisen` frontend folder, copy `.env.local.example` to `.env.local` and substitute your values:
- `DISCORD_CLIENT_ID`: Your Discord Application ID
- `DISCORD_CLIENT_SECRET`: Your Discord App Secret (to facilitate the OAuth flow)
- `DISCORD_REDIRECT_URI`: `http://localhost:3000/api/auth/discord/callback` (or your production URL)
- `NEXT_PUBLIC_API_URL`: Prefer `/api/bot` so the browser hits the Next.js proxy (which forwards to Python and attaches your OAuth token from httpOnly cookies).
- `API_PROXY_TARGET`: Server-side URL of FastAPI (e.g. `http://127.0.0.1:8000`) for the home page and verification route.

On the **Python** host, set `CORS_ORIGINS` (comma-separated) if your dashboard runs on a new domain. Optional: `ENABLE_DEBUG_API=1` enables a minimal `/api/debug` route (never exposes token text).

### 3. Run the Stack Locally
Open three distinct terminals:

**Terminal A: Discord Bot**
```bash
cd "d:/Discord Bot/"
python main.py
```

**Terminal B: FastAPI Backend**
```bash
cd "d:/Discord Bot/"
pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

**Terminal C: Next.js Frontend Dashboard**
```bash
cd "d:/Discord Bot/seisen/"
npm run dev
```
Access the dashboard at `http://localhost:3000`.

## Deployment

### Vercel (dashboard only)

The **Discord bot and `api.py` do not run on Vercel**. Vercel only builds the Next.js app in `seisen/`. They must already be running on a VPS (or similar) with a **public HTTPS URL** (or HTTP for testing only).

**1. Vercel project**

1. Push the repo to GitHub and import it in Vercel.
2. Set **Root Directory** / **Framework** so the app builds from **`seisen`** (subdirectory).
3. Add **environment variables** (Production — and Preview if you use preview URLs):

| Variable | Example / note |
|----------|----------------|
| `DISCORD_CLIENT_ID` | Application ID from the Discord Developer Portal |
| `DISCORD_CLIENT_SECRET` | OAuth2 client secret |
| `DISCORD_REDIRECT_URI` | `https://YOUR-PROJECT.vercel.app/api/auth/discord/callback` (must match the portal exactly) |
| `NEXT_PUBLIC_API_URL` | `/api/bot` — browser calls your own site; the Route Handler proxies to Python and attaches the session cookie |
| `BOT_API_URL` | `https://api.yourdomain.com` — **no path**; Vercel’s `/api/bot` proxy forwards to `https://api.yourdomain.com/api/...`. Use your real public API origin. |
| `API_PROXY_TARGET` | Same origin as `BOT_API_URL` (e.g. `https://api.yourdomain.com`) — used by **server** code (home page guild list, verification route). Omit the `/api` suffix. |

Optional: `BOT_API_TIMEOUT_MS`, `BOT_API_VERIFY_TIMEOUT_MS` if your VPS is slow or far away.

**2. Discord Developer Portal**

- OAuth2 → Redirects: add `https://YOUR-PROJECT.vercel.app/api/auth/discord/callback` (and preview URLs if you log in on previews).

**3. Python API host (same machine as the bot)**

- Set **`CORS_ORIGINS`** to include your Vercel origin, e.g. `https://YOUR-PROJECT.vercel.app` (comma-separate multiple origins).
- Bind API to `0.0.0.0` and expose the port through your host / reverse proxy; **HTTPS** in front (e.g. Caddy or nginx) is strongly recommended so cookies stay `Secure`.

### Python Host (Backend & Bot)
1. Deploy `main.py` alongside `api.py` to a stable VPS (such as DigitalOcean or AWS EC2) or a dedicated Python container host (like Railway or Render). The bot and API now persist state in `database/*.json` through `modules.utils`, so keep that folder writable.
2. The FastAPI server (`uvicorn`) must run simultaneously with the Discord bot. Use a process manager like `PM2` or Docker Compose to spin up both.
```
pm2 start main.py --interpreter python3 --name "seisen-bot"
pm2 start "uvicorn api:app --port 8000 --host 0.0.0.0" --name "seisen-api"
```
3. Ensure the API is reachable from the **public internet** at the URL you put in `BOT_API_URL` / `API_PROXY_TARGET` (firewall, reverse proxy, TLS).