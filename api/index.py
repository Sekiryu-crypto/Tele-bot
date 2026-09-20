"""
Rose-style Telegram group manager for Vercel.

Stack : FastAPI (webhook) + httpx + Upstash Redis (REST).  No Telegram library,
        so there is nothing heavy to break on serverless.
Env   : BOT_TOKEN, WEBHOOK_SECRET, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
        (KV_REST_API_URL / KV_REST_API_TOKEN from the Vercel Upstash integration also work)
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from contextvars import ContextVar

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("groupbot")

# ───────────────────────────── config ─────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
REDIS_URL = (
    os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL") or ""
).strip().rstrip("/")
REDIS_TOKEN = (
    os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN") or ""
).strip()

API = "https://api.telegram.org/bot" + BOT_TOKEN
_id_part = BOT_TOKEN.split(":")[0]
BOT_ID = int(_id_part) if _id_part.isdigit() else 0

ANON_ADMIN_ID = 1087968824      # "GroupAnonymousBot"
SPECIAL_IDS = {ANON_ADMIN_ID, 777000, 136817688}  # anonymous admin, Telegram, Channel_Bot
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]

# Per-request context (a fresh HTTP client per update: safe on serverless)
_client: ContextVar = ContextVar("client")
_admin_cache: ContextVar = ContextVar("admin_cache", default=None)
_ME = {}

app = FastAPI(title="Group Manager Bot")

# ───────────────────────────── low-level APIs ─────────────────────────────


async def tg(method, **params):
    """Call a Telegram Bot API method. Never raises; returns the JSON dict."""
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = await _client.get().post(API + "/" + method, json=params)
        data = r.json()
    except Exception as e:  # network / JSON problems
        log.warning("tg %s failed: %s", method, e)
        return {"ok": False, "description": str(e)}
    if not data.get("ok"):
        log.info("tg %s -> %s", method, data.get("description"))
    return data


def _redis_headers():
    return {"Authorization": "Bearer " + REDIS_TOKEN}


async def rcmd(*cmd):
    """Run one Redis command through the Upstash REST API."""
    r = await _client.get().post(REDIS_URL, headers=_redis_headers(), json=[str(c) for c in cmd])
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("redis: " + str(data["error"]))
    return data.get("result")


async def rpipe(cmds):
    """Run several Redis commands in one round trip."""
    body = [[str(c) for c in cmd] for cmd in cmds]
    r = await _client.get().post(REDIS_URL + "/pipeline", headers=_redis_headers(), json=body)
    out = []
    for item in r.json():
        if isinstance(item, dict) and item.get("error"):
            raise RuntimeError("redis: " + str(item["error"]))
        out.append(item.get("result") if isinstance(item, dict) else item)
    return out


# ───────────────────────────── settings storage ─────────────────────────────


def default_settings():
    return {
        "welcome": {"on": True, "content": None, "clean": False},
        "goodbye": {"on": False, "content": None},
        "rules": None,
        "locks": [],
        "blacklist": [],
        "bl_mode": "delete",
        "filters": {},
        "notes": {},
        "flood": {"limit": 0, "mode": "mute"},
        "warn": {"limit": 3, "mode": "ban"},
        "captcha": False,
        "last_welcome": 0,
    }


async def load_settings(chat_id):
    s = default_settings()
    raw = await rcmd("GET", "c:%s" % chat_id)
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        for k, v in data.items():
            if isinstance(s.get(k), dict) and isinstance(v, dict) and k not in ("filters", "notes"):
                s[k].update(v)
            else:
                s[k] = v
    return s


async def save_settings(chat_id, s):
    await rcmd("SET", "c:%s" % chat_id, json.dumps(s, ensure_ascii=False))


async def remember(u):
    """Remember username -> id so admins can use @username."""
    if u and u.get("username"):
        try:
            await rcmd("SET", "u:" + u["username"].lower(), u["id"], "EX", 60 * 60 * 24 * 90)
        except Exception:
            pass


# ───────────────────────────── text helpers ─────────────────────────────


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def full_name(u):
    n = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
    return n or str(u.get("id", "user"))


def mention(u):
    return '<a href="tg://user?id=%s">%s</a>' % (u["id"], esc(full_name(u)))


def strip_html(t):
    return html.unescape(re.sub(r"<[^>]+>", "", t))


_TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
    "blockquote": ("<blockquote>", "</blockquote>"),
}


def entities_to_html(text, entities):
    """Convert Telegram entities (UTF-16 offsets) to Telegram-HTML, keeping formatting."""
    items = []
    for e in entities or []:
        t = e.get("type")
        if t in _TAGS:
            o, c = _TAGS[t]
        elif t == "text_link" and e.get("url"):
            o, c = '<a href="%s">' % html.escape(e["url"], quote=True), "</a>"
        elif t == "text_mention" and e.get("user"):
            o, c = '<a href="tg://user?id=%s">' % e["user"]["id"], "</a>"
        else:
            continue
        items.append((e["offset"], e["offset"] + e["length"], o, c))
    if not items:
        return esc(text)

    raw = text.encode("utf-16-le")
    n = len(raw) // 2
    opens, closes = {}, set()
    for s_, e_, o, c in items:
        if s_ < 0 or e_ > n or s_ >= e_:
            continue
        opens.setdefault(s_, []).append((e_, o, c))
        closes.add(e_)
    points = sorted(set([0, n]) | set(opens) | closes)
    out, stack = [], []
    for i, p in enumerate(points):
        if any(x[0] == p for x in stack):
            popped = []
            while any(x[0] == p for x in stack):
                x = stack.pop()
                out.append(x[2])
                if x[0] != p:
                    popped.append(x)
            for x in reversed(popped):
                out.append(x[1])
                stack.append(x)
        for e_, o, c in sorted(opens.get(p, []), key=lambda z: -z[0]):
            out.append(o)
            stack.append((e_, o, c))
        if p < n:
            out.append(esc(raw[p * 2:points[i + 1] * 2].decode("utf-16-le")))
    return "".join(out)


def tail_html(m, tail):
    """HTML of `tail`, a suffix of the message text, keeping the user's formatting."""
    if not tail:
        return ""
    text = m.get("text") or m.get("caption") or ""
    ents = m.get("entities") if m.get("text") else m.get("caption_entities")
    start = len(text) - len(tail)
    if start < 0 or text[start:] != tail:
        return esc(tail)
    off = len(text[:start].encode("utf-16-le")) // 2
    shifted = [dict(e, offset=e["offset"] - off) for e in (ents or []) if e["offset"] >= off]
    return entities_to_html(tail, shifted)


BTN_RE = re.compile(r"\[([^\]]+?)\]\(buttonurl://([^)\s]+?)(:same)?\)")


def parse_buttons(text):
    rows = []

    def rep(mt):
        label = html.unescape(mt.group(1))
        url = html.unescape(mt.group(2))
        if url.startswith("t.me/"):
            url = "https://" + url
        elif not re.match(r"^(https?://|tg://)", url):
            url = "https://" + url
        btn = {"text": label, "url": url}
        if mt.group(3) and rows:
            rows[-1].append(btn)
        else:
            rows.append([btn])
        return ""

    clean = BTN_RE.sub(rep, text).strip()
    return clean, rows


