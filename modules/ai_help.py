"""
modules/ai_help.py
AI-powered help system – config, sanitization, slash commands.
"""

from __future__ import annotations

import os
import re
import asyncio
import time

import aiohttp
import discord
from discord import app_commands

from modules.utils import load_json, save_json, AI_HELP_FILE, _as_int

_REASONING_LINE_RE = re.compile(
    r"^(?:okay[, ]|alright[, ]|i\s+(?:should|need to|must|think)|let me|(?:first|then|now)[, ]+i\b|from the knowledge base|the (?:rule|knowledge base) says)",
    re.IGNORECASE,
)

DEFAULT_MODEL = "google/gemini-2.0-flash-exp:free"
AVAILABLE_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-small-24b-instruct-2501:free",
    "qwen/qwen-2.5-72b-instruct:free",
]
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_TIMEOUT_SECONDS = 45
MAX_REPLY_CHARS = 1900
MAX_EMBED_CHARS = 3900
MAX_CONTEXT_LINES = 6
EMBED_COLOR = 0x5CA9FF
MODEL_DISCOVERY_TTL_SECONDS = 600

_MODEL_DISCOVERY_CACHE: dict[str, object] = {
    "expires_at": 0.0,
    "models": [],
}

MODEL_ALIASES: dict[str, str] = {
    "nvidia/nemotron-3-super-120b-a12b:free": "openrouter/auto",
    "minimax/minimax-m2.5:free": "openrouter/auto",
    "google/gemini-2.5-flash-preview:free": "openrouter/auto",
    "qwen/qwen-2.5-72b-instruct:free": "openrouter/auto",
}

AI_HELP_RUNTIME_VERSION = "2026-04-03-be02713"


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_ai_help_config() -> dict:
    default = {
        "global_enabled": True,
        "default_model": DEFAULT_MODEL,
        "available_models": AVAILABLE_MODELS,
        "system_instructions": "You are Seisen AI Help...",
        "guilds": {},
    }
    data = load_json(AI_HELP_FILE, default)
    if "guilds" not in data:
        data["guilds"] = {}
    return data


def save_ai_help_config(data: dict):
    save_json(AI_HELP_FILE, data)


# ── Text sanitization ─────────────────────────────────────────────────────────

