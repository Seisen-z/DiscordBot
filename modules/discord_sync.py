"""
modules/discord_sync.py
Command-access config changes used to be pushed to Discord's application
command permissions API so restricted commands were hidden from users
without the configured roles.

Disabled: Discord's PUT .../commands/{id}/permissions endpoint rejects bot
tokens outright (403 "Bots cannot use this endpoint", code 20001) — it only
accepts a user's OAuth Bearer token with the applications.commands.permissions.update
scope. Without that OAuth flow this can never succeed, so it's a no-op.
Restricted commands are still enforced at runtime by
access_control.owner_or_allowed_for_locked_commands (bound as the tree's
interaction_check); they just aren't visually hidden from the Discord
slash-command picker.
"""

from __future__ import annotations


async def sync_command_permissions_to_discord(guild_id: str) -> None:
    return
