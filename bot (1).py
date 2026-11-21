# -*- coding: utf-8 -*-
"""
Jarvis Telegram Bot (جارویس)
Creator: ZiCo

Two modes per group:
  simple : no AI at all. Slash commands, fixed Persian/English words, custom words.
  ai     : everything from simple mode + natural language commands for admins
           ("جارویس این رو ۱ ساعت سکوت کن") + AI chat for everyone.

Run:  python bot.py
Env : BOT_TOKEN, AI_API_KEY (Groq gsk_... or xAI xai-...), OWNER_ID, DB_PATH, PORT(optional)
"""
import asyncio
import html as htmllib
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

# ============================================================ config
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
AI_API_KEY = (os.getenv("AI_API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
try:
    OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip() or 0)
except ValueError:
    OWNER_ID = 0
DB_PATH = (os.getenv("DB_PATH", "jarvis_bot.db") or "jarvis_bot.db").strip()
PORT = (os.getenv("PORT", "") or "").strip()

FLOOD_MAX = 7          # more than this many messages ...
FLOOD_SECS = 6         # ... inside this many seconds = spam
FLOOD_MUTE_MIN = 5
AI_USER_GAP = 3        # seconds between AI calls per user
AI_CHAT_MAX = 15       # AI calls per minute per chat
RECENT_MAX = 200

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")

router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}

HTTP = None
BOT_INFO = {"id": 0, "username": ""}
CFG = {}
ADMIN_CACHE = {}
SEEN = {}
STAT = defaultdict(int)
RECENT = defaultdict(lambda: deque(maxlen=RECENT_MAX))
FLOOD = defaultdict(deque)
AI_LAST = {}
AI_CHAT_LOG = defaultdict(deque)
AI_HIST = defaultdict(lambda: deque(maxlen=8))
FILTERS = {}
WORDS = {}
PENDING = {}
TASKS = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


# ============================================================ text helpers
_FA = str.maketrans({
    "ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و",
    "\u200c": " ", "\u200f": "", "\u200e": "",
})
_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def norm(t):
    return " ".join(str(t or "").translate(_FA).translate(_DIG).lower().split())


def esc(s):
    return htmllib.escape(str(s if s is not None else ""), quote=False)


def mention(uid, name):
    return '<a href="tg://user?id=%s">%s</a>' % (uid, esc(name) or uid)


def to_int(v, default=None):
    try:
        return int(float(norm(str(v))))
    except (ValueError, TypeError):
        return default


