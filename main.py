import os
import re
import io
import base64
import asyncio
import zipfile
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

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:10000").rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_ID", "").replace(" ", "").split(",") if x}
DEV_NAME = os.environ.get("DEV_NAME", "Developer")
DEV_USERNAME = os.environ.get("DEV_USERNAME", "").lstrip("@")
MAX_SITES = int(os.environ.get("MAX_SITES", "10"))
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "1"))
MAX_TOTAL_MB = float(os.environ.get("MAX_TOTAL_MB", "20"))
MAX_FILES = int(os.environ.get("MAX_FILES", "100"))

# ZIP security limits
ZIP_MAX_FILES = 500
ZIP_MAX_TOTAL = 50 * 1024 * 1024      # 50 MB extracted
ZIP_MAX_DEPTH = 5

redis = Redis(url=os.environ["UPSTASH_REDIS_REST_URL"],
              token=os.environ["UPSTASH_REDIS_REST_TOKEN"])

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,29}$")
RESERVED = {"admin", "api", "health", "static", "login", "bot", "www",
            "index", "favicon-ico", "robots", "sitemap", "well-known"}

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".xml":  "application/xml",
    ".txt":  "text/plain; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".csv":  "text/csv; charset=utf-8",
    ".py":   "text/plain; charset=utf-8",   # browser me view/download
    ".sh":   "text/plain; charset=utf-8",
    ".c":    "text/plain; charset=utf-8",
    ".cpp":  "text/plain; charset=utf-8",
    ".java": "text/plain; charset=utf-8",
    ".ts":   "text/plain; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".svg":  "image/svg+xml",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico":  "image/x-icon",
    ".bmp":  "image/bmp",
    ".mp3":  "audio/mpeg",
    ".wav":  "audio/wav",
    ".ogg":  "audio/ogg",
    ".m4a":  "audio/mp4",
    ".flac": "audio/flac",
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
    ".mov":  "video/quicktime",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".eot":  "application/vnd.ms-fontobject",
    ".pdf":  "application/pdf",
    ".wasm": "application/wasm",
}
ALLOWED_EXT = set(MIME_MAP.keys())


def is_admin(uid): return uid in ADMIN_IDS
def is_banned(uid): return bool(redis.sismember("banned", str(uid)))
def site_url(name): return f"{BASE_URL}/{name}"
def mime_for(path):
    return MIME_MAP.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


# ============ ZIP SAFE EXTRACT ============
def safe_zip_extract(raw_bytes: bytes, base_path: str = ""):
    """
    Returns (files_dict, errors_list)
    files_dict: {path: {"b64": str, "size": int, "mime": str}}
    """
    files = {}
    errors = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile:
        return {}, ["❌ Invalid ZIP file"]

    infos = zf.infolist()
    if len(infos) > ZIP_MAX_FILES:
        return {}, [f"❌ ZIP me {len(infos)} files hain, max {ZIP_MAX_FILES} allowed"]

    total_size = 0
    for info in infos:
        name = info.filename.replace("\\", "/")

        # skip directories
        if name.endswith("/"):
            continue

        # path traversal protection
        if ".." in name.split("/") or name.startswith("/"):
            errors.append(f"⚠️ Skip: {name} (unsafe path)")
            continue

        # depth check
        if name.count("/") > ZIP_MAX_DEPTH:
            errors.append(f"⚠️ Skip: {name} (too deep)")
            continue

        # allowed ext check
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            errors.append(f"⚠️ Skip: {name} ({ext} not allowed)")
            continue

        # size
        if info.file_size > ZIP_MAX_TOTAL:
            errors.append(f"⚠️ Skip: {name} (too large)")
            continue
        total_size += info.file_size
        if total_size > ZIP_MAX_TOTAL:
            errors.append("❌ ZIP total size limit exceeded, baaki skip kiye")
            break

        # read
        try:
            data = zf.read(info)
        except Exception as e:
            errors.append(f"⚠️ Read fail: {name}")
            continue

        full_path = f"{base_path}{name}" if base_path else name
        files[full_path] = {
            "b64": base64.b64encode(data).decode(),
            "size": info.file_size,
            "mime": mime_for(full_path),
        }

    zf.close()

    # Smart base-path detection: agar sab files ek hi folder ke andar hain
    # (jaise "mysite/index.html", "mysite/style.css"), to root hata do
    if files:
        prefixes = set()
        for p in files:
            if "/" in p:
                prefixes.add(p.split("/")[0])
            else:
                prefixes.add("")   # koi file root me hai
        if len(prefixes) == 1 and "" not in prefixes:
            prefix = prefixes.pop() + "/"
            files = {p[len(prefix):]: v for p, v in files.items() if p.startswith(prefix)}

    return files, errors


