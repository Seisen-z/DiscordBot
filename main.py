import sys
import os

# Pterodactyl installs packages with --prefix .local relative to the working
# directory. Insert it into sys.path BEFORE importing discord so that discord's
# has_nacl check (which runs at import time) can find PyNaCl.
_local_site = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".local", "lib",
    f"python{sys.version_info.major}.{sys.version_info.minor}",
    "site-packages",
)
if os.path.isdir(_local_site) and _local_site not in sys.path:
    sys.path.insert(0, _local_site)

# Diagnostic — printed before bot login so we can see it in Pterodactyl logs
try:
    import nacl.secret  # noqa: F401
    print("[Voice] PyNaCl OK — voice support active")
except Exception as _nacl_err:
    print(f"[Voice] PyNaCl FAILED to import: {_nacl_err}")
    print("[Voice] Voice commands will not work until this is resolved")

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
import asyncio
import base64
import aiohttp
from aiohttp import web
import ssl
import certifi
import os
import random
import json
import time
import hmac
import hashlib
import shutil
import io
import re
import sys
import subprocess
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from collections import deque
from urllib.parse import quote, urlparse
from dotenv import load_dotenv
from openai import OpenAI
def _load_pillow_modules():
    try:
        from PIL import Image as PILImage, ImageDraw as PILImageDraw, ImageFont as PILImageFont, ImageOps as PILImageOps
        return PILImage, PILImageDraw, PILImageFont, PILImageOps, None
    except Exception as pillow_err:
        return None, None, None, None, pillow_err


