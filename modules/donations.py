"""
modules/donations.py
Donation panels paid through PayPal (same PayPal REST flow as the Seisen Premium website).

Members click the panel button, enter an amount and get a private PayPal approval link. The bot polls
the order, captures it once the donor approves it, posts a thank-you embed, and can give the donor a
role. Everything (min/max amount, channels, role, panel look) is per-server config stored in
``database/donation_settings.json`` and edited from the dashboard (or /donationpanel setup).

Environment:
  PAYPAL_CLIENT_ID        PayPal REST app client id (required)
  PAYPAL_CLIENT_SECRET    PayPal REST app secret (required)
  PAYPAL_SANDBOX          "true" to use the PayPal sandbox (default: live)
  DONATION_RETURN_URL     Where PayPal sends the donor after approving (default: SITE_URL)
  SITE_URL                Website base URL (default https://seisen.vercel.app)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules.utils import load_json, save_json

_bot: commands.Bot | None = None
SETTINGS_FILE = "donation_settings"
ORDERS_FILE = "donation_orders"
TOTALS_FILE = "donation_totals"

CURRENCY = "EUR"
ABSOLUTE_MIN_AMOUNT = Decimal("1.00")
CHECK_INTERVAL_SECONDS = 10
ORDER_TTL = timedelta(hours=3)  # PayPal approval links stop working after roughly this long
MAX_PENDING_PER_USER = 3
KEEP_FINISHED_ORDERS = 500

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "channel_id": "",
    "message_id": "",
    "announce_channel_id": "",
    "title": "💖 Support Seisen Hub",
    "description": (
        "Enjoying Seisen Hub? You can support it with a donation of any amount.\n"
        "Press the button below and pay securely with PayPal."
    ),
    "button_label": "Donate",
    "embed_color": "#F5B301",
    "embed_thumbnail": "",
    "embed_image": "",
    "embed_footer": "Payments are processed securely by PayPal",
    "min_amount": 1,
    "max_amount": 0,  # 0 = no maximum
    "donor_role_id": "",
    "role_min_total": 0,  # total donated (EUR) needed for the role; 0 = any donation
    "announce": True,
    "dm_thanks": True,
}


def get_settings(guild_id: int | str) -> dict[str, Any]:
    stored = load_json(SETTINGS_FILE, {}).get(str(guild_id), {})
    merged = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def save_settings(guild_id: int | str, settings: dict[str, Any]) -> None:
    data = load_json(SETTINGS_FILE, {})
    data[str(guild_id)] = settings
    save_json(SETTINGS_FILE, data)


def _to_decimal(value: Any, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else default
    except (InvalidOperation, ValueError):
        return default


def amount_limits(settings: dict[str, Any]) -> tuple[Decimal, Decimal | None]:
    """Return (minimum, maximum). Maximum is None when there is no limit."""
    minimum = max(_to_decimal(settings.get("min_amount"), ABSOLUTE_MIN_AMOUNT), ABSOLUTE_MIN_AMOUNT)
    maximum = _to_decimal(settings.get("max_amount"), Decimal(0))
    return minimum, (maximum if maximum > 0 else None)


# ── PayPal REST helper ───────────────────────────────────────────────────────

class PayPalError(Exception):
    pass


class PayPalClient:
    def __init__(self) -> None:
        self.client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
        self.sandbox = os.getenv("PAYPAL_SANDBOX", "").strip().lower() == "true"
        self.base_url = "https://api-m.sandbox.paypal.com" if self.sandbox else "https://api-m.paypal.com"
        self._token: str | None = None
        self._token_expiry: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        async with session.post(
            f"{self.base_url}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=auth,
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise PayPalError(f"PayPal auth failed ({resp.status}): {data}")
        self._token = data["access_token"]
        self._token_expiry = now + timedelta(seconds=max(60, int(data.get("expires_in", 300)) - 300))
        return self._token

    async def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            token = await self._access_token(session)
            async with session.request(
                method,
                f"{self.base_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data

    async def create_order(self, amount: Decimal, custom_id: str, return_url: str) -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": CURRENCY, "value": f"{amount:.2f}"},
                "description": "Donation to Seisen Hub",
                "custom_id": custom_id,
            }],
            "application_context": {
                "brand_name": "Seisen Hub",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": return_url,
            },
        }
        status, data = await self._request("POST", "/v2/checkout/orders", body)
        if status not in (200, 201):
            raise PayPalError(f"Create order failed ({status}): {data}")
        return data

    async def get_order(self, order_id: str) -> dict:
        status, data = await self._request("GET", f"/v2/checkout/orders/{order_id}")
        if status != 200:
            raise PayPalError(f"Get order failed ({status}): {data}")
        return data

    async def capture_order(self, order_id: str) -> dict:
        status, data = await self._request("POST", f"/v2/checkout/orders/{order_id}/capture", {})
        if status in (200, 201):
            return data
        details = data.get("details") if isinstance(data, dict) else None
        if isinstance(details, list) and any(d.get("issue") == "ORDER_ALREADY_CAPTURED" for d in details):
            return await self.get_order(order_id)
        raise PayPalError(f"Capture failed ({status}): {data}")


_paypal = PayPalClient()


def paypal_mode() -> dict[str, Any]:
    return {"configured": _paypal.configured, "sandbox": _paypal.sandbox}


def _approval_link(order: dict) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link.get("href")
    return None


def _capture_of(order: dict) -> dict | None:
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]
    except (KeyError, IndexError, TypeError):
        return None


def _return_url() -> str:
    return (
        os.getenv("DONATION_RETURN_URL")
        or os.getenv("SITE_URL")
        or "https://seisen.vercel.app"
    ).strip()


# ── Amount parsing ───────────────────────────────────────────────────────────

def parse_amount(raw: str, minimum: Decimal = ABSOLUTE_MIN_AMOUNT, maximum: Decimal | None = None) -> Decimal:
    """Parse a donor-typed amount. Raises ValueError with a user-facing message."""
    cleaned = (raw or "").strip().replace(",", ".").replace("€", "").replace("EUR", "").replace("eur", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Please enter a number, for example `5` or `12.50`.")
    if not amount.is_finite():
        raise ValueError("Please enter a valid amount.")
    amount = amount.quantize(Decimal("0.01"))
    if amount < minimum:
        raise ValueError(f"The minimum donation is €{minimum:.2f}.")
    if maximum is not None and amount > maximum:
        raise ValueError(f"The maximum donation is €{maximum:.2f}.")
    return amount


# ── Panel message ────────────────────────────────────────────────────────────

def _parse_color(raw: Any) -> discord.Color:
    try:
        if isinstance(raw, str) and raw.startswith("#"):
            return discord.Color(int(raw.lstrip("#"), 16))
        if raw:
            return discord.Color(int(raw))
    except (ValueError, TypeError):
        pass
    return discord.Color.gold()


def build_panel_embed(settings: dict[str, Any]) -> discord.Embed:
    minimum, maximum = amount_limits(settings)
    limits = f"Minimum **€{minimum:.2f}**" + (f" · Maximum **€{maximum:.2f}**" if maximum else " · No maximum")
    embed = discord.Embed(
        title=settings.get("title") or DEFAULT_SETTINGS["title"],
        description=f"{settings.get('description') or ''}\n\n{limits}".strip(),
        color=_parse_color(settings.get("embed_color")),
    )
    if settings.get("embed_thumbnail"):
        embed.set_thumbnail(url=settings["embed_thumbnail"])
    if settings.get("embed_image"):
        embed.set_image(url=settings["embed_image"])
    footer = (settings.get("embed_footer") or "").strip()
    if footer:
        embed.set_footer(text=footer)
    return embed


async def publish_panel(guild: discord.Guild) -> tuple[bool, str, str | None]:
    """Post the panel, or edit the live one in place. Returns (ok, message, message_id)."""
    settings = get_settings(guild.id)
    channel_id = settings.get("channel_id")
    channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
    if channel is None or not hasattr(channel, "send"):
        return False, "Select a valid panel channel first.", None

    embed = build_panel_embed(settings)
    view = DonationPanelView(button_label=settings.get("button_label") or "Donate")

    existing_id = settings.get("message_id")
    if str(existing_id).isdigit():
        try:
            message = await channel.fetch_message(int(existing_id))
            await message.edit(embed=embed, view=view)
            return True, "Panel updated.", str(message.id)
        except discord.NotFound:
            pass  # message was deleted or the channel changed, post a fresh one
        except Exception as exc:
            return False, f"Failed to edit panel: {exc}", None

    try:
        message = await channel.send(embed=embed, view=view)
    except Exception as exc:
        return False, f"Failed to send panel: {exc}", None

    settings["message_id"] = str(message.id)
    save_settings(guild.id, settings)
    return True, "Panel posted.", str(message.id)


# ── Views ────────────────────────────────────────────────────────────────────

class DonateModal(discord.ui.Modal, title="Donate to Seisen Hub"):
    def __init__(self, settings: dict[str, Any]):
        super().__init__()
        minimum, maximum = amount_limits(settings)
        label = f"Amount in EUR (min {minimum:.0f}" + (f", max {maximum:.0f})" if maximum else ")")
        self.amount_input = discord.ui.TextInput(
            label=label[:45], placeholder=f"{minimum:.2f}", min_length=1, max_length=12, required=True
        )
        self.note_input = discord.ui.TextInput(
            label="Message (optional)", style=discord.TextStyle.paragraph, max_length=200, required=False
        )
        self.add_item(self.amount_input)
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Donations only work inside a server.", ephemeral=True)
            return

        settings = get_settings(guild.id)
        if not settings.get("enabled", True):
            await interaction.response.send_message("❌ Donations are currently disabled.", ephemeral=True)
            return

        minimum, maximum = amount_limits(settings)
        try:
            amount = parse_amount(str(self.amount_input.value), minimum, maximum)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if not _paypal.configured:
            await interaction.response.send_message(
                "❌ Donations are not configured yet. Please tell a staff member.", ephemeral=True
            )
            return

        orders = load_json(ORDERS_FILE, {})
        pending_for_user = sum(
            1 for o in orders.values()
            if o.get("status") == "pending" and o.get("user_id") == interaction.user.id
        )
        if pending_for_user >= MAX_PENDING_PER_USER:
            await interaction.response.send_message(
                "⏳ You already have unfinished donation links. Finish one or wait a few minutes.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            order = await _paypal.create_order(
                amount,
                custom_id=f"donation:{interaction.user.id}",
                return_url=_return_url(),
            )
        except Exception as exc:
            print(f"[Donations] create_order failed: {exc}")
            await interaction.followup.send(
                "❌ Could not start the PayPal checkout. Please try again in a moment.", ephemeral=True
            )
            return

        link = _approval_link(order)
        if not link:
            print(f"[Donations] No approval link in order response: {order}")
            await interaction.followup.send("❌ PayPal did not return a payment link.", ephemeral=True)
            return

        orders = load_json(ORDERS_FILE, {})
        orders[order["id"]] = {
            "status": "pending",
            "user_id": interaction.user.id,
            "guild_id": guild.id,
            "amount": f"{amount:.2f}",
            "currency": CURRENCY,
            "note": str(self.note_input.value or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_json(ORDERS_FILE, orders)

        embed = discord.Embed(
            title="💖 Complete your donation",
            description=(
                f"Amount: **€{amount:.2f}**\n\n"
                "Press the button below to pay with PayPal. Once you approve the payment "
                "this is picked up automatically, no need to come back here.\n"
                "The link expires in about 3 hours."
            ),
            color=discord.Color.gold(),
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Pay with PayPal", style=discord.ButtonStyle.link, url=link))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class DonationPanelView(discord.ui.View):
    def __init__(self, button_label: str = "Donate"):
        super().__init__(timeout=None)
        for child in self.children:
            if getattr(child, "custom_id", None) == "donation:donate":
                child.label = button_label

    @discord.ui.button(label="Donate", style=discord.ButtonStyle.success, emoji="💖", custom_id="donation:donate")
    async def donate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Donations only work inside a server.", ephemeral=True)
            return
        await interaction.response.send_modal(DonateModal(get_settings(interaction.guild.id)))


# ── Order processing ─────────────────────────────────────────────────────────

def _record_total(guild_id: int, user_id: int, amount: Decimal) -> Decimal:
    """Add a completed donation to the donor's running total and return the new total."""
    totals = load_json(TOTALS_FILE, {})
    guild_totals = totals.setdefault(str(guild_id), {})
    entry = guild_totals.setdefault(str(user_id), {"total": "0.00", "count": 0})
    new_total = _to_decimal(entry.get("total"), Decimal(0)) + amount
    entry["total"] = f"{new_total:.2f}"
    entry["count"] = int(entry.get("count", 0)) + 1
    save_json(TOTALS_FILE, totals)
    return new_total