async def fill(text, user, chat):
    count = ""
    if "{count}" in text:
        r = await tg("getChatMemberCount", chat_id=chat["id"])
        count = str(r.get("result", ""))
    uname = "@" + user["username"] if user.get("username") else mention(user)
    values = {
        "{first}": esc(user.get("first_name", "")),
        "{last}": esc(user.get("last_name", "")),
        "{fullname}": esc(full_name(user)),
        "{username}": uname,
        "{mention}": mention(user),
        "{id}": str(user.get("id", "")),
        "{chatname}": esc(chat.get("title", "")),
        "{count}": count,
    }
    for k, v in values.items():
        text = text.replace(k, v)
    return text


# ───────────────────────────── sending ─────────────────────────────


def thread_of(m):
    return m.get("message_thread_id") if m.get("is_topic_message") else None


async def send(chat_id, text, *, reply_to=None, thread=None, markup=None):
    p = dict(
        chat_id=chat_id,
        text=text[:4096],
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
        reply_markup=markup,
        message_thread_id=thread,
    )
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    r = await tg("sendMessage", **p)
    if not r.get("ok") and "parse" in (r.get("description") or "").lower():
        p["text"] = strip_html(text)[:4096]
        p.pop("parse_mode")
        r = await tg("sendMessage", **p)
    return r


async def reply(m, text, markup=None):
    return await send(m["chat"]["id"], text, reply_to=m["message_id"], thread=thread_of(m), markup=markup)


MEDIA = {
    "photo": ("sendPhoto", "photo"),
    "sticker": ("sendSticker", "sticker"),
    "document": ("sendDocument", "document"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "voice": ("sendVoice", "voice"),
    "audio": ("sendAudio", "audio"),
}
MEDIA_ORDER = ["animation", "photo", "sticker", "video", "voice", "audio", "document"]


def content_from_message(msg):
    """Turn a Telegram message into a storable content dict (text or media)."""
    for kind in MEDIA_ORDER:
        if kind in msg:
            obj = msg[kind]
            fid = obj[-1]["file_id"] if kind == "photo" else obj["file_id"]
            cap = entities_to_html(msg.get("caption") or "", msg.get("caption_entities"))
            return {"type": kind, "file_id": fid, "text": cap}
    if msg.get("text"):
        return {"type": "text", "text": entities_to_html(msg["text"], msg.get("entities"))}
    return None


def build_content(m, tail):
    rep = reply_target(m)
    if rep:
        c = content_from_message(rep)
        if c:
            return c
    if tail and tail.strip():
        return {"type": "text", "text": tail_html(m, tail)}
    return None


async def send_content(chat, content, user, *, reply_to=None, thread=None):
    text, kb = parse_buttons(content.get("text") or "")
    text = await fill(text, user, chat)
    markup = {"inline_keyboard": kb} if kb else None
    kind = content.get("type", "text")
    if kind == "text" or kind not in MEDIA:
        if not text.strip():
            return {"ok": False}
        return await send(chat["id"], text, reply_to=reply_to, thread=thread, markup=markup)
    method, field = MEDIA[kind]
    p = {"chat_id": chat["id"], field: content["file_id"], "reply_markup": markup, "message_thread_id": thread}
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if kind != "sticker" and text.strip():
        p["caption"] = text[:1024]
        p["parse_mode"] = "HTML"
    r = await tg(method, **p)
    if not r.get("ok") and "caption" in p and "parse" in (r.get("description") or "").lower():
        p["caption"] = strip_html(p["caption"])
        p.pop("parse_mode", None)
        r = await tg(method, **p)
    return r


# ───────────────────────────── permissions ─────────────────────────────

RIGHT_LABEL = {
    "can_restrict_members": "Ban users",
    "can_delete_messages": "Delete messages",
    "can_pin_messages": "Pin messages",
    "can_promote_members": "Add new admins",
    "can_change_info": "Change group info",
}

PERM_KEYS = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_invite_users",
    "can_change_info", "can_pin_messages", "can_manage_topics",
]
NO_PERMS = {k: False for k in PERM_KEYS}
DEFAULT_PERMS = {k: True for k in PERM_KEYS if k not in ("can_change_info", "can_pin_messages", "can_manage_topics")}


async def restore_perms(chat_id):
    r = await tg("getChat", chat_id=chat_id)
    perms = (r.get("result") or {}).get("permissions") if r.get("ok") else None
    return perms or DEFAULT_PERMS


async def get_member(chat_id, uid):
    cache = _admin_cache.get()
    key = (chat_id, uid)
    if cache is not None and key in cache:
        return cache[key]
    r = await tg("getChatMember", chat_id=chat_id, user_id=uid)
    mem = r.get("result") if r.get("ok") else None
    if cache is not None:
        cache[key] = mem
    return mem


def is_admin_mem(mem):
    return bool(mem) and mem.get("status") in ("creator", "administrator")


def has_right_mem(mem, right):
    if not mem:
        return False
    if mem.get("status") == "creator":
        return True
    return mem.get("status") == "administrator" and bool(mem.get(right))


def is_anon(m):
    return m["from"]["id"] == ANON_ADMIN_ID or (m.get("sender_chat") or {}).get("id") == m["chat"]["id"]


async def sender_is_admin(m):
    if is_anon(m):
        return True
    return is_admin_mem(await get_member(m["chat"]["id"], m["from"]["id"]))


async def need(m, right=None):
    """Return True if the sender may run an admin command; otherwise tell them why."""
    if is_anon(m):
        return True
    mem = await get_member(m["chat"]["id"], m["from"]["id"])
    ok = has_right_mem(mem, right) if right else is_admin_mem(mem)
    if not ok:
        extra = " with the <b>%s</b> right" % RIGHT_LABEL[right] if right in RIGHT_LABEL else ""
        await reply(m, "🚫 You need to be an admin%s to use this command." % extra)
    return ok


async def exempt(m):
    """Automatic enforcement helper: admins are exempt. If Telegram can't tell us
    (API hiccup), fail open and leave the message alone."""
    if is_anon(m):
        return True
    mem = await get_member(m["chat"]["id"], m["from"]["id"])
    return mem is None or is_admin_mem(mem)


def fail_text(r):
    return "⚠️ Couldn't do that: <i>%s</i>\nMake sure I'm an admin with the needed rights." % esc(
        r.get("description", "unknown error")
    )


# ───────────────────────────── target resolution ─────────────────────────────

NO_USER = "I can't find that user. Reply to their message, or give a user ID / an @username I've seen."


def reply_target(m):
    rep = m.get("reply_to_message")
    if rep and rep.get("from") and "forum_topic_created" not in rep:
        return rep
    return None


async def resolve_user(m, args):
    """Return (user_dict | None, remaining_args)."""
    chat_id = m["chat"]["id"]
    rep = reply_target(m)
    if rep:
        await remember(rep["from"])
        return rep["from"], (args or "").strip()
    tok = (args or "").split(None, 1)
    if not tok:
        return None, ""
    first, rest = tok[0], (tok[1] if len(tok) > 1 else "")
    uid = None
    if first.isdigit():
        uid = int(first)
    elif first.startswith("@") and len(first) > 1:
        v = await rcmd("GET", "u:" + first[1:].lower())
        if v:
            uid = int(v)
    else:
        for e in m.get("entities") or []:
            if e.get("type") == "text_mention" and e.get("user"):
                return e["user"], rest.strip()
    if uid is None:
        return None, ""
    mem = await get_member(chat_id, uid)
    if mem and mem.get("user"):
        return mem["user"], rest.strip()
    return {"id": uid, "first_name": str(uid)}, rest.strip()


UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(s):
    mt = re.fullmatch(r"(\d+)([smhdw])", (s or "").lower())
    if not mt:
        return None
    return max(int(mt.group(1)) * UNITS[mt.group(2)], 31)