def _attempt_runtime_pillow_install() -> bool:
    flag = str(os.getenv("AUTO_INSTALL_PILLOW", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False

    try:
        print("[Onboarding] Pillow not found. Attempting runtime install...")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--prefix",
                ".local",
                "Pillow",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if detail:
                detail = detail.splitlines()[-1][:220]
            else:
                detail = f"exit code {result.returncode}"
            print(f"[Onboarding] Pillow runtime install failed: {detail}")
            return False

        print("[Onboarding] Pillow runtime install succeeded.")
        return True
    except Exception as install_err:
        print(f"[Onboarding] Pillow runtime install raised: {install_err}")
        return False


Image, ImageDraw, ImageFont, ImageOps, _PILLOW_IMPORT_ERROR = _load_pillow_modules()
if Image is None and _attempt_runtime_pillow_install():
    Image, ImageDraw, ImageFont, ImageOps, _PILLOW_IMPORT_ERROR = _load_pillow_modules()

if Image is None and _PILLOW_IMPORT_ERROR is not None:
    print(f"[Onboarding] Pillow unavailable after startup checks: {_PILLOW_IMPORT_ERROR}")

sys.stdout.reconfigure(encoding="utf-8")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(_SCRIPT_DIR, ".env"), override=False)

def _looks_like_bot_token(token: str | None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


from modules.utils import normalize_discord_token

TOKEN = normalize_discord_token(os.getenv("DISCORD_TOKEN"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Initialize OpenRouter Client
client = None
if OPENROUTER_API_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )
else:
    print("⚠️ OPENROUTER_API_KEY not found in .env. AI Help will be disabled.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.NoPrivateMessage):
        message = "❌ This command can only be used in a server, not in DMs."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "❌ You don't have permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
    else:
        print(f"[System] Unhandled app command error in '{interaction.command.name if interaction.command else '?'}': {error!r}")
        message = "❌ Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

# Reusable certifi-backed SSL context so Windows' missing system CAs don't break HTTPS
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_DYNAMIC_IMAGE_RUNTIME_WARNING_EMITTED = False



# Import modules directly to avoid any package-level circular import issues
import modules.utils as utils
import modules.polls as polls  
import modules.giveaways as giveaways
import modules.member_counter as member_counter
import modules.role_counter as role_counter
import modules.tickets as tickets
import modules.boost as boost
import modules.sticky as sticky
import modules.moderation as moderation
import modules.vouch as vouch
import modules.access_control as access_control
import modules.autoreply as autoreply
import modules.reaction_roles as reaction_roles
import modules.select_menu_roles as select_menu_roles
import modules.ai_help as ai_help
import modules.roblox_monitor as roblox_monitor
import modules.social_monitor as social_monitor
import modules.onboarding as onboarding
import modules.ipc as ipc
import modules.dashboard_handlers as dashboard_handlers
import modules.general as general
import modules.automod as automod
import modules.anti_spam as anti_spam
import modules.ping_protection as ping_protection
import modules.activity_rewards as activity_rewards
import modules.music as music
import modules.key_panel as key_panel
import modules.generate_key as generate_key
import modules.applications as applications

# Try to import fun module - if it fails, continue without it
fun = None
try:
    import modules.fun as fun
    print("[System] Fun module loaded successfully")
except ImportError as e:
    print(f"[System] Fun module not available: {e}")
    print("[System] Bot will continue without fun commands")

def register_all_modules():
    polls.register(bot)
    giveaways.register(bot)
    member_counter.register(bot)
    role_counter.register(bot)
    tickets.register(bot)
    boost.register(bot)
    sticky.register(bot)
    moderation.register(bot)
    vouch.register(bot)
    access_control.register(bot)
    autoreply.register(bot)
    reaction_roles.register(bot)
    select_menu_roles.register(bot)
    ai_help.register(bot)
    roblox_monitor.register(bot)
    social_monitor.register(bot)
    onboarding.register(bot)
    ipc.register(bot)
    dashboard_handlers.register(bot)
    general.register(bot)
    automod.register(bot)
    anti_spam.register(bot)
    ping_protection.register(bot)
    activity_rewards.register(bot)
    music.register(bot)
    key_panel.register(bot)
    generate_key.register(bot)
    applications.register(bot)
    # Register fun module only if it was successfully imported
    if fun is not None and hasattr(fun, 'setup'):
        fun.setup(bot)
        print("[System] Fun commands registered")
    else:
        print("[System] Fun commands not available")

register_all_modules()


# ── Global state ──────────────────────────────────────────────────────────────
ipc_server_task = None
fastapi_server_task = None
startup_bootstrap_done = False
select_menu_views_registered = False
persistent_views_registered = False
slash_commands_synced = False


@bot.event
async def on_ready():
    global ipc_server_task, fastapi_server_task
    global persistent_views_registered
    global select_menu_views_registered, startup_bootstrap_done
    global slash_commands_synced

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not startup_bootstrap_done:
        print("[System] Music module loaded")
        # Ensure locked commands are enforced even if global checks are bypassed.
        access_control.bind_owner_locks_to_commands(bot)
        print("[System] Commands registered")
        startup_bootstrap_done = True

    # Sync command tree once so newly added slash commands (like /fun ...) appear.
    if not slash_commands_synced:
        try:
            for guild in bot.guilds:
                synced = await bot.tree.sync(guild=guild)
                print(f"[System] Synced {len(synced)} slash commands for guild {guild.id}")

            synced_global = await bot.tree.sync()
            print(f"[System] Synced {len(synced_global)} global slash commands")
            slash_commands_synced = True
        except Exception as sync_err:
            print(f"[System] Slash command sync failed: {sync_err}")

    try:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="Buy Premium!",
        ))
    except Exception as presence_err:
        print(f"[System] Could not set presence: {presence_err}")

    # Load and register persistent Select Menu Role views once per process startup.
    if not select_menu_views_registered:
        smr_data = select_menu_roles.load_select_menu_roles()
        for message_id, data in smr_data.items():
            try:
                guild_id = data.get("guild_id")
                message_data = data.get("message_data") or {}
                components = message_data.get("components") or []
                if components:
                    bot.add_view(select_menu_roles.SelectMenuRoleView(components, guild_id))
            except Exception as e:
                print(f"Failed to register persistent view for message {message_id}: {e}")
        select_menu_views_registered = True

    # Register persistent ticket/key-panel button views
    if not persistent_views_registered:
        bot.add_view(tickets.CreateTicketView())
        bot.add_view(tickets.CloseTicketView())
        bot.add_view(key_panel.KeyPanelButtonView())
        persistent_views_registered = True

    # Start internal IPC trigger receiver (localhost only)
    if ipc_server_task is None:
        ipc_server_task = asyncio.ensure_future(ipc.start_ipc_server())

    # Start public FastAPI dashboard REST API
    if fastapi_server_task is None:
        fastapi_server_task = asyncio.ensure_future(ipc.start_fastapi_server())

    # Start all background task loops
    from modules.giveaways import giveaway_end_check, giveaway_claim_check
    from modules.member_counter import member_counter_update_check
    from modules.role_counter import role_counter_update_loop
    from modules.social_monitor import social_update_check
    from modules.roblox_monitor import roblox_update_check_loop
    from modules.activity_rewards import activity_rewards_draw_loop, activity_rewards_claim_check

    if not giveaway_end_check.is_running():
        giveaway_end_check.start()
    if not giveaway_claim_check.is_running():
        giveaway_claim_check.start()
    if not member_counter_update_check.is_running():
        member_counter_update_check.start()
    if not role_counter_update_loop.is_running():
        role_counter_update_loop.start()
    if not social_update_check.is_running():
        social_update_check.start()
    if roblox_update_check_loop and not roblox_update_check_loop.is_running():
        roblox_update_check_loop.start()
    if not activity_rewards_draw_loop.is_running():
        activity_rewards_draw_loop.start()
    if not activity_rewards_claim_check.is_running():
        activity_rewards_claim_check.start()

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing or empty after normalization.")
    if not _looks_like_bot_token(TOKEN):
        raise RuntimeError(
            "DISCORD_TOKEN format looks invalid. Paste the full Bot Token from Discord Developer Portal -> Bot."
        )

    def _is_discord_login_rate_limited(error: Exception) -> bool:
        if not isinstance(error, discord.HTTPException):
            return False

        status = int(getattr(error, "status", 0) or 0)
        lowered = str(error).lower()
        if status == 429:
            return True

        return (
            "error 1015" in lowered
            or "you are being rate limited" in lowered
            or "access denied | discord.com" in lowered
        )

    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        pass
    except Exception as startup_err:
        if not _is_discord_login_rate_limited(startup_err):
            raise

        wait_seconds = min(900, 40 + random.randint(3, 17))
        print(
            f"[Startup] Discord login rate-limited. "
            f"Waiting {wait_seconds}s before exiting for a clean restart."
        )
        time.sleep(wait_seconds)
        sys.exit(1)
