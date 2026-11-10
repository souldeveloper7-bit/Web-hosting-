import os
import re
import asyncio
import html as H
import logging
import threading

from flask import Flask, Response
from upstash_redis import Redis
from telegram import (
    Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhost-bot")

# ---------------- CONFIG (sab Render ke ENV se aata hai) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000").rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x}
DEV_NAME = os.environ.get("DEV_NAME", "Developer")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "").lstrip("@")
MAX_SITES = int(os.environ.get("MAX_SITES", "10"))
MAX_BYTES = int(float(os.environ.get("MAX_FILE_MB", "1")) * 1024 * 1024)

redis = Redis(
    url=os.environ["UPSTASH_REDIS_REST_URL"],
    token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")
RESERVED = {"admin", "api", "health", "static", "login", "bot", "www", "index", "favicon-ico"}

# ---------------- REDIS KEYS ----------------
# site:<name>        -> html
# owner:<name>       -> user id
# user:<id>:sites    -> set of site names
# users              -> set of all user ids
# banned             -> set of banned user ids


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    return bool(redis.sismember("banned", str(uid)))


def site_url(name: str) -> str:
    return f"{BASE_URL}/{name}"


# ---------------- FLASK (sites serve karta hai) ----------------
web = Flask(__name__)

HOME_HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Web Hosting Bot</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}}
h1{{font-size:2rem}}p{{color:#94a3b8}}</style></head>
<body><div><h1>🌐 Free HTML Hosting</h1>
<p>Telegram bot se apni HTML site live karo.</p>
<p>Developed by {H.escape(DEV_NAME)}</p></div></body></html>"""

NOT_FOUND = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>404</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}</style></head>
<body><div><h1>404</h1><p>Ye site exist nahi karti.</p></div></body></html>"""


@web.route("/")
def home():
    return Response(HOME_HTML, mimetype="text/html")


@web.route("/health")
def health():
    return "ok"


@web.route("/<name>")
def serve_site(name):
    name = name.lower()
    content = redis.get(f"site:{name}")
    if not content:
        return Response(NOT_FOUND, status=404, mimetype="text/html")
    return Response(content, mimetype="text/html")


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------- KEYBOARDS ----------------
def main_menu(uid: int) -> Markup:
    rows = [
        [Btn("📤 Site Upload Karo", callback_data="upload")],
        [Btn("📂 Meri Sites", callback_data="mysites"), Btn("ℹ️ Help", callback_data="help")],
    ]
    dev_row = []
    if DEV_USERNAME:
        dev_row.append(Btn("👨‍💻 Developer", url=f"https://t.me/{DEV_USERNAME}"))
    if is_admin(uid):
        dev_row.append(Btn("🛡 Admin Panel", callback_data="admin"))
    if dev_row:
        rows.append(dev_row)
    return Markup(rows)


def back_btn() -> Markup:
    return Markup([[Btn("🏠 Home", callback_data="home")]])


WELCOME = (
    "👋 <b>Welcome, {name}!</b>\n\n"
    "Main ek <b>Free HTML Hosting Bot</b> hu.\n"
    "Apni <code>.html</code> file bhejo, naam do, aur turant tumhari site live! 🚀\n\n"
    "👇 Neeche button se shuru karo"
)

HELP = (
    "ℹ️ <b>Kaise use karein</b>\n\n"
    "1️⃣ <b>📤 Site Upload Karo</b> dabao\n"
    "2️⃣ Apni <code>.html</code> file bhejo\n"
    "3️⃣ Site ka naam bhejo (a-z, 0-9, -)\n"
    "4️⃣ Link mil jayega ✅\n\n"
    f"📏 Max file size: <b>{MAX_BYTES // (1024 * 1024)} MB</b>\n"
    f"🌐 Max sites per user: <b>{MAX_SITES}</b>\n"
    "✏️ Same naam se dobara upload karoge to site <b>update</b> ho jayegi."
)


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:  # message same ho to error aata hai, ignore
        log.debug("edit skipped: %s", e)


JOIN_TEXT = (
    "🔒 <b>Bot use karne ke liye pehle ye channels join karo</b>\n\n"
    "Join karne ke baad neeche <b>✅ Maine Join Kar Liya</b> dabao."
)
PAGE = 8


def chan_info(cid):
    raw = redis.get(f"chan:{cid}")
    if not raw:
        return str(cid), ""
    title, _, link = str(raw).partition("\t")
    return title or str(cid), link


def user_label(uid):
    raw = redis.get(f"uinfo:{uid}")
    if raw:
        name, _, un = str(raw).partition("\t")
        return (name or str(uid)), un
    return str(uid), ""


async def missing_channels(bot, uid: int):
    out = []
    for cid in sorted(redis.smembers("channels") or []):
        try:
            m = await bot.get_chat_member(int(cid), uid)
            if m.status in ("left", "kicked"):
                out.append(cid)
        except Exception as e:
            log.warning("channel check failed %s: %s", cid, e)
    return out


def join_markup(missing) -> Markup:
    rows = []
    for cid in missing:
        title, link = chan_info(cid)
        if link:
            rows.append([Btn(f"📢 {title[:30]}", url=link)])
    rows.append([Btn("✅ Maine Join Kar Liya", callback_data="chk")])
    return Markup(rows)


async def guard(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """True = aage badho. False = banned ya channel join nahi kiya."""
    u = update.effective_user
    uid = u.id
    if is_banned(uid):
        if update.callback_query:
            await update.callback_query.answer("🚫 Tum banned ho.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 Tumhe is bot se ban kar diya gaya hai.")
        return False
    redis.sadd("users", str(uid))
    redis.set(f"uinfo:{uid}", f"{(u.full_name or '').replace(chr(9), ' ')}\t{u.username or ''}")

    if not is_admin(uid):
        missing = await missing_channels(ctx.bot, uid)
        if missing:
            markup = join_markup(missing)
            q = update.callback_query
            if q:
                await q.answer("❌ Pehle saare channel join karo", show_alert=True)
                if q.data != "chk":
                    await safe_edit(q, JOIN_TEXT, markup)
            else:
                await update.effective_message.reply_text(
                    JOIN_TEXT, parse_mode=ParseMode.HTML, reply_markup=markup)
            return False
    return True


# ---------------- ADMIN SCREENS ----------------
async def show_admin(q):
    users = redis.scard("users")
    banned = redis.scard("banned")
    sites = len(redis.keys("site:*") or [])
    chans = redis.scard("channels")
    await safe_edit(
        q,
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Total users: <b>{users}</b>\n"
        f"🌐 Total sites: <b>{sites}</b>\n"
        f"🚫 Banned: <b>{banned}</b>\n"
        f"📡 Force-join channels: <b>{chans}</b>\n\n"
        "<b>Commands:</b>\n"
        "<code>/ban user_id</code>\n"
        "<code>/unban user_id</code>\n"
        "<code>/delsite name</code>",
        Markup([
            [Btn("👥 Users & Sites", callback_data="users:0")],
            [Btn("📡 Channels", callback_data="channels"), Btn("📢 Broadcast", callback_data="broadcast")],
            [Btn("🔄 Refresh", callback_data="admin"), Btn("🏠 Home", callback_data="home")],
        ]),
    )


async def show_users(q, page: int):
    ids = sorted(redis.smembers("users") or [], key=lambda x: int(x))
    total = len(ids)
    pages = max(1, (total + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * PAGE:(page + 1) * PAGE]
    rows = []
    for x in chunk:
        name, _ = user_label(x)
        cnt = redis.scard(f"user:{x}:sites")
        flag = "🚫 " if is_banned(int(x)) else ""
        rows.append([Btn(f"{flag}{name[:18]} • 🌐 {cnt}", callback_data=f"u:{x}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️ Prev", callback_data=f"users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn("Next ➡️", callback_data=f"users:{page + 1}"))
    rows.append(nav)
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(
        q, f"👥 <b>Users ({total})</b>\n\nKisi user par tap karo, uski sites dikhengi.\n"
           "Format: naam • 🌐 sites ki sankhya", Markup(rows))


async def show_user(q, x: str, page: int):
    name, un = user_label(x)
    sites = sorted(redis.smembers(f"user:{x}:sites") or [])
    banned = is_banned(int(x))
    text = (
        f"👤 <b>{H.escape(name)}</b>\n"
        f"🆔 <code>{x}</code>\n"
        + (f"🔗 @{H.escape(un)}\n" if un else "")
        + f"🌐 Sites: <b>{len(sites)}</b>\n"
        f"🚦 Status: <b>{'Banned 🚫' if banned else 'Active ✅'}</b>\n"
    )
    if sites:
        text += "\n" + "\n".join(f'• <a href="{site_url(n)}">{H.escape(n)}</a>' for n in sites)
    toggle = (Btn("✅ Unban", callback_data=f"unban:{x}:{page}") if banned
              else Btn("🚫 Ban", callback_data=f"ban:{x}:{page}"))
    await safe_edit(q, text, Markup([[toggle], [Btn("⬅️ Back", callback_data=f"users:{page}")]]))


async def show_channels(q):
    chans = sorted(redis.smembers("channels") or [])
    rows = []
    for c in chans:
        title, _ = chan_info(c)
        rows.append([Btn(f"❌ {title[:28]}", callback_data=f"chdel:{c}")])
    rows.append([Btn("➕ Channel Add Karo", callback_data="addch")])
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    text = (
        "📡 <b>Force-Join Channels</b>\n\n"
        "Users ko bot use karne se pehle ye channels join karne honge.\n"
        "Hatane ke liye channel par tap karo.\n\n"
        + (f"Total: <b>{len(chans)}</b>" if chans else "Abhi koi channel add nahi hai.")
    )
    await safe_edit(q, text, Markup(rows))


# ---------------- COMMANDS ----------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    ctx.user_data.clear()
    u = update.effective_user
    await update.message.reply_text(
        WELCOME.format(name=H.escape(u.first_name or "dost")),
        parse_mode=ParseMode.HTML, reply_markup=main_menu(u.id),
    )


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /ban <user_id>")
    redis.sadd("banned", ctx.args[0])
    await update.message.reply_text(f"🚫 {ctx.args[0]} ban ho gaya.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /unban <user_id>")
    redis.srem("banned", ctx.args[0])
    await update.message.reply_text(f"✅ {ctx.args[0]} unban ho gaya.")


async def cmd_delsite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        return await update.message.reply_text("Use: /delsite <name>")
    name = ctx.args[0].lower()
    if not redis.exists(f"site:{name}"):
        return await update.message.reply_text("❌ Aisi site nahi mili.")
    delete_site(name)
    await update.message.reply_text(f"🗑 <code>{H.escape(name)}</code> delete ho gayi.", parse_mode=ParseMode.HTML)


def delete_site(name: str):
    owner = redis.get(f"owner:{name}")
    if owner:
        redis.srem(f"user:{owner}:sites", name)
    redis.delete(f"site:{name}", f"owner:{name}")


# ---------------- BUTTONS ----------------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()

    if data in ("home", "chk"):
        ctx.user_data.clear()
        await safe_edit(q, WELCOME.format(name=H.escape(q.from_user.first_name or "dost")), main_menu(uid))

    elif data == "help":
        await safe_edit(q, HELP, back_btn())

    elif data == "upload":
        count = redis.scard(f"user:{uid}:sites")
        if count >= MAX_SITES and not is_admin(uid):
            return await safe_edit(
                q, f"❌ Tumhari limit ({MAX_SITES} sites) poori ho gayi.\nPehle koi site delete karo.",
                Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]),
            )
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        await safe_edit(
            q, "📤 <b>Ab apni .html file bhejo</b>\n\n(File ko document ki tarah attach karke bhejo)",
            Markup([[Btn("❌ Cancel", callback_data="home")]]),
        )

    elif data == "mysites":
        names = sorted(redis.smembers(f"user:{uid}:sites") or [])
        if not names:
            return await safe_edit(
                q, "📂 Tumhari koi site nahi hai abhi.",
                Markup([[Btn("📤 Upload Karo", callback_data="upload")], [Btn("🏠 Home", callback_data="home")]]),
            )
        rows = [[Btn(f"🌐 {n}", callback_data=f"site:{n}")] for n in names]
        rows.append([Btn("🏠 Home", callback_data="home")])
        await safe_edit(q, f"📂 <b>Tumhari Sites ({len(names)})</b>\n\nKisi par tap karo:", Markup(rows))

    elif data.startswith("site:"):
        name = data[5:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        await safe_edit(
            q, f"🌐 <b>{H.escape(name)}</b>\n\n🔗 {site_url(name)}",
            Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("♻️ Update", callback_data=f"upd:{name}"), Btn("🗑 Delete", callback_data=f"del:{name}")],
                [Btn("⬅️ Back", callback_data="mysites")],
            ]),
        )

    elif data.startswith("upd:"):
        name = data[4:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        ctx.user_data.clear()
        ctx.user_data["state"] = "await_file"
        ctx.user_data["update_name"] = name
        await safe_edit(
            q, f"♻️ <b>{H.escape(name)}</b> update karne ke liye nayi .html file bhejo.",
            Markup([[Btn("❌ Cancel", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("del:"):
        name = data[4:]
        await safe_edit(
            q, f"⚠️ Pakka <b>{H.escape(name)}</b> delete karni hai?",
            Markup([[Btn("✅ Haan, Delete", callback_data=f"delok:{name}"), Btn("❌ Nahi", callback_data=f"site:{name}")]]),
        )

    elif data.startswith("delok:"):
        name = data[6:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi hai.", show_alert=True)
        delete_site(name)
        await safe_edit(q, f"🗑 <b>{H.escape(name)}</b> delete ho gayi.",
                        Markup([[Btn("📂 Meri Sites", callback_data="mysites")], [Btn("🏠 Home", callback_data="home")]]))

    # ----- ADMIN -----
    elif data == "noop":
        pass

    elif data == "admin" and is_admin(uid):
        await show_admin(q)

    elif data.startswith("users:") and is_admin(uid):
        await show_users(q, int(data.split(":")[1]))

    elif data.startswith("u:") and is_admin(uid):
        _, x, pg = data.split(":")
        await show_user(q, x, int(pg))

    elif (data.startswith("ban:") or data.startswith("unban:")) and is_admin(uid):
        act, x, pg = data.split(":")
        if act == "ban":
            redis.sadd("banned", x)
        else:
            redis.srem("banned", x)
        await show_user(q, x, int(pg))

    elif data == "channels" and is_admin(uid):
        await show_channels(q)

    elif data == "addch" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_channel"
        await safe_edit(
            q,
            "📡 <b>Channel add karo</b>\n\n"
            "1️⃣ Pehle bot ko us channel mein <b>Admin</b> banao\n"
            "2️⃣ Phir channel ka <code>@username</code> ya ID bhejo,\n"
            "    ya channel ka koi message yahan <b>forward</b> karo.",
            Markup([[Btn("❌ Cancel", callback_data="channels")]]),
        )

    elif data.startswith("chdel:") and is_admin(uid):
        cid = data[6:]
        redis.srem("channels", cid)
        redis.delete(f"chan:{cid}")
        await show_channels(q)

    elif data == "broadcast" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "broadcast"
        await safe_edit(q, "📢 Jo message sabko bhejna hai wo ab likh kar bhejo.",
                        Markup([[Btn("❌ Cancel", callback_data="admin")]]))


# ---------------- FILE RECEIVE ----------------
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if not fname.endswith((".html", ".htm")):
        return await update.message.reply_text("❌ Sirf <code>.html</code> file bhejo.", parse_mode=ParseMode.HTML)
    if doc.file_size and doc.file_size > MAX_BYTES:
        return await update.message.reply_text(f"❌ File bahut badi hai. Max {MAX_BYTES // (1024 * 1024)} MB.")

    if ctx.user_data.get("state") != "await_file":
        count = redis.scard(f"user:{uid}:sites")
        if count >= MAX_SITES and not is_admin(uid):
            return await update.message.reply_text(f"❌ Limit ({MAX_SITES} sites) poori ho gayi.")

    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    try:
        content = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        content = bytes(raw).decode("latin-1")

    ctx.user_data["html"] = content
    update_name = ctx.user_data.get("update_name")

    if update_name:
        redis.set(f"site:{update_name}", content)
        ctx.user_data.clear()
        return await update.message.reply_text(
            f"✅ <b>{H.escape(update_name)}</b> update ho gayi!\n\n🔗 {site_url(update_name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup([[Btn("🔗 Open Site", url=site_url(update_name))], [Btn("🏠 Home", callback_data="home")]]),
        )

    ctx.user_data["state"] = "await_name"
    await update.message.reply_text(
        "✅ File mil gayi!\n\n✍️ Ab <b>site ka naam</b> bhejo.\n"
        "Rules: 3-30 characters, sirf <code>a-z 0-9 -</code>\n"
        "Example: <code>my-portfolio</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=Markup([[Btn("❌ Cancel", callback_data="home")]]),
    )


# ---------------- TEXT ----------------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, ctx):
        return
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("state")

    if state == "add_channel" and is_admin(uid):
        ref = None
        fo = getattr(update.message, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            ref = fo.chat.id
        elif text.startswith("@"):
            ref = text
        elif text.lstrip("-").isdigit():
            ref = int(text)
        if ref is None:
            return await update.message.reply_text("❌ @username, ID ya forwarded message bhejo.")
        try:
            chat = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi hai")
        except Exception as e:
            log.warning("add channel failed: %s", e)
            return await update.message.reply_text(
                "❌ Channel nahi mila ya bot us channel mein admin nahi hai.\n"
                "Bot ko admin banao aur dobara bhejo.")
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try:
                link = chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id)
            except Exception:
                link = ""
        title = (chat.title or str(chat.id)).replace("\t", " ")
        redis.sadd("channels", str(chat.id))
        redis.set(f"chan:{chat.id}", f"{title}\t{link}")
        ctx.user_data.clear()
        warn = "" if link else "\n\n⚠️ Invite link nahi mila. Bot ko 'Invite users' permission do, warna users ko join button nahi dikhega."
        return await update.message.reply_text(
            f"✅ <b>{H.escape(title)}</b> add ho gaya!{warn}",
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("📡 Channels", callback_data="channels")], [Btn("🛡 Admin Panel", callback_data="admin")]]),
        )

    if state == "broadcast" and is_admin(uid):
        users = redis.smembers("users") or []
        ok = fail = 0
        await update.message.reply_text(f"📢 Bhej raha hu... ({len(users)} users)")
        for u in users:
            try:
                await ctx.bot.send_message(int(u), text)
                ok += 1
            except Exception:
                fail += 1
        ctx.user_data.clear()
        return await update.message.reply_text(f"✅ Done\nSent: {ok}\nFailed: {fail}", reply_markup=back_btn())

    if state == "await_name":
        name = text.lower()
        if not NAME_RE.match(name):
            return await update.message.reply_text(
                "❌ Galat naam. 3-30 characters, sirf <code>a-z 0-9 -</code>, shuru letter/number se.",
                parse_mode=ParseMode.HTML,
            )
        if name in RESERVED:
            return await update.message.reply_text("❌ Ye naam reserved hai, koi aur naam do.")
        if redis.exists(f"site:{name}"):
            return await update.message.reply_text("❌ Ye naam pehle se le liya gaya hai. Koi aur naam do.")

        html_content = ctx.user_data.get("html")
        if not html_content:
            ctx.user_data.clear()
            return await update.message.reply_text("⚠️ File nahi mili, dobara upload karo.", reply_markup=main_menu(uid))

        redis.set(f"site:{name}", html_content)
        redis.set(f"owner:{name}", str(uid))
        redis.sadd(f"user:{uid}:sites", name)
        ctx.user_data.clear()

        return await update.message.reply_text(
            f"🎉 <b>Site live ho gayi!</b>\n\n🌐 Naam: <code>{name}</code>\n🔗 {site_url(name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("📂 Meri Sites", callback_data="mysites"), Btn("🏠 Home", callback_data="home")],
            ]),
        )

    await update.message.reply_text("👇 Menu se option chuno", reply_markup=main_menu(uid))


# ---------------- RUN ----------------
def main():
    threading.Thread(target=run_web, daemon=True).start()
    # Python 3.14 mein event loop khud banana padta hai
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("delsite", cmd_delsite))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
