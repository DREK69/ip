import json, random, zipfile, os, asyncio, shutil, hashlib
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON, Channel, Chat
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest, GetFullChatRequest

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except:
    pass

BOT_TOKEN = "8877183231:AAF6uNSJnnbaEOCsc-HxFX8FueQQ8-_13EU"
API_ID, API_HASH, OWNER_ID = 25723056, "cbda56fac135e92b755e1243aefe9697", 8842115436
USERS_FILE = "approved_users.json"

sessions = {}
entity_cache = {}
approved_users = {}
bot = None

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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

async def send_chunks(client, chat_id, results, header=""):
    lines = results if isinstance(results, list) else list(results)
    chunk, chunks = [], []
    curr_len = len(header)
    for line in lines:
        if curr_len + len(line) + 1 > 3800:
            chunks.append(chunk)
            chunk = [line]
            curr_len = len(line)
        else:
            chunk.append(line)
            curr_len += len(line) + 1
    if chunk:
        chunks.append(chunk)
    total = len(lines)
    success = sum(1 for l in lines if l.startswith("✅"))
    fail = sum(1 for l in lines if l.startswith("❌"))
    summary = f"📊 Total: {total} | ✅ {success} | ❌ {fail}"
    for i, ch in enumerate(chunks):
        part = f"{'Part '+str(i+1)+'/'+str(len(chunks))+chr(10) if len(chunks)>1 else ''}{chr(10).join(ch)}"
        if i == len(chunks)-1:
            part += f"{summary}"
        await client.send_message(chat_id, part)

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
                raise ValueError(f"Join failed: {str(ex)[:50]}")
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
                        entity = d.entity
                        break
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

async def join_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        entity = await resolve(c, ci, sid)
        await c(JoinChannelRequest(entity))
        gid = entity.id if hasattr(entity, 'id') else 'Unknown'
        title = getattr(entity, 'title', 'Unknown')
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'):
            gid = f"-100{abs(gid)}"
        return f"✅ S{sid} | {title} | `{gid}`"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: Wait {fw.seconds}s"
    except Exception as ex:
        return f"❌ S{sid}: {str(ex)[:50]}"

async def leave_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        entity = await resolve(c, ci, sid)
        gid = entity.id if hasattr(entity, 'id') else 'Unknown'
        title = getattr(entity, 'title', 'Unknown')
        if str(gid).lstrip('-').isdigit() and not str(gid).startswith('-100'):
            gid = f"-100{abs(gid)}"
        asyncio.create_task(c(LeaveChannelRequest(entity)))
        return f"✅ S{sid} left | {title} | `{gid}`"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: Wait {fw.seconds}s"
    except Exception as ex:
        return f"❌ S{sid}: {str(ex)[:50]}"

