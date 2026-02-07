import json, random, zipfile, os, asyncio, shutil
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import ImportChatInviteRequest
from motor.motor_asyncio import AsyncIOMotorClient

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except:
    pass

BOT_TOKEN = "8161665118:AAFrISckGPcTGoXNkd9eBe-Vyz83_b1-WpA"
API_ID, API_HASH, OWNER_ID = 25723056, "cbda56fac135e92b755e1243aefe9697", 8101867786
MONGO_URI = "mongodb://localhost:27017"

sessions = {}
bot = None
mongo_client = None
db = None
users_col = None

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

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

async def resolve(c, i):
    i = i.strip()
    if 't.me/' in i or i.startswith('+'):
        hash_part = i.split('t.me/')[-1] if 't.me/' in i else i
        hash_part = hash_part.split('?')[0].strip()
        if hash_part.startswith('+'):
            try:
                result = await c(ImportChatInviteRequest(hash_part[1:]))
                return result.chats[0]
            except Exception as e:
                print(e)
                raise ValueError(f"Cannot join: {str(e)[:30]}")
        else:
            i = ('@' if not hash_part.startswith('@') else '') + hash_part
    ch = i[1:] if i.startswith('@') else i
    if ch.lstrip('-').isdigit():
        chat_id = int(ch)
        try:
            return await c.get_entity(chat_id)
        except Exception as e:
            print(e)
            try:
                dialogs = await c.get_dialogs()
                for dialog in dialogs:
                    if dialog.entity.id == abs(chat_id):
                        return dialog.entity
            except Exception as e:
                print(e)
            raise ValueError(f"Cannot find entity for {chat_id}")
    try: 
        return await c.get_entity(i if i.startswith('@') else int(i))
    except Exception as e:
        print(e)
        return await c.get_entity(('@' if not i.startswith('@') else '') + i)

async def is_approved(uid):
    if uid == OWNER_ID: return True
    user = await users_col.find_one({"user_id": uid, "approved": True})
    return user is not None

async def get_user_name(uid):
    user = await users_col.find_one({"user_id": uid})
    return user.get("name", "Unknown") if user else "Unknown"

async def join_task(sid, c, ci, delay=0):
    try:
        await asyncio.sleep(delay)
        if not c.is_connected(): 
            await c.connect()
        await c(JoinChannelRequest(await resolve(c, ci)))
        return f"✅ Session {sid}"
    except FloodWaitError as e:
        print(f"FloodWait Session {sid}: {e}")
        return f"⏳ Session {sid}: Wait {e.seconds}s"
    except Exception as e:
        print(f"Error Session {sid}: {e}")
        return f"❌ Session {sid}: {str(e)[:30]}"

async def leave_task(sid, c, ci, delay=0):
    try:
        await asyncio.sleep(delay)
        if not c.is_connected(): 
            await c.connect()
        await c(LeaveChannelRequest(await resolve(c, ci)))
        return f"✅ Session {sid}"
    except FloodWaitError as e:
        print(f"FloodWait Session {sid}: {e}")
        return f"⏳ Session {sid}: Wait {e.seconds}s"
    except Exception as e:
        print(f"Error Session {sid}: {e}")
        return f"❌ Session {sid}: {str(e)[:30]}"