# ───────────────────────────── command plumbing ─────────────────────────────

COMMANDS = {}


def command(*names):
    def deco(fn):
        for n in names:
            COMMANDS[n] = fn
        return fn
    return deco


async def bot_username():
    if not _ME.get("u"):
        r = await tg("getMe")
        _ME["u"] = (r.get("result") or {}).get("username", "")
    return _ME.get("u", "")


async def parse_cmd(text):
    if not text or text[0] not in "/!":
        return None
    parts = text.split(None, 1)
    head = parts[0][1:]
    args = parts[1] if len(parts) > 1 else ""
    if "@" in head:
        head, _, target = head.partition("@")
        if target.lower() != (await bot_username()).lower():
            return None
    if not re.fullmatch(r"\w+", head):
        return None
    return head.lower(), args


# ───────────────────────────── punishments & warns ─────────────────────────────

PAST = {"ban": "banned", "kick": "kicked", "mute": "muted"}


async def punish(chat_id, user, mode):
    uid = user["id"]
    if mode == "ban":
        r = await tg("banChatMember", chat_id=chat_id, user_id=uid)
    elif mode == "kick":
        r = await tg("banChatMember", chat_id=chat_id, user_id=uid)
        if r.get("ok"):
            await tg("unbanChatMember", chat_id=chat_id, user_id=uid, only_if_banned=True)
    else:
        r = await tg("restrictChatMember", chat_id=chat_id, user_id=uid, permissions=NO_PERMS)
    return r


async def get_warns(chat_id, uid):
    raw = await rcmd("GET", "w:%s:%s" % (chat_id, uid))
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


async def set_warns(chat_id, uid, lst):
    key = "w:%s:%s" % (chat_id, uid)
    if lst:
        await rcmd("SET", key, json.dumps(lst, ensure_ascii=False))
    else:
        await rcmd("DEL", key)


async def do_warn(chat_id, user, reason, s, reply_to=None, thread=None):
    uid = user["id"]
    warns = await get_warns(chat_id, uid)
    warns.append(reason or "No reason given")
    limit, mode = s["warn"]["limit"], s["warn"]["mode"]
    markup = None
    if len(warns) >= limit:
        await set_warns(chat_id, uid, [])
        r = await punish(chat_id, user, mode)
        if r.get("ok"):
            text = "⚠️ %s reached <b>%d/%d</b> warns and has been <b>%s</b>." % (
                mention(user), limit, limit, PAST.get(mode, mode))
        else:
            text = "⚠️ %s reached the warn limit.\n%s" % (mention(user), fail_text(r))
    else:
        await set_warns(chat_id, uid, warns)
        text = "⚠️ %s has been warned (<b>%d/%d</b>)." % (mention(user), len(warns), limit)
        if reason:
            text += "\n<b>Reason:</b> " + esc(reason)
        markup = {"inline_keyboard": [[{"text": "🗑 Remove warn", "callback_data": "unwarn:%s" % uid}]]}
    await send(chat_id, text, reply_to=reply_to, thread=thread, markup=markup)


# ───────────────────────────── moderation commands ─────────────────────────────

MOD_EMOJI = {"ban": "🔨", "kick": "👢", "mute": "🔇"}


async def do_moderate(m, s, args, *, name, action, timed=False, silent=False, dele=False):
    chat_id = m["chat"]["id"]
    if not await need(m, "can_restrict_members"):
        return
    user, rest = await resolve_user(m, args)
    if not user:
        return await reply(m, NO_USER)
    uid = user["id"]
    if uid == BOT_ID:
        return await reply(m, "I'm not going to do that to myself. 😅")
    if uid in SPECIAL_IDS:
        return await reply(m, "I can't do that to this account.")
    mem = await get_member(chat_id, uid)
    if is_admin_mem(mem):
        return await reply(m, "I can't do that to an admin.")

    until, reason, dur = None, rest.strip(), ""
    if timed:
        parts = rest.split(None, 1)
        secs = parse_duration(parts[0]) if parts else None
        if not secs:
            return await reply(m, "Give me a duration, e.g. <code>/%s 30m [reason]</code> (s, m, h, d, w)." % name)
        until = int(time.time()) + secs
        dur = parts[0]
        reason = parts[1].strip() if len(parts) > 1 else ""
    if dele:
        rep = reply_target(m)
        if rep:
            await tg("deleteMessage", chat_id=chat_id, message_id=rep["message_id"])

    if action == "ban":
        r = await tg("banChatMember", chat_id=chat_id, user_id=uid, until_date=until)
    elif action == "kick":
        r = await tg("banChatMember", chat_id=chat_id, user_id=uid)
        if r.get("ok"):
            await tg("unbanChatMember", chat_id=chat_id, user_id=uid, only_if_banned=True)
    else:
        r = await tg("restrictChatMember", chat_id=chat_id, user_id=uid, permissions=NO_PERMS, until_date=until)

    if silent:
        await tg("deleteMessage", chat_id=chat_id, message_id=m["message_id"])
        return
    if not r.get("ok"):
        return await reply(m, fail_text(r))
    txt = "%s %s %s" % (MOD_EMOJI[action], PAST[action].capitalize(), mention(user))
    if dur:
        txt += " for <b>%s</b>" % esc(dur)
    txt += "!"
    if reason:
        txt += "\n<b>Reason:</b> " + esc(reason)
    await reply(m, txt)


def _mk_mod(name, **opts):
    async def handler(m, s, a):
        await do_moderate(m, s, a, name=name, **opts)
    return handler


for _n, _o in {
    "ban": dict(action="ban"),
    "sban": dict(action="ban", silent=True),
    "dban": dict(action="ban", dele=True),
    "tban": dict(action="ban", timed=True),
    "kick": dict(action="kick"),
    "mute": dict(action="mute"),
    "dmute": dict(action="mute", dele=True),
    "tmute": dict(action="mute", timed=True),
}.items():
    COMMANDS[_n] = _mk_mod(_n, **_o)