async def load_session_file(sf, sid):
    try:
        c = TelegramClient(f"sessions/{sf.replace('.session', '')}", API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            sessions[sid] = c
            log(f"✅ S{sid}: {sf}")
            return True
        await c.disconnect()
    except Exception as ex:
        try:
            os.makedirs("failed_sessions", exist_ok=True)
            src = os.path.join("sessions", sf)
            if os.path.exists(src):
                shutil.move(src, os.path.join("failed_sessions", sf))
        except:
            pass
        log(f"❌ S{sid} failed to load {sf}: {ex}")
    return False

async def load_existing_sessions():
    os.makedirs("sessions", exist_ok=True)
    files = sorted(f for f in os.listdir("sessions") if f.endswith(".session"))
    if not files: return 0
    log("📂 Loading sessions...")
    semaphore = asyncio.Semaphore(10)
    async def limited_load(sf, i):
        async with semaphore:
            await asyncio.sleep(i * 0.05)
            return await load_session_file(sf, i+1)
    results = await asyncio.gather(*[limited_load(sf, i) for i, sf in enumerate(files)])
    loaded = sum(results)
    log(f"✅ {loaded} sessions loaded")
    return loaded

async def setup_handlers(client):

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
            results.append(builder.article(title="❌ Invalid Format", text="Usage: `@bot <session_id> <chat>`"))
            return await e.answer(results)

        sid_arg, chat_input = parts[0].strip(), parts[1].strip()
        try: sid = int(sid_arg)
        except ValueError:
            results.append(builder.article(title="❌ Invalid Session ID", text="Must be a number"))
            return await e.answer(results)

        if sid not in sessions:
            results.append(builder.article(title=f"❌ Session {sid} Missing", text=f"Available: {list(sessions.keys())}"))
            return await e.answer(results)

        c = sessions[sid]
        try:
            if not c.is_connected(): await c.connect()
            ent = await resolve(c, chat_input, sid)
            if isinstance(ent, Channel): fc = await c(GetFullChannelRequest(channel=ent))
            elif isinstance(ent, Chat): fc = await c(GetFullChatRequest(chat_id=ent.id))
            else:
                results.append(builder.article(title="❌ Unsupported Type", text="Unsupported chat"))
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
                    results.append(builder.article(
                        title="❌ Not Enough Candidates",
                        text=f"S{sid}: Expected at least 2 candidates, got {len(candidates)}"
                    ))
                    asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
                    await e.answer(results)
                    return
                data = candidates[1]
                ip = data.get("ip", "N/A")
                port = data.get("port", "N/A")
                if ip == "N/A" or port == "N/A":
                    results.append(builder.article(
                        title="❌ Missing IP/Port",
                        text=f"S{sid}: Could not extract IP or Port from response"
                    ))
                    asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
                    await e.answer(results)
                    return
                asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
                results.append(builder.article(
                    title=f"✅ {getattr(ent, 'title', 'Success')}",
                    text=f"🛜 **IP Extracted**\n\n**Session:** {sid}\n**Chat:** {getattr(ent, 'title', '?')}\n**IP:** `{ip}`\n**PORT:** `{port}`\n**CMD:** `/attack {ip} {port} 30`",
                    description=f"IP: {ip} | Port: {port}",
                    buttons=[[Button.url("👤 Owner", "https://t.me/dustbydust")]]
                ))
            except (KeyError, IndexError, json.JSONDecodeError) as je:
                results.append(builder.article(
                    title="❌ Data Parse Error",
                    text=f"S{sid}: Failed to parse response data\n\nError: {str(je)[:100]}"
                ))
                asyncio.create_task(c(LeaveGroupCallRequest(call=fc.full_chat.call, source=0)))
        except Exception as ex:
            results.append(builder.article(title="❌ Error", text=f"S{sid} | {chat_input}\n\n{str(ex)[:200]}"))

        await e.answer(results)

    @client.on(events.NewMessage)
    async def handle_message(e):
        if not e.text: return
        cmd, args = get_cmd(e.text)
        if not cmd: return
        uid = e.sender_id
        user = await e.get_sender()
        log(f"CMD: {cmd} | {getattr(user, 'first_name', uid)} ({uid})")

        if cmd == 'start':
            me = await client.get_me()
            btns = [
                [Button.url("➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
                [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", "https://t.me/dustbydust")]
            ]
            if uid == OWNER_ID: btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
            await e.reply(
                f"нєу [{user.first_name}](tg://user?id={uid})!\n\n"
                f"๏ ᴛʜɪs ɪs [{me.first_name}](tg://user?id={me.id})!\n\n"
                f"➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n"
                f"──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ᴛᴏ sᴇᴇ ᴄᴏᴍᴍᴀɴᴅs.",
                buttons=btns, parse_mode='markdown'
            )

        elif cmd == 'approve' and uid == OWNER_ID:
            target_id = None
            try:
                if args:
                    try:
                        target_id = int(args.strip())
                    except:
                        return await e.reply("❌ Invalid ID")
                elif e.reply_to:
                    target = await e.get_reply_message()
                    target_id = target.sender_id
                if not target_id:
                    return await e.reply("❌ Usage: `/approve <id>` or reply to user")
                if target_id == OWNER_ID:
                    return await e.reply("❌ Owner is always approved")
                if target_id in approved_users:
                    return await e.reply(f"✅ Already approved: `{target_id}`")
                approved_users[target_id] = {"name": "Approved"}
                save_users()
                await e.reply(f"✅ Approved: `{target_id}`")
            except Exception as ex:
                await e.reply(f"❌ Error: {ex}")

        elif cmd == 'remove' and uid == OWNER_ID:
            if not args:
                return await e.reply("❌ Usage: `/remove <id>`")
            try:
                target_id = int(args.strip())
            except:
                return await e.reply("❌ Invalid ID")
            if target_id == OWNER_ID:
                return await e.reply("❌ Cannot remove owner")
            if target_id not in approved_users:
                return await e.reply(f"❌ Not approved: `{target_id}`")
            del approved_users[target_id]
            save_users()
            await e.reply(f"✅ Removed: `{target_id}`")

        elif cmd == 'approved' and uid == OWNER_ID:
            if not approved_users:
                return await e.reply("❌ No approved users")
            lines = "\n".join(f"• `{k}` - {v.get('name', '?')}" for k, v in approved_users.items())
            await e.reply(f"✅ **Approved Users ({len(approved_users)}):**\n\n{lines}")

        elif cmd == 'join' and is_approved(uid):
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                return await e.reply("❌ Usage: `/join <session|all> <chat>`")
            sid_arg, chat_input = parts[0].strip(), parts[1].strip()
            if sid_arg.lower() == 'all':
                if not sessions:
                    return await e.reply("❌ No sessions loaded")
                msg = await e.reply("⏳ Joining all...")
                tasks = [join_task(sid, c, chat_input) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                await msg.delete()
                await send_chunks(bot, e.chat_id, list(results), "Join Results")
            else:
                try:
                    sid = int(sid_arg)
                except:
                    return await e.reply("❌ Invalid session ID")
                if sid not in sessions:
                    return await e.reply(f"❌ Session {sid} not found")
                msg = await e.reply("⏳ Joining...")
                result = await join_task(sid, sessions[sid], chat_input)
                await msg.edit(result)

        elif cmd == 'leave' and is_approved(uid):
            parts = args.split(maxsplit=1)
            if len(parts) < 2:
                return await e.reply("❌ Usage: `/leave <session|all> <chat>`")
            sid_arg, chat_input = parts[0].strip(), parts[1].strip()
            if sid_arg.lower() == 'all':
                if not sessions:
                    return await e.reply("❌ No sessions loaded")
                msg = await e.reply("⏳ Leaving all...")
                tasks = [leave_task(sid, c, chat_input) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                await msg.delete()
                await send_chunks(bot, e.chat_id, list(results), "Leave Results")
            else:
                try:
                    sid = int(sid_arg)
                except:
                    return await e.reply("❌ Invalid session ID")
                if sid not in sessions:
                    return await e.reply(f"❌ Session {sid} not found")
                msg = await e.reply("⏳ Leaving...")
                result = await leave_task(sid, sessions[sid], chat_input)
                await msg.edit(result)

        elif cmd == 'clearsessions' and uid == OWNER_ID:
            if not sessions:
                return await e.reply("❌ No sessions loaded")
            await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
            sessions.clear()
            entity_cache.clear()
            shutil.rmtree("sessions", ignore_errors=True)
            os.makedirs("sessions", exist_ok=True)
            await e.reply("✅ All sessions cleared")

        elif cmd == 'exportsessions' and uid == OWNER_ID:
            if not sessions: return await e.reply("❌ No sessions loaded")
            try:
                msg = await e.reply("⏳ Exporting...")
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
                    caption=f"📦 **Exported Sessions**\n\n✅ Total: {total}\n🔢 S{sorted_sids[0]} → S{sorted_sids[-1]}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    reply_to=e.id
                )
                os.remove(zip_path)
                await msg.delete()
            except Exception as ex:
                await e.reply(f"❌ Export error: {ex}")

    @client.on(events.NewMessage(func=lambda e: e.file and e.file.name and e.file.name.endswith('.zip')))
    async def handle_zip(e):
        if e.sender_id != OWNER_ID: return
        msg = await e.reply("⏳ Loading sessions...")
        zp = None
        try:
            os.makedirs("sessions", exist_ok=True)
            os.makedirs("temp_sessions", exist_ok=True)
            zp = await e.download_media(file="temp_sessions/uploaded.zip")
            if not zp or not os.path.exists(zp):
                return await msg.edit("❌ Download failed")
            if not zipfile.is_zipfile(zp):
                return await msg.edit("❌ Invalid ZIP file")
            session_files = []
            with zipfile.ZipFile(zp, 'r') as z:
                for info in z.infolist():
                    name = os.path.basename(info.filename)
                    if not name.endswith(".session"):
                        continue
                    try:
                        data = z.read(info.filename)
                        out_path = os.path.join("sessions", name)
                        with open(out_path, 'wb') as f:
                            f.write(data)
                        session_files.append(name)
                    except Exception as ex:
                        log(f"❌ Extract failed {name}: {ex}")
            if not session_files:
                return await msg.edit("❌ No .session files found in ZIP")
            next_id = max(sessions.keys()) + 1 if sessions else 1
            loaded = failed = 0
            lock = asyncio.Lock()
            zip_sem = asyncio.Semaphore(10)
            async def try_load(sf):
                nonlocal next_id, loaded, failed
                async with zip_sem:
                    try:
                        c = TelegramClient(f"sessions/{sf.replace('.session', '')}", API_ID, API_HASH)
                        await c.connect()
                        if await c.is_user_authorized():
                            async with lock:
                                sid = next_id
                                next_id += 1
                                sessions[sid] = c
                                loaded += 1
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
                        except:
                            pass
                        async with lock: failed += 1
                        log(f"❌ {sf}: {ex}")
            await asyncio.gather(*[try_load(sf) for sf in session_files])
            await msg.edit(f"✅ Loaded {loaded} new\n❌ Failed {failed}\n📊 Total: {len(sessions)}")
        except Exception as ex:
            log(f"❌ ZIP error: {ex}")
            await msg.edit(f"❌ Error: {str(ex)[:200]}")
        finally:
            try:
                if zp and os.path.exists(zp): os.remove(zp)
                shutil.rmtree("temp_sessions", ignore_errors=True)
            except: pass

    @client.on(events.CallbackQuery(pattern=b"owner_panel"))
    async def owner_panel_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await e.edit(
            f"🔐 **Owner Panel**\n\n✅ Sessions: {len(sessions)}\n✅ Approved: {len(approved_users)}\n\n➤ Upload .zip or use commands",
            buttons=[
                [Button.inline("👥 Approved Users", b"owner_approved")],
                [Button.inline("📦 Session Info", b"owner_sessions")],
                [Button.inline("🗑️ Clear Sessions", b"owner_clear")],
                [Button.inline("🏠 Back", b"home")]
            ]
        )

    @client.on(events.CallbackQuery(pattern=b"owner_approved"))
    async def owner_approved_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        if not approved_users: return await e.answer("❌ No approved users", alert=True)
        lines = "\n".join(f"• [{v.get('name','?')}](tg://user?id={k}) (`{k}`)" for k, v in approved_users.items())
        await e.edit(f"✅ **Approved ({len(approved_users)}):**\n\n{lines}", buttons=[[Button.inline("🔙 Back", b"owner_panel")]], parse_mode='markdown')

    @client.on(events.CallbackQuery(pattern=b"owner_sessions"))
    async def owner_sessions_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        body = "\n".join(f"Session {k}" for k in sorted(sessions.keys())) if sessions else "No sessions"
        await e.edit(f"📦 **Session Info**\n\n✅ Total: {len(sessions)}\n\n{body}", buttons=[[Button.inline("🔙 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"owner_clear"))
    async def owner_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await e.edit("⚠️ **Warning!**\n\nClear ALL sessions?\n\nCannot be undone.",
            buttons=[[Button.inline("✅ Yes, Clear All", b"confirm_clear")], [Button.inline("❌ Cancel", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"confirm_clear"))
    async def confirm_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        await asyncio.gather(*[s.disconnect() for s in sessions.values()], return_exceptions=True)
        sessions.clear(); entity_cache.clear()
        shutil.rmtree("sessions", ignore_errors=True)
        os.makedirs("sessions", exist_ok=True)
        await e.edit("✅ All sessions cleared", buttons=[[Button.inline("🏠 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"help"))
    async def help_cb(e):
        me = await client.get_me()
        await e.edit(
            "📚 **ʜᴇʟᴩ ᴍᴇɴᴜ**\n\n"
            "**๏ ᴜsᴇʀ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n"
            "  ❂ `/approve <id>` - ᴀᴩᴩʀᴏᴠᴇ ᴜsᴇʀ\n"
            "  ❂ `/approve` (ʀᴇᴩʟʏ) - ᴀᴩᴩʀᴏᴠᴇ ᴠɪᴀ ʀᴇᴩʟʏ\n"
            "  ❂ `/remove <id>` - ʀᴇᴍᴏᴠᴇ ᴜsᴇʀ\n"
            "  ❂ `/approved` - ʟɪsᴛ ᴀᴩᴩʀᴏᴠᴇᴅ\n\n"
            "**๏ sᴇssɪᴏɴ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n"
            "  ❂ sᴇɴᴅ .ᴢɪᴩ - ʟᴏᴀᴅ sᴇssɪᴏɴs\n"
            "  ❂ `/clearsessions` - ᴄʟᴇᴀʀ ᴀʟʟ\n"
            "  ❂ `/exportsessions` - ᴇxᴩᴏʀᴛ ᴢɪᴩ\n\n"
            "**๏ ɢʀᴏᴜᴩ ᴀᴄᴛɪᴏɴs:**\n"
            "  ❂ `/join <session|all> <chat>`\n"
            "  ❂ `/leave <session|all> <chat>`\n\n"
            "**๏ ɪᴩ ᴇxᴛʀᴀᴄᴛɪᴏɴ:**\n"
            "  ❂ `/getip <session> <chat>`\n"
            "  ❂ ʀᴇǫᴜɪʀᴇs ᴀᴄᴛɪᴠᴇ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ",
            buttons=[
                [Button.url("➕ Add to Group", f"https://t.me/{me.username}?startgroup=true")],
                [Button.inline("🏠 Home", b"home"), Button.url("👤 Owner", "https://t.me/dustbydust")]
            ]
        )

    @client.on(events.CallbackQuery(pattern=b"home"))
    async def home_cb(e):
        user = await e.get_sender()
        me = await client.get_me()
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

async def main():
    global bot
    log("🚀 Starting...")
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