# ============ FLASK ============
web = Flask(__name__)


@web.route("/")
def home():
    return Response(
        f"<h1>🌐 Free Website Hosting</h1><p>Developed by {H.escape(DEV_NAME)}</p>",
        mimetype="text/html")


@web.route("/health")
def health():
    return "ok"


@web.route("/<name>")
def site_root(name):
    name = name.lower()
    for candidate in ("index.html", "index.htm"):
        raw = redis.get(f"file:{name}:{candidate}")
        if raw:
            return Response(base64.b64decode(raw), mimetype=mime_for(candidate))
    if redis.get(f"site:{name}"):
        files = sorted(redis.smembers(f"site:{name}:files") or [])
        links = "".join(f'<li><a href="/{name}/{f}">{H.escape(f)}</a></li>' for f in files)
        return Response(
            f"<h1>📁 {H.escape(name)}</h1><ul>{links}</ul>",
            mimetype="text/html")
    return Response("<h1>404</h1>", status=404, mimetype="text/html")


@web.route("/<name>/<path:file>")
def site_file(name, file):
    name = name.lower()
    raw = redis.get(f"file:{name}:{file}")
    if not raw:
        return Response("<h1>404</h1>", status=404, mimetype="text/html")
    return Response(base64.b64decode(raw), mimetype=mime_for(file))


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)