async def getip_task(sid, c, ci, delay=0):
    try:
        await asyncio.sleep(delay)
        if not c.is_connected(): 
            await c.connect()
        
        entity = await resolve(c, ci)
        full = await c(GetFullChannelRequest(entity))
        
        if not full.full_chat.call:
            return f"❌ Session {sid}: No active voice chat", None
        
        call = full.full_chat.call
        params = DataJSON(data=json.dumps({"ufrag": random.choice(["test", "user"]), "pwd": "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=32))}))
        
        result = await c(JoinGroupCallRequest(call=call, params=params, muted=True, video_stopped=True))
        
        transport = None
        for update in result.updates:
            if hasattr(update, 'call') and hasattr(update.call, 'params'):
                call_params = json.loads(update.call.params.data)
                transport = call_params.get('transport')
                break
        
        await c(LeaveGroupCallRequest(call=call))
        
        if transport and 'candidates' in transport:
            candidates = transport['candidates']
            ips = set()
            for cand in candidates:
                if 'ip' in cand:
                    ips.add(cand['ip'])
            
            if ips:
                return f"✅ Session {sid}", list(ips)
            else:
                return f"⚠️ Session {sid}: No IPs found", None
        else:
            return f"⚠️ Session {sid}: No transport data", None
            
    except FloodWaitError as e:
        print(f"FloodWait Session {sid}: {e}")
        return f"⏳ Session {sid}: Wait {e.seconds}s", None
    except Exception as e:
        print(f"Error Session {sid}: {e}")
        return f"❌ Session {sid}: {str(e)[:50]}", None

async def setup_handlers(client):
    @client.on(events.InlineQuery)
    async def inline_handler(e):
        if e.sender_id != OWNER_ID:
            return
        
        query = e.text.strip()
        if not query:
            return
        
        try:
            builder = e.builder
            results = []
            
            if not sessions:
                results.append(builder.article(
                    title="❌ No Sessions Loaded",
                    text="Please load sessions first",
                    description="Upload .zip file with sessions"
                ))
                await e.answer(results)
                return
            
            session_id = list(sessions.keys())[0]
            c = sessions[session_id]
            
            if not c.is_connected():
                await c.connect()
            
            try:
                entity = await resolve(c, query)
                
                info_text = f"📊 **Entity Information**\n\n"
                info_text += f"**Query:** `{query}`\n"
                info_text += f"**ID:** `{entity.id}`\n"
                info_text += f"**Title:** {getattr(entity, 'title', getattr(entity, 'username', 'N/A'))}\n"
                
                if hasattr(entity, 'username') and entity.username:
                    info_text += f"**Username:** @{entity.username}\n"
                    info_text += f"**Link:** https://t.me/{entity.username}\n"
                
                if hasattr(entity, 'participants_count'):
                    info_text += f"**Members:** {entity.participants_count}\n"
                
                info_text += f"\n**Type:** {entity.__class__.__name__}"
                
                results.append(builder.article(
                    title=f"✅ {getattr(entity, 'title', getattr(entity, 'username', 'Entity'))}",
                    text=info_text,
                    description=f"ID: {entity.id}",
                    buttons=[[Button.url("👤 Owner", "https://t.me/dustbydust")]]
                ))
                
            except Exception as e:
                print(f"Inline query error: {e}")
                results.append(builder.article(
                    title=f"❌ Error: {query}",
                    text=f"❌ **Error**\n\n{str(e)[:200]}",
                    description=str(e)[:100]
                ))
            
            await e.answer(results)
            
        except Exception as e:
            print(f"❌ Inline query error: {e}")

    @client.on(events.NewMessage)
    async def handle_message(e):
        if not e.text: return
        cmd, args = get_cmd(e.text)
        if not cmd: return
        
        user = await e.get_sender()
        log(f"Command: {cmd} | From: {user.first_name} ({user.id})")
        
        if cmd == 'start':
            btns = [[Button.url("➕ Add to Group", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                    [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", f"https://t.me/dustbydust")]]
            if e.sender_id == OWNER_ID:
                btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
            await e.reply(f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n๏ ᴛʜɪs ɪs [{(await client.get_me()).first_name}](tg://user?id={(await client.get_me()).id})!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ ᴡɪᴛʜ sᴏᴍᴇ ᴀᴡᴇsᴏᴍᴇ ғᴇᴀᴛᴜʀᴇs.\n──────────────────\n๏ ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ ʜᴇʟᴩ ʙᴜᴛᴛᴏɴ ᴛᴏ ɢᴇᴛ ɪɴғᴏʀᴍᴀᴛɪᴏɴ ᴀʙᴏᴜᴛ ᴍʏ ᴍᴏᴅᴜʟᴇs ᴀɴᴅ ᴄᴏᴍᴍᴀɴᴅs.", buttons=btns, parse_mode='markdown')
        
        elif cmd == 'approve':
            if e.sender_id != OWNER_ID: return await e.reply("❌ Owner only")
            if e.is_reply:
                r = await e.get_reply_message()
                uid = r.sender_id
                uname = (await r.get_sender()).first_name
            else:
                if not args: return await e.reply("Usage: approve <user_id> or reply to user")
                try: uid = int(args.strip())
                except: return await e.reply("❌ Invalid user ID")
                uname = await get_user_name(uid)
            await users_col.update_one({"user_id": uid}, {"$set": {"user_id": uid, "name": uname, "approved": True}}, upsert=True)
            await e.reply(f"✅ Approved [{uname}](tg://user?id={uid})", parse_mode='markdown')
        
        elif cmd == 'remove':
            if e.sender_id != OWNER_ID: return await e.reply("❌ Owner only")
            if e.is_reply:
                r = await e.get_reply_message()
                uid = r.sender_id
                uname = (await r.get_sender()).first_name
            else:
                if not args: return await e.reply("Usage: remove <user_id> or reply to user")
                try: uid = int(args.strip())
                except: return await e.reply("❌ Invalid user ID")
                uname = await get_user_name(uid)
            await users_col.delete_one({"user_id": uid})
            await e.reply(f"✅ Removed [{uname}](tg://user?id={uid})", parse_mode='markdown')
        
        elif cmd == 'approved':
            if e.sender_id != OWNER_ID: return await e.reply("❌ Owner only")
            users = await users_col.find({"approved": True}).to_list(100)
            if not users: return await e.reply("❌ No approved users")
            txt = f"✅ **Approved ({len(users)}):**\n\n" + "\n".join([f"• [{u.get('name', 'Unknown')}](tg://user?id={u['user_id']}) (`{u['user_id']}`)" for u in users])
            await e.reply(txt, parse_mode='markdown')
        
        elif cmd == 'join':
            if not await is_approved(e.sender_id): return await e.reply("❌ Not approved")
            if not sessions: return await e.reply("❌ No sessions loaded")
            if not args: return await e.reply("Usage: join <session|all> <chat>")
            parts = args.split(maxsplit=1)
            if len(parts) < 2: return await e.reply("Usage: join <session|all> <chat>")
            sid_arg, ci = parts[0].strip(), parts[1].strip()
            
            msg = await e.reply("⏳ Processing...")
            if sid_arg == 'all':
                tasks = []
                for idx, (k, v) in enumerate(sessions.items()):
                    delay = idx * 2  # 2 second delay between each session
                    tasks.append(join_task(k, v, ci, delay))
                results = await asyncio.gather(*tasks)
                await msg.edit("\n".join(results))
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions: return await msg.edit(f"❌ Session {sid} not found")
                    res = await join_task(sid, sessions[sid], ci)
                    await msg.edit(res)
                except ValueError:
                    await msg.edit("❌ Invalid session ID")
                except Exception as e:
                    print(e)
                    await msg.edit(f"❌ Error: {str(e)[:50]}")
        
        elif cmd == 'leave':
            if not await is_approved(e.sender_id): return await e.reply("❌ Not approved")
            if not sessions: return await e.reply("❌ No sessions loaded")
            if not args: return await e.reply("Usage: leave <session|all> <chat>")
            parts = args.split(maxsplit=1)
            if len(parts) < 2: return await e.reply("Usage: leave <session|all> <chat>")
            sid_arg, ci = parts[0].strip(), parts[1].strip()
            
            msg = await e.reply("⏳ Processing...")
            if sid_arg == 'all':
                tasks = []
                for idx, (k, v) in enumerate(sessions.items()):
                    delay = idx * 2  # 2 second delay between each session
                    tasks.append(leave_task(k, v, ci, delay))
                results = await asyncio.gather(*tasks)
                await msg.edit("\n".join(results))
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions: return await msg.edit(f"❌ Session {sid} not found")
                    res = await leave_task(sid, sessions[sid], ci)
                    await msg.edit(res)
                except ValueError:
                    await msg.edit("❌ Invalid session ID")
                except Exception as e:
                    print(e)
                    await msg.edit(f"❌ Error: {str(e)[:50]}")
        
        elif cmd == 'getip':
            if not await is_approved(e.sender_id): return await e.reply("❌ Not approved")
            if not sessions: return await e.reply("❌ No sessions loaded")
            if not args: return await e.reply("Usage: getip <session|all> <chat>")
            parts = args.split(maxsplit=1)
            if len(parts) < 2: return await e.reply("Usage: getip <session|all> <chat>")
            sid_arg, ci = parts[0].strip(), parts[1].strip()
            
            msg = await e.reply("⏳ Extracting IPs...")
            if sid_arg == 'all':
                tasks = []
                for k, v in sessions.items():
                    tasks.append(getip_task(k, v, ci, 0))  # No delay - execute all simultaneously
                results = await asyncio.gather(*tasks)
                
                all_ips = set()
                status_msgs = []
                for status, ips in results:
                    status_msgs.append(status)
                    if ips:
                        all_ips.update(ips)
                
                output = "\n".join(status_msgs)
                if all_ips:
                    output += f"\n\n🌐 **Extracted IPs:**\n" + "\n".join([f"• `{ip}`" for ip in sorted(all_ips)])
                
                await msg.edit(output, parse_mode='markdown')
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions: return await msg.edit(f"❌ Session {sid} not found")
                    status, ips = await getip_task(sid, sessions[sid], ci)
                    
                    output = status
                    if ips:
                        output += f"\n\n🌐 **Extracted IPs:**\n" + "\n".join([f"• `{ip}`" for ip in ips])
                    
                    await msg.edit(output, parse_mode='markdown')
                except ValueError:
                    await msg.edit("❌ Invalid session ID")
                except Exception as e:
                    print(e)
                    await msg.edit(f"❌ Error: {str(e)[:50]}")
        
        elif cmd == 'clearsessions':
            if e.sender_id != OWNER_ID: return await e.reply("❌ Owner only")
            try:
                for s in sessions.values():
                    try: await s.disconnect()
                    except Exception as e: print(e)
                sessions.clear()
                if os.path.exists("sessions"):
                    shutil.rmtree("sessions")
                os.makedirs("sessions", exist_ok=True)
                await e.reply("✅ All sessions cleared")
            except Exception as e:
                print(e)
                await e.reply(f"❌ Error: {str(e)}")

    @client.on(events.NewMessage(func=lambda e: e.media and hasattr(e.media, 'document') and e.file and e.file.name and e.file.name.endswith('.zip')))
    async def load_sess(e):
        if e.sender_id != OWNER_ID: return
        try:
            log(f"📦 Loading sessions from {e.file.name}...")
            msg = await e.reply("⏳ Loading...")
            zp = await e.download_media()
            os.makedirs("sessions", exist_ok=True)
            
            temp_dir = "temp_sessions"
            os.makedirs(temp_dir, exist_ok=True)
            
            with zipfile.ZipFile(zp, 'r') as z: 
                z.extractall(temp_dir)
            
            import hashlib
            
            def get_file_hash(filepath):
                with open(filepath, 'rb') as f:
                    return hashlib.md5(f.read()).hexdigest()
            
            existing_hashes = {}
            for existing_file in os.listdir("sessions"):
                if existing_file.endswith(".session"):
                    filepath = os.path.join("sessions", existing_file)
                    existing_hashes[get_file_hash(filepath)] = existing_file
            
            loaded = failed = skipped = 0
            new_session_files = [f for f in os.listdir(temp_dir) if f.endswith(".session")]
            next_id = max(sessions.keys()) + 1 if sessions else 1
            
            for sf in new_session_files:
                try:
                    temp_path = os.path.join(temp_dir, sf)
                    file_hash = get_file_hash(temp_path)
                    
                    if file_hash in existing_hashes:
                        log(f"⏭️ Skipping duplicate: {sf} (same as {existing_hashes[file_hash]})")
                        skipped += 1
                        continue
                    
                    final_path = os.path.join("sessions", sf)
                    shutil.move(temp_path, final_path)
                    
                    session_path = f"sessions/{sf.replace('.session', '')}"
                    c = TelegramClient(session_path, API_ID, API_HASH)
                    await c.connect()
                    
                    if await c.is_user_authorized():
                        sessions[next_id] = c
                        loaded += 1
                        log(f"✅ Session {next_id} loaded: {sf}")
                        next_id += 1
                    else:
                        await c.disconnect()
                        failed += 1
                except Exception as e:
                    print(e)
                    failed += 1
            
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.remove(zp)
            
            log(f"✅ Total: {len(sessions)} | New: {loaded} | Skipped: {skipped} | Failed: {failed}")
            await msg.edit(f"✅ Loaded {loaded} new sessions\n⏭️ Skipped {skipped} duplicates\n❌ Failed {failed} sessions\n📊 Total: {len(sessions)}")
        except Exception as e:
            print(e)
            await e.reply(f"❌ {str(e)}")

    @client.on(events.CallbackQuery(pattern=b"owner_panel"))
    async def owner_panel_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        approved_count = await users_col.count_documents({"approved": True})
        btns = [[Button.inline("👥 Approved Users", b"owner_approved")],
                [Button.inline("📦 Session Info", b"owner_sessions")],
                [Button.inline("🗑️ Clear Sessions", b"owner_clear")],
                [Button.inline("🏠 Back", b"home")]]
        await e.edit(f"🔐 **Owner Panel**\n\n✅ Sessions: {len(sessions)}\n✅ Approved: {approved_count}\n\n➤ Upload .zip or use commands", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"owner_approved"))
    async def owner_approved_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        users = await users_col.find({"approved": True}).to_list(100)
        if not users: return await e.answer("❌ No approved users", alert=True)
        txt = f"✅ **Approved ({len(users)}):**\n\n" + "\n".join([f"• [{u.get('name', 'Unknown')}](tg://user?id={u['user_id']}) (`{u['user_id']}`)" for u in users])
        await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"owner_panel")]], parse_mode='markdown')

    @client.on(events.CallbackQuery(pattern=b"owner_sessions"))
    async def owner_sessions_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        txt = f"📦 **Session Info**\n\n✅ Total: {len(sessions)}\n✅ Active: {len(sessions)}\n\n" + ("\n".join([f"Session {k}" for k in sorted(sessions.keys())]) if sessions else "No sessions")
        await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"owner_clear"))
    async def owner_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        btns = [[Button.inline("✅ Yes, Clear All", b"confirm_clear")], [Button.inline("❌ Cancel", b"owner_panel")]]
        await e.edit("⚠️ **Warning!**\n\nClear ALL sessions?\n\nCannot be undone.", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"confirm_clear"))
    async def confirm_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        try:
            for s in sessions.values():
                try: await s.disconnect()
                except Exception as e: print(e)
            sessions.clear()
            if os.path.exists("sessions"):
                shutil.rmtree("sessions")
            os.makedirs("sessions", exist_ok=True)
            await e.edit("✅ All sessions cleared", buttons=[[Button.inline("🏠 Back", b"owner_panel")]])
        except Exception as e:
            print(e)
            await e.edit(f"❌ Error: {str(e)}")

    @client.on(events.CallbackQuery(pattern=b"help"))
    async def help_cb(e):
        btns = [[Button.url("➕ Add to Group", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                [Button.inline("🏠 Home", b"home"), Button.url("👤 Owner", f"https://t.me/dustbydust")]]
        await e.edit("📚 **ʜᴇʟᴩ ᴍᴇɴᴜ**\n\n**๏ ᴜsᴇʀ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n  ❂ `approve <user_id>` - ᴀᴩᴩʀᴏᴠᴇ ᴜsᴇʀ\n  ❂ `approve` (ʀᴇᴩʟʏ) - ᴀᴩᴩʀᴏᴠᴇ ᴠɪᴀ ʀᴇᴩʟʏ\n  ❂ `remove <user_id>` - ʀᴇᴍᴏᴠᴇ ᴜsᴇʀ\n  ❂ `remove` (ʀᴇᴩʟʏ) - ʀᴇᴍᴏᴠᴇ ᴠɪᴀ ʀᴇᴩʟʏ\n  ❂ `approved` - ʟɪsᴛ ᴀʟʟ ᴀᴩᴩʀᴏᴠᴇᴅ ᴜsᴇʀs\n\n**๏ sᴇssɪᴏɴ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n  ❂ sᴇɴᴅ .ᴢɪᴩ ғɪʟᴇ - ʟᴏᴀᴅ sᴇssɪᴏɴs\n  ❂ sᴜᴩᴩᴏʀᴛs ᴍᴜʟᴛɪᴩʟᴇ .sᴇssɪᴏɴ ғɪʟᴇs\n  ❂ `clearsessions` - ᴄʟᴇᴀʀ ᴀʟʟ sᴇssɪᴏɴs\n\n**๏ ɢʀᴏᴜᴩ ᴀᴄᴛɪᴏɴs:**\n  ❂ `join <session|all> <chat>` - ᴊᴏɪɴ ɢʀᴏᴜᴩ\n  ❂ `leave <session|all> <chat>` - ʟᴇᴀᴠᴇ ɢʀᴏᴜᴩ\n  ❂ sᴜᴩᴩᴏʀᴛs: @ᴜsᴇʀɴᴀᴍᴇ, ʟɪɴᴋs, ᴄʜᴀᴛ ɪᴅ\n\n**๏ ɪᴩ ᴇxᴛʀᴀᴄᴛɪᴏɴ:**\n  ❂ `getip <session|all> <chat>` - ᴇxᴛʀᴀᴄᴛ ɪᴩ\n  ❂ ʀᴇǫᴜɪʀᴇs ᴀᴄᴛɪᴠᴇ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ\n\n**๏ ɴᴏᴛᴇ:**\n  ❂ ᴀʟʟ ᴢɪᴩs ᴀᴅᴅ ɴᴇᴡ sᴇssɪᴏɴs\n  ❂ ᴜsᴇ ᴄʟᴇᴀʀsᴇssɪᴏɴs ᴛᴏ ʀᴇsᴇᴛ", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"home"))
    async def home_cb(e):
        user = await e.get_sender()
        btns = [[Button.url("➕ Add to Group", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", f"https://t.me/dustbydust")]]
        if e.sender_id == OWNER_ID:
            btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
        await e.edit(f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n๏ ᴛʜɪs ɪs [{(await client.get_me()).first_name}](tg://user?id={(await client.get_me()).id})!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ ᴡɪᴛʜ sᴏᴍᴇ ᴀᴡᴇsᴏᴍᴇ ғᴇᴀᴛᴜʀᴇs.\n──────────────────\n๏ ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ ʜᴇʟᴩ ʙᴜᴛᴛᴏɴ ᴛᴏ ɢᴇᴛ ɪɴғᴏʀᴍᴀᴛɪᴏɴ ᴀʙᴏᴜᴛ ᴍʏ ᴍᴏᴅᴜʟᴇs ᴀɴᴅ ᴄᴏᴍᴍᴀɴᴅs.", buttons=btns, parse_mode='markdown')

async def load_existing_sessions():
    if not os.path.exists("sessions"):
        os.makedirs("sessions", exist_ok=True)
        return 0
    
    loaded = 0
    session_files = [f for f in os.listdir("sessions") if f.endswith(".session")]
    
    if not session_files:
        return 0
    
    log("📂 Loading existing sessions from disk...")
    
    next_id = 1
    for sf in session_files:
        try:
            session_path = f"sessions/{sf.replace('.session', '')}"
            c = TelegramClient(session_path, API_ID, API_HASH)
            await c.connect()
            
            if await c.is_user_authorized():
                sessions[next_id] = c
                loaded += 1
                log(f"✅ Session {next_id} loaded: {sf}")
                next_id += 1
            else:
                await c.disconnect()
        except Exception as e:
            print(f"❌ Failed to load {sf}: {e}")
    
    log(f"✅ Loaded {loaded} existing sessions from disk")
    return loaded

async def main():
    global bot, mongo_client, db, users_col
    log("🚀 Starting bot...")
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client['bot_db']
    users_col = db['users']
    log("✅ MongoDB connected")
    
    await load_existing_sessions()
    
    bot = TelegramClient('bot_session', API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    log(f"✅ Bot started: @{(await bot.get_me()).username}")
    await setup_handlers(bot)
    log("✅ Handlers registered")
    log("🎉 Bot is running...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
