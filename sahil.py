import json, random, zipfile, os, asyncio, shutil, hashlib
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import DataJSON, Channel, Chat
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest, GetFullChatRequest
from motor.motor_asyncio import AsyncIOMotorClient

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except: pass

BOT_TOKEN = "8161665118:AAFrISckGPcTGoXNkd9eBe-Vyz83_b1-WpA"
API_ID, API_HASH, OWNER_ID = 25723056, "cbda56fac135e92b755e1243aefe9697", 8101867786
MONGO_URI = "mongodb://localhost:27017"

sessions = {}
bot = mongo_client = db = users_col = None
entity_cache = {}

log = lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}")

def get_cmd(t):
    if not t: return None, ""
    t = t.strip()
    for p in ['/', '.', '!', ';', '&']:
        if t.startswith(p):
            r = t[1:].strip()
            if not r: return None, ""
            p = r.split(maxsplit=1)
            return p[0].lower(), (p[1] if len(p) > 1 else "")
    return None, t

async def resolve(c, i):
    i = i.strip()
    cache_key = f"{id(c)}:{i}"
    if cache_key in entity_cache:
        return entity_cache[cache_key]
    
    if 't.me/' in i or i.startswith('+'):
        h = i.split('t.me/')[-1] if 't.me/' in i else i
        h = h.split('?')[0].strip()
        if h.startswith('+'):
            try:
                r = await c(ImportChatInviteRequest(h[1:]))
                entity_cache[cache_key] = r.chats[0]
                return r.chats[0]
            except UserAlreadyParticipantError as ex:
                if hasattr(ex, 'updates') and ex.updates and ex.updates.chats:
                    entity_cache[cache_key] = ex.updates.chats[0]
                    return ex.updates.chats[0]
                raise ValueError("Use chat ID: -1001234567890")
            except: raise ValueError("Cannot join")
        else:
            i = ('@' if not h.startswith('@') else '') + h
    
    ch = i[1:] if i.startswith('@') else i
    if ch.lstrip('-').isdigit():
        cid = int(ch)
        try:
            ent = await c.get_entity(cid)
            entity_cache[cache_key] = ent
            return ent
        except:
            try:
                async for d in c.iter_dialogs(limit=300):
                    if d.entity.id in (abs(cid), cid, -cid):
                        entity_cache[cache_key] = d.entity
                        return d.entity
            except: pass
            
            # Try input peer
            try:
                from telethon.tl.types import InputPeerChannel, InputPeerChat
                if str(cid).startswith('-100'):
                    peer = InputPeerChannel(int(str(cid)[4:]), 0)
                else:
                    peer = InputPeerChat(abs(cid))
                ent = await c.get_entity(peer)
                entity_cache[cache_key] = ent
                return ent
            except: pass
            
            raise ValueError(f"Session not in chat {cid}. First join with .join command")
    
    try:
        ent = await c.get_entity(i if i.startswith('@') else int(i))
        entity_cache[cache_key] = ent
        return ent
    except:
        ent = await c.get_entity(('@' if not i.startswith('@') else '') + i)
        entity_cache[cache_key] = ent
        return ent

async def is_approved(uid):
    return uid == OWNER_ID or await users_col.find_one({"user_id": uid, "approved": True}) is not None

async def join_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        await c(JoinChannelRequest(await resolve(c, ci)))
        return f"✅ Session {sid}"
    except FloodWaitError as fw: return f"⏳ Session {sid}: Wait {fw.seconds}s"
    except Exception as ex: return f"❌ Session {sid}: {str(ex)[:30]}"

async def leave_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        await c(LeaveChannelRequest(await resolve(c, ci)))
        return f"✅ Session {sid}"
    except FloodWaitError as fw: return f"⏳ Session {sid}: Wait {fw.seconds}s"
    except Exception as ex: return f"❌ Session {sid}: {str(ex)[:30]}"