# ============ KEYBOARDS ============
def main_menu(uid):
    rows = [
        [Btn("📤 Website Upload Karo", callback_data="upload")],
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


def back_btn(): return Markup([[Btn("🏠 Home", callback_data="home")]])


WELCOME = (
    "👋 <b>Welcome, {name}!</b>\n\n"
    "Main ek <b>Full Website Hosting Bot</b> hu.\n"
    "HTML, CSS, JS, images, MP3, <code>.py</code>, <code>.zip</code> — sab upload karo, ek professional website live karo! 🚀\n\n"
    "👇 Neeche se shuru karo"
)

HELP = (
    "ℹ️ <b>Kaise use karein</b>\n\n"
    "1️⃣ <b>📤 Website Upload Karo</b> dabao\n"
    "2️⃣ Saari files bhejo (HTML, CSS, JS, images, MP3, .py...)\n"
    "3️⃣ <b>ya</b> ek <b>.zip</b> file bhejo — bot auto extract karega\n"
    "4️⃣ Jab ho jaye <b>✅ Done</b> dabao\n"
    "5️⃣ Site ka naam bhejo (a-z, 0-9, -)\n"
    "6️⃣ Link mil jayega ✅\n\n"
    "💡 <b>Folder path</b>: file ka caption me path likho, jaise <code>assets/logo.png</code>\n\n"
    "📦 <b>ZIP</b>: andar ki saari files (folders ke saath) auto add ho jayengi. "
    "<code>index.html</code> zaroor include karo.\n\n"
    "🐍 <b>.py</b> files download/view ke liye serve hoti hain (browser me execute nahi hotin — "
    "browser me chalane ke liye <b>PyScript</b> wali HTML use karo).\n\n"
    f"📏 Max per file: <b>{MAX_FILE_MB} MB</b>\n"
    f"📦 Max total per site: <b>{MAX_TOTAL_MB} MB</b>\n"
    f"📄 Max files per site: <b>{MAX_FILES}</b>\n"
    f"🌐 Max sites per user: <b>{MAX_SITES}</b>"
)


async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        log.debug("edit skipped: %s", e)


JOIN_TEXT = ("🔒 <b>Bot use karne ke liye pehle ye channels join karo</b>\n\n"
             "Join karne ke baad <b>✅ Maine Join Kar Liya</b> dabao.")
PAGE = 8


def chan_info(cid):
    raw = redis.get(f"chan:{cid}")
    if not raw: return str(cid), ""
    title, _, link = str(raw).partition("\t")
    return title or str(cid), link


def user_label(uid):
    raw = redis.get(f"uinfo:{uid}")
    if raw:
        name, _, un = str(raw).partition("\t")
        return (name or str(uid)), un
    return str(uid), ""


async def missing_channels(bot, uid):
    out = []
    for cid in sorted(redis.smembers("channels") or []):
        try:
            m = await bot.get_chat_member(int(cid), uid)
            if m.status in ("left", "kicked"):
                out.append(cid)
        except Exception as e:
            log.warning("channel check failed %s: %s", cid, e)
    return out


def join_markup(missing):
    rows = []
    for cid in missing:
        title, link = chan_info(cid)
        if link:
            rows.append([Btn(f"📢 {title[:30]}", url=link)])
    rows.append([Btn("✅ Maine Join Kar Liya", callback_data="chk")])
    return Markup(rows)


async def guard(update, ctx):
    u = update.effective_user
    uid = u.id
    if is_banned(uid):
        if update.callback_query:
            await update.callback_query.answer("🚫 Tum banned ho.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 Tum banned ho.")
        return False
    redis.sadd("users", str(uid))
    redis.set(f"uinfo:{uid}",
              f"{(u.full_name or '').replace(chr(9), ' ')}\t{u.username or ''}")
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


# ============ ADMIN ============
async def show_admin(q):
    users = redis.scard("users")
    banned = redis.scard("banned")
    chans = redis.scard("channels")
    await safe_edit(
        q,
        "🛡 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{users}</b>\n🚫 Banned: <b>{banned}</b>\n"
        f"📡 Channels: <b>{chans}</b>\n\n"
        "<b>Commands:</b>\n<code>/ban user_id</code>\n"
        "<code>/unban user_id</code>\n<code>/delsite name</code>",
        Markup([
            [Btn("👥 Users & Sites", callback_data="users:0")],
            [Btn("📡 Channels", callback_data="channels"),
             Btn("📢 Broadcast", callback_data="broadcast")],
            [Btn("🔄 Refresh", callback_data="admin"), Btn("🏠 Home", callback_data="home")],
        ]))


async def show_users(q, page):
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
    if page > 0: nav.append(Btn("⬅️ Prev", callback_data=f"users:{page-1}"))
    nav.append(Btn(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1: nav.append(Btn("Next ➡️", callback_data=f"users:{page+1}"))
    rows.append(nav)
    rows.append([Btn("⬅️ Back", callback_data="admin")])
    await safe_edit(q, f"👥 <b>Users ({total})</b>", Markup(rows))


async def show_user(q, x, page):
    name, un = user_label(x)
    sites = sorted(redis.smembers(f"user:{x}:sites") or [])
    banned = is_banned(int(x))
    text = (f"👤 <b>{H.escape(name)}</b>\n🆔 <code>{x}</code>\n"
            + (f"🔗 @{H.escape(un)}\n" if un else "")
            + f"🌐 Sites: <b>{len(sites)}</b>\n"
            f"🚦 Status: <b>{'Banned 🚫' if banned else 'Active ✅'}</b>\n")
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
    text = ("📡 <b>Force-Join Channels</b>\n\n"
            + (f"Total: <b>{len(chans)}</b>" if chans else "Koi channel nahi hai."))
    await safe_edit(q, text, Markup(rows))


# ============ COMMANDS ============
async def start(update, ctx):
    if not await guard(update, ctx): return
    ctx.user_data.clear()
    u = update.effective_user
    await update.message.reply_text(
        WELCOME.format(name=H.escape(u.first_name or "dost")),
        parse_mode=ParseMode.HTML, reply_markup=main_menu(u.id))


async def cmd_ban(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /ban <user_id>")
    redis.sadd("banned", ctx.args[0])
    await update.message.reply_text(f"🚫 {ctx.args[0]} ban ho gaya.")


async def cmd_unban(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Use: /unban <user_id>")
    redis.srem("banned", ctx.args[0])
    await update.message.reply_text(f"✅ {ctx.args[0]} unban ho gaya.")


async def cmd_delsite(update, ctx):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        return await update.message.reply_text("Use: /delsite <name>")
    name = ctx.args[0].lower()
    if not redis.get(f"site:{name}"):
        return await update.message.reply_text("❌ Aisi site nahi mili.")
    delete_site(name)
    await update.message.reply_text(f"🗑 <code>{H.escape(name)}</code> delete ho gayi.",
                                    parse_mode=ParseMode.HTML)


def delete_site(name):
    owner = redis.get(f"owner:{name}")
    if owner:
        redis.srem(f"user:{owner}:sites", name)
    files = redis.smembers(f"site:{name}:files") or []
    for f in files:
        redis.delete(f"file:{name}:{f}")
    redis.delete(f"site:{name}", f"owner:{name}", f"site:{name}:files")


# ============ BUTTONS ============
async def on_button(update, ctx):
    if not await guard(update, ctx): return
    q = update.callback_query
    uid = q.from_user.id
    data = q.data
    await q.answer()

    if data in ("home", "chk"):
        ctx.user_data.clear()
        await safe_edit(q, WELCOME.format(name=H.escape(q.from_user.first_name or "dost")),
                        main_menu(uid))

    elif data == "help":
        await safe_edit(q, HELP, back_btn())

    elif data == "upload":
        count = redis.scard(f"user:{uid}:sites")
        if count >= MAX_SITES and not is_admin(uid):
            return await safe_edit(
                q, f"❌ Limit ({MAX_SITES}) poori ho gayi.",
                Markup([[Btn("📂 Meri Sites", callback_data="mysites")],
                        [Btn("🏠 Home", callback_data="home")]]))
        ctx.user_data.clear()
        ctx.user_data["state"] = "collecting"
        ctx.user_data["files"] = {}
        ctx.user_data["total_bytes"] = 0
        await safe_edit(
            q,
            "📤 <b>Website Upload</b>\n\n"
            "Ab saari files bhejo (HTML, CSS, JS, images, MP3, .py...).\n"
            "Ya ek <b>.zip</b> file bhejo — bot auto extract karega.\n\n"
            "💡 <b>Folder path</b> set karne ke liye caption me path likho:\n"
            "<code>assets/logo.png</code>\n\n"
            "Jab sab ho jaye <b>✅ Done</b> dabao.",
            Markup([
                [Btn("✅ Done", callback_data="done_upload")],
                [Btn("🗑 Sab Clear", callback_data="clear_upload"),
                 Btn("❌ Cancel", callback_data="home")],
            ]))

    elif data == "done_upload":
        files = ctx.user_data.get("files") or {}
        if not files:
            return await q.answer("❌ Pehle koi file bhejo", show_alert=True)
        has_index = any(p in ("index.html", "index.htm") for p in files)
        if not has_index:
            return await q.answer("❌ index.html zaroor include karo (homepage ke liye)",
                                  show_alert=True)
        ctx.user_data["state"] = "await_name"
        total = ctx.user_data.get("total_bytes", 0)
        preview = sorted(files.keys())
        filelist = "\n".join(f"• <code>{H.escape(p)}</code>" for p in preview[:25])
        if len(preview) > 25:
            filelist += f"\n... +{len(preview)-25} more"
        await safe_edit(
            q,
            f"✅ <b>{len(files)} files</b> receive ho gayi ({total/1024/1024:.2f} MB)\n\n"
            f"{filelist}\n\n"
            "✍️ Ab <b>site ka naam</b> bhejo.\n"
            "Rules: 3-30 chars, sirf <code>a-z 0-9 -</code>",
            Markup([[Btn("❌ Cancel", callback_data="home")]]))

    elif data == "clear_upload":
        ctx.user_data["files"] = {}
        ctx.user_data["total_bytes"] = 0
        await q.answer("🗑 Sab clear ho gaya", show_alert=True)

    elif data == "mysites":
        names = sorted(redis.smembers(f"user:{uid}:sites") or [])
        if not names:
            return await safe_edit(
                q, "📂 Koi site nahi hai.",
                Markup([[Btn("📤 Upload Karo", callback_data="upload")],
                        [Btn("🏠 Home", callback_data="home")]]))
        rows = [[Btn(f"🌐 {n}", callback_data=f"site:{n}")] for n in names]
        rows.append([Btn("🏠 Home", callback_data="home")])
        await safe_edit(q, f"📂 <b>Tumhari Sites ({len(names)})</b>", Markup(rows))

    elif data.startswith("site:"):
        name = data[5:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi.", show_alert=True)
        files = sorted(redis.smembers(f"site:{name}:files") or [])
        text = f"🌐 <b>{H.escape(name)}</b>\n\n🔗 {site_url(name)}\n\n📁 Files ({len(files)}):\n"
        text += "\n".join(f"• <code>{H.escape(f)}</code>" for f in files[:20])
        if len(files) > 20:
            text += f"\n... +{len(files)-20} more"
        await safe_edit(
            q, text,
            Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("🗑 Delete Site", callback_data=f"del:{name}")],
                [Btn("⬅️ Back", callback_data="mysites")],
            ]))

    elif data.startswith("del:"):
        name = data[4:]
        await safe_edit(
            q, f"⚠️ Pakka <b>{H.escape(name)}</b> delete karni hai?",
            Markup([[Btn("✅ Haan", callback_data=f"delok:{name}"),
                     Btn("❌ Nahi", callback_data=f"site:{name}")]]))

    elif data.startswith("delok:"):
        name = data[6:]
        if str(redis.get(f"owner:{name}")) != str(uid) and not is_admin(uid):
            return await q.answer("Ye tumhari site nahi.", show_alert=True)
        delete_site(name)
        await safe_edit(q, f"🗑 <b>{H.escape(name)}</b> delete ho gayi.",
                        Markup([[Btn("📂 Meri Sites", callback_data="mysites")],
                                [Btn("🏠 Home", callback_data="home")]]))

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
        if act == "ban": redis.sadd("banned", x)
        else: redis.srem("banned", x)
        await show_user(q, x, int(pg))

    elif data == "channels" and is_admin(uid):
        await show_channels(q)

    elif data == "addch" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "add_channel"
        await safe_edit(
            q, "📡 <b>Channel add karo</b>\n\n"
            "Bot ko pehle us channel ka <b>Admin</b> banao, "
            "phir <code>@username</code> / ID / forwarded message bhejo.",
            Markup([[Btn("❌ Cancel", callback_data="channels")]]))

    elif data.startswith("chdel:") and is_admin(uid):
        cid = data[6:]
        redis.srem("channels", cid)
        redis.delete(f"chan:{cid}")
        await show_channels(q)

    elif data == "broadcast" and is_admin(uid):
        ctx.user_data.clear()
        ctx.user_data["state"] = "broadcast"
        await safe_edit(q, "📢 Jo message sabko bhejna hai wo bhejo.",
                        Markup([[Btn("❌ Cancel", callback_data="admin")]]))


# ============ FILE RECEIVE ============
async def on_document(update, ctx):
    if not await guard(update, ctx): return
    uid = update.effective_user.id
    doc = update.message.document

    if ctx.user_data.get("state") != "collecting":
        return await update.message.reply_text(
            "👇 Pehle <b>📤 Website Upload Karo</b> dabao.",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(uid))

    fname = doc.file_name or "file"
    ext = os.path.splitext(fname)[1].lower()
    files = ctx.user_data.setdefault("files", {})
    total_bytes = ctx.user_data.get("total_bytes", 0)

    # ==== ZIP ====
    if ext == ".zip":
        fsize = doc.file_size or 0
        if fsize > MAX_FILE_MB * 1024 * 1024:
            return await update.message.reply_text(
                f"❌ ZIP > {MAX_FILE_MB} MB (Telegram/limit)")
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
        status_msg = await update.message.reply_text("📦 ZIP extract ho raha hai...")

        extracted, errors = safe_zip_extract(bytes(raw))

        if not extracted:
            await status_msg.edit_text(
                "❌ ZIP me koi valid file nahi mili.\n\n" + "\n".join(errors[:10]),
                parse_mode=ParseMode.HTML)
            return

        # Merge with existing files, respect limits
        added = 0
        skipped_limit = 0
        for path, meta in extracted.items():
            if len(files) >= MAX_FILES and path not in files:
                skipped_limit += 1
                continue
            old = files.get(path)
            if old:
                total_bytes -= old["size"]
            if total_bytes + meta["size"] > MAX_TOTAL_MB * 1024 * 1024:
                skipped_limit += 1
                continue
            files[path] = meta
            total_bytes += meta["size"]
            added += 1

        ctx.user_data["files"] = files
        ctx.user_data["total_bytes"] = total_bytes

        msg = (f"📦 <b>ZIP extract ho gaya!</b>\n\n"
               f"✅ Added: <b>{added}</b> files\n"
               f"📊 Total: <b>{len(files)}</b> files, "
               f"<b>{total_bytes/1024/1024:.2f} MB</b>\n")
        if skipped_limit:
            msg += f"⚠️ {skipped_limit} files skip (limit reach)\n"
        if errors:
            msg += f"\n<b>Warnings:</b>\n" + "\n".join(errors[:8])
        if len(errors) > 8:
            msg += f"\n... +{len(errors)-8} more warnings"

        msg += "\n\nAur files bhejo ya <b>✅ Done</b> dabao."
        await status_msg.edit_text(
            msg, parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("✅ Done", callback_data="done_upload")],
                                 [Btn("🗑 Clear", callback_data="clear_upload")]]))
        return

    # ==== Individual file ====
    if ext not in ALLOWED_EXT:
        return await update.message.reply_text(
            f"❌ <code>{H.escape(ext)}</code> allowed nahi.\n"
            f"Allowed: {', '.join(sorted(ALLOWED_EXT))}",
            parse_mode=ParseMode.HTML)

    caption = (update.message.caption or "").strip()
    path = caption if caption else fname
    path = path.lstrip("/").replace("..", "").replace("//", "/")

    if len(files) >= MAX_FILES and path not in files:
        return await update.message.reply_text(f"❌ Max {MAX_FILES} files allowed.")

    fsize = doc.file_size or 0
    if fsize > MAX_FILE_MB * 1024 * 1024:
        return await update.message.reply_text(f"❌ File > {MAX_FILE_MB} MB.")

    old = files.get(path)
    if old:
        total_bytes -= old["size"]
    if total_bytes + fsize > MAX_TOTAL_MB * 1024 * 1024:
        return await update.message.reply_text(f"❌ Total size > {MAX_TOTAL_MB} MB.")

    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    files[path] = {
        "b64": base64.b64encode(bytes(raw)).decode(),
        "size": fsize,
        "mime": mime_for(path),
    }
    ctx.user_data["files"] = files
    ctx.user_data["total_bytes"] = total_bytes + fsize

    await update.message.reply_text(
        f"✅ <code>{H.escape(path)}</code> added\n"
        f"📦 Total: <b>{len(files)}</b> files, "
        f"<b>{(total_bytes + fsize)/1024/1024:.2f} MB</b>\n\n"
        "Aur files bhejo ya <b>✅ Done</b> dabao.",
        parse_mode=ParseMode.HTML,
        reply_markup=Markup([[Btn("✅ Done", callback_data="done_upload")],
                             [Btn("🗑 Clear", callback_data="clear_upload")]]))


