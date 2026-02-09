import json, random, zipfile, os, asyncio, shutil
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON, Channel, Chat
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest, GetFullChatRequest
from motor.motor_asyncio import AsyncIOMotorClient
from concurrent.futures import ThreadPoolExecutor

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
executor = ThreadPoolExecutor(max_workers=50)

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
                chat = result.chats[0]
                log(f"✅ Joined via invite link | Group ID: {chat.id} | Title: {getattr(chat, 'title', 'Unknown')}")
                return chat
            except UserAlreadyParticipantError as ex:
                if hasattr(ex, 'updates') and ex.updates and hasattr(ex.updates, 'chats') and ex.updates.chats:
                    chat = ex.updates.chats[0]
                    log(f"ℹ️ Already member | Group ID: {chat.id} | Title: {getattr(chat, 'title', 'Unknown')}")
                    return chat
                raise ValueError("Already member. Provide chat ID: -1001234567890")
            except Exception as ex:
                raise ValueError(f"Join failed: {str(ex)[:50]}")
        else:
            i = ('@' if not hash_part.startswith('@') else '') + hash_part
    ch = i[1:] if i.startswith('@') else i
    if ch.lstrip('-').isdigit():
        chat_id = int(ch)
        try:
            return await c.get_entity(chat_id)
        except:
            try:
                dialogs = await c.get_dialogs()
                for dialog in dialogs:
                    if dialog.entity.id == abs(chat_id):
                        return dialog.entity
            except:
                pass
            raise ValueError(f"Entity not found: {chat_id}")
    try: 
        return await c.get_entity(i if i.startswith('@') else int(i))
    except: 
        return await c.get_entity(('@' if not i.startswith('@') else '') + i)

async def is_approved(uid):
    if uid == OWNER_ID: return True
    user = await users_col.find_one({"user_id": uid, "approved": True})
    return user is not None

async def get_user_name(uid):
    user = await users_col.find_one({"user_id": uid})
    return user.get("name", "Unknown") if user else "Unknown"

async def join_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        entity = await resolve(c, ci)
        await c(JoinChannelRequest(entity))
        group_id = entity.id if hasattr(entity, 'id') else 'Unknown'
        group_title = getattr(entity, 'title', 'Unknown')
        # Enhanced logging with group ID
        log(f"✅ Session {sid} joined | Group ID: {group_id} | Title: {group_title}")
        return f"✅ S{sid} joined\n📋 **Group ID:** `{group_id}`\n📝 **Title:** {group_title}"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: Wait {fw.seconds}s"
    except Exception as ex:
        return f"❌ S{sid}: {str(ex)}"