def sanitize_ai_reply(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    # Standard <think> tag exclusion
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    # Common block markers for thinking
    text = re.sub(r"```(?:thinking|analysis|reasoning)[^\n]*\n[\s\S]*?```", "", text, flags=re.IGNORECASE).strip()
    
    # Remove single line markers if they appear at the very beginning
    lowered = text.lower()
    for marker in ("final answer:", "final response:", "response:", "answer:"):
        if lowered.startswith(marker):
            text = text[len(marker):].strip()
            break
            
    filtered: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if filtered and filtered[-1] != "":
                filtered.append("")
            continue
        # Only filter out reasoning lines if it's very clearly reasoning
        # and doesn't look like part of a helpful step
        if _REASONING_LINE_RE.match(stripped) and len(stripped) < 150:
            continue
        filtered.append(line)
        
    text = "\n".join(filtered).strip() if filtered else ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    
    if len(text) > 4000:
        text = text[:3997].rstrip() + "..."
    return text


def is_internal_reasoning_reply(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return True
    thought_lines = sum(1 for line in lines if _REASONING_LINE_RE.match(line))
    if thought_lines >= 3 and thought_lines / len(lines) >= 0.35:
        return True
    lowered = cleaned.lower()
    leakage_markers = ("let me formulate", "i should respond", "i need to refer to", "i should adhere to", "internal reasoning")
    return any(marker in lowered for marker in leakage_markers)


def is_ai_help_channel(message: discord.Message, config: dict) -> tuple[bool, dict | None]:
    """Return (True, guild_config) if this message's channel should be handled by AI Help."""
    if not message.guild:
        return False, None
    guild_id = str(message.guild.id)

    g_cfg = config.get("guilds", {}).get(guild_id)

    if not g_cfg or not g_cfg.get("enabled", True):
        return False, None

    channel_id = message.channel.id
    category_id = getattr(message.channel, "category_id", None)

    targets = g_cfg.get("targets", [])

    for t in targets:
        if isinstance(t, str):
            if t.startswith("category-"):
                target_id = _as_int(t.replace("category-", ""))
                is_cat = True
            else:
                target_id = _as_int(t)
                is_cat = False
        else:
            target_id = _as_int(t.get("id"))
            is_cat = t.get("is_category", False)
            
        if target_id is None:
            continue
        if is_cat and category_id and target_id == category_id:
            return True, g_cfg
        if not is_cat and target_id == channel_id:
            return True, g_cfg

    return False, None


def _unique_model_list(config: dict, g_cfg: dict) -> list[str]:
    ordered: list[str] = []

    def _append_if_valid(raw: str | None):
        model = str(raw or "").strip()
        if model in MODEL_ALIASES:
            model = MODEL_ALIASES[model]
        if model and model not in ordered:
            ordered.append(model)

    _append_if_valid(g_cfg.get("model"))

    guild_models = g_cfg.get("models")
    if isinstance(guild_models, list):
        for item in guild_models:
            _append_if_valid(item)

    _append_if_valid(config.get("default_model"))

    available = config.get("available_models")
    if isinstance(available, list):
        for item in available:
            _append_if_valid(item)

    for item in AVAILABLE_MODELS:
        _append_if_valid(item)

    _append_if_valid("openrouter/auto")

    return ordered[:12]


def _extract_http_status_from_error(text: str) -> int | None:
    match = re.search(r"HTTP\s+(\d{3})", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _should_try_dynamic_model_discovery(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    http_code = _extract_http_status_from_error(lowered)
    if http_code in {404, 408, 409, 422, 429, 500, 502, 503, 504}:
        return True
    keywords = (
        "no endpoints",
        "temporarily rate-limited",
        "rate limit",
        "guardrail restrictions",
        "data policy",
        "provider returned error",
        "timeout",
    )
    return any(k in lowered for k in keywords)


def _is_text_completion_model(model_id: str) -> bool:
    mid = str(model_id or "").strip().lower()
    if not mid:
        return False
    blocked_terms = (
        "embedding",
        "moderation",
        "transcription",
        "rerank",
        "tts",
        "whisper",
        "speech",
        "vision-only",
    )
    return not any(term in mid for term in blocked_terms)


def _model_priority_score(model_id: str) -> tuple[int, int, str]:
    mid = str(model_id or "").strip().lower()
    is_auto = 0 if mid == "openrouter/auto" else 1
    is_free = 0 if mid.endswith(":free") else 1
    return (is_auto, is_free, mid)


async def _fetch_openrouter_model_catalog(api_key: str) -> list[str]:
    now = time.time()
    expires_at = float(_MODEL_DISCOVERY_CACHE.get("expires_at") or 0.0)
    cached = _MODEL_DISCOVERY_CACHE.get("models")
    if now < expires_at and isinstance(cached, list) and cached:
        return [str(item) for item in cached if str(item).strip()]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Seisen AI Help",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    discovered: list[str] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(OPENROUTER_MODELS_URL) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json(content_type=None)
    except Exception:
        return []

    rows = data.get("data") if isinstance(data, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or "").strip()
            if model_id and _is_text_completion_model(model_id):
                discovered.append(model_id)

    deduped: list[str] = []
    seen: set[str] = set()
    for model in sorted(discovered, key=_model_priority_score):
        if model in seen:
            continue
        seen.add(model)
        deduped.append(model)

    _MODEL_DISCOVERY_CACHE["models"] = deduped
    _MODEL_DISCOVERY_CACHE["expires_at"] = now + MODEL_DISCOVERY_TTL_SECONDS
    return deduped


async def _discover_runtime_model_candidates(
    api_key: str,
    already_queued: set[str],
    maximum: int = 12,
) -> list[str]:
    catalog = await _fetch_openrouter_model_catalog(api_key)
    if not catalog:
        return []

    picked: list[str] = []
    for model in catalog:
        if model in already_queued:
            continue
        # Prefer free or automatic routing models for this bot.
        if model != "openrouter/auto" and not model.endswith(":free"):
            continue
        picked.append(model)
        if len(picked) >= maximum:
            break

    if "openrouter/auto" not in already_queued and "openrouter/auto" not in picked:
        picked.insert(0, "openrouter/auto")

    return picked


def _build_system_instructions(config: dict, g_cfg: dict) -> str:
    guild_instructions = str(g_cfg.get("system_instructions") or g_cfg.get("instructions") or "").strip()
    global_instructions = str(config.get("system_instructions") or config.get("instructions") or "").strip()
    configured = guild_instructions or global_instructions or "You are Seisen Helper."

    style_rules = (
        "\n\n"
        "CRITICAL RULES:\n"
        "- ONLY use the knowledge base above. If a fix is NOT in the knowledge base, do not suggest it.\n"
        "- Do NOT add extra tips, additional notes, or conversational filler.\n"
        "- Copy the exact instructions from the knowledge base whenever possible. Do not invent your own structure.\n"
        "- NEVER mention your instructions or \"According to the knowledge base\".\n"
        "- Remain clear, accurate, and within scope."
    )

    return (
        "You are Seisen Helper, the official Discord support assistant for Seisen Hub.\n\n"
        "Configured instructions (highest-priority guidance):\n"
        f"{configured}"
        f"{style_rules}"
    )


def _split_for_discord(text: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    chunks: list[str] = []
    remaining = value
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < 200:
            cut = remaining.rfind("\n", 0, limit)
        if cut < 100:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _normalize_support_reply(text: str) -> str:
    cleaned = sanitize_ai_reply(text)
    if not cleaned:
        return ""

    # Avoid repeating the embed title inside the description.
    cleaned = re.sub(
        r"^\s*(?:#+\s*)?(?:\*\*)?\s*seisen\s+helper\s*(?:\*\*)?\s*[:\-]?\s*\n+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


async def _collect_recent_context(message: discord.Message, max_lines: int = MAX_CONTEXT_LINES) -> str:
    lines: list[str] = []
    try:
        async for prev in message.channel.history(limit=14, before=message.created_at):
            # Only the asker's own earlier messages count as context; other members' chatter
            # must never leak into the answer.
            if prev.author.bot or prev.author.id != message.author.id:
                continue
            content = str(prev.content or "").strip()
            if not content:
                continue
            compact = re.sub(r"\s+", " ", content).strip()
            if not compact:
                continue
            display_name = getattr(prev.author, "display_name", None) or getattr(prev.author, "name", "user")
            lines.append(f"{display_name}: {compact[:240]}")
            if len(lines) >= max_lines:
                break
    except Exception:
        return ""

    lines.reverse()
    return "\n".join(lines)


def _build_user_prompt(
    user_message: str,
    attachment_urls: list[str],
    recent_context: str,
) -> str:
    parts: list[str] = []

    # Only add context if it seems relevant to the current question
    if recent_context:
        # Check if the user's question is a simple request that doesn't need context
        user_lower = user_message.lower().strip()
        simple_requests = [
            'supported games', 'list of', 'what games', 'which games',
            'what executors', 'which executors', 'executor list', 'games list',
            'supported executors', 'working games', 'available games'
        ]
        
        needs_context = not any(req in user_lower for req in simple_requests)
        
        if needs_context:
            parts.extend([
                "Channel Context (for reference):",
                recent_context,
                ""
            ])

    # Make the user's question more prominent
    parts.extend([
        "USER QUESTION:",
        user_message
    ])

    if attachment_urls:
        parts.extend([
            "",
            "Attachments:",
            "\n".join(attachment_urls),
        ])

    return "\n".join(parts)


# ── Live update feed (Seisen website) ─────────────────────────────────────────

SITE_UPDATES_URL = str(os.getenv("SEISEN_SITE_UPDATES_URL") or "https://seisen.vercel.app/api/site-updates?limit=200").strip()
SITE_UPDATES_TTL_SECONDS = 300
SITE_UPDATES_TIMEOUT_SECONDS = 8
UPDATE_ENTRY_MAX_CHARS = 700
UPDATE_ENTRIES_PER_GAME = 3
UPDATE_ENTRIES_GENERAL = 5
UPDATE_ENTRIES_MATCHED_CAP = 6

_SITE_UPDATES_CACHE: dict[str, object] = {"expires_at": 0.0, "rows": []}

_UPDATE_INTENT_RE = re.compile(
    r"\b(updat\w*|patch\w*|changelog|latest|newest|recent|what'?s new|whats new|new (?:script|game|feature)s?|"
    r"release\w*|version|outdated|fixed|added)\b",
    re.IGNORECASE,
)
_GAME_STOPWORDS = {"the", "a", "an", "of", "to", "and", "script", "scripts", "hub", "update", "new", "game", "release", "general"}


_QUERY_FILLER = {
    "latest", "last", "recent", "newest", "what", "whats", "new", "any", "is", "it", "did", "does", "do", "when", "was",
    "were", "there", "has", "have", "got", "get", "any", "about", "for", "on", "in", "i", "me", "you", "can", "tell",
    "show", "please", "pls", "update", "updat", "updated", "patch", "patche", "changelog", "release", "version", "fixed",
    "added", "outdated", "working", "work", "still", "now", "today", "yet", "recently", "ago", "date", "time", "day", "s",
}
_SMALL_WORDS = {"the", "a", "an", "and", "of", "to"}


def _acronyms(source: str) -> set[str]:
    """Abbreviations derived from a game name: "Pet Simulator 99" -> ps99, "Anime Astral Simulator" -> aas."""
    cleaned = re.sub(r"\[[^\]]*\]", " ", str(source or "").lower())
    tokens = re.findall(r"[a-z0-9]+", cleaned)
    out: set[str] = set()
    for variant in (tokens, [t for t in tokens if t not in _SMALL_WORDS]):
        acr = "".join(t if t.isdigit() else t[0] for t in variant)
        if len(acr) >= 3:
            out.add(acr)
    return out


def _compact(text: str) -> str:
    cleaned = re.sub(r"\[[^\]]*\]", " ", str(text or "").lower())
    return re.sub(r"[^a-z0-9]+", "", cleaned).replace("s", "")


async def _fetch_site_updates() -> list[dict]:
    """Return the website's update posts (newest first), cached; falls back to stale data on failure."""
    now = time.time()
    if now < float(_SITE_UPDATES_CACHE["expires_at"]):
        return list(_SITE_UPDATES_CACHE["rows"])  # type: ignore[arg-type]
    try:
        timeout = aiohttp.ClientTimeout(total=SITE_UPDATES_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(SITE_UPDATES_URL, headers={"Accept": "application/json"}) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
        rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
        _SITE_UPDATES_CACHE["rows"] = rows
        _SITE_UPDATES_CACHE["expires_at"] = now + SITE_UPDATES_TTL_SECONDS
        return list(rows)
    except Exception as err:
        print(f"[AI Help] Could not fetch site updates: {err}")
        _SITE_UPDATES_CACHE["expires_at"] = now + 60  # retry soon, keep serving the last good copy
        return list(_SITE_UPDATES_CACHE["rows"])  # type: ignore[arg-type]


def _normalize_words(text: str) -> set[str]:
    cleaned = re.sub(r"\[[^\]]*\]", " ", str(text or ""))  # drop [UPDATE] / [BETA] style tags
    cleaned = re.sub(r"[^a-z0-9+]+", " ", cleaned.lower())
    words: set[str] = set()
    for w in cleaned.split():
        w = w.strip("+") or w
        if w in _GAME_STOPWORDS:
            continue
        words.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return words


def _update_game_keys(row: dict) -> list[tuple[set[str], str, set[str]]]:
    """Candidate (word set, run-together name, abbreviations) for an update row (website names are decorated and inconsistent)."""
    keys: list[tuple[set[str], str, set[str]]] = []
    title = re.sub(r"(?i)new (?:game )?script(?: release)?", " ", str(row.get("title") or ""))
    for source in (row.get("game_name"), title):
        words = _normalize_words(source or "")
        if words:
            keys.append((words, _compact(source or ""), _acronyms(source or "")))
    return keys


def _format_update_entry(row: dict, max_chars: int) -> str:
    created = str(row.get("created_at") or "")[:10] or "unknown date"
    label = str(row.get("game_name") or row.get("title") or "General").strip()
    label = re.sub(r"\s+", " ", label)
    tag = str(row.get("tag") or "Update").strip()
    body = str(row.get("content") or "")
    body = body.split("\n----")[0]  # drop the vouch footer
    body = re.sub(r"\n{2,}", "\n", body).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "..."
    return f"- Posted {created} | {label} | {tag}\n{body}"


async def _build_update_feed_block(user_message: str) -> str:
    """Live update data to append to the system prompt, only for update-related questions."""
    if not _UPDATE_INTENT_RE.search(user_message or ""):
        return ""
    rows = await _fetch_site_updates()
    if not rows:
        return ""

    msg_words = _normalize_words(user_message)
    msg_compact = _compact(user_message)
    msg_tokens = set(re.findall(r"[a-z0-9]+", str(user_message).lower()))
    matched: list[tuple[set[str], dict]] = []
    for row in rows:
        for key, ckey, acr in _update_game_keys(row):
            # Full name as words ("anime dice"), run together ("animeorigin"), or its abbreviation ("ps99", "aas").
            if key and (key <= msg_words or (len(ckey) >= 6 and ckey in msg_compact) or (acr & msg_tokens)):
                matched.append((key, row))
                break

    if not matched:
        # Partial name ("slayer", "anime astral"): every non-filler word of the question is part of the game name.
        query = {w for w in msg_words if w not in _QUERY_FILLER}
        if query and any(len(w) >= 4 or w.isdigit() for w in query):
            for row in rows:
                for key, _, _ in _update_game_keys(row):
                    # Each word is a game-name word or a prefix of one ("pet sim 99", "anim astral").
                    if key and all(any(kw == q or (len(q) >= 3 and kw.startswith(q)) for kw in key) for q in query):
                        matched.append((key, row))
                        break

    if matched:
        # Prefer the most specific game when one name is a subset of another ("forge" vs "loot forge").
        keys = [k for k, _ in matched]
        specific = [(k, r) for k, r in matched if not any(k < other for other in keys)]
        per_game: dict[frozenset, int] = {}
        picked: list[dict] = []
        for key, row in specific:
            fk = frozenset(key)
            if per_game.get(fk, 0) >= UPDATE_ENTRIES_PER_GAME:
                continue
            per_game[fk] = per_game.get(fk, 0) + 1
            picked.append(row)
            if len(picked) >= UPDATE_ENTRIES_MATCHED_CAP:
                break
        scope = "Updates matching the game in the user's question"
        max_chars = UPDATE_ENTRY_MAX_CHARS
    else:
        picked = rows[:UPDATE_ENTRIES_GENERAL]
        scope = (
            "The most recent updates across all games. These are NOT updates for any specific game the user may have named: "
            "if the user named a game, say there is no recent update post for it and do not substitute these"
        )
        max_chars = 350

    if not picked:
        return ""

    entries = "\n\n".join(_format_update_entry(r, max_chars) for r in picked)
    return (
        "\n\nLIVE UPDATE FEED (official update posts from the Seisen website, newest first; "
        "this is current data and overrides anything else about update dates):\n"
        f"{scope}:\n{entries}\n\n"
        "UPDATE FEED RULES:\n"
        "- For questions about the latest update or what changed, answer only from this feed: name the game, "
        "give the exact posted date, and list the key changes briefly.\n"
        "- If the user's game has no entry in this feed, say there is no recent update post for that game and point to the update-log channel.\n"
        "- Never invent dates, versions, or changes that are not in this feed."
    )


def _build_ai_help_embeds(reply_text: str, thumbnail_url: str | None = None) -> list[discord.Embed]:
    parts = _split_for_discord(reply_text, limit=MAX_EMBED_CHARS)
    embeds: list[discord.Embed] = []
    for idx, part in enumerate(parts):
        title = "Seisen Helper" if idx == 0 else "Seisen Helper (cont.)"
        embed = discord.Embed(
            title=title,
            description=part,
            color=discord.Color(EMBED_COLOR),
        )
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        embed.set_footer(text="Powered by Seisen Hub")
        embeds.append(embed)
    return embeds


async def _openrouter_chat_completion(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "Seisen AI Help",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.1,
        "reasoning": {"exclude": True},
    }

    timeout = aiohttp.ClientTimeout(total=OPENROUTER_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(OPENROUTER_CHAT_URL, json=payload) as resp:
            body_text = await resp.text()
            if resp.status >= 400:
                snippet = (body_text or "").strip().replace("\n", " ")
                if len(snippet) > 280:
                    snippet = snippet[:277] + "..."
                raise RuntimeError(f"OpenRouter HTTP {resp.status}: {snippet}")

            try:
                data = await resp.json(content_type=None)
            except Exception as parse_err:
                raise RuntimeError(f"OpenRouter response parse failed: {parse_err}") from parse_err

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices.")

    message = choices[0].get("message") or {}
    content = message.get("content")

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text_part = item.get("text")
                if text_part:
                    parts.append(str(text_part))
        content = "\n".join(parts).strip()

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenRouter returned an empty completion.")

    return content.strip()


async def _generate_ai_reply(message: discord.Message, config: dict, g_cfg: dict) -> str:
    api_key = str(os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    attachment_urls = [att.url for att in getattr(message, "attachments", []) if getattr(att, "url", None)]

    raw_prompt = str(message.content or "").strip()
    if not raw_prompt:
        if attachment_urls:
            raw_prompt = "Please help with these attachments and explain the likely issue."
    if not raw_prompt:
        raise RuntimeError("No message content to process.")

    recent_context = await _collect_recent_context(message)
    user_prompt = _build_user_prompt(raw_prompt, attachment_urls, recent_context)

    system_prompt = _build_system_instructions(config, g_cfg)
    system_prompt += await _build_update_feed_block(raw_prompt)
    model_candidates = _unique_model_list(config, g_cfg)
    if not model_candidates:
        raise RuntimeError("No AI model is configured.")

    queue: list[str] = list(model_candidates)
    tried: set[str] = set()
    errors: list[str] = []
    discovery_attempted = False

    while queue:
        model = queue.pop(0)
        if model in tried:
            continue
        tried.add(model)

        for attempt in range(2):
            try:
                response = await _openrouter_chat_completion(
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                cleaned = _normalize_support_reply(response)
                if not cleaned or is_internal_reasoning_reply(cleaned):
                    raise RuntimeError("Model produced reasoning-only or empty output after sanitization.")
                return cleaned
            except Exception as model_err:
                err_text = str(model_err).strip() or type(model_err).__name__
                errors.append(f"{model} (attempt {attempt+1}): {err_text}")

                # Handle Rate Limits (429) specifically
                if "429" in err_text:
                    await asyncio.sleep(1.5)
                    continue

                if (not discovery_attempted) and _should_try_dynamic_model_discovery(err_text):
                    discovery_attempted = True
                    discovered = await _discover_runtime_model_candidates(
                        api_key,
                        already_queued=tried.union(set(queue)),
                    )
                    for discovered_model in discovered:
                        if discovered_model not in tried and discovered_model not in queue:
                            queue.append(discovered_model)
                
                # If it's not a rate limit and not something we want to retry immediately on the same model, break to try next model
                if "429" not in err_text:
                    break

    raise RuntimeError("; ".join(errors)[:1200] or "All models failed.")


async def on_ai_help_message(message: discord.Message):

    if message.author.bot or message.webhook_id:
        return
    if not message.guild:
        return

    stripped = str(message.content or "").strip()

    if stripped.startswith("!") or stripped.startswith("/"):
        return

    config = load_ai_help_config()

    if not config.get("global_enabled", True):
        return

    matches, g_cfg = is_ai_help_channel(message, config)

    if not matches or not g_cfg:
        return

    # A member replying to another member is a conversation between them, not a question for the bot.
    ref = getattr(message, "reference", None)
    ref_msg = getattr(ref, "resolved", None) if ref else None
    ref_author = getattr(ref_msg, "author", None)
    if ref_author is not None and not ref_author.bot and ref_author.id != message.author.id:
        return

    try:
        async with message.channel.typing():
            reply = await _generate_ai_reply(message, config, g_cfg)
    except Exception as ai_err:
        err_msg = str(ai_err).strip() or type(ai_err).__name__
        print(f"[AI Help] Response generation failed in guild {message.guild.id}: {err_msg}")
        try:
            fallback_embed = discord.Embed(
                title="Seisen Helper",
                description=(
                    "I could not generate a reply right now because available AI models are "
                    "currently unavailable or rate-limited.\n\n"
                    "Please try again shortly. If this keeps happening, set the model to "
                    "`openrouter/auto` in AI Help settings."
                ),
                color=discord.Color(EMBED_COLOR),
            )
            fallback_embed.set_footer(text="Powered by Seisen Hub")
            # Using channel.send instead of message.reply to avoid potential .client attribute errors in certain setups
            await message.channel.send(embed=fallback_embed, reference=message, mention_author=False)
        except Exception as fallback_err:
            print(f"[AI Help] Fallback message also failed: {fallback_err}")
        return

    if not reply:
        return

    try:
        bot_avatar = None
        try:
            bot_avatar = str(message.guild.me.display_avatar.url) if message.guild.me else None
        except Exception:
            pass

        embeds = _build_ai_help_embeds(reply, thumbnail_url=bot_avatar)
        if not embeds:
            return

        # Using channel.send with reference as a safer alternative to message.reply
        try:
            await message.channel.send(embed=embeds[0], reference=message, mention_author=False)
        except Exception:
            await message.channel.send(embed=embeds[0])

        for embed in embeds[1:]:
            await message.channel.send(embed=embed)
    except Exception as send_err:
        print(f"[AI Help] Failed to send reply in guild {message.guild.id}: {send_err}")


def _ensure_guild_cfg(config: dict, guild_id: str) -> dict:
    """Return the guild config, creating defaults if missing."""
    return config["guilds"].setdefault(guild_id, {
        "enabled": True, "targets": [], "system_instructions": None, "model": None,
    })


# ── Slash commands ────────────────────────────────────────────────────────────

ai_help_group = app_commands.Group(name="ai_help", description="Manage AI-powered auto help")


@ai_help_group.command(name="setup", description="Configure which channels/categories the AI should monitor")
@app_commands.describe(
    channel1="Channel to watch", channel2="2nd channel (optional)", channel3="3rd channel (optional)",
    category1="Category to watch", category2="2nd category (optional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_help_setup(
    interaction: discord.Interaction,
    channel1: discord.TextChannel = None, channel2: discord.TextChannel = None, channel3: discord.TextChannel = None,
    category1: discord.CategoryChannel = None, category2: discord.CategoryChannel = None,
):
    config = load_ai_help_config()
    guild_id = str(interaction.guild_id)
    _ensure_guild_cfg(config, guild_id)
    targets = []
    for ch in [channel1, channel2, channel3]:
        if ch:
            targets.append({"id": ch.id, "name": ch.name, "is_category": False})
    for cat in [category1, category2]:
        if cat:
            targets.append({"id": cat.id, "name": cat.name, "is_category": True})
    if not targets:
        await interaction.response.send_message("❌ Please provide at least one channel or category.", ephemeral=True)
        return
    config["guilds"][guild_id]["targets"] = targets
    save_ai_help_config(config)
    target_names = ", ".join(f"**{t['name']}**" for t in targets)
    await interaction.response.send_message(f"✅ AI Help setup complete! Monitoring: {target_names}", ephemeral=True)


@ai_help_group.command(name="instructions", description="Set the system instructions/knowledge base for the AI")
@app_commands.describe(instructions="The information the AI should know and follow")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_help_instructions(interaction: discord.Interaction, instructions: str):
    config = load_ai_help_config()
    guild_id = str(interaction.guild_id)
    _ensure_guild_cfg(config, guild_id)
    config["guilds"][guild_id]["system_instructions"] = instructions
    save_ai_help_config(config)
    await interaction.response.send_message("✅ AI Help instructions updated!", ephemeral=True)


@ai_help_group.command(name="model", description="Set the OpenRouter model to use for this server")
@app_commands.describe(model="Model ID (e.g., google/gemini-2.0-flash-exp:free)")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_help_model(interaction: discord.Interaction, model: str):
    config = load_ai_help_config()
    guild_id = str(interaction.guild_id)
    _ensure_guild_cfg(config, guild_id)
    config["guilds"][guild_id]["model"] = model
    save_ai_help_config(config)
    await interaction.response.send_message(f"✅ AI model set to `{model}`", ephemeral=True)


@ai_help_group.command(name="toggle", description="Enable or disable AI Help for this server")
@app_commands.describe(enabled="Whether AI Help is enabled")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_help_toggle(interaction: discord.Interaction, enabled: bool):
    config = load_ai_help_config()
    guild_id = str(interaction.guild_id)
    _ensure_guild_cfg(config, guild_id)
    config["guilds"][guild_id]["enabled"] = enabled
    save_ai_help_config(config)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"✅ AI Help has been **{status}** for this server.", ephemeral=True)


@ai_help_group.command(name="list", description="Show the current AI Help configuration")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_help_list(interaction: discord.Interaction):
    config = load_ai_help_config()
    g_cfg = config["guilds"].get(str(interaction.guild_id))
    if not g_cfg:
        await interaction.response.send_message("📭 AI Help is not configured yet. Use `/ai_help setup`.", ephemeral=True)
        return
    embed = discord.Embed(title="🤖 AI Help Configuration", color=discord.Color.blue())
    embed.add_field(name="Status", value="✅ Enabled" if g_cfg["enabled"] else "❌ Disabled", inline=True)
    embed.add_field(name="Model", value=f"`{g_cfg['model'] or config['default_model']}`", inline=True)
    targets = g_cfg.get("targets", [])
    target_lines = [("📁" if t["is_category"] else "💬") + f" **{t['name']}**" for t in targets]
    embed.add_field(name="Monitoring", value="\n".join(target_lines) or "No targets set", inline=False)
    instr = g_cfg.get("system_instructions") or "Default system instructions"
    if len(instr) > 1024:
        instr = instr[:1021] + "..."
    embed.add_field(name="System Instructions", value=instr, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    bot.tree.add_command(ai_help_group)
    bot.add_listener(on_ai_help_message, "on_message")
    print(f"[AI Help] Runtime version: {AI_HELP_RUNTIME_VERSION}")
