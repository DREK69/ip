from pyrogram import enums
import json, random, zipfile, os, asyncio, shutil
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON, Channel, Chat, KeyboardButtonCopy, ReplyInlineMarkup, KeyboardButtonRow
from telethon.errors import (FloodWaitError, UserAlreadyParticipantError, PeerFloodError,
    AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError, UserDeactivatedError)
from telethon.tl.functions.messages import ImportChatInviteRequest, GetFullChatRequest
from telethon.sessions import StringSession, SQLiteSession
from pyrogram import Client, filters
from pyrogram.types import (InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent)
from pyrogram.handlers import MessageHandler, CallbackQueryHandler, InlineQueryHandler

try:
    import uvloop; asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except: pass

BOT_TOKEN  = "8877183231:AAF6uNSJnnbaEOCsc-HxFX8FueQQ8-_13EU"
API_ID     = 25723056
API_HASH   = "cbda56fac135e92b755e1243aefe9697"
OWNER_ID   = 8406994939
USERS_FILE = "approved_users.json"

sessions, entity_cache, approved_users = {}, {}, {}
pyro: Client = None

def load_users():
    global approved_users
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            approved_users = {int(k): v for k, v in json.load(f).items()}

def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump({str(k): v for k, v in approved_users.items()}, f)

def is_approved(uid): return uid == OWNER_ID or uid in approved_users
def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def is_dead(ex):
    return isinstance(ex, (AuthKeyUnregisteredError, SessionRevokedError,
                           UserDeactivatedBanError, UserDeactivatedError)) or \
           any(k in str(ex).lower() for k in ["method that is not", "auth_key_unregistered",
                                               "session_revoked", "user_deactivated"])

def copy_btn(text, copy_text):
    return ReplyInlineMarkup([[KeyboardButtonRow([KeyboardButtonCopy(text=text, copy_text=copy_text)])]])

def ikb(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])

def ikb_url(*rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, url=u) for t, u in row] for row in rows])

async def kill_session(sid, c):
    try:
        sf = getattr(c.session, 'filename', None)
        if sf:
            if not sf.endswith('.session'): sf += '.session'
            os.makedirs("failed_sessions", exist_ok=True)
            for f in [sf, sf + "-journal"]:
                if os.path.exists(f): shutil.move(f, os.path.join("failed_sessions", os.path.basename(f)))
    except: pass
    try: await c.disconnect()
    except: pass
    sessions.pop(sid, None)
    for k in [k for k in entity_cache if k[0] == sid]: del entity_cache[k]
    log(f"🗑️ S{sid} removed")

async def send_chunks(chat_id, lines):
    ok  = sum(1 for l in lines if l.startswith("✅"))
    fl  = sum(1 for l in lines if l.startswith("⏳"))
    dd  = sum(1 for l in lines if l.startswith("🗑️"))
    summary = f"\n\n📊 Total:{len(lines)} ✅{ok} ⏳{fl} 🗑️{dd} ❌{len(lines)-ok-fl-dd}"
    chunk, cur, chunks = [], 0, []
    for line in lines:
        if cur + len(line) + 1 > 3800:
            chunks.append(chunk); chunk, cur = [line], len(line)
        else:
            chunk.append(line); cur += len(line) + 1
    if chunk: chunks.append(chunk)
    for i, ch in enumerate(chunks):
        pre = f"**Part {i+1}/{len(chunks)}**\n" if len(chunks) > 1 else ""
        suf = summary if i == len(chunks) - 1 else ""
        await pyro.send_message(chat_id, pre + "\n".join(ch) + suf)

async def connect(c):
    if not c.is_connected():
        await asyncio.wait_for(c.connect(), timeout=15)