async def getip_task(sid, c, ci):
    try:
        if not c.is_connected(): await c.connect()
        ent = await resolve(c, ci)
        fc = await c(GetFullChannelRequest(channel=ent) if isinstance(ent, Channel) else GetFullChatRequest(chat_id=ent.id))
        call = fc.full_chat.call
        if not call: return f"❌ Session {sid}: No voice chat"
        
        await c(JoinGroupCallRequest(call=call, muted=True, params=DataJSON(data="{}")))
        fc2 = await c(GetFullChannelRequest(channel=ent) if isinstance(ent, Channel) else GetFullChatRequest(chat_id=ent.id))
        
        if not fc2.full_chat.call or not fc2.full_chat.call.params:
            try: await c(LeaveGroupCallRequest(call=call))
            except: pass
            return f"❌ Session {sid}: No IP"
        
        data = json.loads(fc2.full_chat.call.params.data)
        ip = data.get("transport", [{}])[0].get("candidates", [{}])[0].get("ip", "Not found")
        try: await c(LeaveGroupCallRequest(call=call))
        except: pass
        return f"✅ Session {sid}: {ip}"
    except FloodWaitError as fw: return f"⏳ Session {sid}: Wait {fw.seconds}s"
    except Exception as ex: return f"❌ Session {sid}: {str(ex)[:40]}"