async def _grant_donor_role(guild: discord.Guild, user_id: int, total: Decimal, settings: dict[str, Any]) -> None:
    role_id = settings.get("donor_role_id")
    if not str(role_id).isdigit():
        return
    needed = _to_decimal(settings.get("role_min_total"), Decimal(0))
    if total < needed:
        return
    role = guild.get_role(int(role_id))
    if role is None:
        return
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if role not in member.roles:
            await member.add_roles(role, reason="Donation")
    except discord.NotFound:
        pass  # donor left the server
    except Exception as exc:
        print(f"[Donations] Could not give role {role_id} to {user_id}: {exc}")


async def _announce(order_id: str, record: dict, amount: str, total: Decimal) -> None:
    if _bot is None:
        return
    guild = _bot.get_guild(int(record["guild_id"]))
    if guild is None:
        return
    settings = get_settings(guild.id)
    user_id = record["user_id"]

    await _grant_donor_role(guild, user_id, total, settings)

    if settings.get("announce", True):
        channel_id = settings.get("announce_channel_id") or settings.get("channel_id")
        channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if channel is not None:
            embed = discord.Embed(
                title="💖 New donation",
                description=f"<@{user_id}> donated **€{amount}**. Thank you for supporting Seisen Hub!",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            note = record.get("note")
            if note:
                embed.add_field(name="Message", value=note[:200], inline=False)
            try:
                await channel.send(
                    embed=embed, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)])
                )
            except Exception as exc:
                print(f"[Donations] Could not announce {order_id}: {exc}")

    if settings.get("dm_thanks", True):
        try:
            user = _bot.get_user(user_id) or await _bot.fetch_user(user_id)
            await user.send(f"💖 Thank you! Your **€{amount}** donation to Seisen Hub was received.")
        except Exception:
            pass  # DMs closed