async def resolve(c, i, sid=None):
    key = (sid, i)
    if key in entity_cache: return entity_cache[key]
    i = i.strip(); entity = None
    if 't.me/' in i or i.startswith('+'):
        hp = (i.split('t.me/')[-1] if 't.me/' in i else i).split('?')[0].strip()
        if hp.startswith('+'):
            try: entity = (await c(ImportChatInviteRequest(hp[1:]))).chats[0]
            except UserAlreadyParticipantError as ex:
                if hasattr(ex, 'updates') and ex.updates and hasattr(ex.updates, 'chats') and ex.updates.chats:
                    entity = ex.updates.chats[0]
                else: raise ValueError("Already member. Provide chat ID.")
            except Exception as ex: raise ValueError(f"Join failed: {str(ex)[:60]}")
        else: i = ('' if hp.startswith('@') else '@') + hp
    if not entity:
        ch = i.lstrip('@')
        if ch.lstrip('-').isdigit():
            cid = int(ch)
            try: entity = await c.get_entity(cid)
            except:
                for d in await c.get_dialogs():
                    if d.entity.id == abs(cid): entity = d.entity; break
                if not entity: raise ValueError(f"Not found: {cid}")
        else:
            try: entity = await c.get_entity(i if i.startswith('@') else int(i))
            except: entity = await c.get_entity(('@' if not i.startswith('@') else '') + i)
    entity_cache[key] = entity
    return entity

async def join_task(sid, c, ci):
    try:
        await connect(c); ent = await resolve(c, ci, sid)
        await c(JoinChannelRequest(ent))
        gid = ent.id if hasattr(ent, 'id') else '?'
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'): gid = f"-100{abs(gid)}"
        return f"✅ S{sid} | {getattr(ent,'title','?')} | `{gid}`"
    except UserAlreadyParticipantError: return f"⚠️ S{sid}: Already member"
    except FloodWaitError as fw: return f"⏳ S{sid}: Flood {fw.seconds}s"
    except Exception as ex:
        if is_dead(ex): await kill_session(sid, c); return f"🗑️ S{sid}: Dead (removed)"
        return f"❌ S{sid}: {str(ex)[:60]}"

async def leave_task(sid, c, ci):
    try:
        await connect(c); ent = await resolve(c, ci, sid)
        gid = ent.id if hasattr(ent, 'id') else '?'
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'): gid = f"-100{abs(gid)}"
        asyncio.create_task(c(LeaveChannelRequest(ent)))
        return f"✅ S{sid} left | {getattr(ent,'title','?')} | `{gid}`"
    except FloodWaitError as fw: return f"⏳ S{sid}: Flood {fw.seconds}s"
    except Exception as ex:
        if is_dead(ex): await kill_session(sid, c); return f"🗑️ S{sid}: Dead (removed)"
        return f"❌ S{sid}: {str(ex)[:60]}"

async def getip_task(sid, c, ci):
    await connect(c); ent = await resolve(c, ci, sid)
    if isinstance(ent, Channel):  fc = await c(GetFullChannelRequest(channel=ent))
    elif isinstance(ent, Chat):   fc = await c(GetFullChatRequest(chat_id=ent.id))
    else: raise ValueError("Unsupported chat type")
    if not fc.full_chat.call: raise ValueError("No active voice chat")
    res = await c(JoinGroupCallRequest(
        call=fc.full_chat.call, join_as=await c.get_me(), muted=True, video_stopped=True,
        params=DataJSON(data=json.dumps({"ssrc": random.getrandbits(32)}))
    ))
    asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
    data  = json.loads(res.updates[-1].params.data)
    cands = data.get("transport", {}).get("candidates", [])
    if len(cands) < 2: raise ValueError(f"Only {len(cands)} candidates")
    ip, port = cands[1].get("ip"), cands[1].get("port")
    if not ip or not port: raise ValueError("IP/Port missing")
    return ip, port, getattr(ent, 'title', '?')

