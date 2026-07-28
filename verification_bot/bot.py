"""
verification_bot/bot.py
Entry point wrapper for WispByte / Pterodactyl hosts where PY_FILE is set to bot.py.
"""

from main import bot, TOKEN

if __name__ == "__main__":
    if not TOKEN:
        print("[VerificationBot] ERROR: No DISCORD_BOT_TOKEN found in .env or environment!")
    else:
        bot.run(TOKEN)