async def _process_order(order_id: str, record: dict) -> str | None:
    """Advance one pending order. Returns the new status, or None if it is still pending."""
    created = datetime.fromisoformat(record["created_at"])
    order = await _paypal.get_order(order_id)
    status = order.get("status")

    if status == "APPROVED":
        order = await _paypal.capture_order(order_id)
        status = order.get("status")

    if status == "COMPLETED":
        capture = _capture_of(order)
        if capture is None:
            return None
        cap_status = capture.get("status")
        if cap_status == "COMPLETED":
            amount = capture["amount"]
            record["captured_amount"] = amount["value"]
            record["captured_currency"] = amount["currency_code"]
            record["transaction_id"] = capture.get("id")
            record["payer_email"] = (order.get("payer") or {}).get("email_address")
            return "completed"
        if cap_status in ("DECLINED", "FAILED", "REFUNDED"):
            return "failed"
        return None  # PENDING capture (e.g. under review), keep polling

    if status == "VOIDED":
        return "expired"

    if datetime.now(timezone.utc) - created > ORDER_TTL:
        return "expired"
    return None


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def donation_check_loop():
    if _bot is None or not _paypal.configured:
        return

    orders = load_json(ORDERS_FILE, {})
    pending = [(oid, rec) for oid, rec in orders.items() if rec.get("status") == "pending"]
    if not pending:
        return

    changed = False
    for order_id, record in pending:
        try:
            new_status = await _process_order(order_id, record)
        except Exception as exc:
            print(f"[Donations] Error checking order {order_id}: {exc}")
            continue
        if new_status is None:
            continue

        record["status"] = new_status
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        orders[order_id] = record
        changed = True
        # Persist before announcing so a crash can never announce the same donation twice.
        save_json(ORDERS_FILE, orders)
        if new_status == "completed":
            captured = _to_decimal(record.get("captured_amount") or record["amount"], Decimal(0))
            total = _record_total(int(record["guild_id"]), record["user_id"], captured)
            await _announce(order_id, record, f"{captured:.2f}", total)

    if changed:
        finished = [k for k, v in orders.items() if v.get("status") != "pending"]
        for old in finished[:-KEEP_FINISHED_ORDERS]:
            del orders[old]
        save_json(ORDERS_FILE, orders)