async def setup_handlers(client):
    @client.on(events.InlineQuery)
    async def inline_handler(e):
        if e.sender_id != OWNER_ID: return
        q = e.text.strip()
        if not q: return
        
        builder = e.builder
        results = []
        
        if not sessions:
            results.append(builder.article(title="❌ No Sessions", text="Load sessions first"))
            await e.answer(results)
            return
        
        parts = q.split(maxsplit=1)
        if len(parts) < 2:
            results.append(builder.article(title="❌ Invalid", text="Usage: <session_id> <chat>"))
            await e.answer(results)
            return
        
        try:
            sid = int(parts[0])
            if sid not in sessions:
                results.append(builder.article(title=f"❌ Session {sid} Not Found", text=f"Available: {list(sessions.keys())}"))
                await e.answer(results)
                return
        except:
            results.append(builder.article(title="❌ Invalid ID", text="Must be number"))
            await e.answer(results)
            return
        
        c = sessions[sid]
        if not c.is_connected(): await c.connect()
        
        try:
            ent = await resolve(c, parts[1])
            fc = await c(GetFullChannelRequest(channel=ent) if isinstance(ent, Channel) else GetFullChatRequest(chat_id=ent.id))
            
            title = getattr(ent, 'title', 'Unknown')
            members = getattr(fc.full_chat, 'participants_count', 0)
            
            results.append(builder.article(
                title=f"✅ {title}",
                text=f"**{title}**\n👥 {members:,} members\n📱 Session {sid}",
                description=f"{members:,} members"
            ))
        except Exception as ex:
            results.append(builder.article(title="❌ Error", text=str(ex)[:200]))
        
        await e.answer(results)

    @client.on(events.NewMessage)
    async def handle_message(e):
        if not e.text: return
        cmd, args = get_cmd(e.text)
        if not cmd: return
        
        user = await e.get_sender()
        
        if cmd == 'start':
            btns = [[Button.url("➕ Add", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                    [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", "https://t.me/dustbydust")]]
            if e.sender_id == OWNER_ID: btns.append([Button.inline("🔐 Panel", b"owner_panel")])
            await e.reply(f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n๏ ᴛʜɪs ɪs [{(await client.get_me()).first_name}](tg://user?id={(await client.get_me()).id})!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ғᴏʀ ᴄᴏᴍᴍᴀɴᴅs.", buttons=btns, parse_mode='markdown')
        
        elif cmd == 'approve' and e.sender_id == OWNER_ID:
            try:
                if e.is_reply:
                    r = await e.get_reply_message()
                    uid, fn = r.sender_id, (await r.get_sender()).first_name or "User"
                else:
                    if not args: return await e.reply("❌ Usage: approve <user_id>")
                    uid = int(args.split()[0])
                    try: fn = (await client.get_entity(uid)).first_name or "User"
                    except: fn = "User"
                await users_col.update_one({"user_id": uid}, {"$set": {"user_id": uid, "name": fn, "approved": True}}, upsert=True)
                await e.reply(f"✅ [{fn}](tg://user?id={uid}) approved", parse_mode='markdown')
            except: await e.reply("❌ Error")
        
        elif cmd == 'remove' and e.sender_id == OWNER_ID:
            try:
                uid = (await e.get_reply_message()).sender_id if e.is_reply else int(args.split()[0]) if args else None
                if not uid: return await e.reply("❌ Usage: remove <user_id>")
                await users_col.delete_one({"user_id": uid})
                await e.reply(f"❌ Removed", parse_mode='markdown')
            except: await e.reply("❌ Error")
        
        elif cmd == 'approved' and e.sender_id == OWNER_ID:
            users = await users_col.find({"approved": True}).to_list(100)
            if not users: return await e.reply("❌ None")
            await e.reply(f"✅ **Approved ({len(users)}):**\n\n" + "\n".join([f"• [{u.get('name', 'Unknown')}](tg://user?id={u['user_id']})" for u in users]), parse_mode='markdown')
        
        elif cmd == 'clearsessions' and e.sender_id == OWNER_ID:
            for s in sessions.values():
                try: await s.disconnect()
                except: pass
            sessions.clear()
            entity_cache.clear()
            if os.path.exists("sessions"): shutil.rmtree("sessions")
            os.makedirs("sessions", exist_ok=True)
            await e.reply("✅ Cleared")
        
        elif cmd == 'join' and await is_approved(e.sender_id):
            if not args or len(args.split(maxsplit=1)) < 2: return await e.reply("❌ Usage: join <session|all> <chat>")
            parts = args.split(maxsplit=1)
            si, ci = parts[0], parts[1]
            
            if si.lower() == "all":
                msg = await e.reply("⏳ Joining...")
                tasks = [join_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                await msg.edit("\n".join(results)[:4000])
            else:
                sid = int(si)
                if sid not in sessions: return await e.reply("❌ Not loaded")
                msg = await e.reply("⏳ Joining...")
                c = sessions[sid]
                if not c.is_connected(): await c.connect()
                await c(JoinChannelRequest(await resolve(c, ci)))
                await msg.edit(f"✅ Session {sid} joined")
        
        elif cmd == 'leave' and await is_approved(e.sender_id):
            if not args or len(args.split(maxsplit=1)) < 2: return await e.reply("❌ Usage: leave <session|all> <chat>")
            parts = args.split(maxsplit=1)
            si, ci = parts[0], parts[1]
            
            if si.lower() == "all":
                msg = await e.reply("⏳ Leaving...")
                tasks = [leave_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                await msg.edit("\n".join(results)[:4000])
            else:
                sid = int(si)
                if sid not in sessions: return await e.reply("❌ Not loaded")
                msg = await e.reply("⏳ Leaving...")
                c = sessions[sid]
                if not c.is_connected(): await c.connect()
                await c(LeaveChannelRequest(await resolve(c, ci)))
                await msg.edit(f"✅ Session {sid} left")
        
        elif cmd == 'getip' and await is_approved(e.sender_id):
            if not args or len(args.split(maxsplit=1)) < 2:
                await e.reply("📚 **Get IP**\n\n`.getip <session|all> <chat>`\n\n**Examples:**\n• `.getip 1 @channel`\n• `.getip all https://t.me/+invite`\n• `.getip 1 -1001234567890`\n\n**Note:** Voice chat must be active!")
                return
            
            parts = args.split(maxsplit=1)
            si, ci = parts[0], parts[1]
            msg = await e.reply("🔍 Processing...")
            
            if si.lower() == "all":
                if not sessions: return await msg.edit("❌ No sessions")
                tasks = [getip_task(sid, c, ci) for sid, c in sessions.items()]
                results = await asyncio.gather(*tasks)
                await msg.edit(f"📊 **Results**\n\n" + "\n\n".join(results)[:4000])
            else:
                try:
                    sid = int(si)
                    if sid not in sessions: return await msg.edit(f"❌ Session {sid} not found")
                    result = await getip_task(sid, sessions[sid], ci)
                    await msg.edit(result)
                except ValueError: await msg.edit("❌ Invalid session ID")

    @client.on(events.NewMessage(func=lambda e: e.media and hasattr(e.media, 'document') and e.file and e.file.name and e.file.name.endswith('.zip')))
    async def load_sess(e):
        if e.sender_id != OWNER_ID: return
        
        msg = await e.reply("⏳ Loading...")
        zp = await e.download_media()
        os.makedirs("sessions", exist_ok=True)
        temp_dir = "temp_sessions"
        os.makedirs(temp_dir, exist_ok=True)
        
        with zipfile.ZipFile(zp, 'r') as z: z.extractall(temp_dir)
        
        existing_hashes = {hashlib.md5(open(os.path.join("sessions", f), 'rb').read()).hexdigest(): f 
                          for f in os.listdir("sessions") if f.endswith(".session")}
        
        loaded = skipped = failed = 0
        new_files = [f for f in os.listdir(temp_dir) if f.endswith(".session")]
        next_id = max(sessions.keys()) + 1 if sessions else 1
        
        for sf in new_files:
            temp_path = os.path.join(temp_dir, sf)
            fh = hashlib.md5(open(temp_path, 'rb').read()).hexdigest()
            
            if fh in existing_hashes:
                skipped += 1
                continue
            
            shutil.move(temp_path, os.path.join("sessions", sf))
            sp = f"sessions/{sf.replace('.session', '')}"
            c = TelegramClient(sp, API_ID, API_HASH)
            await c.connect()
            
            if await c.is_user_authorized():
                sessions[next_id] = c
                loaded += 1
                next_id += 1
            else:
                await c.disconnect()
                failed += 1
        
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        os.remove(zp)
        
        await msg.edit(f"✅ Loaded {loaded}\n⏭️ Skipped {skipped}\n❌ Failed {failed}\n📊 Total: {len(sessions)}")

    @client.on(events.CallbackQuery(pattern=b"owner_panel"))
    async def owner_panel_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        approved_count = await users_col.count_documents({"approved": True})
        btns = [[Button.inline("👥 Approved", b"owner_approved")],
                [Button.inline("📦 Sessions", b"owner_sessions")],
                [Button.inline("🗑️ Clear", b"owner_clear")],
                [Button.inline("🏠 Back", b"home")]]
        await e.edit(f"🔐 **Panel**\n\n✅ Sessions: {len(sessions)}\n✅ Approved: {approved_count}", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"owner_approved"))
    async def owner_approved_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        users = await users_col.find({"approved": True}).to_list(100)
        if not users: return await e.answer("❌ None", alert=True)
        txt = f"✅ **Approved ({len(users)}):**\n\n" + "\n".join([f"• [{u.get('name', 'Unknown')}](tg://user?id={u['user_id']})" for u in users])
        await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"owner_panel")]], parse_mode='markdown')

    @client.on(events.CallbackQuery(pattern=b"owner_sessions"))
    async def owner_sessions_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        txt = f"📦 **Sessions**\n\n✅ Total: {len(sessions)}\n\n" + ("\n".join([f"Session {k}" for k in sorted(sessions.keys())]) if sessions else "None")
        await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"owner_clear"))
    async def owner_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        btns = [[Button.inline("✅ Yes", b"confirm_clear")], [Button.inline("❌ No", b"owner_panel")]]
        await e.edit("⚠️ **Clear ALL sessions?**\n\nCannot be undone.", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"confirm_clear"))
    async def confirm_clear_cb(e):
        if e.sender_id != OWNER_ID: return await e.answer("❌ Owner Only", alert=True)
        for s in sessions.values():
            try: await s.disconnect()
            except: pass
        sessions.clear()
        entity_cache.clear()
        if os.path.exists("sessions"): shutil.rmtree("sessions")
        os.makedirs("sessions", exist_ok=True)
        await e.edit("✅ Cleared", buttons=[[Button.inline("🏠 Back", b"owner_panel")]])

    @client.on(events.CallbackQuery(pattern=b"help"))
    async def help_cb(e):
        btns = [[Button.url("➕ Add", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                [Button.inline("🏠 Home", b"home"), Button.url("👤 Owner", "https://t.me/dustbydust")]]
        await e.edit("📚 **ʜᴇʟᴩ**\n\n**๏ ᴜsᴇʀ:**\n  ❂ `approve <id>` - ᴀᴩᴩʀᴏᴠᴇ\n  ❂ `remove <id>` - ʀᴇᴍᴏᴠᴇ\n  ❂ `approved` - ʟɪsᴛ\n\n**๏ sᴇssɪᴏɴ:**\n  ❂ sᴇɴᴅ .ᴢɪᴩ - ʟᴏᴀᴅ\n  ❂ `clearsessions` - ᴄʟᴇᴀʀ\n\n**๏ ᴀᴄᴛɪᴏɴs:**\n  ❂ `join <s|all> <chat>`\n  ❂ `leave <s|all> <chat>`\n  ❂ `getip <s|all> <chat>`", buttons=btns)

    @client.on(events.CallbackQuery(pattern=b"home"))
    async def home_cb(e):
        user = await e.get_sender()
        btns = [[Button.url("➕ Add", f"https://t.me/{(await client.get_me()).username}?startgroup=true")],
                [Button.inline("📚 Help", b"help"), Button.url("👤 Owner", "https://t.me/dustbydust")]]
        if e.sender_id == OWNER_ID: btns.append([Button.inline("🔐 Panel", b"owner_panel")])
        await e.edit(f"нєу [{user.first_name}](tg://user?id={user.id})!\n\n๏ ᴛʜɪs ɪs [{(await client.get_me()).first_name}](tg://user?id={(await client.get_me()).id})!\n\n➻ ᴀ ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴛᴇʟᴇɢʀᴀᴍ ɪᴩ ᴇxᴛʀᴀᴄᴛᴏʀ ʙᴏᴛ.\n──────────────────\n๏ ᴄʟɪᴄᴋ ʜᴇʟᴩ ғᴏʀ ᴄᴏᴍᴍᴀɴᴅs.", buttons=btns, parse_mode='markdown')

async def load_existing_sessions():
    if not os.path.exists("sessions"):
        os.makedirs("sessions", exist_ok=True)
        return 0
    
    loaded = 0
    session_files = [f for f in os.listdir("sessions") if f.endswith(".session")]
    if not session_files: return 0
    
    next_id = 1
    for sf in session_files:
        try:
            sp = f"sessions/{sf.replace('.session', '')}"
            c = TelegramClient(sp, API_ID, API_HASH)
            await c.connect()
            if await c.is_user_authorized():
                sessions[next_id] = c
                loaded += 1
                next_id += 1
            else:
                await c.disconnect()
        except: pass
    
    return loaded

async def main():
    global bot, mongo_client, db, users_col
    log("🚀 Starting...")
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client['bot_db']
    users_col = db['users']
    await users_col.create_index("user_id", unique=True)
    log("✅ MongoDB")
    
    await load_existing_sessions()
    log(f"✅ Loaded {len(sessions)} sessions")
    
    bot = TelegramClient('bot_session', API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    log(f"✅ Bot: @{(await bot.get_me()).username}")
    await setup_handlers(bot)
    log("✅ Running...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