async def load_client(path, sid):
    try:
        c = TelegramClient(path, API_ID, API_HASH, connection_retries=2, timeout=15)
        await asyncio.wait_for(c.connect(), timeout=20)
        if await c.is_user_authorized():
            sessions[sid] = c; log(f"✅ S{sid}"); return True
        await c.disconnect()
    except Exception as ex:
        log(f"❌ {path}: {ex}")
        src = (path if path.endswith('.session') else path + '.session')
        try:
            if os.path.exists(src):
                os.makedirs("failed_sessions", exist_ok=True)
                shutil.move(src, os.path.join("failed_sessions", os.path.basename(src)))
        except: pass
    return False

async def load_existing_sessions():
    os.makedirs("sessions", exist_ok=True)
    files = sorted(f for f in os.listdir("sessions") if f.endswith(".session"))
    if not files: return 0
    log(f"📂 Loading {len(files)} sessions…")
    sem = asyncio.Semaphore(10)
    async def lim(sf, i):
        async with sem:
            await asyncio.sleep(i * 0.05)
            return await load_client(f"sessions/{sf[:-8]}", i + 1)
    loaded = sum(await asyncio.gather(*[lim(sf, i) for i, sf in enumerate(files)]))
    log(f"✅ {loaded}/{len(files)} loaded"); return loaded

def get_cmd(t):
    if not t: return None, ""
    t = t.strip()
    for p in ['/', '.', '!', ';', '&']:
        if t.startswith(p):
            parts = t[1:].strip().split(maxsplit=1)
            return (parts[0].lower(), parts[1] if len(parts) > 1 else "") if parts else (None, "")
    return None, t

HOME_TEXT = "нєу {name}!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ᴛᴏ sᴇᴇ ᴄᴏᴍᴍᴀɴᴅs."
HELP_TEXT = (
    "📚 **Commands**\n\n"
    "**🗂 Sessions:**\n"
    "• /addsession <string> — add via string\n"
    "• /delsession <sid> — delete session\n"
    "• /clearsessions — clear all\n"
    "• /exportsessions — export ZIP\n"
    "• Send .zip — bulk load\n\n"
    "**⚡️ Actions:**\n"
    "• /join <sid|all> <chat>\n"
    "• /leave <sid|all> <chat>\n"
    "• /getip <sid> <chat>\n"
    "• @bot <sid> <chat> — inline IP + copy\n\n"
    "**👥 Users (owner only):**\n"
    "• /approve <id|reply>\n"
    "• /remove <id>\n"
    "• /approved"
)