@donation_check_loop.before_loop
async def _before_donation_loop():
    if _bot is not None:
        await _bot.wait_until_ready()


def guild_stats(guild_id: int | str) -> dict[str, Any]:
    """Totals and recent donations for the dashboard."""
    gid = str(guild_id)
    donors = load_json(TOTALS_FILE, {}).get(gid, {})
    raised = sum((_to_decimal(d.get("total"), Decimal(0)) for d in donors.values()), Decimal(0))
    count = sum(int(d.get("count", 0)) for d in donors.values())

    recent = [
        {
            "user_id": str(rec.get("user_id")),
            "amount": rec.get("captured_amount") or rec.get("amount"),
            "note": rec.get("note", ""),
            "finished_at": rec.get("finished_at"),
        }
        for rec in load_json(ORDERS_FILE, {}).values()
        if rec.get("status") == "completed" and str(rec.get("guild_id")) == gid
    ]
    recent.sort(key=lambda r: r.get("finished_at") or "", reverse=True)

    return {
        "total_raised": f"{raised:.2f}",
        "donation_count": count,
        "donor_count": len(donors),
        "recent": recent[:10],
    }


# ── Slash commands ───────────────────────────────────────────────────────────

donationpanel_group = app_commands.Group(name="donationpanel", description="Manage the donation panel")


