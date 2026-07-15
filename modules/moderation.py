"""
modules/moderation.py
Moderation slash commands – clear, kick, ban, unban, banlist, baninfo, purge, invite, invitelist.
Includes BanListView paginator.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands


# ── Ban List Paginator ────────────────────────────────────────────────────────

class BanListView(discord.ui.View):
    PAGE_SIZE = 15

    def __init__(self, lines: list[str], guild_name: str, total: int, author_id: int):
        super().__init__(timeout=120)
        self.lines = lines
        self.guild_name = guild_name
        self.total = total
        self.author_id = author_id
        self.page = 0
        self.max_page = (len(lines) - 1) // self.PAGE_SIZE
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page == self.max_page

    def build_embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        chunk = self.lines[start : start + self.PAGE_SIZE]
        embed = discord.Embed(
            title=f"🔨 Banned Users — {self.guild_name}",
            description="\n".join(chunk),
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {self.total} ban(s)")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_commands(bot: discord.ext.commands.Bot):
    @bot.tree.command(name="clear", description="Delete a number of messages (1-100)")
    @app_commands.describe(amount="Number of messages to delete (1-100)", ignore_old="Skip messages older than 14 days to prevent rate limits (default: True)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def clear(interaction: discord.Interaction, amount: int, ignore_old: bool = True):
        if not 1 <= amount <= 100:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Please provide a number between 1 and 100.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Please provide a number between 1 and 100.", ephemeral=True)
            return
        
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            if e.code != 40060:
                raise
        
        # When bulk deleting, older messages (>14 days) fail in bulk and fall back to single deletions,
        # executing up to 100 quick API calls and causing 429 rate limit errors.
        # This checks age and stops single deletions unless specifically asked for.
        if ignore_old:
            import datetime
            import discord.utils
            two_weeks_ago = discord.utils.utcnow() - datetime.timedelta(days=14)
            await interaction.channel.purge(limit=amount, after=two_weeks_ago, bulk=True)
            await interaction.followup.send(f"✅ Deleted up to **{amount}** recent message(s). Older ones were skipped.", ephemeral=True)
        else:
            deleted = await interaction.channel.purge(limit=amount, bulk=True)
            await interaction.followup.send(f"✅ Deleted **{len(deleted)}** message(s).", ephemeral=True)

    @bot.tree.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(
        member="The member to kick",
        reason="Reason for the kick",
        delete_messages="Delete this user's recent messages",
    )
    @app_commands.choices(delete_messages=[
        app_commands.Choice(name="Don't delete", value=0),
        app_commands.Choice(name="Previous 1 hour", value=3600),
        app_commands.Choice(name="Previous 6 hours", value=21600),
        app_commands.Choice(name="Previous 12 hours", value=43200),
        app_commands.Choice(name="Previous 24 hours", value=86400),
        app_commands.Choice(name="Previous 3 days", value=259200),
        app_commands.Choice(name="Previous 7 days", value=604800),
    ])
    @app_commands.default_permissions(kick_members=True)
    async def kick(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
        delete_messages: int = 0,
    ):
        await interaction.response.defer()
        member_id = member.id
        member_str = str(member)
        await member.kick(reason=reason)

        deleted_count = 0
        if delete_messages > 0:
            import datetime as dt
            cutoff = discord.utils.utcnow() - dt.timedelta(seconds=delete_messages)
            for channel in interaction.guild.text_channels:
                try:
                    msgs = [
                        m async for m in channel.history(limit=500, after=cutoff)
                        if m.author.id == member_id
                    ]
                    if msgs:
                        for chunk in [msgs[i:i+100] for i in range(0, len(msgs), 100)]:
                            try:
                                await channel.delete_messages(chunk)
                                deleted_count += len(chunk)
                            except discord.HTTPException:
                                pass
                except (discord.Forbidden, discord.HTTPException):
                    pass

        msg = f"👢 **{member_str}** has been kicked. Reason: {reason}"
        if deleted_count:
            msg += f"\n🗑️ Deleted **{deleted_count}** message(s) from the past {delete_messages // 3600 if delete_messages >= 3600 else delete_messages // 60} {'hour(s)' if delete_messages >= 3600 else 'minute(s)'}."
        await interaction.followup.send(msg)

    @bot.tree.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="The member to ban",
        reason="Reason for the ban",
        delete_messages="Delete this user's recent messages",
    )
    @app_commands.choices(delete_messages=[
        app_commands.Choice(name="Don't delete", value=0),
        app_commands.Choice(name="Previous 1 hour", value=3600),
        app_commands.Choice(name="Previous 6 hours", value=21600),
        app_commands.Choice(name="Previous 12 hours", value=43200),
        app_commands.Choice(name="Previous 24 hours", value=86400),
        app_commands.Choice(name="Previous 3 days", value=259200),
        app_commands.Choice(name="Previous 7 days", value=604800),
    ])
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
        delete_messages: int = 0,
    ):
        await member.ban(reason=reason, delete_message_seconds=delete_messages)
        msg = f"🔨 **{member}** has been banned. Reason: {reason}"
        if delete_messages > 0:
            hours = delete_messages // 3600
            label = f"{hours} hour(s)" if hours >= 1 else f"{delete_messages // 60} minute(s)"
            msg += f"\n🗑️ Discord will delete their messages from the past {label}."
        await interaction.response.send_message(msg)

    @bot.tree.command(name="unban", description="Unban a user from the server by their ID")
    @app_commands.describe(user_id="The ID of the user to unban", reason="Reason for the unban")
    @app_commands.default_permissions(ban_members=True)
    async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        try:
            user = await bot.fetch_user(int(user_id))
        except (ValueError, discord.NotFound):
            await interaction.response.send_message("❌ Could not find a user with that ID.", ephemeral=True)
            return
        try:
            await interaction.guild.unban(user, reason=reason)
            await interaction.response.send_message(f"✅ **{user}** has been unbanned. Reason: {reason}")
        except discord.NotFound:
            await interaction.response.send_message("❌ That user is not banned.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unban members.", ephemeral=True)

    @bot.tree.command(name="banlist", description="List all banned users in the server")
    @app_commands.default_permissions(ban_members=True)
    async def banlist(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bans = [entry async for entry in interaction.guild.bans()]
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to view bans.", ephemeral=True)
            return
        if not bans:
            await interaction.followup.send("✅ There are no banned users in this server.", ephemeral=True)
            return
        lines = [
            f"**{entry.user}** (`{entry.user.id}`) — {entry.reason or 'No reason provided'}"
            for entry in bans
        ]
        view = BanListView(lines, interaction.guild.name, len(bans), interaction.user.id)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    @bot.tree.command(name="baninfo", description="View the profile of a banned user")
    @app_commands.describe(user_id="The ID of the banned user")
    @app_commands.default_permissions(ban_members=True)
    async def baninfo(interaction: discord.Interaction, user_id: str):
        try:
            uid = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Please provide a valid user ID.", ephemeral=True)
            return
        try:
            ban_entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            await interaction.response.send_message("❌ That user is not banned in this server.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to view bans.", ephemeral=True)
            return
        user = ban_entry.user
        embed = discord.Embed(title=f"🔨 Banned User — {user}", color=discord.Color.red(), timestamp=datetime.utcnow())
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Username", value=str(user), inline=True)
        embed.add_field(name="User ID", value=user.id, inline=True)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(user.created_at, "D"), inline=True)
        embed.add_field(name="Ban Reason", value=ban_entry.reason or "No reason provided", inline=False)
        embed.set_image(url=user.display_avatar.url)
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="purge", description="Bulk delete messages in a channel (up to 1000)")
    @app_commands.describe(amount="Number of messages to delete (1-1000)", channel="Channel to purge (defaults to current)")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int, channel: discord.TextChannel = None):
        if not 1 <= amount <= 1000:
            await interaction.response.send_message("❌ Please provide a number between 1 and 1000.", ephemeral=True)
            return
        channel = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await channel.purge(limit=amount, bulk=True)
            await interaction.followup.send(f"✅ Deleted **{len(deleted)}** message(s) in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to delete messages in that channel.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @bot.tree.command(name="invite", description="Get a permanent invite link for this server")
    async def invite(interaction: discord.Interaction):
        try:
            existing = await interaction.guild.invites()
            permanent = next((i for i in existing if i.max_age == 0 and i.max_uses == 0), None)
            inv = permanent or await interaction.channel.create_invite(max_age=0, max_uses=0, unique=True)
            embed = discord.Embed(
                title=f"Invite to {interaction.guild.name}",
                description=f"**{inv.url}**",
                color=discord.Color.blurple(),
            )
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            embed.set_footer(text=f"Requested by {interaction.user}")
            await interaction.response.send_message(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create or view invites.", ephemeral=True)

    @bot.tree.command(name="invitelist", description="List all active invites in the server")
    @app_commands.default_permissions(manage_guild=True)
    async def invitelist(interaction: discord.Interaction):
        try:
            invites = await interaction.guild.invites()
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to view invites.", ephemeral=True)
            return
        if not invites:
            await interaction.response.send_message("📭 There are no active invites in this server.", ephemeral=True)
            return
        lines = []
        for inv in invites:
            uses = f"{inv.uses}/{inv.max_uses if inv.max_uses else '∞'}"
            expiry = discord.utils.format_dt(inv.expires_at, "R") if inv.expires_at else "Never"
            channel = inv.channel.mention if inv.channel else "Unknown"
            creator = str(inv.inviter) if inv.inviter else "Unknown"
            lines.append(f"`{inv.url}` — {channel} — Uses: **{uses}** — Expires: {expiry} — By: {creator}")
        description = "\n".join(lines[:10])
        if len(lines) > 10:
            description += f"\n\n*Showing first 10 of {len(lines)} invites.*"
        embed = discord.Embed(
            title=f"Active Invites — {interaction.guild.name}",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Total: {len(invites)} invite(s) • Requested by {interaction.user}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Registration ──────────────────────────────────────────────────────────────

def register(bot: discord.ext.commands.Bot):
    _register_commands(bot)