def home_buttons(uid, username):
    rows = [[InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{username}?startgroup=true")],
            [InlineKeyboardButton("📚 Help", callback_data="help"),
             InlineKeyboardButton("👤 Owner", url="https://t.me/h3rzah")]]
    if uid == OWNER_ID: rows.append([InlineKeyboardButton("🔐 Owner Panel", callback_data="owner_panel")])
    return InlineKeyboardMarkup(rows)

async def setup_pyrogram(app: Client):

    @app.on_message(filters.command("start"))
    async def cmd_start(_, m):
        me = await app.get_me()
        await m.reply(HOME_TEXT.format(name=m.from_user.first_name),
                      reply_markup=home_buttons(m.from_user.id, me.username))

    @app.on_message(filters.command("help"))
    async def cmd_help(_, m):
        me = await app.get_me()
        await m.reply(HELP_TEXT, reply_markup=ikb_url(
            [(f"➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
            [("👤 Owner", "https://t.me/h3rzah")]
        ))

    @app.on_message(filters.command("approve") & filters.user(OWNER_ID))
    async def cmd_approve(_, m):
        try:
            tid = None
            if len(m.command) > 1:
                arg = m.command[1].strip().lstrip("@")
                if arg.lstrip("-").isdigit():
                    tid = int(arg)
                else:
                    u = await app.get_users(arg)
                    tid = u.id
            elif m.reply_to_message:
                tid = m.reply_to_message.from_user.id
            if not tid: return await m.reply("❌ /approve <id|@username> or reply")
            if tid == OWNER_ID: return await m.reply("❌ Owner always approved")
            if tid in approved_users: return await m.reply(f"✅ Already: `{tid}`")
            approved_users[tid] = {"name": "Approved"}; save_users()
            await m.reply(f"✅ Approved: `{tid}`")
        except Exception as ex: await m.reply(f"❌ {ex}")

    @app.on_message(filters.command("remove") & filters.user(OWNER_ID))
    async def cmd_remove(_, m):
        try:
            tid = None
            if len(m.command) > 1:
                arg = m.command[1].strip().lstrip("@")
                if arg.lstrip("-").isdigit():
                    tid = int(arg)
                else:
                    u = await app.get_users(arg)
                    tid = u.id
            elif m.reply_to_message:
                tid = m.reply_to_message.from_user.id
            if not tid: return await m.reply("❌ /remove <id|@username> or reply")
            if tid == OWNER_ID: return await m.reply("❌ Cannot remove owner")
            if tid not in approved_users: return await m.reply(f"❌ Not found: `{tid}`")
            del approved_users[tid]; save_users()
            await m.reply(f"✅ Removed: `{tid}`")
        except Exception as ex: await m.reply(f"❌ {ex}")

    @app.on_message(filters.command("approved") & filters.user(OWNER_ID))
    async def cmd_approved(_, m):
        if not approved_users: return await m.reply("❌ No approved users")
        await m.reply("✅ **Approved:**\n" + "\n".join(f"• `{k}`" for k in approved_users))

    @app.on_message(filters.command("addsession") & filters.create(lambda _, __, m: is_approved(m.from_user.id)))
    async def cmd_addsession(_, m):
        if len(m.command) < 2: return await m.reply("❌ /addsession <string_session>")
        msg = await m.reply("⏳ Connecting…")
        try:
            string = m.command[1].strip()
            ss = StringSession(string)
            # temp connect to get account ID
            tmp = TelegramClient(StringSession(string), API_ID, API_HASH, connection_retries=2, timeout=15)
            await asyncio.wait_for(tmp.connect(), timeout=20)
            if not await tmp.is_user_authorized():
                await tmp.disconnect()
                return await msg.edit("❌ Session not authorized")
            me = await tmp.get_me()
            await tmp.disconnect()
            # save using account ID as filename — persistent across restarts
            os.makedirs("sessions", exist_ok=True)
            fname = f"sessions/{me.id}"
            if any(getattr(c.session, 'filename', '').endswith(str(me.id)) for c in sessions.values()):
                return await msg.edit(f"⚠️ Already loaded: [{me.first_name}](tg://user?id={me.id}) `{me.id}`")
            sq = SQLiteSession(fname)
            sq.set_dc(ss.dc_id, ss.server_address, ss.port)
            sq.auth_key = ss.auth_key; sq.save()
            c = TelegramClient(fname, API_ID, API_HASH, connection_retries=2, timeout=15)
            await asyncio.wait_for(c.connect(), timeout=20)
            if not await c.is_user_authorized():
                await c.disconnect()
                return await msg.edit("❌ Could not verify after save")
            sid = max(sessions.keys()) + 1 if sessions else 1
            sessions[sid] = c
            await msg.edit(f"✅ S{sid} added — permanently saved\n👤 [{me.first_name}](tg://user?id={me.id}) `{me.id}`")
            log(f"✅ S{sid} added via string: {me.id}")
        except Exception as ex: await msg.edit(f"❌ {str(ex)[:100]}")

    @app.on_message(filters.command("delsession") & filters.create(lambda _, __, m: is_approved(m.from_user.id)))
    async def cmd_delsession(_, m):
        try: sid = int(m.command[1])
        except: return await m.reply("❌ /delsession <sid>")
        if sid not in sessions: return await m.reply(f"❌ S{sid} not found")
        await kill_session(sid, sessions[sid])
        await m.reply(f"✅ S{sid} deleted")

    @app.on_message(filters.command("join") & filters.create(lambda _, __, m: is_approved(m.from_user.id)))
    async def cmd_join(_, m):
        if len(m.command) < 3: return await m.reply("❌ /join <sid|all> <chat>")
        sid_arg, chat = m.command[1], " ".join(m.command[2:])
        if sid_arg.lower() == 'all':
            if not sessions: return await m.reply("❌ No sessions")
            msg = await m.reply(f"⏳ Joining {len(sessions)} sessions…")
            sem = asyncio.Semaphore(15)
            async def dj(s, c):
                async with sem: return await join_task(s, c, chat)
            res = await asyncio.gather(*[dj(s, c) for s, c in dict(sessions).items()])
            await msg.delete(); await send_chunks(m.chat.id, res)
        else:
            try: sid = int(sid_arg)
            except: return await m.reply("❌ Invalid ID")
            if sid not in sessions: return await m.reply(f"❌ S{sid} not found")
            msg = await m.reply("⏳ Joining…")
            await msg.edit(await join_task(sid, sessions[sid], chat))

    @app.on_message(filters.command("leave") & filters.create(lambda _, __, m: is_approved(m.from_user.id)))
    async def cmd_leave(_, m):
        if len(m.command) < 3: return await m.reply("❌ /leave <sid|all> <chat>")
        sid_arg, chat = m.command[1], " ".join(m.command[2:])
        if sid_arg.lower() == 'all':
            if not sessions: return await m.reply("❌ No sessions")
            msg = await m.reply(f"⏳ Leaving {len(sessions)} sessions…")
            sem = asyncio.Semaphore(15)
            async def dl(s, c):
                async with sem: return await leave_task(s, c, chat)
            res = await asyncio.gather(*[dl(s, c) for s, c in dict(sessions).items()])
            await msg.delete(); await send_chunks(m.chat.id, res)
        else:
            try: sid = int(sid_arg)
            except: return await m.reply("❌ Invalid ID")
            if sid not in sessions: return await m.reply(f"❌ S{sid} not found")
            msg = await m.reply("⏳ Leaving…")
            await msg.edit(await leave_task(sid, sessions[sid], chat))

    @app.on_message(filters.command("getip") & filters.create(lambda _, __, m: is_approved(m.from_user.id)))
    async def cmd_getip(_, m):
        if len(m.command) < 3: return await m.reply("❌  /getip <sid> <chat>")
        try: sid = int(m.command[1])
        except: return await m.reply("❌ Invalid session ID")
        if sid not in sessions: return await m.reply(f"❌ S{sid} not found")
        msg = await m.reply("🔍 Extracting IP…")
        try:
            ip, port, title = await getip_task(sid, sessions[sid], " ".join(m.command[2:]))
            cmd = f"/attack {ip} {port} 30"
            await msg.edit(
                f"🛜 **IP Extracted**\n\n**Session:** S{sid}\n**Chat:** {title}\n**IP:** `{ip}`\n**Port:** `{port}`",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Copy Command", copy_text=cmd)
                ]])
            )
        except Exception as ex: await msg.edit(f"❌ S{sid}: {str(ex)[:80]}")

    @app.on_message(filters.command("clearsessions") & filters.user(OWNER_ID))
    async def cmd_clearsessions(_, m):
        if not sessions: return await m.reply("❌ No sessions")
        await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
        sessions.clear(); entity_cache.clear()
        shutil.rmtree("sessions", ignore_errors=True); os.makedirs("sessions", exist_ok=True)
        await m.reply("✅ All sessions cleared")

    @app.on_message(filters.command("exportsessions") & filters.user(OWNER_ID))
    async def cmd_exportsessions(_, m):
        if not sessions: return await m.reply("❌ No sessions")
        msg = await m.reply("⏳ Exporting…")
        try:
            zp = "exported_sessions.zip"
            sids = sorted(sessions.keys())
            with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as zf:
                for sid in sids:
                    sf = getattr(sessions[sid].session, 'filename', None)
                    if not sf: continue
                    if not sf.endswith('.session'): sf += '.session'
                    if os.path.exists(sf): zf.write(sf, os.path.basename(sf))
            with zipfile.ZipFile(zp) as zf: total = len(zf.namelist())
            await app.send_document(m.chat.id, zp,
                caption=f"📦 **Exported**\n✅ {total} sessions\n🔢 S{sids[0]}→S{sids[-1]}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                reply_to_message_id=m.id)
            os.remove(zp); await msg.delete()
        except Exception as ex: await m.reply(f"❌ {ex}")

    @app.on_message(filters.document & filters.create(lambda _, __, m: is_approved(m.from_user.id) and m.document.file_name.endswith('.zip')))
    async def handle_zip(_, m):
        msg = await m.reply("⏳ Loading sessions…"); zp = None
        try:
            os.makedirs("sessions", exist_ok=True); os.makedirs("temp_sessions", exist_ok=True)
            zp = await m.download("temp_sessions/up.zip")
            if not zipfile.is_zipfile(zp): return await msg.edit("❌ Invalid ZIP")
            sfs = []
            with zipfile.ZipFile(zp) as z:
                for info in z.infolist():
                    name = os.path.basename(info.filename)
                    if not name.endswith(".session"): continue
                    try:
                        with open(os.path.join("sessions", name), 'wb') as f: f.write(z.read(info.filename))
                        sfs.append(name)
                    except Exception as ex: log(f"❌ Extract {name}: {ex}")
            if not sfs: return await msg.edit("❌ No .session files in ZIP")
            nid = max(sessions.keys()) + 1 if sessions else 1
            loaded = failed = 0; lock = asyncio.Lock(); sem = asyncio.Semaphore(10)
            async def try_load(sf):
                nonlocal nid, loaded, failed
                async with sem:
                    try:
                        c = TelegramClient(f"sessions/{sf[:-8]}", API_ID, API_HASH, connection_retries=2, timeout=15)
                        await asyncio.wait_for(c.connect(), timeout=20)
                        if await c.is_user_authorized():
                            async with lock:
                                sessions[nid] = c; log(f"✅ S{nid}: {sf}"); nid += 1; loaded += 1
                        else:
                            await c.disconnect()
                            async with lock: failed += 1
                    except Exception as ex:
                        src = os.path.join("sessions", sf)
                        try:
                            if os.path.exists(src):
                                os.makedirs("failed_sessions", exist_ok=True)
                                shutil.move(src, os.path.join("failed_sessions", sf))
                        except: pass
                        async with lock: failed += 1; log(f"❌ {sf}: {ex}")
            await asyncio.gather(*[try_load(sf) for sf in sfs])
            await msg.edit(f"✅ Loaded: {loaded}\n❌ Failed: {failed}\n📊 Total: {len(sessions)}")
        except Exception as ex: await msg.edit(f"❌ {str(ex)[:200]}")
        finally:
            try:
                if zp and os.path.exists(zp): os.remove(zp)
                shutil.rmtree("temp_sessions", ignore_errors=True)
            except: pass

    @app.on_inline_query(filters.user(OWNER_ID))
    async def handle_inline(_, q):
        parts = q.query.strip().split(maxsplit=1)
        if len(parts) < 2 or not sessions:
            return await q.answer([InlineQueryResultArticle(
                title="❌ Usage: <sid> <chat>",
                input_message_content=InputTextMessageContent("❌ No sessions or invalid query")
            )], cache_time=0)
        try: sid = int(parts[0])
        except:
            return await q.answer([InlineQueryResultArticle(
                title="❌ Invalid session ID",
                input_message_content=InputTextMessageContent("❌ sid must be a number")
            )], cache_time=0)
        if sid not in sessions:
            return await q.answer([InlineQueryResultArticle(
                title=f"❌ S{sid} not found",
                input_message_content=InputTextMessageContent(f"Available: {list(sessions.keys())}")
            )], cache_time=0)
        try:
            ip, port, title = await getip_task(sid, sessions[sid], parts[1].strip())
            cmd = f"/attack {ip} {port} 30"
            await q.answer([InlineQueryResultArticle(
                title=f"✅ {title} — {ip}:{port}",
                description=f"/attack {ip} {port} 30",
                input_message_content=InputTextMessageContent(
                    f"🛜 **IP Extracted**\n\n**Session:** S{sid}\n**Chat:** {title}\n**IP:** `{ip}`\n**Port:** `{port}`"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Copy Command", copy_text=cmd)
                ]])
            )], cache_time=0)
        except Exception as ex:
            await q.answer([InlineQueryResultArticle(
                title=f"❌ {str(ex)[:40]}",
                input_message_content=InputTextMessageContent(f"❌ S{sid}: {str(ex)[:100]}")
            )], cache_time=0)

    @app.on_callback_query(filters.regex("^home$"))
    async def cb_home(_, q):
        me = await app.get_me()
        await q.message.edit_text(HOME_TEXT.format(name=q.from_user.first_name),
                                  reply_markup=home_buttons(q.from_user.id, me.username))

    @app.on_callback_query(filters.regex("^help$"))
    async def cb_help(_, q):
        me = await app.get_me()
        await q.message.edit_text(HELP_TEXT, reply_markup=ikb_url(
            [(f"➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
            [("👤 Owner", "https://t.me/h3rzah")]
        ))
        await q.answer()

    @app.on_callback_query(filters.regex("^owner_panel$") & filters.user(OWNER_ID))
    async def cb_owner_panel(_, q):
        await q.message.edit_text(
            f"🔐 **Owner Panel**\n\n✅ Sessions: {len(sessions)}\n👥 Approved: {len(approved_users)}",
            reply_markup=ikb(
                [("👥 Approved Users", "owner_approved")],
                [("📦 Session Info",   "owner_sessions")],
                [("🗑️ Clear Sessions", "owner_clear")],
                [("🏠 Back",           "home")]
            )
        ); await q.answer()

    @app.on_callback_query(filters.regex("^owner_approved$") & filters.user(OWNER_ID))
    async def cb_owner_approved(_, q):
        if not approved_users: return await q.answer("❌ No approved users", show_alert=True)
        lines = "\n".join(f"• `{k}`" for k in approved_users)
        await q.message.edit_text(f"✅ **Approved ({len(approved_users)}):**\n\n{lines}",
                                  reply_markup=ikb([("🔙 Back", "owner_panel")]))
        await q.answer()

    @app.on_callback_query(filters.regex("^owner_sessions$") & filters.user(OWNER_ID))
    async def cb_owner_sessions(_, q):
        sids = sorted(sessions.keys())
        body = f"✅ Active: {len(sessions)}"
        if sids: body += f"\n🔢 S{sids[0]} → S{sids[-1]}"
        await q.message.edit_text(f"📦 **Sessions**\n\n{body}",
                                  reply_markup=ikb([("🔙 Back", "owner_panel")]))
        await q.answer()

    @app.on_callback_query(filters.regex("^owner_clear$") & filters.user(OWNER_ID))
    async def cb_owner_clear(_, q):
        await q.message.edit_text("⚠️ Clear ALL sessions?", reply_markup=ikb(
            [("✅ Yes", "confirm_clear")], [("❌ Cancel", "owner_panel")]
        )); await q.answer()

    @app.on_callback_query(filters.regex("^confirm_clear$") & filters.user(OWNER_ID))
    async def cb_confirm_clear(_, q):
        await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
        sessions.clear(); entity_cache.clear()
        shutil.rmtree("sessions", ignore_errors=True); os.makedirs("sessions", exist_ok=True)
        await q.message.edit_text("✅ Cleared", reply_markup=ikb([("🏠 Back", "owner_panel")]))
        await q.answer()

async def main():
    global pyro
    log("🚀 Starting…"); load_users()
    log(f"✅ {len(approved_users)} approved users")
    await load_existing_sessions()
    pyro = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, parse_mode=enums.ParseMode.MARKDOWN)
    await setup_pyrogram(pyro)
    await pyro.start()
    log(f"✅ Bot: @{(await pyro.get_me()).username}")
    log("🎉 Running!")
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