@donationpanel_group.command(name="setup", description="Post (or refresh) the PayPal donation panel in a channel")
@app_commands.describe(
    panel_channel="Channel where the donation panel will be posted",
    announce_channel="Channel for thank-you messages (default: the panel channel)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def donationpanel_setup(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    announce_channel: discord.TextChannel | None = None,
):
    await interaction.response.defer(ephemeral=True)

    if not _paypal.configured:
        await interaction.followup.send(
            "❌ Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in the bot's .env first.", ephemeral=True
        )
        return

    settings = get_settings(interaction.guild.id)
    if str(settings.get("channel_id")) != str(panel_channel.id):
        settings["message_id"] = ""  # moving channels: post a new panel instead of editing the old one
    settings["channel_id"] = str(panel_channel.id)
    if announce_channel is not None:
        settings["announce_channel_id"] = str(announce_channel.id)
    save_settings(interaction.guild.id, settings)

    ok, message, _ = await publish_panel(interaction.guild)
    if not ok:
        await interaction.followup.send(f"❌ {message}", ephemeral=True)
        return

    mode = "SANDBOX (test payments only)" if _paypal.sandbox else "LIVE"
    await interaction.followup.send(
        f"✅ {message} in {panel_channel.mention}. PayPal mode: **{mode}**.\n"
        "Minimum/maximum, donor role and the panel look are edited in the dashboard (Donations).",
        ephemeral=True,
    )


@donationpanel_group.command(name="delete", description="Delete the donation panel message")
@app_commands.checks.has_permissions(manage_guild=True)
async def donationpanel_delete(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = get_settings(interaction.guild.id)
    message_id = settings.get("message_id")
    channel_id = settings.get("channel_id")
    if not str(message_id).isdigit():
        await interaction.followup.send("❌ There is no donation panel posted.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
    if channel:
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete()
        except Exception:
            pass

    settings["message_id"] = ""
    save_settings(interaction.guild.id, settings)
    await interaction.followup.send("🗑️ Donation panel deleted.", ephemeral=True)


# ── Registration ─────────────────────────────────────────────────────────────

def register(bot: commands.Bot):
    global _bot
    _bot = bot
    bot.tree.add_command(donationpanel_group)
    bot.add_view(DonationPanelView())