# ============ TEXT ============
async def on_text(update, ctx):
    if not await guard(update, ctx): return
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
            return await update.message.reply_text("❌ @username, ID ya forward bhejo.")
        try:
            chat = await ctx.bot.get_chat(ref)
            me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
            if me.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi")
        except Exception as e:
            log.warning("add channel: %s", e)
            return await update.message.reply_text("❌ Channel nahi mila ya bot admin nahi.")
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try: link = chat.invite_link or await ctx.bot.export_chat_invite_link(chat.id)
            except Exception: link = ""
        title = (chat.title or str(chat.id)).replace("\t", " ")
        redis.sadd("channels", str(chat.id))
        redis.set(f"chan:{chat.id}", f"{title}\t{link}")
        ctx.user_data.clear()
        return await update.message.reply_text(
            f"✅ <b>{H.escape(title)}</b> add ho gaya!",
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([[Btn("📡 Channels", callback_data="channels")],
                                 [Btn("🛡 Admin", callback_data="admin")]]))

    if state == "broadcast" and is_admin(uid):
        users = redis.smembers("users") or []
        ok = fail = 0
        await update.message.reply_text(f"📢 Sending to {len(users)} users...")
        for u in users:
            try:
                await ctx.bot.send_message(int(u), text)
                ok += 1
            except Exception:
                fail += 1
        ctx.user_data.clear()
        return await update.message.reply_text(f"✅ Sent: {ok}\nFailed: {fail}",
                                                reply_markup=back_btn())

    if state == "await_name":
        name = text.lower()
        if not NAME_RE.match(name):
            return await update.message.reply_text(
                "❌ Galat naam. 3-30 chars, <code>a-z 0-9 -</code>",
                parse_mode=ParseMode.HTML)
        if name in RESERVED:
            return await update.message.reply_text("❌ Reserved naam, koi aur do.")
        if redis.get(f"site:{name}"):
            return await update.message.reply_text("❌ Naam already taken.")

        files = ctx.user_data.get("files") or {}
        if not files:
            ctx.user_data.clear()
            return await update.message.reply_text("⚠️ Files nahi mili, dobara upload karo.",
                                                    reply_markup=main_menu(uid))

        for path, meta in files.items():
            redis.set(f"file:{name}:{path}", meta["b64"])
        redis.sadd(f"site:{name}:files", *files.keys())
        redis.set(f"site:{name}", "1")
        redis.set(f"owner:{name}", str(uid))
        redis.sadd(f"user:{uid}:sites", name)
        ctx.user_data.clear()

        await update.message.reply_text(
            f"🎉 <b>Website live ho gayi!</b>\n\n"
            f"🌐 Naam: <code>{name}</code>\n"
            f"📁 Files: <b>{len(files)}</b>\n"
            f"🔗 {site_url(name)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=Markup([
                [Btn("🔗 Open Site", url=site_url(name))],
                [Btn("📂 Meri Sites", callback_data="mysites"),
                 Btn("🏠 Home", callback_data="home")],
            ]))
        return

    await update.message.reply_text("👇 Menu se option chuno", reply_markup=main_menu(uid))


# ============ RUN ============
def main():
    threading.Thread(target=run_web, daemon=True).start()
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