def fmt_minutes(m):
    if m < 60:
        return "%d دقیقه" % m
    if m < 1440:
        return "%d ساعت" % (m // 60)
    return "%d روز" % (m // 1440)


UNITS = {
    "دقیقه": 1, "min": 1, "m": 1,
    "ساعت": 60, "h": 60, "hr": 60, "hour": 60,
    "روز": 1440, "d": 1440, "day": 1440,
    "هفته": 10080, "w": 10080,
}


def _clean_args(args):
    t = norm(" ".join(args) if isinstance(args, (list, tuple)) else args)
    t = re.sub(r"@\S+", " ", t)
    t = re.sub(r"(?<!\d)\d{6,}(?![\da-zآ-ی])", " ", t)
    return t


def parse_minutes(args, default=60):
    t = _clean_args(args)
    if any(w in t for w in ("همیشه", "forever", "دائم")):
        return 0
    m = re.search(r"(\d+)\s*([a-zآ-ی]+)?", t)
    if not m:
        return default
    n = int(m.group(1))
    unit = UNITS.get(m.group(2) or "", 1)
    return max(1, min(n * unit, 527040))


def first_int(args):
    m = re.search(r"\d+", _clean_args(args))
    return int(m.group(0)) if m else None


def extract_json(raw):
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e <= s:
        return {"action": "chat", "reply": raw.strip()[:1500]}
    try:
        return json.loads(raw[s:e + 1])
    except ValueError:
        return {"action": "chat", "reply": raw.strip()[:1500]}


# ============================================================ locks / keywords
LOCK_TYPES = ["links", "forward", "media", "sticker", "voice"]
LOCK_FA = {"links": "لینک", "forward": "فوروارد", "media": "مدیا", "sticker": "استیکر", "voice": "ویس"}
LOCK_ALIAS = {
    "لینک": "links", "link": "links", "links": "links",
    "فوروارد": "forward", "forward": "forward",
    "مدیا": "media", "media": "media", "رسانه": "media",
    "استیکر": "sticker", "sticker": "sticker",
    "ویس": "voice", "voice": "voice",
}

KEYWORDS = {
    "بن": "ban", "ban": "ban",
    "آنبن": "unban", "unban": "unban", "رفع بن": "unban",
    "اخراج": "kick", "kick": "kick",
    "سکوت": "mute", "mute": "mute",
    "رفع سکوت": "unmute", "unmute": "unmute",
    "اخطار": "warn", "warn": "warn",
    "حذف اخطار": "unwarn", "unwarn": "unwarn",
    "حذف": "delete", "del": "delete",
    "پین": "pin", "pin": "pin",
    "آنپین": "unpin", "unpin": "unpin",
    "پاکسازی": "purge", "purge": "purge",
    "قفل": "lock", "lock": "lock",
    "باز کردن": "unlock", "بازکردن": "unlock", "unlock": "unlock",
    "حالت هوشمند": "mode_ai", "حالت ساده": "mode_simple",
    "قوانین": "rules", "rules": "rules",
    "آمار": "stats", "stats": "stats",
    "برترین": "top", "top": "top",
}
PUBLIC_KW = {"rules", "stats", "top"}

NEEDS_TARGET = {"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn"}
CONFIRM = {"ban", "kick", "purge", "broadcast"}
TEMP_ACTIONS = {"delete", "purge", "announce"}
ALLOWED = {
    "chat", "ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "delete",
    "pin", "unpin", "purge", "lock", "unlock", "announce", "broadcast",
    "set_welcome", "set_rules", "summarize",
}
ACTION_FA = {
    "ban": "بن", "kick": "اخراج", "purge": "پاکسازی", "broadcast": "پخش همگانی",
}


def parse_keyword(text):
    t = norm(text)
    if not t or len(t) > 60:
        return None
    parts = t.split()
    if len(parts) > 6:
        return None
    for n in (2, 1):
        if len(parts) >= n:
            phrase = " ".join(parts[:n])
            if phrase in KEYWORDS:
                return KEYWORDS[phrase], parts[n:]
    return None


def lock_from_args(args):
    for tok in norm(" ".join(args)).split():
        if tok in LOCK_ALIAS:
            return LOCK_ALIAS[tok]
    return None


def lock_set(cfg):
    return {x for x in (cfg.get("locks") or "").split(",") if x}


# ============================================================ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY, title TEXT DEFAULT '', type TEXT DEFAULT '',
    active INTEGER DEFAULT 1, mode TEXT DEFAULT 'simple', welcome TEXT DEFAULT '',
    ai_welcome INTEGER DEFAULT 0, rules TEXT DEFAULT '', warn_limit INTEGER DEFAULT 3,
    warn_action TEXT DEFAULT 'mute', flood_on INTEGER DEFAULT 1, locks TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', name TEXT DEFAULT '',
    started INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER, user_id INTEGER, cnt INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS filters (
    chat_id INTEGER, word TEXT, PRIMARY KEY (chat_id, word)
);
CREATE TABLE IF NOT EXISTS words (
    chat_id INTEGER, trig TEXT, reply TEXT, PRIMARY KEY (chat_id, trig)
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id INTEGER, user_id INTEGER, cnt INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id)
);
"""


async def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await db.commit()


async def db_run(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


async def db_one(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None


async def db_all(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_cfg(chat_id, chat=None):
    c = CFG.get(chat_id)
    if c:
        return c
    row = await db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    if not row:
        title = (getattr(chat, "title", "") or "") if chat else ""
        ctype = str(getattr(getattr(chat, "type", ""), "value", getattr(chat, "type", ""))) if chat else ""
        await db_run("INSERT OR IGNORE INTO chats(chat_id,title,type) VALUES(?,?,?)", (chat_id, title, ctype))
        row = await db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    CFG[chat_id] = row
    return row


async def set_cfg(chat_id, **fields):
    cfg = await get_cfg(chat_id)
    sets = ",".join("%s=?" % k for k in fields)
    await db_run("UPDATE chats SET %s WHERE chat_id=?" % sets, (*fields.values(), chat_id))
    cfg.update(fields)


async def get_filters(chat_id):
    if chat_id not in FILTERS:
        rows = await db_all("SELECT word FROM filters WHERE chat_id=?", (chat_id,))
        FILTERS[chat_id] = [r["word"] for r in rows]
    return FILTERS[chat_id]


async def get_words(chat_id):
    if chat_id not in WORDS:
        rows = await db_all("SELECT trig, reply FROM words WHERE chat_id=?", (chat_id,))
        WORDS[chat_id] = {r["trig"]: r["reply"] for r in rows}
    return WORDS[chat_id]


async def add_warn(chat_id, uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warns(chat_id,user_id,cnt) VALUES(?,?,1) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET cnt=cnt+1",
            (chat_id, uid),
        )
        await db.commit()
        cur = await db.execute("SELECT cnt FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
        row = await cur.fetchone()
    return row[0] if row else 1


async def get_warns(chat_id, uid):
    row = await db_one("SELECT cnt FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return row["cnt"] if row else 0


async def flush_stats():
    if not STAT:
        return
    items = list(STAT.items())
    STAT.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO stats(chat_id,user_id,cnt) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET cnt=cnt+excluded.cnt",
            [(c, u, n) for (c, u), n in items],
        )
        await db.commit()


async def flusher():
    while True:
        await asyncio.sleep(30)
        try:
            await flush_stats()
        except Exception:
            log.exception("stats flush failed")


def user_key(u):
    return ((u.username or "").lower(), u.full_name or "")


async def upsert_user(u, started=False):
    uname, name = user_key(u)
    if started:
        await db_run(
            "INSERT INTO users(user_id,username,name,started) VALUES(?,?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name,started=1",
            (u.id, uname, name),
        )
    else:
        await db_run(
            "INSERT INTO users(user_id,username,name) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name",
            (u.id, uname, name),
        )


def track_user(u, chat_id):
    STAT[(chat_id, u.id)] += 1
    k = user_key(u)
    if SEEN.get(u.id) != k:
        SEEN[u.id] = k
        spawn(upsert_user(u))


# ============================================================ telegram helpers
def real_reply(message):
    r = message.reply_to_message
    if r is None or getattr(r, "forum_topic_created", None):
        return None
    return r


async def say(message, text, html_mode=True, temp=False, markup=None):
    m = await message.answer(
        text[:4000],
        parse_mode=("HTML" if html_mode else None),
        reply_markup=markup,
    )
    if temp:
        spawn(delete_later(message.bot, m.chat.id, m.message_id, 6))
    return m


async def delete_later(bot, chat_id, mid, secs):
    await asyncio.sleep(secs)
    try:
        await bot.delete_message(chat_id, mid)
    except TelegramAPIError:
        pass


async def is_admin(bot, chat_id, uid):
    if OWNER_ID and uid == OWNER_ID:
        return True
    hit = ADMIN_CACHE.get((chat_id, uid))
    if hit and time.time() - hit[1] < 30:
        return hit[0]
    try:
        m = await bot.get_chat_member(chat_id, uid)
    except TelegramAPIError:
        return False
    ok = m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    ADMIN_CACHE[(chat_id, uid)] = (ok, time.time())
    return ok


async def sender_is_admin(message):
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # anonymous admin
    if message.sender_chat:
        return False
    u = message.from_user
    if not u:
        return False
    return await is_admin(message.bot, message.chat.id, u.id)


async def admin_only(message):
    if await sender_is_admin(message):
        return True
    await say(message, "⛔ فقط ادمین‌ها می‌تونن از این دستور استفاده کنن.", temp=True)
    return False


async def find_target(message, arg_text=""):
    r = real_reply(message)
    if r and r.from_user and not r.sender_chat and r.from_user.id != 1087968824:
        return r.from_user.id, r.from_user.full_name
    text = message.text or message.caption or ""
    ents = message.entities or message.caption_entities or []
    bot_name = BOT_INFO["username"].lower()
    for e in ents:
        if e.type == "text_mention" and e.user:
            return e.user.id, e.user.full_name
        if e.type == "mention":
            uname = e.extract_from(text).lstrip("@").lower()
            if uname == bot_name:
                continue
            row = await db_one("SELECT user_id,name FROM users WHERE username=?", (uname,))
            if row:
                return row["user_id"], row["name"]
    m = re.search(r"(?<!\d)(\d{6,12})(?!\d)", norm(arg_text))
    if m:
        uid = int(m.group(1))
        row = await db_one("SELECT name FROM users WHERE user_id=?", (uid,))
        return uid, (row["name"] if row else str(uid))
    return None, ""


async def check_target(bot, chat_id, uid):
    if not uid:
        return "⚠️ کاربر مشخص نیست. روی پیامش ریپلای کن یا @یوزرنیم بنویس."
    if uid == BOT_INFO["id"] or (OWNER_ID and uid == OWNER_ID) or await is_admin(bot, chat_id, uid):
        return "⛔ نمی‌تونم روی ادمین‌ها، مالک یا خودم این کار رو انجام بدم."
    return None


# ============================================================ actions
async def execute(bot, chat_id, action, p):
    """Run one action. Returns (text, is_html)."""
    cfg = await get_cfg(chat_id)
    uid = p.get("target_id")
    name = p.get("target_name") or ""
    tag = ""
    if action in NEEDS_TARGET:
        err = await check_target(bot, chat_id, uid)
        if err:
            return err, True
        tag = mention(uid, name or uid)

    if action == "ban":
        await bot.ban_chat_member(chat_id, uid)
        return "🚫 %s بن شد." % tag, True

    if action == "unban":
        await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        return "✅ بن %s برداشته شد." % tag, True

    if action == "kick":
        await bot.ban_chat_member(chat_id, uid)
        await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        return "👢 %s از گروه اخراج شد." % tag, True

    if action == "mute":
        minutes = p.get("minutes")
        if minutes is None:
            minutes = 60
        until = None if minutes == 0 else datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await bot.restrict_chat_member(
            chat_id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=until
        )
        label = "برای همیشه" if minutes == 0 else fmt_minutes(minutes)
        return "🔇 %s %s سکوت شد." % (tag, label), True

    if action == "unmute":
        chat = await bot.get_chat(chat_id)
        perms = getattr(chat, "permissions", None) or ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True,
        )
        await bot.restrict_chat_member(chat_id, uid, permissions=perms)
        return "🔊 سکوت %s برداشته شد." % tag, True

    if action == "warn":
        cnt = await add_warn(chat_id, uid)
        limit = int(cfg.get("warn_limit") or 3)
        if cnt >= limit:
            await db_run("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
            act = cfg.get("warn_action") or "mute"
            if act == "ban":
                await bot.ban_chat_member(chat_id, uid)
                extra = "بن شد"
            elif act == "kick":
                await bot.ban_chat_member(chat_id, uid)
                await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
                extra = "اخراج شد"
            else:
                until = datetime.now(timezone.utc) + timedelta(minutes=1440)
                await bot.restrict_chat_member(
                    chat_id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=until
                )
                extra = "۱ روز سکوت شد"
            return "⚠️ %s به سقف اخطارها (%d) رسید و %s." % (tag, limit, extra), True
        return "⚠️ %s اخطار گرفت (%d/%d)." % (tag, cnt, limit), True

    if action == "unwarn":
        await db_run("UPDATE warns SET cnt=MAX(cnt-1,0) WHERE chat_id=? AND user_id=?", (chat_id, uid))
        cnt = await get_warns(chat_id, uid)
        return "✅ یک اخطار %s برداشته شد (%d/%d)." % (tag, cnt, int(cfg.get("warn_limit") or 3)), True

    if action == "delete":
        rid = p.get("reply_msg_id")
        if not rid:
            return "⚠️ روی پیامی که می‌خوای حذف بشه ریپلای کن.", True
        await bot.delete_message(chat_id, rid)
        if p.get("cmd_msg_id"):
            try:
                await bot.delete_message(chat_id, p["cmd_msg_id"])
            except TelegramAPIError:
                pass
        return "🗑 حذف شد.", True

    if action == "pin":
        rid = p.get("reply_msg_id")
        if not rid:
            return "⚠️ روی پیامی که می‌خوای پین بشه ریپلای کن.", True
        await bot.pin_chat_message(chat_id, rid, disable_notification=False)
        return "📌 پین شد.", True

    if action == "unpin":
        rid = p.get("reply_msg_id")
        if rid:
            await bot.unpin_chat_message(chat_id, message_id=rid)
        else:
            await bot.unpin_chat_message(chat_id)
        return "📍 پین برداشته شد.", True

    if action == "purge":
        cmd_id = p.get("cmd_msg_id") or 0
        rid = p.get("reply_msg_id")
        n = min(max(int(p.get("count") or 0), 0), 100)
        if rid:
            start = rid
        elif n and cmd_id:
            start = cmd_id - n
        else:
            return "⚠️ روی پیام شروع ریپلای کن یا تعداد بنویس (مثلاً پاکسازی ۲۰).", True
        ids = list(range(max(start, 1), cmd_id + 1))[-100:]
        if not ids:
            return "⚠️ پیامی برای پاکسازی پیدا نشد.", True
        await bot.delete_messages(chat_id, ids)
        return "🧹 پاکسازی انجام شد.", True

    if action in ("lock", "unlock"):
        lt = p.get("lock_type")
        if lt not in LOCK_TYPES:
            names = "، ".join(LOCK_FA[x] for x in LOCK_TYPES)
            return "⚠️ نوع قفل رو بنویس: %s" % names, True
        locks = lock_set(cfg)
        if action == "lock":
            locks.add(lt)
        else:
            locks.discard(lt)
        await set_cfg(chat_id, locks=",".join(sorted(locks)))
        state = "قفل شد 🔒" if action == "lock" else "باز شد 🔓"
        return "%s %s." % (LOCK_FA[lt], state), True

    if action == "announce":
        text = (p.get("text") or "").strip()
        if not text:
            return "⚠️ متن اعلامیه مشخص نیست.", True
        sent = await bot.send_message(chat_id, "📢 " + text[:3900], parse_mode=None)
        if p.get("pin"):
            await bot.pin_chat_message(chat_id, sent.message_id, disable_notification=False)
        return "✅ اعلامیه ارسال شد.", True

    if action == "set_welcome":
        await set_cfg(chat_id, welcome=(p.get("text") or "").strip()[:1500])
        return "✅ پیام خوش‌آمد ذخیره شد.", True

    if action == "set_rules":
        await set_cfg(chat_id, rules=(p.get("text") or "").strip()[:3000])
        return "✅ قوانین گروه ذخیره شد.", True

    if action == "summarize":
        n = min(max(int(p.get("count") or 50), 10), 150)
        return await summarize(chat_id, n)

    if action == "broadcast":
        text = (p.get("text") or "").strip()
        if not text:
            return "⚠️ متن پیام مشخص نیست.", True
        if p.get("kind") == "users":
            rows = await db_all("SELECT user_id AS id FROM users WHERE started=1")
        else:
            rows = await db_all(
                "SELECT chat_id AS id FROM chats WHERE active=1 AND type IN ('group','supergroup')"
            )
        sent = failed = 0
        for r in rows:
            for attempt in range(2):
                try:
                    await bot.send_message(r["id"], text[:4000], parse_mode=None)
                    sent += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramAPIError:
                    failed += 1
                    break
            await asyncio.sleep(0.07)
        return "📣 ارسال تموم شد: %d موفق، %d ناموفق." % (sent, failed), True

    return "⚠️ دستور ناشناخته.", True


def ai_error_text(e):
    code = getattr(e, "code", "")
    if code == "off":
        return "⚠️ هوش مصنوعی فعال نیست (AI_API_KEY ست نشده)."
    if code == "busy":
        return "⏳ همه مدل‌ها فعلاً محدود شدن. چند ثانیه بعد دوباره امتحان کن."
    if code == "auth":
        return "⚠️ کلید هوش مصنوعی نامعتبره یا اعتبار نداره."
    return "⚠️ خطا در هوش مصنوعی. دوباره امتحان کن."


async def run_action(bot, chat_id, action, p):
    try:
        return await execute(bot, chat_id, action, p)
    except TelegramAPIError as e:
        detail = esc(getattr(e, "message", str(e)))
        return (
            "❌ تلگرام اجازه نداد: %s\nمطمئن شو ربات ادمینه و دسترسی لازم رو داره." % detail,
            True,
        )
    except AIError as e:
        return esc(ai_error_text(e)), True


async def handle_mod(message, action, args):
    bot = message.bot
    r = real_reply(message)
    arg_text = " ".join(args)
    p = {
        "cmd_msg_id": message.message_id,
        "reply_msg_id": r.message_id if r else None,
    }
    if action in NEEDS_TARGET:
        uid, name = await find_target(message, arg_text)
        p["target_id"], p["target_name"] = uid, name
    if action == "mute":
        p["minutes"] = parse_minutes(args)
    if action == "purge":
        p["count"] = first_int(args)
    if action in ("lock", "unlock"):
        p["lock_type"] = lock_from_args(args)
    text, h = await run_action(bot, message.chat.id, action, p)
    await say(message, text, h, temp=action in TEMP_ACTIONS)


# ============================================================ pending confirmations
def new_pending(chat_id, issuer, action, p):
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["exp"] < now]:
        PENDING.pop(k, None)
    pid = secrets.token_hex(4)
    PENDING[pid] = {"chat_id": chat_id, "issuer": issuer, "action": action, "p": p, "exp": now + 180}
    return pid


def confirm_markup(pid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ بله، انجام بده", callback_data="ok:" + pid),
        InlineKeyboardButton(text="❌ لغو", callback_data="no:" + pid),
    ]])


def describe(action, p):
    if action in ("ban", "kick"):
        who = mention(p.get("target_id"), p.get("target_name") or p.get("target_id"))
        verb = "بن" if action == "ban" else "اخراج"
        return "❓ %s رو %s کنم؟" % (who, verb)
    if action == "purge":
        if p.get("reply_msg_id"):
            return "❓ همه پیام‌ها از پیام ریپلای‌شده تا اینجا پاک بشن؟"
        return "❓ %d پیام آخر پاک بشن؟" % min(int(p.get("count") or 0), 100)
    if action == "broadcast":
        where = "همه کاربرهای ربات" if p.get("kind") == "users" else "همه گروه‌ها"
        return "📣 این متن برای %s ارسال بشه؟\n\n%s" % (where, esc((p.get("text") or "")[:500]))
    return "❓ تایید می‌کنی؟"


@router.callback_query(F.data.startswith(("ok:", "no:")))
async def cb_confirm(cb: CallbackQuery):
    kind, _, pid = (cb.data or "").partition(":")
    msg = cb.message
    item = PENDING.get(pid)
    if not item:
        await cb.answer("این درخواست منقضی شده.", show_alert=True)
        return
    if cb.from_user.id != item["issuer"]:
        await cb.answer("فقط کسی که دستور داده می‌تونه تایید کنه.", show_alert=True)
        return
    PENDING.pop(pid, None)
    if not isinstance(msg, Message):
        await cb.answer()
        return
    if kind == "no":
        await cb.answer("لغو شد")
        try:
            await msg.edit_text("❌ لغو شد.")
        except TelegramAPIError:
            pass
        return
    await cb.answer("⏳")
    try:
        await msg.edit_text("⏳ در حال انجام...")
    except TelegramAPIError:
        pass
    text, h = await run_action(cb.bot, item["chat_id"], item["action"], item["p"])
    try:
        await msg.edit_text(text[:4000], parse_mode=("HTML" if h else None))
    except TelegramAPIError:
        pass


# ============================================================ settings panel
def settings_text(cfg):
    ai = cfg.get("mode") == "ai"
    locks = lock_set(cfg)
    lock_names = "، ".join(LOCK_FA[x] for x in LOCK_TYPES if x in locks) or "هیچ‌کدام"
    return (
        "⚙️ <b>تنظیمات گروه</b>\n\n"
        "حالت: %s\n"
        "ضدفلود: %s\n"
        "قفل‌ها: %s\n"
        "اخطار: سقف %s، مجازات: %s\n"
        "خوش‌آمد: %s"
    ) % (
        "🤖 هوشمند (AI)" if ai else "⚡ ساده (بدون AI)",
        "روشن" if cfg.get("flood_on") else "خاموش",
        lock_names,
        cfg.get("warn_limit") or 3,
        cfg.get("warn_action") or "mute",
        "روشن" if (cfg.get("welcome") or cfg.get("ai_welcome")) else "خاموش",
    )


def settings_markup(cfg):
    ai = cfg.get("mode") == "ai"
    locks = lock_set(cfg)
    rows = [
        [InlineKeyboardButton(text=("🤖 حالت: هوشمند" if ai else "⚡ حالت: ساده"), callback_data="set:mode")],
        [InlineKeyboardButton(
            text="🌊 ضدفلود: %s" % ("روشن" if cfg.get("flood_on") else "خاموش"), callback_data="set:flood")],
    ]
    row = []
    for t in LOCK_TYPES:
        row.append(InlineKeyboardButton(
            text="%s %s" % ("🔒" if t in locks else "🔓", LOCK_FA[t]), callback_data="set:lock:" + t))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery):
    msg = cb.message
    if not isinstance(msg, Message):
        await cb.answer()
        return
    chat = msg.chat
    if not await is_admin(cb.bot, chat.id, cb.from_user.id):
        await cb.answer("⛔ فقط ادمین‌ها", show_alert=True)
        return
    cfg = await get_cfg(chat.id, chat)
    parts = (cb.data or "").split(":")
    if parts[1] == "mode":
        new = "simple" if cfg["mode"] == "ai" else "ai"
        if new == "ai" and not AI.models:
            await cb.answer("هوش مصنوعی فعال نیست (AI_API_KEY ست نشده).", show_alert=True)
            return
        await set_cfg(chat.id, mode=new)
    elif parts[1] == "flood":
        await set_cfg(chat.id, flood_on=0 if cfg.get("flood_on") else 1)
    elif parts[1] == "lock" and len(parts) > 2 and parts[2] in LOCK_TYPES:
        locks = lock_set(cfg)
        if parts[2] in locks:
            locks.discard(parts[2])
        else:
            locks.add(parts[2])
        await set_cfg(chat.id, locks=",".join(sorted(locks)))
    cfg = await get_cfg(chat.id)
    try:
        await msg.edit_text(settings_text(cfg), reply_markup=settings_markup(cfg), parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await cb.answer("✅")


async def set_mode(message, mode):
    if mode == "ai" and not AI.models:
        await say(message, "⚠️ هوش مصنوعی فعال نیست (AI_API_KEY ست نشده).")
        return
    await set_cfg(message.chat.id, mode=mode)
    if mode == "ai":
        await say(message, "🤖 <b>حالت هوشمند فعال شد.</b>\nبنویس «جارویس ...» و هر کاری خواستی بگو.")
    else:
        await say(message, "⚡ <b>حالت ساده فعال شد.</b>\nفقط دستورها و کلمات ثابت کار می‌کنن (بدون هوش مصنوعی).")


# ============================================================ AI client
class AIError(Exception):
    def __init__(self, code="error"):
        super().__init__(code)
        self.code = code


class AIState:
    models = []
    dead = set()
    cooldown = {}
    base = ""
    key = ""
    no_reasoning = False


AI = AIState()
GROQ_BAD = ("whisper", "guard", "tts", "playai", "embed", "orpheus", "safeguard", "compound", "qwen")
GROQ_PREFS = ("llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant")
XAI_PREFS = ("grok-4", "grok-3", "grok-3-mini")


async def init_ai():
    if not AI_API_KEY:
        log.warning("AI_API_KEY not set: AI features are off.")
        return
    AI.key = AI_API_KEY
    is_xai = AI.key.startswith("xai-")
    AI.base = "https://api.x.ai/v1" if is_xai else "https://api.groq.com/openai/v1"
    try:
        async with HTTP.get(
            AI.base + "/models",
            headers={"Authorization": "Bearer " + AI.key},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                log.error("AI model list failed: HTTP %s", r.status)
                return
            data = await r.json()
    except Exception as e:
        log.error("AI init failed: %s", e)
        return
    ids = [m["id"] for m in data.get("data", [])]
    if is_xai:
        ids = [i for i in ids if i.startswith("grok") and "image" not in i and "imagine" not in i and "vision" not in i]
        prefs = XAI_PREFS
    else:
        ids = [i for i in ids if not any(b in i for b in GROQ_BAD)]
        prefs = GROQ_PREFS
    ordered = [p for p in prefs if p in ids]
    for i in sorted(ids):
        if i not in ordered:
            ordered.append(i)
    AI.models = ordered
    log.info("AI ready. Models: %s", ", ".join(ordered))


async def ai_call(messages, max_tokens=600, temperature=0.6):
    if not AI.models:
        raise AIError("off")
    tried = set()
    while True:
        now = time.time()
        model = None
        for m in AI.models:
            if m in AI.dead or m in tried:
                continue
            if AI.cooldown.get(m, 0) <= now:
                model = m
                break
        if model is None:
            raise AIError("busy")
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if "gpt-oss" in model and not AI.no_reasoning:
            body["reasoning_effort"] = "low"
        try:
            async with HTTP.post(
                AI.base + "/chat/completions",
                headers={"Authorization": "Bearer " + AI.key, "Content-Type": "application/json"},
                json=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                code = r.status
                raw = await r.text()
                retry_after = r.headers.get("retry-after")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            tried.add(model)
            continue

        if code == 200:
            try:
                content = json.loads(raw)["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                tried.add(model)
                continue
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            if content:
                return content
            tried.add(model)
            continue
        if code in (401, 403):
            raise AIError("auth")
        if code == 429:
            try:
                wait = float(retry_after) if retry_after else 65
            except ValueError:
                wait = 65
            AI.cooldown[model] = time.time() + min(max(wait, 5), 3600)
            tried.add(model)
            log.info("AI model %s rate limited, switching", model)
            continue
        if code == 400 and "reasoning" in raw.lower() and not AI.no_reasoning:
            AI.no_reasoning = True
            continue
        if code in (400, 404) and "model" in raw.lower():
            AI.dead.add(model)
            tried.add(model)
            continue
        if code == 413 or code >= 500:
            tried.add(model)
            continue
        log.error("AI error HTTP %s: %s", code, raw[:300])
        raise AIError("error")


CHAT_SYSTEM = (
    "تو «جارویس» هستی، دستیار هوشمند فارسی‌زبان. سازنده‌ات «زیکو» (ZiCo) است. "
    "اگر پرسیدند کی هستی یا کی ساختت، بگو: من جارویس هستم و زیکو من را ساخته. "
    "هرگز نگو گروک، لاما، چت‌جی‌پی‌تی یا ساخته شرکت دیگری هستی. "
    "به زبان کاربر جواب بده، کوتاه، دوستانه و مستقیم. "
    "از مارک‌داون (ستاره، هشتگ)، جدول و ایموجی زیاد استفاده نکن. اگر چیزی را نمی‌دانی بگو نمی‌دانم."
)

TR_SYSTEM = (
    "You are a translator. If the user's text is Persian, translate it to English. "
    "Otherwise translate it to Persian. Output only the translation."
)

PLAN_SYSTEM = (
    "You convert one Telegram group admin's request (usually Persian) into ONE JSON action. "
    "Output ONLY a JSON object, no other text.\n"
    'Schema: {"action": string, "minutes": number|null, "count": number|null, '
    '"lock_type": "links|forward|media|sticker|voice"|null, "text": string|null, '
    '"pin": boolean, "reply": string|null}\n'
    "Actions: ban, unban, kick, mute, unmute, warn, unwarn, delete, pin, unpin, purge, "
    "lock, unlock, announce, broadcast, set_welcome, set_rules, summarize, chat.\n"
    "Rules:\n"
    "- mute: put the duration in minutes (1 hour = 60, 1 day = 1440, forever = 0). Default 60.\n"
    "- purge: count = number of recent messages to delete (max 100).\n"
    "- summarize: count = how many recent messages to summarize (default 50).\n"
    "- announce: write a polished short Persian announcement in text; pin=true only if asked to pin it.\n"
    "- broadcast: the admin wants a message sent to ALL groups; write the Persian message in text.\n"
    "- set_welcome: text is the welcome message; use {name} and {group} placeholders.\n"
    "- set_rules: text is the rules text.\n"
    "- chat: for questions or normal conversation; write a short Persian answer in reply.\n"
    "- Never invent users; the bot finds the target itself. If the request is unclear use chat and ask in reply."
)


async def ai_plan(body):
    raw = await ai_call(
        [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": body[:1500]}],
        max_tokens=700,
        temperature=0.2,
    )
    return extract_json(raw)


async def ai_chat(key, text):
    if len(AI_HIST) > 2000:
        AI_HIST.clear()
    hist = AI_HIST[key]
    t = text[:1500]
    msgs = [{"role": "system", "content": CHAT_SYSTEM}] + list(hist) + [{"role": "user", "content": t}]
    ans = await ai_call(msgs, max_tokens=700)
    hist.append({"role": "user", "content": t})
    hist.append({"role": "assistant", "content": ans[:1500]})
    return ans


async def summarize(chat_id, n):
    items = list(RECENT[chat_id])[-n:]
    if len(items) < 5:
        return "ℹ️ هنوز پیام کافی برای خلاصه ندارم (ربات فقط پیام‌های بعد از روشن شدنش رو می‌بینه).", True
    convo = "\n".join("%s: %s" % (a, t) for a, t in items)[:6000]
    ans = await ai_call(
        [
            {"role": "system", "content": (
                "چت گروه تلگرام زیر را به فارسی در حداکثر ۸ خط کوتاه (با خط تیره) خلاصه کن: "
                "موضوع‌های اصلی و تصمیم‌ها. بدون مارک‌داون.")},
            {"role": "user", "content": convo},
        ],
        max_tokens=500,
        temperature=0.3,
    )
    return esc("📝 خلاصه گفتگو:\n" + ans), True


async def ai_rate_ok(message):
    now = time.time()
    uid = message.from_user.id if message.from_user else 0
    if now - AI_LAST.get(uid, 0) < AI_USER_GAP:
        return False
    dq = AI_CHAT_LOG[message.chat.id]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= AI_CHAT_MAX:
        return False
    AI_LAST[uid] = now
    dq.append(now)
    return True


async def ai_gate(message):
    if not AI.models:
        await message.reply("⚠️ هوش مصنوعی فعال نیست (AI_API_KEY ست نشده).", parse_mode=None)
        return False
    if message.chat.type != ChatType.PRIVATE:
        cfg = await get_cfg(message.chat.id, message.chat)
        if cfg["mode"] != "ai":
            await message.reply(
                "⚡ این گروه در حالت ساده است. ادمین با /mode می‌تونه حالت هوشمند رو روشن کنه.",
                parse_mode=None,
            )
            return False
    return await ai_rate_ok(message)


def addressed(message, text):
    t = text.strip()
    low = norm(t)
    r = message.reply_to_message
    if r and r.from_user and r.from_user.id == BOT_INFO["id"]:
        return True, t
    for trig in ("جارویس", "jarvis"):
        if low.startswith(trig):
            return True, re.sub(r"^\S+\s*[،,:\-]?\s*", "", t, count=1)
    uname = BOT_INFO["username"].lower()
    if uname and ("@" + uname) in low:
        return True, re.sub("@" + re.escape(uname), "", t, flags=re.I).strip()
    return False, t


async def ai_in_group(message, cfg, body, admin):
    if not body.strip():
        await message.reply("بله؟ بگو چی کار کنم 🙂", parse_mode=None)
        return
    if not await ai_rate_ok(message):
        return
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except TelegramAPIError:
        pass
    try:
        if admin:
            plan = await ai_plan(body)
            await apply_plan(message, plan, body)
        else:
            ans = await ai_chat((message.chat.id, message.from_user.id), body)
            await message.reply(ans[:3900], parse_mode=None)
    except AIError as e:
        await message.reply(ai_error_text(e), parse_mode=None)


async def apply_plan(message, plan, body):
    bot = message.bot
    chat_id = message.chat.id
    issuer = message.from_user.id if message.from_user else 0
    action = str(plan.get("action") or "chat").lower().strip()
    if action not in ALLOWED:
        action = "chat"
    if action == "chat":
        ans = str(plan.get("reply") or "").strip() or "متوجه نشدم، دقیق‌تر بگو."
        await message.reply(ans[:3900], parse_mode=None)
        return

    r = real_reply(message)
    p = {
        "cmd_msg_id": message.message_id,
        "reply_msg_id": r.message_id if r else None,
        "minutes": to_int(plan.get("minutes")),
        "count": to_int(plan.get("count")),
        "lock_type": LOCK_ALIAS.get(norm(str(plan.get("lock_type") or ""))),
        "text": str(plan.get("text") or "").strip(),
        "pin": bool(plan.get("pin")),
    }
    if action in NEEDS_TARGET:
        uid, name = await find_target(message, body)
        p["target_id"], p["target_name"] = uid, name
        err = await check_target(bot, chat_id, uid)
        if err:
            await say(message, err, temp=True)
            return
    if action == "broadcast":
        if not OWNER_ID or issuer != OWNER_ID:
            await say(message, "⛔ فقط مالک ربات می‌تونه برای همه گروه‌ها پخش کنه.", temp=True)
            return
        p["kind"] = "groups"
    if action in ("announce", "broadcast", "set_welcome", "set_rules") and not p["text"]:
        await say(message, "⚠️ متن مشخص نیست. دقیق‌تر بگو چی نوشته بشه.", temp=True)
        return
    if action in CONFIRM:
        pid = new_pending(chat_id, issuer, action, p)
        await message.reply(describe(action, p), reply_markup=confirm_markup(pid), parse_mode="HTML")
        return
    text, h = await run_action(bot, chat_id, action, p)
    await say(message, text, h, temp=action in TEMP_ACTIONS)


# ============================================================ protections
LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|\b[a-z0-9-]+\.(com|ir|net|org|me|io|xyz|info|co)\b)", re.I
)


def has_link(message, text):
    for e in (message.entities or []) + (message.caption_entities or []):
        if e.type in ("url", "text_link"):
            return True
    return bool(text and LINK_RE.search(text))


def is_forward(message):
    return (
        getattr(message, "forward_origin", None) is not None
        or getattr(message, "forward_date", None) is not None
    )


def has_media(message):
    return bool(
        message.photo or message.video or message.document
        or message.animation or message.video_note or message.audio
    )


SERVICE_ATTRS = (
    "left_chat_member", "pinned_message", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "migrate_to_chat_id", "forum_topic_created", "forum_topic_closed",
    "forum_topic_reopened", "forum_topic_edited",
)


def is_service(message):
    return any(getattr(message, a, None) for a in SERVICE_ATTRS)


async def protect(message, cfg, text):
    """Returns True if the message was removed."""
    chat_id = message.chat.id
    locks = lock_set(cfg)
    reason = ""
    if "links" in locks and has_link(message, text):
        reason = "ارسال لینک ممنوعه"
    elif "forward" in locks and is_forward(message):
        reason = "فوروارد ممنوعه"
    elif "media" in locks and has_media(message):
        reason = "ارسال مدیا ممنوعه"
    elif "sticker" in locks and message.sticker:
        reason = "ارسال استیکر ممنوعه"
    elif "voice" in locks and message.voice:
        reason = "ارسال ویس ممنوعه"
    if not reason and text:
        nt = norm(text)
        for w in await get_filters(chat_id):
            if w and w in nt:
                reason = "استفاده از کلمه ممنوعه"
                break
    u = message.from_user
    who = mention(u.id, u.full_name) if u else ""
    if reason:
        try:
            await message.delete()
        except TelegramAPIError:
            return False
        await say(message, "🚫 %s: %s." % (who, reason), temp=True)
        return True

    if cfg.get("flood_on") and u:
        key = (chat_id, u.id)
        dq = FLOOD[key]
        now = time.time()
        dq.append(now)
        while dq and now - dq[0] > FLOOD_SECS:
            dq.popleft()
        if len(dq) > FLOOD_MAX:
            dq.clear()
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MIN)
                await message.bot.restrict_chat_member(
                    chat_id, u.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
                )
            except TelegramAPIError:
                return False
            await say(message, "🌊 %s به‌خاطر اسپم %d دقیقه سکوت شد." % (who, FLOOD_MUTE_MIN), temp=True)
            return True
    return False


# ============================================================ keyword + custom words
async def send_rules(message, cfg):
    rules = (cfg.get("rules") or "").strip()
    if not rules:
        await say(message, "ℹ️ هنوز قوانینی برای این گروه ثبت نشده.")
        return
    await say(message, "📜 <b>قوانین گروه</b>\n\n" + esc(rules))


async def send_top(message):
    await flush_stats()
    rows = await db_all(
        "SELECT s.user_id AS uid, s.cnt AS cnt, u.name AS name FROM stats s "
        "LEFT JOIN users u ON u.user_id=s.user_id WHERE s.chat_id=? ORDER BY s.cnt DESC LIMIT 10",
        (message.chat.id,),
    )
    if not rows:
        await say(message, "ℹ️ هنوز آماری ثبت نشده.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>فعال‌ترین‌ها</b>\n"]
    for i, r in enumerate(rows):
        mark = medals[i] if i < 3 else "%d." % (i + 1)
        lines.append("%s %s — %d پیام" % (mark, mention(r["uid"], r["name"] or r["uid"]), r["cnt"]))
    await say(message, "\n".join(lines))


async def send_stats(message, cfg):
    await flush_stats()
    try:
        members = await message.bot.get_chat_member_count(message.chat.id)
    except TelegramAPIError:
        members = "؟"
    row = await db_one("SELECT SUM(cnt) AS total FROM stats WHERE chat_id=?", (message.chat.id,))
    total = (row or {}).get("total") or 0
    mode = "🤖 هوشمند" if cfg.get("mode") == "ai" else "⚡ ساده"
    await say(message, "📊 <b>آمار گروه</b>\n\nاعضا: %s\nپیام‌های ثبت‌شده: %s\nحالت: %s" % (members, total, mode))


async def handle_keyword(message, cfg, text, admin):
    kw = parse_keyword(text)
    if not kw:
        return False
    action, args = kw

    if action in PUBLIC_KW:
        if action == "rules":
            await send_rules(message, cfg)
        elif action == "stats":
            await send_stats(message, cfg)
        else:
            await send_top(message)
        return True

    if not admin:
        return False

    if action in ("mode_ai", "mode_simple"):
        await set_mode(message, "ai" if action == "mode_ai" else "simple")
        return True

    r = real_reply(message)
    if action in NEEDS_TARGET:
        uid, _ = await find_target(message, " ".join(args))
        if not uid:
            return False
    elif action in ("delete", "pin"):
        if not r:
            return False
    elif action in ("lock", "unlock"):
        if not lock_from_args(args):
            return False
    elif action == "purge":
        if not r and not first_int(args):
            return False

    await handle_mod(message, action, args)
    return True


async def handle_custom_word(message):
    words = await get_words(message.chat.id)
    if not words:
        return False
    rep = words.get(norm(message.text))
    if not rep:
        return False
    name = message.from_user.full_name if message.from_user else ""
    await message.reply(rep.replace("{name}", name)[:3900], parse_mode=None)
    return True


# ============================================================ texts
START_TEXT = (
    "👋 سلام! من <b>جارویس</b> هستم؛ ربات مدیریت گروه و دستیار هوشمند.\n"
    "سازنده من: زیکو\n\n"
    "🔹 اینجا در چت خصوصی باهام گپ بزن.\n"
    "🔹 منو به گروهت اضافه کن و ادمینم کن تا گروه رو مدیریت کنم.\n\n"
    "راهنما: /help"
)

GROUP_INTRO = (
    "👋 سلام! من جارویسم.\n\n"
    "برای کار کردن:\n"
    "1) منو ادمین کنید (حذف پیام، محدود کردن کاربران، پین کردن)\n"
    "2) از BotFather گزینه Group Privacy رو Disable کنید\n\n"
    "بعد بنویسید /settings تا حالت‌ها رو تنظیم کنید. راهنما: /help"
)

HELP_TEXT = (
    "🤖 <b>راهنمای جارویس</b>\n\n"
    "<b>دو حالت</b>\n"
    "⚡ ساده: بدون هوش مصنوعی. دستورها، کلمات ثابت و کلمات سفارشی.\n"
    "🤖 هوشمند: همه‌ی موارد بالا + بنویس «جارویس ...» و فارسی عادی دستور بده.\n"
    "تغییر حالت (ادمین): /mode یا بنویس «حالت هوشمند» / «حالت ساده»\n\n"
    "<b>کلمات ثابت (ریپلای روی پیام کاربر، فقط ادمین)</b>\n"
    "بن • آنبن • اخراج • سکوت ۳۰ • رفع سکوت • اخطار • حذف اخطار\n"
    "حذف • پین • آنپین • پاکسازی ۲۰\n"
    "قفل لینک • باز کردن لینک (لینک، فوروارد، مدیا، استیکر، ویس)\n"
    "برای همه: قوانین • آمار • برترین\n"
    "مدت سکوت: «سکوت ۳۰» دقیقه، «سکوت ۲ ساعت»، «سکوت ۱ روز»، «سکوت همیشه»\n\n"
    "<b>دستورهای ادمین</b>\n"
    "/ban /unban /kick /mute /unmute /warn /unwarn /del /purge /pin /unpin /lock /unlock\n"
    "/settings — پنل تنظیمات (حالت، قفل‌ها، ضدفلود)\n"
    "/setrules متن • /setwelcome متن ({name} و {group})\n"
    "/welcome off|ai • /warnlimit N • /warnaction mute|ban|kick • /flood on|off\n"
    "/addword کلمه | پاسخ • /delword کلمه • /words\n"
    "/addfilter کلمه • /delfilter کلمه • /filters\n"
    "/announce [pin] متن • /summary [تعداد]\n\n"
    "<b>برای همه</b>\n"
    "/rules /top /stats /id /info /report /ask سوال /tr متن\n\n"
    "<b>نمونه در حالت هوشمند</b>\n"
    "«جارویس این رو ۱ ساعت سکوت کن» (ریپلای)\n"
    "«جارویس یه اعلامیه برای جلسه فردا بنویس و پین کن»\n"
    "«جارویس ۳۰ تا پیام آخر رو پاک کن»\n"
    "«جارویس چت رو خلاصه کن»\n"
    "کارهای حساس (بن، اخراج، پاکسازی، پخش همگانی) قبل از اجرا تایید می‌گیرن."
)


# ============================================================ handlers: start / help / id
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_private(message: Message):
    u = message.from_user
    if u:
        SEEN[u.id] = user_key(u)
        await upsert_user(u, started=True)
    url = (
        "https://t.me/%s?startgroup=true&admin=delete_messages+restrict_members+pin_messages+invite_users"
        % BOT_INFO["username"]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ افزودن به گروه", url=url)]])
    await message.answer(START_TEXT, reply_markup=kb)


@router.message(Command("start"), F.chat.type.in_(GROUPS))
async def start_group(message: Message):
    await say(message, "👋 من جارویسم. راهنما: /help")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("id"))
async def cmd_id(message: Message):
    lines = ["🆔 چت: <code>%s</code>" % message.chat.id]
    if message.from_user:
        lines.append("👤 شما: <code>%s</code>" % message.from_user.id)
    r = real_reply(message)
    if r and r.from_user:
        lines.append("↩️ کاربر ریپلای‌شده: <code>%s</code>" % r.from_user.id)
    await message.answer("\n".join(lines))


@router.message(Command("info"), F.chat.type.in_(GROUPS))
async def cmd_info(message: Message):
    uid, name = await find_target(message, message.text or "")
    if not uid and message.from_user:
        uid, name = message.from_user.id, message.from_user.full_name
    if not uid:
        return
    try:
        m = await message.bot.get_chat_member(message.chat.id, uid)
        status = str(getattr(m.status, "value", m.status))
        uname = getattr(m.user, "username", None)
    except TelegramAPIError:
        status, uname = "؟", None
    await flush_stats()
    row = await db_one("SELECT cnt FROM stats WHERE chat_id=? AND user_id=?", (message.chat.id, uid))
    warns = await get_warns(message.chat.id, uid)
    lines = [
        "👤 %s" % mention(uid, name or uid),
        "🆔 <code>%s</code>" % uid,
        "یوزرنیم: %s" % (("@" + esc(uname)) if uname else "ندارد"),
        "وضعیت: %s" % esc(status),
        "پیام‌ها: %s" % (row["cnt"] if row else 0),
        "اخطار: %s" % warns,
    ]
    await say(message, "\n".join(lines))


# ============================================================ handlers: moderation commands
MOD_CMDS = {
    "ban": "ban", "unban": "unban", "kick": "kick", "mute": "mute", "unmute": "unmute",
    "warn": "warn", "unwarn": "unwarn", "del": "delete", "purge": "purge",
    "pin": "pin", "unpin": "unpin", "lock": "lock", "unlock": "unlock",
}


@router.message(Command(*MOD_CMDS.keys()), F.chat.type.in_(GROUPS))
async def cmd_mod(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    action = MOD_CMDS.get((command.command or "").lower())
    if not action:
        return
    await handle_mod(message, action, (command.args or "").split())


@router.message(Command("mode", "settings"), F.chat.type.in_(GROUPS))
async def cmd_mode(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    cfg = await get_cfg(message.chat.id, message.chat)
    arg = norm(command.args or "")
    if arg in ("ai", "هوشمند"):
        await set_mode(message, "ai")
        return
    if arg in ("simple", "ساده"):
        await set_mode(message, "simple")
        return
    await message.answer(settings_text(cfg), reply_markup=settings_markup(cfg), parse_mode="HTML")


@router.message(Command("rules"), F.chat.type.in_(GROUPS))
async def cmd_rules(message: Message):
    await send_rules(message, await get_cfg(message.chat.id, message.chat))


@router.message(Command("setrules"), F.chat.type.in_(GROUPS))
async def cmd_setrules(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    text = (command.args or "").strip()
    r = real_reply(message)
    if not text and r and (r.text or r.caption):
        text = r.text or r.caption
    if not text:
        await say(message, "استفاده: /setrules متن قوانین (یا ریپلای روی متن قوانین)")
        return
    await set_cfg(message.chat.id, rules=text[:3000])
    await say(message, "✅ قوانین ذخیره شد.")


@router.message(Command("setwelcome"), F.chat.type.in_(GROUPS))
async def cmd_setwelcome(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    text = (command.args or "").strip()
    if not text:
        await say(message, "استفاده: /setwelcome متن خوش‌آمد\nمی‌تونی از {name} و {group} استفاده کنی.")
        return
    await set_cfg(message.chat.id, welcome=text[:1500], ai_welcome=0)
    await say(message, "✅ پیام خوش‌آمد ذخیره شد.")


@router.message(Command("welcome"), F.chat.type.in_(GROUPS))
async def cmd_welcome(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    arg = norm(command.args or "")
    if arg == "off":
        await set_cfg(message.chat.id, welcome="", ai_welcome=0)
        await say(message, "✅ خوش‌آمدگویی خاموش شد.")
    elif arg == "ai":
        if not AI.models:
            await say(message, "⚠️ هوش مصنوعی فعال نیست (AI_API_KEY ست نشده).")
            return
        await set_cfg(message.chat.id, ai_welcome=1)
        await say(message, "✅ خوش‌آمدگویی هوشمند روشن شد (فقط در حالت هوشمند کار می‌کنه).")
    else:
        await say(message, "استفاده: /welcome off  یا  /welcome ai")


@router.message(Command("warnlimit"), F.chat.type.in_(GROUPS))
async def cmd_warnlimit(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    n = first_int([command.args or ""])
    if not n or not 1 <= n <= 10:
        await say(message, "استفاده: /warnlimit عدد (۱ تا ۱۰)")
        return
    await set_cfg(message.chat.id, warn_limit=n)
    await say(message, "✅ سقف اخطار روی %d تنظیم شد." % n)


@router.message(Command("warnaction"), F.chat.type.in_(GROUPS))
async def cmd_warnaction(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    arg = norm(command.args or "")
    if arg not in ("mute", "ban", "kick"):
        await say(message, "استفاده: /warnaction mute  یا  ban  یا  kick")
        return
    await set_cfg(message.chat.id, warn_action=arg)
    await say(message, "✅ مجازات رسیدن به سقف اخطار: %s" % arg)


@router.message(Command("flood"), F.chat.type.in_(GROUPS))
async def cmd_flood(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    arg = norm(command.args or "")
    if arg not in ("on", "off"):
        await say(message, "استفاده: /flood on  یا  /flood off")
        return
    await set_cfg(message.chat.id, flood_on=1 if arg == "on" else 0)
    await say(message, "✅ ضدفلود %s شد." % ("روشن" if arg == "on" else "خاموش"))


@router.message(Command("warns"), F.chat.type.in_(GROUPS))
async def cmd_warns(message: Message):
    uid, name = await find_target(message, message.text or "")
    if not uid and message.from_user:
        uid, name = message.from_user.id, message.from_user.full_name
    cfg = await get_cfg(message.chat.id, message.chat)
    cnt = await get_warns(message.chat.id, uid)
    await say(message, "⚠️ اخطارهای %s: %d/%d" % (mention(uid, name or uid), cnt, int(cfg.get("warn_limit") or 3)))


# ----- custom words and filters
@router.message(Command("addword"), F.chat.type.in_(GROUPS))
async def cmd_addword(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    raw = command.args or ""
    r = real_reply(message)
    if "|" in raw:
        trig, rep = raw.split("|", 1)
    elif r and (r.text or r.caption) and raw.strip():
        trig, rep = raw, (r.text or r.caption)
    else:
        await say(message, "استفاده: /addword کلمه | پاسخ\nمی‌تونی توی پاسخ از {name} استفاده کنی.")
        return
    trig, rep = norm(trig), rep.strip()
    if not trig or not rep:
        await say(message, "کلمه و پاسخ نباید خالی باشن.")
        return
    await db_run("INSERT OR REPLACE INTO words(chat_id,trig,reply) VALUES(?,?,?)", (message.chat.id, trig, rep[:3000]))
    WORDS.pop(message.chat.id, None)
    await say(message, "✅ کلمه «%s» ثبت شد." % esc(trig))


@router.message(Command("delword"), F.chat.type.in_(GROUPS))
async def cmd_delword(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    trig = norm(command.args or "")
    if not trig:
        await say(message, "استفاده: /delword کلمه")
        return
    await db_run("DELETE FROM words WHERE chat_id=? AND trig=?", (message.chat.id, trig))
    WORDS.pop(message.chat.id, None)
    await say(message, "✅ حذف شد.")


@router.message(Command("words"), F.chat.type.in_(GROUPS))
async def cmd_words(message: Message):
    words = await get_words(message.chat.id)
    if not words:
        await say(message, "ℹ️ هیچ کلمه سفارشی‌ای ثبت نشده.")
        return
    await say(message, "🔤 <b>کلمات سفارشی</b>\n" + "\n".join("• " + esc(w) for w in list(words)[:60]))


@router.message(Command("addfilter"), F.chat.type.in_(GROUPS))
async def cmd_addfilter(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    w = norm(command.args or "")
    if not w:
        await say(message, "استفاده: /addfilter کلمه")
        return
    await db_run("INSERT OR IGNORE INTO filters(chat_id,word) VALUES(?,?)", (message.chat.id, w))
    FILTERS.pop(message.chat.id, None)
    await say(message, "✅ کلمه «%s» فیلتر شد." % esc(w))


@router.message(Command("delfilter"), F.chat.type.in_(GROUPS))
async def cmd_delfilter(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    w = norm(command.args or "")
    if not w:
        await say(message, "استفاده: /delfilter کلمه")
        return
    await db_run("DELETE FROM filters WHERE chat_id=? AND word=?", (message.chat.id, w))
    FILTERS.pop(message.chat.id, None)
    await say(message, "✅ از فیلتر خارج شد.")


@router.message(Command("filters"), F.chat.type.in_(GROUPS))
async def cmd_filters(message: Message):
    words = await get_filters(message.chat.id)
    if not words:
        await say(message, "ℹ️ هیچ کلمه فیلترشده‌ای نیست.")
        return
    await say(message, "🚫 <b>کلمات فیلترشده</b>\n" + "\n".join("• " + esc(w) for w in words[:60]))


# ----- announce / stats / report
@router.message(Command("announce"), F.chat.type.in_(GROUPS))
async def cmd_announce(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    text = (command.args or "").strip()
    pin = False
    if norm(text).startswith("pin "):
        pin = True
        text = text[4:].strip()
    if not text:
        await say(message, "استفاده: /announce [pin] متن اعلامیه")
        return
    r = real_reply(message)
    p = {"text": text, "pin": pin, "cmd_msg_id": message.message_id, "reply_msg_id": r.message_id if r else None}
    out, h = await run_action(message.bot, message.chat.id, "announce", p)
    await say(message, out, h, temp=True)


@router.message(Command("top"), F.chat.type.in_(GROUPS))
async def cmd_top(message: Message):
    await send_top(message)


@router.message(Command("stats"), F.chat.type.in_(GROUPS))
async def cmd_stats(message: Message):
    await send_stats(message, await get_cfg(message.chat.id, message.chat))


@router.message(Command("report"), F.chat.type.in_(GROUPS))
async def cmd_report(message: Message):
    r = real_reply(message)
    if not r:
        await say(message, "برای گزارش، روی پیام مورد نظر ریپلای کن و /report بزن.", temp=True)
        return
    try:
        admins = await message.bot.get_chat_administrators(message.chat.id)
    except TelegramAPIError:
        return
    tags = " ".join(mention(a.user.id, a.user.full_name) for a in admins if not a.user.is_bot)[:900]
    who = mention(message.from_user.id, message.from_user.full_name) if message.from_user else ""
    await r.reply("🚨 گزارش از %s\n%s" % (who, tags), parse_mode="HTML")


# ----- AI commands
@router.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject):
    q = (command.args or "").strip()
    r = real_reply(message)
    if not q and r:
        q = r.text or r.caption or ""
    if not q:
        await message.reply("استفاده: /ask سوالت", parse_mode=None)
        return
    if not await ai_gate(message):
        return
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        ans = await ai_chat((message.chat.id, message.from_user.id), q)
        await message.reply(ans[:3900], parse_mode=None)
    except AIError as e:
        await message.reply(ai_error_text(e), parse_mode=None)


@router.message(Command("tr"))
async def cmd_tr(message: Message, command: CommandObject):
    q = (command.args or "").strip()
    r = real_reply(message)
    if not q and r:
        q = r.text or r.caption or ""
    if not q:
        await message.reply("استفاده: /tr متن (یا ریپلای روی یک پیام)", parse_mode=None)
        return
    if not await ai_gate(message):
        return
    try:
        ans = await ai_call(
            [{"role": "system", "content": TR_SYSTEM}, {"role": "user", "content": q[:2000]}],
            max_tokens=600,
            temperature=0.2,
        )
        await message.reply(ans[:3900], parse_mode=None)
    except AIError as e:
        await message.reply(ai_error_text(e), parse_mode=None)


@router.message(Command("summary"), F.chat.type.in_(GROUPS))
async def cmd_summary(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    if not await ai_gate(message):
        return
    n = first_int([command.args or ""]) or 50
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except TelegramAPIError:
        pass
    text, h = await run_action(message.bot, message.chat.id, "summarize", {"count": n})
    await say(message, text, h)


# ----- owner commands
def is_owner(message):
    return bool(OWNER_ID and message.from_user and message.from_user.id == OWNER_ID)


@router.message(Command("broadcast", "broadcast_users"), F.chat.type == ChatType.PRIVATE)
async def cmd_broadcast(message: Message, command: CommandObject):
    if not is_owner(message):
        await message.answer("⛔ این دستور فقط برای مالک ربات است.")
        return
    text = (command.args or "").strip()
    r = real_reply(message)
    if not text and r and (r.text or r.caption):
        text = r.text or r.caption
    if not text:
        await message.answer(
            "استفاده:\n/broadcast متن  ← همه گروه‌ها\n/broadcast_users متن  ← همه کاربرهای ربات"
        )
        return
    kind = "users" if (command.command or "").lower() == "broadcast_users" else "groups"
    p = {"text": text, "kind": kind}
    pid = new_pending(message.chat.id, message.from_user.id, "broadcast", p)
    await message.answer(describe("broadcast", p), reply_markup=confirm_markup(pid), parse_mode="HTML")


@router.message(Command("chats"), F.chat.type == ChatType.PRIVATE)
async def cmd_chats(message: Message):
    if not is_owner(message):
        return
    rows = await db_all(
        "SELECT title, mode FROM chats WHERE active=1 AND type IN ('group','supergroup') ORDER BY title LIMIT 40"
    )
    users = await db_one("SELECT COUNT(*) AS n FROM users WHERE started=1")
    lines = ["📋 <b>گروه‌های فعال: %d</b>" % len(rows)]
    for r in rows:
        lines.append("%s %s" % ("🤖" if r["mode"] == "ai" else "⚡", esc(r["title"] or "بدون نام")))
    lines.append("\n👤 کاربران خصوصی: %d" % (users["n"] if users else 0))
    await message.answer("\n".join(lines))


# ============================================================ join / status events
@router.message(F.new_chat_members)
async def on_join(message: Message):
    chat = message.chat
    if chat.type not in GROUPS:
        return
    cfg = await get_cfg(chat.id, chat)
    for u in message.new_chat_members or []:
        if u.id == BOT_INFO["id"]:
            await say(message, GROUP_INTRO, html_mode=False)
            continue
        if u.is_bot:
            continue
        SEEN[u.id] = user_key(u)
        spawn(upsert_user(u))
        text = None
        if cfg.get("ai_welcome") and cfg.get("mode") == "ai" and AI.models:
            try:
                text = await ai_call(
                    [
                        {"role": "system", "content": "یک خوش‌آمدگویی گرم و کوتاه (حداکثر ۲ جمله) به فارسی بنویس. بدون مارک‌داون."},
                        {"role": "user", "content": "اسم کاربر: %s | اسم گروه: %s" % (u.full_name, chat.title or "")},
                    ],
                    max_tokens=150,
                    temperature=0.8,
                )
            except AIError:
                text = None
        if text:
            await say(message, text, html_mode=False)
        elif cfg.get("welcome"):
            t = esc(cfg["welcome"]).replace("{name}", mention(u.id, u.full_name)).replace("{group}", esc(chat.title or ""))
            await say(message, t)


@router.my_chat_member()
async def on_my_status(event: ChatMemberUpdated):
    chat = event.chat
    st = event.new_chat_member.status
    gone = st in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    if chat.type == ChatType.PRIVATE:
        if gone:
            await db_run("UPDATE users SET started=0 WHERE user_id=?", (chat.id,))
        return
    if gone:
        await db_run("UPDATE chats SET active=0 WHERE chat_id=?", (chat.id,))
    else:
        await get_cfg(chat.id, chat)
        await db_run("UPDATE chats SET active=1, title=? WHERE chat_id=?", (chat.title or "", chat.id))
    CFG.pop(chat.id, None)


# ============================================================ private chat + group catch-all
@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def private_chat(message: Message):
    if not AI.models:
        await message.answer("⚠️ هوش مصنوعی فعال نیست (AI_API_KEY ست نشده).")
        return
    if not await ai_rate_ok(message):
        return
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        ans = await ai_chat((message.chat.id, message.from_user.id), message.text)
        await message.answer(ans[:3900], parse_mode=None)
    except AIError as e:
        await message.answer(ai_error_text(e), parse_mode=None)


@router.message(F.chat.type.in_(GROUPS))
async def on_group(message: Message):
    if getattr(message, "is_automatic_forward", None) or is_service(message):
        return
    chat = message.chat
    cfg = await get_cfg(chat.id, chat)
    u = message.from_user
    text = message.text or message.caption or ""
    if u and not u.is_bot and not message.sender_chat:
        track_user(u, chat.id)
        if message.text:
            RECENT[chat.id].append((u.full_name, message.text[:300]))
    admin = await sender_is_admin(message)
    if not admin:
        if await protect(message, cfg, text):
            return
    if not message.text:
        return
    if await handle_keyword(message, cfg, message.text, admin):
        return
    if await handle_custom_word(message):
        return
    if cfg.get("mode") == "ai" and AI.models:
        hit, body = addressed(message, message.text)
        if hit:
            await ai_in_group(message, cfg, body, admin)


# ============================================================ main
async def start_health():
    if not PORT:
        return None
    try:
        port = int(PORT)
    except ValueError:
        return None
    app = web.Application()

    async def ok(_request):
        return web.Response(text="Jarvis bot is running")

    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server on port %s", port)
    return runner


async def main():
    global HTTP
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set. Put it in the environment variables or in a .env file.")
        return
    await init_db()
    HTTP = aiohttp.ClientSession()
    runner = None
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await init_ai()
        me = await bot.get_me()
        BOT_INFO["id"], BOT_INFO["username"] = me.id, me.username or ""
        log.info("Bot started as @%s (owner id: %s)", me.username, OWNER_ID or "not set")
        dp = Dispatcher()
        dp.include_router(router)
        runner = await start_health()
        spawn(flusher())
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        try:
            await flush_stats()
        except Exception:
            pass
        if runner:
            await runner.cleanup()
        await HTTP.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