@command("unban")
async def c_unban(m, s, a):
    if not await need(m, "can_restrict_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    r = await tg("unbanChatMember", chat_id=m["chat"]["id"], user_id=user["id"], only_if_banned=True)
    await reply(m, ("✅ Unbanned %s!" % mention(user)) if r.get("ok") else fail_text(r))


@command("unmute")
async def c_unmute(m, s, a):
    if not await need(m, "can_restrict_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    chat_id = m["chat"]["id"]
    r = await tg("restrictChatMember", chat_id=chat_id, user_id=user["id"], permissions=await restore_perms(chat_id))
    await reply(m, ("🔊 Unmuted %s!" % mention(user)) if r.get("ok") else fail_text(r))


@command("kickme")
async def c_kickme(m, s, a):
    if await sender_is_admin(m):
        return await reply(m, "I can't kick admins. 😅")
    r = await punish(m["chat"]["id"], m["from"], "kick")
    await reply(m, "👋 Bye!" if r.get("ok") else fail_text(r))


# warns ----------------------------------------------------------------------


async def _warn_cmd(m, s, a, dele=False):
    if not await need(m, "can_restrict_members"):
        return
    chat_id = m["chat"]["id"]
    user, rest = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    if user["id"] == BOT_ID or user["id"] in SPECIAL_IDS:
        return await reply(m, "I can't warn this account.")
    if is_admin_mem(await get_member(chat_id, user["id"])):
        return await reply(m, "I can't warn an admin.")
    if dele:
        rep = reply_target(m)
        if rep:
            await tg("deleteMessage", chat_id=chat_id, message_id=rep["message_id"])
    await do_warn(chat_id, user, rest.strip(), s, reply_to=m["message_id"], thread=thread_of(m))


@command("warn")
async def c_warn(m, s, a):
    await _warn_cmd(m, s, a)


@command("dwarn")
async def c_dwarn(m, s, a):
    await _warn_cmd(m, s, a, dele=True)


@command("unwarn", "rmwarn")
async def c_unwarn(m, s, a):
    if not await need(m, "can_restrict_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    chat_id = m["chat"]["id"]
    warns = await get_warns(chat_id, user["id"])
    if not warns:
        return await reply(m, "%s has no warnings." % mention(user))
    warns.pop()
    await set_warns(chat_id, user["id"], warns)
    await reply(m, "✅ Removed one warn from %s (%d/%d)." % (mention(user), len(warns), s["warn"]["limit"]))


@command("resetwarns", "resetwarn")
async def c_resetwarns(m, s, a):
    if not await need(m, "can_restrict_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    await set_warns(m["chat"]["id"], user["id"], [])
    await reply(m, "✅ Warns reset for %s." % mention(user))


@command("warns")
async def c_warns(m, s, a):
    user, _ = await resolve_user(m, a)
    if not user:
        if (a or "").strip():
            return await reply(m, NO_USER)
        user = m["from"]
    warns = await get_warns(m["chat"]["id"], user["id"])
    limit = s["warn"]["limit"]
    if not warns:
        return await reply(m, "%s has no warnings." % mention(user))
    lines = ["%d. %s" % (i + 1, esc(w)) for i, w in enumerate(warns)]
    await reply(m, "%s has <b>%d/%d</b> warns:\n%s" % (mention(user), len(warns), limit, "\n".join(lines)))


@command("setwarnlimit")
async def c_setwarnlimit(m, s, a):
    if not await need(m, "can_change_info"):
        return
    arg = (a or "").strip()
    if not arg.isdigit() or not 1 <= int(arg) <= 20:
        return await reply(m, "Usage: <code>/setwarnlimit 3</code> (1–20). Current: <b>%d</b>" % s["warn"]["limit"])
    s["warn"]["limit"] = int(arg)
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Warn limit set to <b>%s</b>." % arg)


@command("setwarnmode")
async def c_setwarnmode(m, s, a):
    if not await need(m, "can_change_info"):
        return
    arg = (a or "").strip().lower()
    if arg not in PAST:
        return await reply(m, "Usage: <code>/setwarnmode ban|kick|mute</code>. Current: <b>%s</b>" % s["warn"]["mode"])
    s["warn"]["mode"] = arg
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Users reaching the warn limit will be <b>%s</b>." % PAST[arg])


# admin tools ----------------------------------------------------------------


@command("pin")
async def c_pin(m, s, a):
    if not await need(m, "can_pin_messages"):
        return
    rep = reply_target(m)
    if not rep:
        return await reply(m, "Reply to the message you want to pin.")
    loud = (a or "").strip().lower() in ("loud", "notify", "violent")
    r = await tg("pinChatMessage", chat_id=m["chat"]["id"], message_id=rep["message_id"], disable_notification=not loud)
    if not r.get("ok"):
        await reply(m, fail_text(r))


@command("unpin")
async def c_unpin(m, s, a):
    if not await need(m, "can_pin_messages"):
        return
    rep = reply_target(m)
    r = await tg("unpinChatMessage", chat_id=m["chat"]["id"], message_id=rep["message_id"] if rep else None)
    await reply(m, "📌 Unpinned." if r.get("ok") else fail_text(r))


@command("purge")
async def c_purge(m, s, a):
    if not await need(m, "can_delete_messages"):
        return
    rep = reply_target(m)
    if not rep:
        return await reply(m, "Reply to the message you want to purge from.")
    chat_id = m["chat"]["id"]
    start, end = rep["message_id"], m["message_id"]
    if end - start > 1000:
        return await reply(m, "That's too many messages (max 1000 at once).")
    ids = list(range(start, end + 1))
    results = await asyncio.gather(
        *[tg("deleteMessages", chat_id=chat_id, message_ids=ids[i:i + 100]) for i in range(0, len(ids), 100)]
    )
    if results and not any(r.get("ok") for r in results):
        return await send(chat_id, fail_text(results[0]), thread=thread_of(m))
    await send(chat_id, "🧹 Purge complete.", thread=thread_of(m))


@command("del")
async def c_del(m, s, a):
    if not await need(m, "can_delete_messages"):
        return
    rep = reply_target(m)
    if not rep:
        return await reply(m, "Reply to the message you want to delete.")
    chat_id = m["chat"]["id"]
    await tg("deleteMessage", chat_id=chat_id, message_id=rep["message_id"])
    await tg("deleteMessage", chat_id=chat_id, message_id=m["message_id"])


@command("promote")
async def c_promote(m, s, a):
    if not await need(m, "can_promote_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    if user["id"] == BOT_ID or user["id"] in SPECIAL_IDS:
        return await reply(m, "I can't promote this account.")
    r = await tg(
        "promoteChatMember", chat_id=m["chat"]["id"], user_id=user["id"],
        can_delete_messages=True, can_restrict_members=True, can_pin_messages=True,
        can_invite_users=True, can_manage_video_chats=True,
    )
    await reply(m, ("⭐ Promoted %s!" % mention(user)) if r.get("ok") else fail_text(r))


@command("demote")
async def c_demote(m, s, a):
    if not await need(m, "can_promote_members"):
        return
    user, _ = await resolve_user(m, a)
    if not user:
        return await reply(m, NO_USER)
    r = await tg(
        "promoteChatMember", chat_id=m["chat"]["id"], user_id=user["id"],
        is_anonymous=False, can_manage_chat=False, can_delete_messages=False, can_restrict_members=False,
        can_pin_messages=False, can_invite_users=False, can_manage_video_chats=False, can_change_info=False,
        can_promote_members=False,
    )
    await reply(m, ("⬇️ Demoted %s." % mention(user)) if r.get("ok") else fail_text(r))


@command("adminlist", "admins")
async def c_adminlist(m, s, a):
    r = await tg("getChatAdministrators", chat_id=m["chat"]["id"])
    if not r.get("ok"):
        return await reply(m, fail_text(r))
    lines = []
    for adm in r["result"]:
        u = adm["user"]
        if u.get("is_bot"):
            continue
        icon = "👑" if adm.get("status") == "creator" else "•"
        line = "%s %s" % (icon, esc(full_name(u)))
        if u.get("username"):
            line += " (@%s)" % esc(u["username"])
        if adm.get("custom_title"):
            line += " — <i>%s</i>" % esc(adm["custom_title"])
        lines.append(line)
    await reply(m, "<b>Admins in %s</b>\n%s" % (esc(m["chat"].get("title", "")), "\n".join(lines) or "None"))


@command("id")
async def c_id(m, s, a):
    rep = reply_target(m)
    user = rep["from"] if rep else m["from"]
    txt = "<b>User ID:</b> <code>%s</code>\n<b>Chat ID:</b> <code>%s</code>" % (user["id"], m["chat"]["id"])
    await reply(m, txt)


@command("info")
async def c_info(m, s, a):
    user, _ = await resolve_user(m, a)
    if not user:
        if (a or "").strip():
            return await reply(m, NO_USER)
        user = m["from"]
    mem = await get_member(m["chat"]["id"], user["id"])
    lines = ["<b>User info</b>", "ID: <code>%s</code>" % user["id"], "Name: %s" % esc(full_name(user))]
    if user.get("username"):
        lines.append("Username: @%s" % esc(user["username"]))
    lines.append("Status here: <b>%s</b>" % esc(mem["status"] if mem else "unknown"))
    await reply(m, "\n".join(lines))


@command("ping")
async def c_ping(m, s, a):
    await reply(m, "🏓 Pong!")


@command("report")
async def c_report(m, s, a):
    await do_report(m)


async def do_report(m):
    rep = reply_target(m)
    if not rep:
        return await reply(m, "Reply to the message you want to report.")
    if rep["from"]["id"] == m["from"]["id"]:
        return await reply(m, "You can't report yourself.")
    chat_id = m["chat"]["id"]
    r = await tg("getChatAdministrators", chat_id=chat_id)
    pings = ""
    if r.get("ok"):
        pings = "".join(
            '<a href="tg://user?id=%s">\u200b</a>' % a["user"]["id"] for a in r["result"] if not a["user"].get("is_bot")
        )
    await send(
        chat_id,
        "🚨 %s reported %s to the admins.%s" % (mention(m["from"]), mention(rep["from"]), pings),
        reply_to=rep["message_id"], thread=thread_of(m),
    )


# ───────────────────────────── welcome / goodbye / captcha / rules ─────────────────────────────

DEFAULT_WELCOME = {
    "type": "text",
    "text": "Hey {mention}, welcome to <b>{chatname}</b>! 🎉\nPlease read the /rules and enjoy your stay.",
}
DEFAULT_GOODBYE = {"type": "text", "text": "Goodbye {first}! 👋"}
FILL_HELP = "{first} {last} {fullname} {username} {mention} {id} {chatname} {count}"


async def send_welcome(chat, user, s, thread=None):
    content = s["welcome"].get("content") or DEFAULT_WELCOME
    r = await send_content(chat, content, user, thread=thread)
    if s["welcome"].get("clean") and r.get("ok"):
        if s.get("last_welcome"):
            await tg("deleteMessage", chat_id=chat["id"], message_id=s["last_welcome"])
        s["last_welcome"] = r["result"]["message_id"]
        await save_settings(chat["id"], s)


async def on_join(m, s):
    chat = m["chat"]
    adder = m.get("from") or {}
    thread = thread_of(m)
    for u in m["new_chat_members"]:
        if u["id"] == BOT_ID:
            await send(
                chat["id"],
                "👋 Thanks for adding me!\nMake me an <b>admin</b> (delete messages, ban users, pin messages) "
                "so I can work properly, then check /help.",
                thread=thread,
            )
            continue
        await remember(u)
        if u.get("is_bot"):
            if "bot" in s["locks"] and adder.get("id") != u["id"] and not await exempt(m):
                await punish(chat["id"], u, "ban")
            continue
        if s["captcha"] and adder.get("id") == u["id"]:
            r = await tg("restrictChatMember", chat_id=chat["id"], user_id=u["id"], permissions=NO_PERMS)
            if r.get("ok"):
                await send(
                    chat["id"],
                    "👋 Welcome %s!\nTap the button below to prove you're human and unlock the chat." % mention(u),
                    thread=thread,
                    markup={"inline_keyboard": [[{"text": "✅ I'm human", "callback_data": "cap:%s" % u["id"]}]]},
                )
                continue
        if s["welcome"]["on"]:
            await send_welcome(chat, u, s, thread)


async def on_left(m, s):
    u = m["left_chat_member"]
    if u["id"] == BOT_ID or u.get("is_bot") or not s["goodbye"]["on"]:
        return
    content = s["goodbye"].get("content") or DEFAULT_GOODBYE
    await send_content(m["chat"], content, u, thread=thread_of(m))


async def _set_content(m, s, a, key, label):
    if not await need(m, "can_change_info"):
        return
    content = build_content(m, a)
    if not content:
        return await reply(
            m,
            "Send the text after the command, or reply to a message.\n"
            "Fillings: <code>%s</code>\nButtons: <code>[Label](buttonurl://https://example.com)</code>" % FILL_HELP,
        )
    s[key]["content"] = content
    s[key]["on"] = True
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ %s message updated." % label)


async def _toggle(m, s, a, key, label, default):
    arg = (a or "").strip().lower()
    if arg in ("on", "yes", "enable"):
        if not await need(m, "can_change_info"):
            return
        s[key]["on"] = True
        await save_settings(m["chat"]["id"], s)
        return await reply(m, "✅ %s messages are now <b>ON</b>." % label)
    if arg in ("off", "no", "disable"):
        if not await need(m, "can_change_info"):
            return
        s[key]["on"] = False
        await save_settings(m["chat"]["id"], s)
        return await reply(m, "✅ %s messages are now <b>OFF</b>." % label)
    state = "ON" if s[key]["on"] else "OFF"
    await reply(m, "%s messages are <b>%s</b>. Current message:" % (label, state))
    await send_content(m["chat"], s[key].get("content") or default, m["from"], thread=thread_of(m))


@command("setwelcome")
async def c_setwelcome(m, s, a):
    await _set_content(m, s, a, "welcome", "Welcome")


@command("welcome")
async def c_welcome(m, s, a):
    await _toggle(m, s, a, "welcome", "Welcome", DEFAULT_WELCOME)


@command("resetwelcome")
async def c_resetwelcome(m, s, a):
    if not await need(m, "can_change_info"):
        return
    s["welcome"]["content"] = None
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Welcome message reset to default.")


@command("setgoodbye")
async def c_setgoodbye(m, s, a):
    await _set_content(m, s, a, "goodbye", "Goodbye")


@command("goodbye")
async def c_goodbye(m, s, a):
    await _toggle(m, s, a, "goodbye", "Goodbye", DEFAULT_GOODBYE)


@command("resetgoodbye")
async def c_resetgoodbye(m, s, a):
    if not await need(m, "can_change_info"):
        return
    s["goodbye"]["content"] = None
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Goodbye message reset to default.")


@command("cleanwelcome")
async def c_cleanwelcome(m, s, a):
    arg = (a or "").strip().lower()
    if arg not in ("on", "off", "yes", "no"):
        return await reply(m, "Usage: <code>/cleanwelcome on|off</code>. Currently <b>%s</b>." % ("ON" if s["welcome"].get("clean") else "OFF"))
    if not await need(m, "can_change_info"):
        return
    s["welcome"]["clean"] = arg in ("on", "yes")
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Clean welcome is now <b>%s</b>." % ("ON" if s["welcome"]["clean"] else "OFF"))


@command("captcha")
async def c_captcha(m, s, a):
    arg = (a or "").strip().lower()
    if arg not in ("on", "off", "yes", "no"):
        return await reply(
            m,
            "Usage: <code>/captcha on|off</code>. Currently <b>%s</b>.\n"
            "New members are muted until they tap a button. I need the <b>Ban users</b> right." % ("ON" if s["captcha"] else "OFF"),
        )
    if not await need(m, "can_change_info"):
        return
    s["captcha"] = arg in ("on", "yes")
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Captcha is now <b>%s</b>." % ("ON" if s["captcha"] else "OFF"))


@command("setrules")
async def c_setrules(m, s, a):
    if not await need(m, "can_change_info"):
        return
    content = build_content(m, a)
    if not content:
        return await reply(m, "Send the rules after the command, or reply to a message.")
    s["rules"] = content
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Rules saved. Members can read them with /rules.")


@command("rules")
async def c_rules(m, s, a):
    if not s["rules"]:
        return await reply(m, "No rules have been set yet.")
    await send_content(m["chat"], s["rules"], m["from"], reply_to=m["message_id"], thread=thread_of(m))


@command("clearrules")
async def c_clearrules(m, s, a):
    if not await need(m, "can_change_info"):
        return
    s["rules"] = None
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Rules cleared.")


# ───────────────────────────── notes & filters ─────────────────────────────

NAME_RE = re.compile(r"^[\w\-]{1,40}$")
MAX_ITEMS = 150


@command("save", "note")
async def c_save(m, s, a):
    if not await need(m, "can_change_info"):
        return
    parts = (a or "").split(None, 1)
    if not parts:
        return await reply(m, "Usage: <code>/save name text</code> — or reply to a message with <code>/save name</code>.")
    name = parts[0].lower().lstrip("#")
    if not NAME_RE.match(name):
        return await reply(m, "Note names can only contain letters, numbers, - and _.")
    content = build_content(m, parts[1] if len(parts) > 1 else "")
    if not content:
        return await reply(m, "Give me the note text, or reply to the message you want to save.")
    if name not in s["notes"] and len(s["notes"]) >= MAX_ITEMS:
        return await reply(m, "This group has too many notes already.")
    s["notes"][name] = content
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Saved note <code>%s</code>. Get it with /get %s or #%s" % (esc(name), esc(name), esc(name)))


async def send_note(m, s, name):
    c = s["notes"].get(name.lower())
    if not c:
        return False
    rep = reply_target(m)
    await send_content(m["chat"], c, m["from"], reply_to=(rep or m)["message_id"], thread=thread_of(m))
    return True


@command("get")
async def c_get(m, s, a):
    name = (a or "").split(None, 1)[0].lstrip("#").lower() if (a or "").strip() else ""
    if not name:
        return await reply(m, "Usage: <code>/get name</code>")
    if not await send_note(m, s, name):
        await reply(m, "I couldn't find a note called <code>%s</code>." % esc(name))


@command("notes", "saved")
async def c_notes(m, s, a):
    if not s["notes"]:
        return await reply(m, "No notes saved in this group yet.")
    names = "\n".join("• <code>#%s</code>" % esc(n) for n in sorted(s["notes"]))
    await reply(m, "<b>Notes in %s</b>\n%s\n\nGet one with /get name or #name" % (esc(m["chat"].get("title", "")), names))


@command("clear")
async def c_clear(m, s, a):
    if not await need(m, "can_change_info"):
        return
    name = (a or "").strip().lstrip("#").lower()
    if name not in s["notes"]:
        return await reply(m, "I couldn't find that note.")
    del s["notes"][name]
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Note <code>%s</code> deleted." % esc(name))


def split_keyword(args):
    args = (args or "").strip()
    if args.startswith('"'):
        end = args.find('"', 1)
        if end > 1:
            return args[1:end].strip(), args[end + 1:].lstrip()
    parts = args.split(None, 1)
    if not parts:
        return "", ""
    return parts[0], (parts[1] if len(parts) > 1 else "")


@command("filter")
async def c_filter(m, s, a):
    if not await need(m, "can_change_info"):
        return
    kw, rest = split_keyword(a)
    kw = kw.lower()
    if not kw:
        return await reply(m, 'Usage: <code>/filter word reply text</code>\nFor phrases: <code>/filter "good morning" Hello!</code>')
    content = build_content(m, rest)
    if not content:
        return await reply(m, "Give me a reply text, or reply to a message with <code>/filter word</code>.")
    if kw not in s["filters"] and len(s["filters"]) >= MAX_ITEMS:
        return await reply(m, "This group has too many filters already.")
    s["filters"][kw] = content
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Filter saved for <code>%s</code>." % esc(kw))


@command("stop", "unfilter")
async def c_stop(m, s, a):
    if not await need(m, "can_change_info"):
        return
    kw, _ = split_keyword(a)
    kw = kw.lower()
    if kw not in s["filters"]:
        return await reply(m, "I couldn't find that filter.")
    del s["filters"][kw]
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Filter <code>%s</code> removed." % esc(kw))


@command("filters")
async def c_filters(m, s, a):
    if not s["filters"]:
        return await reply(m, "No filters in this group yet.")
    lines = "\n".join("• <code>%s</code>" % esc(k) for k in sorted(s["filters"]))
    await reply(m, "<b>Filters in %s</b>\n%s" % (esc(m["chat"].get("title", "")), lines))


def word_match(text_lower, word):
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text_lower) is not None


# ───────────────────────────── locks / blacklist / flood ─────────────────────────────

LOCK_TYPES = [
    "all", "url", "invitelink", "email", "forward", "sticker", "gif", "photo", "video",
    "voice", "audio", "document", "contact", "location", "poll", "inline", "bot",
]
INVITE_RE = re.compile(r"(t\.me|telegram\.me|telegram\.dog)/(joinchat/|\+)", re.I)


def violated_lock(m, locks):
    if not locks:
        return None
    ents = (m.get("entities") or []) + (m.get("caption_entities") or [])
    et = {e.get("type") for e in ents}
    txt = m.get("text") or m.get("caption") or ""
    checks = {
        "all": True,
        "url": bool(et & {"url", "text_link"}),
        "email": "email" in et,
        "invitelink": bool(INVITE_RE.search(txt)) or any(
            INVITE_RE.search(e.get("url", "")) for e in ents if e.get("type") == "text_link"
        ),
        "forward": "forward_origin" in m or "forward_date" in m,
        "sticker": "sticker" in m,
        "gif": "animation" in m,
        "photo": "photo" in m,
        "video": "video" in m or "video_note" in m,
        "voice": "voice" in m,
        "audio": "audio" in m,
        "document": "document" in m and "animation" not in m,
        "contact": "contact" in m,
        "location": "location" in m or "venue" in m,
        "poll": "poll" in m,
        "inline": "via_bot" in m,
    }
    for lock in locks:
        if checks.get(lock):
            return lock
    return None


@command("lock")
async def c_lock(m, s, a):
    if not await need(m, "can_change_info"):
        return
    types = [t.lower() for t in (a or "").split()]
    if not types:
        return await reply(m, "Usage: <code>/lock url sticker</code>\nTypes: <code>%s</code>" % " ".join(LOCK_TYPES))
    bad = [t for t in types if t not in LOCK_TYPES]
    if bad:
        return await reply(m, "Unknown lock type: <code>%s</code>\nTypes: <code>%s</code>" % (esc(" ".join(bad)), " ".join(LOCK_TYPES)))
    for t in types:
        if t not in s["locks"]:
            s["locks"].append(t)
    await save_settings(m["chat"]["id"], s)
    await reply(m, "🔒 Locked: <code>%s</code>\n(I need the <b>Delete messages</b> right for this to work.)" % " ".join(types))


@command("unlock")
async def c_unlock(m, s, a):
    if not await need(m, "can_change_info"):
        return
    types = [t.lower() for t in (a or "").split()]
    if not types:
        return await reply(m, "Usage: <code>/unlock url sticker</code>")
    s["locks"] = [t for t in s["locks"] if t not in types]
    await save_settings(m["chat"]["id"], s)
    await reply(m, "🔓 Unlocked: <code>%s</code>" % esc(" ".join(types)))


@command("locks")
async def c_locks(m, s, a):
    lines = ["%s <code>%s</code>" % ("🔒" if t in s["locks"] else "🔓", t) for t in LOCK_TYPES]
    await reply(m, "<b>Locks in this group</b>\n" + "\n".join(lines))


@command("addblacklist")
async def c_addblacklist(m, s, a):
    if not await need(m, "can_change_info"):
        return
    words = [w.strip().lower() for w in (a or "").split("\n") if w.strip()]
    if not words:
        return await reply(m, "Usage: <code>/addblacklist word</code> (one word or phrase per line).")
    added = []
    for w in words:
        if w not in s["blacklist"] and len(s["blacklist"]) < 500:
            s["blacklist"].append(w)
            added.append(w)
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Blacklisted: %s" % (", ".join("<code>%s</code>" % esc(w) for w in added) or "nothing new"))


@command("unblacklist", "rmblacklist")
async def c_unblacklist(m, s, a):
    if not await need(m, "can_change_info"):
        return
    w = (a or "").strip().lower()
    if w not in s["blacklist"]:
        return await reply(m, "That word isn't blacklisted.")
    s["blacklist"].remove(w)
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Removed <code>%s</code> from the blacklist." % esc(w))


@command("blacklist")
async def c_blacklist(m, s, a):
    if not s["blacklist"]:
        return await reply(m, "The blacklist is empty.")
    words = "\n".join("• <code>%s</code>" % esc(w) for w in sorted(s["blacklist"]))
    await reply(m, "<b>Blacklisted words</b> (action: <b>%s</b>)\n%s" % (s["bl_mode"], words))


@command("blacklistmode")
async def c_blacklistmode(m, s, a):
    if not await need(m, "can_change_info"):
        return
    arg = (a or "").strip().lower()
    if arg not in ("delete", "warn", "mute", "kick", "ban"):
        return await reply(m, "Usage: <code>/blacklistmode delete|warn|mute|kick|ban</code>. Current: <b>%s</b>" % s["bl_mode"])
    s["bl_mode"] = arg
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Blacklist action set to <b>%s</b>." % arg)


@command("setflood")
async def c_setflood(m, s, a):
    if not await need(m, "can_change_info"):
        return
    arg = (a or "").strip().lower()
    if arg in ("off", "no", "0"):
        s["flood"]["limit"] = 0
        msg = "✅ Antiflood is now <b>OFF</b>."
    elif arg.isdigit() and 3 <= int(arg) <= 100:
        s["flood"]["limit"] = int(arg)
        msg = "✅ Antiflood on: more than <b>%s</b> messages in 10 seconds → <b>%s</b>." % (arg, s["flood"]["mode"])
    else:
        return await reply(m, "Usage: <code>/setflood 6</code> (3–100) or <code>/setflood off</code>")
    await save_settings(m["chat"]["id"], s)
    await reply(m, msg)


@command("setfloodmode")
async def c_setfloodmode(m, s, a):
    if not await need(m, "can_change_info"):
        return
    arg = (a or "").strip().lower()
    if arg not in PAST:
        return await reply(m, "Usage: <code>/setfloodmode ban|kick|mute</code>. Current: <b>%s</b>" % s["flood"]["mode"])
    s["flood"]["mode"] = arg
    await save_settings(m["chat"]["id"], s)
    await reply(m, "✅ Flooders will be <b>%s</b>." % PAST[arg])


@command("flood")
async def c_flood(m, s, a):
    lim = s["flood"]["limit"]
    if not lim:
        return await reply(m, "Antiflood is <b>OFF</b>. Turn it on with /setflood 6")
    await reply(m, "Antiflood: more than <b>%d</b> messages in 10 seconds → <b>%s</b>." % (lim, s["flood"]["mode"]))


SKIP_SENDERS = SPECIAL_IDS


async def enforce(m, s, check_flood=True):
    """Locks, blacklist, antiflood. Returns True if the message was handled/removed."""
    user = m["from"]
    if user["id"] in SKIP_SENDERS or m.get("is_automatic_forward"):
        return False
    chat_id = m["chat"]["id"]

    lock = violated_lock(m, s["locks"])
    if lock and not await exempt(m):
        await tg("deleteMessage", chat_id=chat_id, message_id=m["message_id"])
        return True

    text = m.get("text") or m.get("caption") or ""
    if text and s["blacklist"]:
        low = text.lower()
        hit = next((w for w in s["blacklist"] if word_match(low, w)), None)
        if hit and not await exempt(m):
            await tg("deleteMessage", chat_id=chat_id, message_id=m["message_id"])
            mode = s["bl_mode"]
            if mode == "warn":
                await do_warn(chat_id, user, "Blacklisted word: " + hit, s, thread=thread_of(m))
            elif mode in PAST:
                r = await punish(chat_id, user, mode)
                if r.get("ok"):
                    await send(chat_id, "🚫 %s was %s for using a blacklisted word." % (mention(user), PAST[mode]), thread=thread_of(m))
            return True

    limit = s["flood"]["limit"]
    if limit and check_flood:
        key = "f:%s:%s:%s" % (chat_id, user["id"], int(time.time()) // 10)
        res = await rpipe([["INCR", key], ["EXPIRE", key, 30]])
        if int(res[0] or 0) > limit and not await exempt(m):
            mode = s["flood"]["mode"]
            r = await punish(chat_id, user, mode)
            if r.get("ok"):
                await send(chat_id, "🚫 %s was %s for flooding." % (mention(user), PAST[mode]), thread=thread_of(m))
                return True
    return False


# ───────────────────────────── help / start ─────────────────────────────

HELP_TEXT = """<b>🌹 Group Manager — commands</b>

<b>Moderation (admins)</b>
/ban /sban /dban /tban &lt;time&gt; · /unban
/kick · /kickme · /mute /dmute /tmute &lt;time&gt; · /unmute
/warn /dwarn · /unwarn · /warns · /resetwarns
/setwarnlimit &lt;n&gt; · /setwarnmode ban|kick|mute
/promote · /demote · /adminlist
/pin [loud] · /unpin · /purge · /del

<b>Welcome &amp; rules</b>
/setwelcome · /welcome on|off · /resetwelcome · /cleanwelcome on|off
/setgoodbye · /goodbye on|off · /resetgoodbye
/captcha on|off · /setrules · /rules · /clearrules

<b>Notes &amp; filters</b>
/save name text · /get name · #name · /notes · /clear name
/filter word reply · /stop word · /filters

<b>Protection</b>
/lock · /unlock · /locks
/setflood &lt;n&gt;|off · /setfloodmode ban|kick|mute · /flood
/addblacklist · /unblacklist · /blacklist · /blacklistmode

<b>Other</b>
/id · /info · /report or @admin · /ping

<b>Fillings:</b> <code>{first} {last} {fullname} {username} {mention} {id} {chatname} {count}</code>
<b>Buttons:</b> <code>[Label](buttonurl://https://example.com)</code> (add <code>:same</code> to keep the same row)
<b>Time:</b> 30m, 2h, 1d, 1w

Reply to a user (or give an ID / @username) for user commands."""


@command("help")
async def c_help(m, s, a):
    await reply(m, HELP_TEXT)


@command("start")
async def c_start(m, s, a):
    await reply(m, "✅ I'm alive and ready! Make sure I'm an <b>admin</b>, then use /help to see what I can do.")


async def on_private(m):
    cmd = await parse_cmd(m.get("text") or "")
    uname = await bot_username()
    if cmd and cmd[0] in ("start", "help"):
        rights = "change_info+delete_messages+invite_users+restrict_members+pin_messages+manage_video_chats+promote_members"
        markup = None
        if uname:
            markup = {"inline_keyboard": [[{
                "text": "➕ Add me to your group",
                "url": "https://t.me/%s?startgroup=true&admin=%s" % (uname, rights),
            }]]}
        intro = "👋 <b>Hi! I'm a group management bot.</b>\nAdd me to a group and make me admin.\n\n"
        return await send(m["chat"]["id"], intro + HELP_TEXT, markup=markup)
    await send(m["chat"]["id"], "I manage groups — add me to one and send /help there. 🙂")


# ───────────────────────────── group entry point ─────────────────────────────

SERVICE_KEYS = (
    "new_chat_title", "new_chat_photo", "delete_chat_photo", "pinned_message", "group_chat_created",
    "supergroup_chat_created", "message_auto_delete_timer_changed", "forum_topic_created",
    "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened", "video_chat_started",
    "video_chat_ended", "video_chat_scheduled", "video_chat_participants_invited", "boost_added",
    "general_forum_topic_hidden", "general_forum_topic_unhidden", "migrate_from_chat_id",
)
ADMIN_PING_RE = re.compile(r"(?<!\w)@admins?(?!\w)", re.I)


async def on_group(m):
    chat_id = m["chat"]["id"]
    if m.get("migrate_to_chat_id"):
        raw = await rcmd("GET", "c:%s" % chat_id)
        if raw:
            await rcmd("SET", "c:%s" % m["migrate_to_chat_id"], raw)
        return
    if m.get("new_chat_members") or m.get("left_chat_member"):
        s = await load_settings(chat_id)
        if m.get("new_chat_members"):
            return await on_join(m, s)
        return await on_left(m, s)
    if not m.get("from") or any(k in m for k in SERVICE_KEYS):
        return

    s = await load_settings(chat_id)
    if await enforce(m, s):
        return

    text = m.get("text") or ""
    cmd = await parse_cmd(text)
    if cmd:
        fn = COMMANDS.get(cmd[0])
        if fn:
            if m["from"]["id"] not in SPECIAL_IDS:
                await remember(m["from"])
            await fn(m, s, cmd[1])
        return

    body = text or m.get("caption") or ""
    if not body:
        return
    if reply_target(m) and ADMIN_PING_RE.search(body):
        return await do_report(m)
    if text.startswith("#"):
        mt = re.match(r"#([\w\-]{1,40})", text)
        if mt and await send_note(m, s, mt.group(1)):
            return
    if s["filters"]:
        low = body.lower()
        for kw, content in s["filters"].items():
            if word_match(low, kw):
                rep = reply_target(m)
                await send_content(m["chat"], content, m["from"], reply_to=(rep or m)["message_id"], thread=thread_of(m))
                break


# ───────────────────────────── callbacks ─────────────────────────────


async def answer(cq, text=None, alert=False):
    await tg("answerCallbackQuery", callback_query_id=cq["id"], text=text, show_alert=alert or None)


async def on_callback(cq):
    data = cq.get("data") or ""
    user = cq["from"]
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return await answer(cq)

    if data.startswith("cap:"):
        try:
            target = int(data[4:])
        except ValueError:
            return await answer(cq)
        if user["id"] != target:
            return await answer(cq, "This button isn't for you.", True)
        r = await tg("restrictChatMember", chat_id=chat_id, user_id=target, permissions=await restore_perms(chat_id))
        if not r.get("ok"):
            return await answer(cq, "Something went wrong — please ask an admin to unmute you.", True)
        await answer(cq, "✅ Verified — welcome!")
        await tg("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        s = await load_settings(chat_id)
        if s["welcome"]["on"]:
            await send_welcome(chat, user, s, msg.get("message_thread_id") if msg.get("is_topic_message") else None)
        return

    if data.startswith("unwarn:"):
        try:
            target = int(data[7:])
        except ValueError:
            return await answer(cq)
        mem = await get_member(chat_id, user["id"])
        if not has_right_mem(mem, "can_restrict_members"):
            return await answer(cq, "Only admins can remove warns.", True)
        warns = await get_warns(chat_id, target)
        if warns:
            warns.pop()
            await set_warns(chat_id, target, warns)
        await answer(cq, "Warn removed.")
        await tg(
            "editMessageText", chat_id=chat_id, message_id=msg["message_id"], parse_mode="HTML",
            text="✅ Warn removed by %s." % mention(user),
        )
        return

    await answer(cq)


# ───────────────────────────── update router ─────────────────────────────


async def on_edited(m):
    """Edited messages: re-check locks and blacklist only (no commands, no flood)."""
    if m.get("chat", {}).get("type") not in ("group", "supergroup") or not m.get("from"):
        return
    if any(k in m for k in SERVICE_KEYS):
        return
    s = await load_settings(m["chat"]["id"])
    if s["locks"] or s["blacklist"]:
        await enforce(m, s, check_flood=False)


async def handle_update(u):
    if "callback_query" in u:
        return await on_callback(u["callback_query"])
    if "edited_message" in u:
        return await on_edited(u["edited_message"])
    m = u.get("message")
    if not m or "chat" not in m:
        return
    ctype = m["chat"].get("type")
    if ctype == "private":
        return await on_private(m)
    if ctype in ("group", "supergroup"):
        return await on_group(m)


# ───────────────────────────── web routes ─────────────────────────────


async def webhook(request: Request):
    if not WEBHOOK_SECRET or request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        t1 = _client.set(client)
        t2 = _admin_cache.set({})
        try:
            await asyncio.wait_for(handle_update(update), timeout=25)
        except Exception:
            log.exception("update failed")
        finally:
            _client.reset(t1)
            _admin_cache.reset(t2)
    return {"ok": True}  # always 200 so Telegram never retries in a loop


async def set_webhook(request: Request, key: str = ""):
    if not WEBHOOK_SECRET or key != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden — pass ?key=<WEBHOOK_SECRET>"}, status_code=403)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    url = "https://%s/api/webhook" % host
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(API + "/setWebhook", json={
            "url": url,
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ALLOWED_UPDATES,
            "drop_pending_updates": True,
            "max_connections": 40,
        })
        info = await c.post(API + "/setMyCommands", json={"commands": [
            {"command": "help", "description": "Show all commands"},
            {"command": "rules", "description": "Show the group rules"},
            {"command": "notes", "description": "List saved notes"},
            {"command": "adminlist", "description": "List group admins"},
            {"command": "id", "description": "Show your ID"},
            {"command": "report", "description": "Report a message to admins"},
        ]})
    return {"webhook_url": url, "telegram": r.json(), "commands": info.json()}


async def health(request: Request):
    out = {
        "bot_token_set": bool(BOT_TOKEN),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "redis_url_set": bool(REDIS_URL),
        "redis_token_set": bool(REDIS_TOKEN),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        tok = _client.set(client)
        try:
            out["redis_ping"] = await rcmd("PING") if REDIS_URL and REDIS_TOKEN else "not configured"
        except Exception as e:
            out["redis_ping"] = "error: %s" % e
        try:
            me = await tg("getMe")
            out["telegram"] = me.get("result", {}).get("username") or me.get("description")
            wh = await tg("getWebhookInfo")
            out["webhook"] = wh.get("result", {}).get("url")
            out["last_error"] = wh.get("result", {}).get("last_error_message")
        finally:
            _client.reset(tok)
    return out


async def index():
    return {"status": "ok", "bot": "group-manager", "hint": "open /api/health to check setup"}

app.add_api_route("/webhook", webhook, methods=["POST"])
app.add_api_route("/setwebhook", set_webhook, methods=["GET"])
app.add_api_route("/health", health, methods=["GET"])
app.add_api_route("/", index, methods=["GET"])