async def leave_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        entity = await resolve(c, ci)
        await c(LeaveChannelRequest(entity))
        group_id = entity.id if hasattr(entity, 'id') else 'Unknown'
        group_title = getattr(entity, 'title', 'Unknown')
        log(f"✅ Session {sid} left | Group ID: {group_id} | Title: {group_title}")
        return f"✅ S{sid} left\n📋 **Group ID:** `{group_id}`\n📝 **Title:** {group_title}"
    except FloodWaitError as fw:
        return f"⏳ S{sid}: Wait {fw.seconds}s"
    except Exception as ex:
        return f"❌ S{sid}: {str(ex)}"

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
                    title="❌ No Sessions",
                    text="Load sessions first",
                    description="Upload .zip with sessions"
                ))
                await e.answer(results)
                return
            
            parts = query.split(maxsplit=1)
            if len(parts) < 2:
                results.append(builder.article(
                    title="❌ Invalid Format",
                    text="**Usage:** `@bot <session_id> <chat>`\n\n**Example:**\n`@bot 1 @channel`\n`@bot 1 https://t.me/+xxxxx`\n`@bot 1 -1001234567890`",
                    description="Format: <session_id> <chat>"
                ))
                await e.answer(results)
                return
            
            sid_arg, chat_input = parts[0].strip(), parts[1].strip()
            
            try:
                sid = int(sid_arg)
                if sid not in sessions:
                    results.append(builder.article(
                        title=f"❌ Session {sid} Missing",
                        text=f"Session {sid} not found",
                        description=f"Available: {list(sessions.keys())}"
                    ))
                    await e.answer(results)
                    return
            except ValueError:
                results.append(builder.article(
                    title="❌ Invalid Session ID",
                    text="Session ID must be number",
                    description="Example: 1, 2, 3"
                ))
                await e.answer(results)
                return
            
            c = sessions[sid]
            
            if not c.is_connected():
                await c.connect()
            
            try:
                ent = await resolve(c, chat_input)
                
                if isinstance(ent, Channel):
                    fc = await c(GetFullChannelRequest(channel=ent))
                elif isinstance(ent, Chat):
                    fc = await c(GetFullChatRequest(chat_id=ent.id))
                else:
                    fc = None
                
                title = getattr(ent, 'title', 'Unknown')
                username = getattr(ent, 'username', None)
                group_id = getattr(ent, 'id', 'Unknown')
                
                participants = 0
                if isinstance(ent, Channel) and fc:
                    participants = fc.full_chat.participants_count
                elif isinstance(ent, Chat) and fc:
                    participants = fc.full_chat.participants_count
                
                # Enhanced inline result with group ID prominently displayed
                desc = f"ID: {group_id} | Members: {participants}"
                if username:
                    desc += f" | @{username}"
                
                text = f"📋 **Group Information**\n\n"
                text += f"**Title:** {title}\n"
                text += f"**Group ID:** `{group_id}`\n"
                if username:
                    text += f"**Username:** @{username}\n"
                text += f"**Members:** {participants}\n"
                text += f"**Session:** {sid}\n\n"
                text += f"Use this ID to join: `{group_id}`"
                
                results.append(builder.article(
                    title=f"✅ {title}",
                    text=text,
                    description=desc
                ))
                
                await e.answer(results)
            except Exception as ex:
                results.append(builder.article(
                    title="❌ Error",
                    text=f"Failed to get info: {str(ex)[:100]}",
                    description="Check chat input"
                ))
                await e.answer(results)
        except Exception as ex:
            pass

    @client.on(events.NewMessage(pattern=r'^[/!.;&]'))
    async def command_handler(e):
        cmd, arg = get_cmd(e.text)
        if not cmd: return
        
        uid = e.sender_id
        is_owner = uid == OWNER_ID
        approved = await is_approved(uid)
        
        if not is_owner and not approved:
            if cmd not in ['start']:
                return
        
        if cmd == 'start':
            user = await e.get_sender()
            btns = [[Button.url("➕ Add to Group", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                    [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", f"https://t.me/dustbydust")]]
            if is_owner:
                btns.append([Button.inline("🔐 Owner Panel", b"owner_panel")])
            await e.reply(f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n๏ ᴛʜɪs ɪs [{(await client.get_me()).first_name}](tg://user?id={(await client.get_me()).id})!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ ᴡɪᴛʜ sᴏᴍᴇ ᴀᴡᴇsᴏᴍᴇ ғᴇᴀᴛᴜʀᴇs.\n──────────────────\n๏ ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ ʜᴇʟᴩ ʙᴜᴛᴛᴏɴ ᴛᴏ ɢᴇᴛ ɪɴғᴏʀᴍᴀᴛɪᴏɴ ᴀʙᴏᴜᴛ ᴍʏ ᴍᴏᴅᴜʟᴇs ᴀɴᴅ ᴄᴏᴍᴍᴀɴᴅs.", buttons=btns, parse_mode='markdown')
        
        elif cmd == 'join':
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                return await e.reply("❌ Usage: `join <session|all> <chat>`", parse_mode='markdown')
            
            sid_arg, ci = parts[0], parts[1]
            
            if sid_arg.lower() == 'all':
                if not sessions:
                    return await e.reply("❌ No sessions loaded")
                msg = await e.reply(f"🔄 Joining with {len(sessions)} sessions...")
                tasks = [join_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                # Format results to show group ID clearly
                output = "**Join Results:**\n\n" + "\n\n".join(results)
                await msg.edit(output, parse_mode='markdown')
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions:
                        return await e.reply(f"❌ Session {sid} not found. Available: {list(sessions.keys())}")
                    msg = await e.reply(f"🔄 Joining with session {sid}...")
                    result = await join_task(sid, sessions[sid], ci)
                    await msg.edit(result, parse_mode='markdown')
                except ValueError:
                    await e.reply("❌ Invalid session ID")
        
        elif cmd == 'leave':
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                return await e.reply("❌ Usage: `leave <session|all> <chat>`", parse_mode='markdown')
            
            sid_arg, ci = parts[0], parts[1]
            
            if sid_arg.lower() == 'all':
                if not sessions:
                    return await e.reply("❌ No sessions loaded")
                msg = await e.reply(f"🔄 Leaving with {len(sessions)} sessions...")
                tasks = [leave_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                output = "**Leave Results:**\n\n" + "\n\n".join(results)
                await msg.edit(output, parse_mode='markdown')
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions:
                        return await e.reply(f"❌ Session {sid} not found")
                    msg = await e.reply(f"🔄 Leaving with session {sid}...")
                    result = await leave_task(sid, sessions[sid], ci)
                    await msg.edit(result, parse_mode='markdown')
                except ValueError:
                    await e.reply("❌ Invalid session ID")
        
        elif cmd == 'getip':
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                return await e.reply("❌ Usage: `getip <session|all> <chat>`", parse_mode='markdown')
            
            sid_arg, ci = parts[0], parts[1]
            
            async def get_ip_task(sid, c, ci):
                try:
                    if not c.is_connected(): await c.connect()
                    entity = await resolve(c, ci)
                    
                    group_id = getattr(entity, 'id', 'Unknown')
                    group_title = getattr(entity, 'title', 'Unknown')
                    
                    fc = await c(GetFullChannelRequest(channel=entity)) if isinstance(entity, Channel) else await c(GetFullChatRequest(chat_id=entity.id))
                    call = fc.full_chat.call
                    
                    if not call:
                        return f"❌ S{sid}: No active voice chat\n📋 Group ID: `{group_id}`"
                    
                    join_resp = await c(JoinGroupCallRequest(call=call, params=DataJSON(data=json.dumps({"ufrag":"0000","pwd":"0"*22})), muted=True))
                    
                    if hasattr(join_resp, 'updates'):
                        for update in join_resp.updates:
                            if hasattr(update, 'call') and hasattr(update.call, 'connections'):
                                ips = [conn.ip for conn in update.call.connections if hasattr(conn, 'ip')]
                                if ips:
                                    try:
                                        await c(LeaveGroupCallRequest(call=call))
                                    except:
                                        pass
                                    log(f"✅ S{sid} IP extracted | Group ID: {group_id} | Title: {group_title}")
                                    return f"✅ S{sid}\n📋 **Group ID:** `{group_id}`\n📝 **Title:** {group_title}\n🌐 **IP:** `{ips[0]}`"
                    
                    return f"❌ S{sid}: No IP found\n📋 Group ID: `{group_id}`"
                except FloodWaitError as fw:
                    return f"⏳ S{sid}: Wait {fw.seconds}s"
                except Exception as ex:
                    return f"❌ S{sid}: {str(ex)[:80]}"
            
            if sid_arg.lower() == 'all':
                if not sessions:
                    return await e.reply("❌ No sessions loaded")
                msg = await e.reply(f"🔄 Extracting IPs with {len(sessions)} sessions...")
                tasks = [get_ip_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                output = "**IP Extraction Results:**\n\n" + "\n\n".join(results)
                await msg.edit(output, parse_mode='markdown')
            else:
                try:
                    sid = int(sid_arg)
                    if sid not in sessions:
                        return await e.reply(f"❌ Session {sid} not found")
                    msg = await e.reply(f"🔄 Extracting IP with session {sid}...")
                    result = await get_ip_task(sid, sessions[sid], ci)
                    await msg.edit(result, parse_mode='markdown')
                except ValueError:
                    await e.reply("❌ Invalid session ID")
        
        elif cmd == 'approve' and is_owner:
            target_id = None
            if e.is_reply:
                reply = await e.get_reply_message()
                target_id = reply.sender_id
            elif arg.strip().isdigit():
                target_id = int(arg.strip())
            
            if not target_id:
                return await e.reply("❌ Reply to user or provide user_id")
            
            if target_id == OWNER_ID:
                return await e.reply("❌ Owner is always approved")
            
            try:
                user_entity = await client.get_entity(target_id)
                name = user_entity.first_name or "Unknown"
            except:
                name = "Unknown"
            
            await users_col.update_one(
                {"user_id": target_id},
                {"$set": {"user_id": target_id, "approved": True, "name": name}},
                upsert=True
            )
            await e.reply(f"✅ Approved [{name}](tg://user?id={target_id})", parse_mode='markdown')
        
        elif cmd == 'remove' and is_owner:
            target_id = None
            if e.is_reply:
                reply = await e.get_reply_message()
                target_id = reply.sender_id
            elif arg.strip().isdigit():
                target_id = int(arg.strip())
            
            if not target_id:
                return await e.reply("❌ Reply to user or provide user_id")
            
            if target_id == OWNER_ID:
                return await e.reply("❌ Cannot remove owner")
            
            result = await users_col.delete_one({"user_id": target_id})
            if result.deleted_count > 0:
                await e.reply(f"✅ Removed user {target_id}")
            else:
                await e.reply(f"❌ User {target_id} not found in database")
        
        elif cmd == 'approved' and is_owner:
            users = await users_col.find({"approved": True}).to_list(100)
            if not users:
                return await e.reply("❌ No approved users")
            txt = f"✅ **Approved Users ({len(users)}):**\n\n" + "\n".join([f"• [{u.get('name', 'Unknown')}](tg://user?id={u['user_id']}) (`{u['user_id']}`)" for u in users])
            await e.reply(txt, parse_mode='markdown')
        
        elif cmd == 'clearsessions' and is_owner:
            try:
                for s in sessions.values():
                    try:
                        await s.disconnect()
                    except:
                        pass
                sessions.clear()
                if os.path.exists("sessions"):
                    shutil.rmtree("sessions")
                os.makedirs("sessions", exist_ok=True)
                await e.reply("✅ All sessions cleared")
            except Exception as ex:
                await e.reply(f"❌ Error: {ex}")

    @client.on(events.NewMessage(func=lambda e: e.document and e.document.mime_type == 'application/zip'))
    async def zip_handler(e):
        if e.sender_id != OWNER_ID:
            return
        
        try:
            msg = await e.reply("📥 Downloading & extracting...")
            zp = await e.download_media("temp.zip")
            
            if not os.path.exists("sessions"):
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
                except:
                    failed += 1
            
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            os.remove(zp)
            
            log(f"✅ Total: {len(sessions)} | New: {loaded} | Skipped: {skipped} | Failed: {failed}")
            await msg.edit(f"✅ Loaded {loaded} new sessions\n⏭️ Skipped {skipped} duplicates\n❌ Failed {failed} sessions\n📊 Total: {len(sessions)}")
        except Exception as ex:
            await e.reply(f"❌ {ex}")

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
                except: pass
            sessions.clear()
            if os.path.exists("sessions"):
                shutil.rmtree("sessions")
            os.makedirs("sessions", exist_ok=True)
            await e.edit("✅ All sessions cleared", buttons=[[Button.inline("🏠 Back", b"owner_panel")]])
        except Exception as ex:
            await e.edit(f"❌ Error: {ex}")

    @client.on(events.CallbackQuery(pattern=b"help"))
    async def help_cb(e):
        btns = [[Button.url("➕ Add to Group", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                [Button.inline("🏠 Home", b"home"), Button.url("👤 Owner", f"https://t.me/dustbydust")]]
        await e.edit("📚 **ʜᴇʟᴩ ᴍᴇɴᴜ**\n\n**๏ ᴜsᴇʀ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n  ❂ `approve <user_id>` - ᴀᴩᴩʀᴏᴠᴇ ᴜsᴇʀ\n  ❂ `approve` (ʀᴇᴩʟʏ) - ᴀᴩᴩʀᴏᴠᴇ ᴠɪᴀ ʀᴇᴩʟʏ\n  ❂ `remove <user_id>` - ʀᴇᴍᴏᴠᴇ ᴜsᴇʀ\n  ❂ `remove` (ʀᴇᴩʟʏ) - ʀᴇᴍᴏᴠᴇ ᴠɪᴀ ʀᴇᴩʟʏ\n  ❂ `approved` - ʟɪsᴛ ᴀʟʟ ᴀᴩᴩʀᴏᴠᴇᴅ ᴜsᴇʀs\n\n**๏ sᴇssɪᴏɴ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ:**\n  ❂ sᴇɴᴅ .ᴢɪᴩ ғɪʟᴇ - ʟᴏᴀᴅ sᴇssɪᴏɴs\n  ❂ sᴜᴩᴩᴏʀᴛs ᴍᴜʟᴛɪᴩʟᴇ .sᴇssɪᴏɴ ғɪʟᴇs\n  ❂ `clearsessions` - ᴄʟᴇᴀʀ ᴀʟʟ sᴇssɪᴏɴs\n\n**๏ ɢʀᴏᴜᴩ ᴀᴄᴛɪᴏɴs:**\n  ❂ `join <session|all> <chat>` - ᴊᴏɪɴ ɢʀᴏᴜᴩ\n  ❂ `leave <session|all> <chat>` - ʟᴇᴀᴠᴇ ɢʀᴏᴜᴩ\n  ❂ sᴜᴩᴩᴏʀᴛs: @ᴜsᴇʀɴᴀᴍᴇ, ʟɪɴᴋs, ᴄʜᴀᴛ ɪᴅ\n\n**๏ ɪᴩ ᴇxᴛʀᴀᴄᴛɪᴏɴ:**\n  ❂ `getip <session|all> <chat>` - ᴇxᴛʀᴀᴄᴛ ɪᴩ\n  ❂ ʀᴇǫᴜɪʀᴇs ᴀᴄᴛɪᴠᴇ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ\n\n**๏ ɴᴏᴛᴇ:**\n  ❂ ᴀʟʟ ᴢɪᴩs ᴀᴅᴅ ɴᴇᴡ sᴇssɪᴏɴs\n  ❂ ᴜsᴇ ᴄʟᴇᴀʀsᴇssɪᴏɴs ᴛᴏ ʀᴇsᴇᴛ\n  ❂ **Group IDs are shown after join/leave**", buttons=btns)

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
    
    tasks = []
    
    async def load_session(sf, sid):
        try:
            session_path = f"sessions/{sf.replace('.session', '')}"
            c = TelegramClient(session_path, API_ID, API_HASH)
            await c.connect()
            
            if await c.is_user_authorized():
                log(f"✅ Session {sid} loaded: {sf}")
                return (sid, c)
            else:
                await c.disconnect()
                return None
        except:
            return None
    
    for idx, sf in enumerate(session_files, 1):
        tasks.append(load_session(sf, idx))
    
    results = await asyncio.gather(*tasks)
    
    for result in results:
        if result:
            sid, client = result
            sessions[sid] = client
            loaded += 1
    
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
