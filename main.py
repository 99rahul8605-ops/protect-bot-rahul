import os
import logging
import uuid
import base64
import asyncio
import datetime
import io
import requests
from typing import Optional, List, Dict, Any
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember, ChatInviteLink
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup (MongoDB) ---
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

# Initialize MongoDB client and select database/collection
client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]
links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]
channels_collection = db["channels"]

# Add ad tracking collection
ad_impressions_collection = db["ad_impressions"]

def init_db():
    """Verifies the MongoDB connection."""
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        
        # Create indexes
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        channels_collection.create_index("channel_id", unique=True)
        # Add index for ad impressions
        ad_impressions_collection.create_index([("user_id", 1), ("timestamp", -1)])
        ad_impressions_collection.create_index("ad_type")
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise

def reset_and_set_commands():
    """Reset and set premium-style bot commands."""
    try:
        bot_token = os.environ.get("TELEGRAM_TOKEN")
        if not bot_token:
            logger.error("❌ TELEGRAM_TOKEN not found in environment")
            return
        
        url = f"https://api.telegram.org/bot{bot_token}/setMyCommands"
        
        # New premium-style commands
        commands = [
            {"command": "start", "description": "🚀 Start the bot"},
            {"command": "protect", "description": "🔗 Create protected link"},
            {"command": "revoke", "description": "❌ Revoke active links"},
            {"command": "broadcast", "description": "📢 Broadcast (Admin)"},
            {"command": "stats", "description": "📊 Statistics (Admin)"},
            {"command": "help", "description": "📖 Show help guide"}
        ]
        
        # Set new commands
        response = requests.post(url, json={"commands": commands})
        
        if response.status_code == 200:
            logger.info("✅ Bot commands updated successfully")
            logger.info(f"✅ Commands set: {[cmd['command'] for cmd in commands]}")
        else:
            logger.error(f"❌ Failed to update commands: {response.text}")
            
    except Exception as e:
        logger.error(f"❌ Error setting bot commands: {e}")

async def get_channel_invite_link(context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> str:
    """Get or create an invite link for a channel. Uses cached link if available."""
    try:
        # Try to get from database first
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if channel_data and channel_data.get("invite_link"):
            # Check if link is still valid (created within last 24 hours)
            if channel_data.get("created_at") and \
               (datetime.datetime.now() - channel_data["created_at"]).days < 1:
                return channel_data["invite_link"]
        
        # Convert channel_id to appropriate format
        try:
            chat_id = int(channel_id)
        except ValueError:
            if channel_id.startswith('@'):
                chat_id = channel_id
            else:
                chat_id = f"@{channel_id}"
        
        # Try to create a new invite link
        try:
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
                name="Bot Access Link",
                expire_date=None,
                member_limit=None
            )
            invite_url = invite_link.invite_link
            
            # Save to database
            channels_collection.update_one(
                {"channel_id": channel_id},
                {"$set": {
                    "invite_link": invite_url,
                    "created_at": datetime.datetime.now(),
                    "last_updated": datetime.datetime.now()
                }},
                upsert=True
            )
            
            logger.info(f"✅ Created new invite link for channel {channel_id}")
            return invite_url
            
        except BadRequest as e:
            logger.warning(f"⚠️ Cannot create invite link (admin rights?): {e}")
            # Fallback: Try to get existing invite link
            try:
                chat = await context.bot.get_chat(chat_id)
                if chat.invite_link:
                    return chat.invite_link
                elif chat.username:
                    return f"https://t.me/{chat.username}"
            except Exception as e2:
                logger.error(f"❌ Failed to get chat info: {e2}")
                
            # If all fails, use t.me format
            if channel_id.startswith('-100'):
                return f"https://t.me/c/{channel_id[4:]}"
            elif channel_id.startswith('@'):
                return f"https://t.me/{channel_id[1:]}"
            else:
                return f"https://t.me/{channel_id}"
                
    except Exception as e:
        logger.error(f"❌ Error getting channel invite link: {e}")
        # Final fallback
        if channel_id.startswith('-100'):
            return f"https://t.me/c/{channel_id[4:]}"
        elif channel_id.startswith('@'):
            return f"https://t.me/{channel_id[1:]}"
        else:
            return f"https://t.me/{channel_id}"

def get_support_channels() -> List[str]:
    """Get list of support channels from environment variable."""
    support_channels_str = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if not support_channels_str:
        # Fallback to single channel for backward compatibility
        single_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
        return [single_channel] if single_channel else []
    
    # Split by comma and strip whitespace
    channels = [ch.strip() for ch in support_channels_str.split(",") if ch.strip()]
    return channels

def format_channel_name(channel_id: str) -> str:
    """Format channel ID for display."""
    if channel_id.startswith('@'):
        return channel_id[1:].replace('_', ' ').title()
    elif channel_id.startswith('-100'):
        # Private channel - try to get name from database or show as "Private Channel"
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if channel_data and channel_data.get("title"):
            return channel_data["title"]
        else:
            return f"Private Channel ({channel_id[-6:]})"
    elif channel_id.startswith('-'):
        # Other private chat
        return f"Chat {channel_id}"
    else:
        return channel_id

async def get_channel_title(bot, channel_id: str) -> str:
    """Get the actual title/name of a channel."""
    try:
        # Convert channel_id to appropriate format
        try:
            chat_id = int(channel_id)
        except ValueError:
            if channel_id.startswith('@'):
                chat_id = channel_id
            else:
                chat_id = f"@{channel_id}"
        
        # Get chat information
        chat = await bot.get_chat(chat_id)
        
        # Return the title
        return chat.title or format_channel_name(channel_id)
    except Exception as e:
        logger.error(f"Failed to get channel title for {channel_id}: {e}")
        return format_channel_name(channel_id)

async def get_channel_invite_links(context: ContextTypes.DEFAULT_TYPE, channels: List[str]) -> List[Dict[str, str]]:
    """Get invite links for multiple channels."""
    channel_links = []
    
    for channel in channels:
        try:
            invite_link = await get_channel_invite_link(context, channel)
            channel_links.append({
                "channel": channel,
                "invite_link": invite_link,
                "display_name": format_channel_name(channel)
            })
        except Exception as e:
            logger.error(f"Failed to get invite link for {channel}: {e}")
            # Add fallback link
            if channel.startswith('-100'):
                fallback_link = f"https://t.me/c/{channel[4:]}"
            elif channel.startswith('@'):
                fallback_link = f"https://t.me/{channel[1:]}"
            else:
                fallback_link = f"https://t.me/{channel}"
            
            channel_links.append({
                "channel": channel,
                "invite_link": fallback_link,
                "display_name": format_channel_name(channel)
            })
    
    return channel_links

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of ALL support channels."""
    support_channels = get_support_channels()
    if not support_channels:
        return True
    
    for channel in support_channels:
        try:
            # Convert channel string to appropriate chat_id format
            if channel.startswith('@'):
                # Public channel with username
                chat_id = channel
            elif channel.startswith('-100'):
                # Private channel/group with ID
                chat_id = int(channel)
            else:
                # Try to handle as username or ID
                try:
                    chat_id = int(channel)
                except ValueError:
                    # Assume it's a username without @
                    chat_id = f"@{channel}"
            
            # Debug: Log what we're checking
            logger.info(f"DEBUG: Checking membership for user {user_id} in channel {channel} (chat_id: {chat_id})")
            
            # Try to get chat member with error handling
            try:
                chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                logger.info(f"DEBUG: User {user_id} status in {channel}: {chat_member.status}")
                
                # Check if user is a member
                if chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                    logger.info(f"✅ User {user_id} is a member of {channel}")
                    continue
                else:
                    logger.info(f"❌ User {user_id} is not a member of {channel}. Status: {chat_member.status}")
                    return False
                    
            except BadRequest as e:
                error_msg = str(e).lower()
                logger.error(f"BadRequest error for channel {channel}: {error_msg}")
                
                if "user not found" in error_msg:
                    logger.warning(f"User {user_id} not found in {channel}. They might have left or been kicked.")
                    return False
                elif "chat not found" in error_msg:
                    logger.warning(f"Chat {channel} not found. Bot may not have access.")
                    return False
                elif "user not participant" in error_msg:
                    logger.info(f"User {user_id} is not a participant in {channel}")
                    return False
                elif "bot was kicked" in error_msg:
                    logger.warning(f"Bot was kicked from {channel}. Cannot check membership.")
                    return False
                elif "bot is not a member" in error_msg:
                    logger.warning(f"Bot is not a member of {channel}. Cannot check membership.")
                    return False
                else:
                    logger.error(f"Unknown BadRequest error for {channel}: {e}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Channel check error for {channel}: {e}")
            return False
    
    logger.info(f"✅ All membership checks passed for user {user_id}")
    return True

async def verify_user_membership(user_id: int) -> bool:
    """Check if user is member of ALL support channels without context."""
    from telegram import Bot
    
    support_channels = get_support_channels()
    if not support_channels:
        return True
    
    try:
        bot_token = os.environ.get("TELEGRAM_TOKEN")
        if not bot_token:
            logger.error("TELEGRAM_TOKEN not found")
            return False
            
        # Create a bot instance
        bot = Bot(token=bot_token)
        
        for channel in support_channels:
            try:
                # Convert channel string to appropriate chat_id format
                if channel.startswith('@'):
                    chat_id = channel
                elif channel.startswith('-100'):
                    chat_id = int(channel)
                else:
                    try:
                        chat_id = int(channel)
                    except ValueError:
                        chat_id = f"@{channel}"
                
                logger.info(f"DEBUG (verify): Checking membership for user {user_id} in channel {channel}")
                
                try:
                    chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                    logger.info(f"DEBUG (verify): User {user_id} status in {channel}: {chat_member.status}")
                    
                    if chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                        logger.info(f"✅ User {user_id} is a member of {channel}")
                        continue
                    else:
                        logger.info(f"❌ User {user_id} is not a member of {channel}. Status: {chat_member.status}")
                        return False
                        
                except BadRequest as e:
                    error_msg = str(e).lower()
                    logger.error(f"BadRequest error for channel {channel}: {error_msg}")
                    
                    if "user not found" in error_msg:
                        logger.warning(f"User {user_id} not found in {channel}")
                        return False
                    elif "chat not found" in error_msg:
                        logger.warning(f"Chat {channel} not found.")
                        return False
                    elif "user not participant" in error_msg:
                        logger.info(f"User {user_id} is not a participant in {channel}")
                        return False
                    elif "bot was kicked" in error_msg:
                        logger.warning(f"Bot was kicked from {channel}")
                        return False
                    elif "bot is not a member" in error_msg:
                        logger.warning(f"Bot is not a member of {channel}")
                        return False
                    else:
                        logger.error(f"Unknown BadRequest error for {channel}: {e}")
                        return False
                        
            except Exception as e:
                logger.error(f"Error processing channel {channel}: {e}")
                return False
                
        logger.info(f"✅ All membership checks passed for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Bot initialization error: {e}")
        return False

async def is_bot_admin(bot, chat_id: str) -> bool:
    """Check if bot is admin in the chat."""
    try:
        me = await bot.get_me()
        chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        is_admin = chat_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        logger.info(f"Bot admin status in {chat_id}: {is_admin}")
        return is_admin
    except Exception as e:
        logger.error(f"Error checking bot admin status in {chat_id}: {e}")
        return False

async def get_channel_photo_url(bot, channel_id: str) -> Optional[str]:
    """Get channel photo and return a proxied URL."""
    try:
        # Check database first
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if channel_data and channel_data.get("photo_id"):
            # Return our proxy URL
            return f"{os.environ.get('RENDER_EXTERNAL_URL')}/channel_photo/{channel_id}"
        
        # Convert channel_id to appropriate format
        try:
            chat_id = int(channel_id)
        except ValueError:
            if channel_id.startswith('@'):
                chat_id = channel_id
            else:
                chat_id = f"@{channel_id}"
        
        # Get chat information
        chat = await bot.get_chat(chat_id)
        
        if chat.photo:
            # Store photo file_id in database
            channels_collection.update_one(
                {"channel_id": channel_id},
                {"$set": {
                    "photo_id": chat.photo.big_file_id,
                    "last_updated": datetime.datetime.now()
                }},
                upsert=True
            )
            
            # Return our proxy URL
            return f"{os.environ.get('RENDER_EXTERNAL_URL')}/channel_photo/{channel_id}"
        
        return None
    except Exception as e:
        logger.error(f"Failed to get channel photo for {channel_id}: {e}")
        return None

async def get_channel_promo_info(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
    """Get channel promotional info (title, invite link) quickly using cached data."""
    support_channels = get_support_channels()
    if not support_channels:
        return []
    
    promo_info = []
    for channel in support_channels:
        # Try to get from database first
        channel_data = channels_collection.find_one({"channel_id": channel})
        title = channel_data.get("title") if channel_data else None
        invite_link = channel_data.get("invite_link") if channel_data else None
        
        # If missing, fetch now (this may be slow but happens only once per channel)
        if not title or not invite_link:
            try:
                # Convert channel_id to appropriate format
                try:
                    chat_id = int(channel)
                except ValueError:
                    if channel.startswith('@'):
                        chat_id = channel
                    else:
                        chat_id = f"@{channel}"
                
                chat = await context.bot.get_chat(chat_id)
                title = chat.title or format_channel_name(channel)
                # Get invite link
                if chat.invite_link:
                    invite_link = chat.invite_link
                elif chat.username:
                    invite_link = f"https://t.me/{chat.username}"
                else:
                    # Try to create one
                    try:
                        invite = await context.bot.create_chat_invite_link(
                            chat_id=chat_id,
                            creates_join_request=True,
                            name="Bot Access Link"
                        )
                        invite_link = invite.invite_link
                    except:
                        if channel.startswith('-100'):
                            invite_link = f"https://t.me/c/{channel[4:]}"
                        elif channel.startswith('@'):
                            invite_link = f"https://t.me/{channel[1:]}"
                        else:
                            invite_link = f"https://t.me/{channel}"
                
                # Save to database for future use
                channels_collection.update_one(
                    {"channel_id": channel},
                    {"$set": {
                        "title": title,
                        "invite_link": invite_link,
                        "last_updated": datetime.datetime.now()
                    }},
                    upsert=True
                )
            except Exception as e:
                logger.error(f"Error fetching channel info for {channel}: {e}")
                title = format_channel_name(channel)
                # Fallback link
                if channel.startswith('-100'):
                    invite_link = f"https://t.me/c/{channel[4:]}"
                elif channel.startswith('@'):
                    invite_link = f"https://t.me/{channel[1:]}"
                else:
                    invite_link = f"https://t.me/{channel}"
        
        promo_info.append({
            "channel": channel,
            "title": title,
            "invite_link": invite_link,
        })
    
    return promo_info

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    user_id = update.effective_user.id
    
    # Store user (fast, no heavy ops)
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )
    
    # Check if this is a protected link (has argument)
    if context.args:
        encoded_id = context.args[0]
        link_data = links_collection.find_one({"_id": encoded_id, "active": True})

        if link_data:
            # Updated: Include user_id in the WebApp URL for ad tracking
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/verify?token={encoded_id}&user_id={update.effective_user.id}"
            
            keyboard = [[InlineKeyboardButton(
                "🔗 Join Group",
                web_app=WebAppInfo(url=web_app_url),
                api_kwargs={'style': 'primary'}  # blue
            )]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔐 This is a Protected Link\n\n"
                "Click the button below to proceed.",
                reply_markup=reply_markup,
                disable_web_page_preview=True  # Turn off link preview
            )
        else:
            await update.message.reply_text("❌ Link expired or revoked", disable_web_page_preview=True)
        return
    
    # If no args, show beautiful welcome message (fast path)
    user_name = update.effective_user.first_name or "User"
    
    # Create the beautiful welcome message
    welcome_msg = """╔──────── ✧ ────────╗
      Welcome {username}
╚──────── ✧ ────────╝

🤖 I am your Link Protection Bot
I help you keep your channel links safe & secure.

🛠 Commands:
• /start – Start the bot
• /protect – Generate protected link
• /help – Show help options

🌟 Features:
• 🔒 Advanced Link Encryption
• 🚀 Instant Link Generation
• 🛡️ Anti-Forward Protection
• 🎯 Easy to use UI""".format(username=user_name)
    
    # Create keyboard with support channel buttons (using cached data)
    keyboard = []
    promo_channels = await get_channel_promo_info(context)
    if promo_channels:
        # Show channels in rows of 2
        for i in range(0, len(promo_channels), 2):
            row_buttons = []
            for j in range(2):
                if i + j < len(promo_channels):
                    ch = promo_channels[i + j]
                    button_text = f"🌟 {ch['title'][:15]}"  # Limit text length
                    row_buttons.append(InlineKeyboardButton(
                        button_text,
                        url=ch["invite_link"],
                        api_kwargs={'style': 'primary'}  # blue
                    ))
            if row_buttons:
                keyboard.append(row_buttons)
    
    # Add tutorial and contact buttons (blue primary)
    keyboard.append([
        InlineKeyboardButton(
            "📺 Tutorial",
            url="https://t.me/team_secret_tutorial_video/5",
            api_kwargs={'style': 'primary'}
        ),
        InlineKeyboardButton(
            "📞 Contact",
            url="https://t.me/team_secret_cont_bot",
            api_kwargs={'style': 'primary'}
        )
    ])
    
    # Add create link button (green success)
    keyboard.append([InlineKeyboardButton(
        "🚀 Create Protected Link",
        callback_data="create_link",
        api_kwargs={'style': 'success'}  # green
    )])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup, disable_web_page_preview=True)

# The rest of the bot handlers (protect, revoke, etc.) remain unchanged, 
# but we'll include them for completeness. (Only start is modified above,
# other functions are as before but we must keep them.)

# ... (Insert all other functions from the original code, like protect_command, revoke_command, etc.)
# Since the user only asked to fix start and uptime, we'll keep them as in the previous version,
# but for brevity I'll skip pasting the entire 1000+ lines again. In the final answer,
# I will provide only the modified sections and the new health endpoint.

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def on_startup():
    """Start bot."""
    logger.info("Starting bot...")
    
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"Missing {var}")
            raise Exception(f"Missing {var}")
    
    init_db()
    
    # Set bot commands on startup
    reset_and_set_commands()
    
    # Pre-fetch channel info to cache it for fast /start responses
    try:
        # We'll use the bot instance to fetch channel info once at startup
        # But we need context.bot; we can do it after bot is initialized
        pass
    except Exception as e:
        logger.error(f"Error pre-fetching channel info: {e}")
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook: {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"Bot: @{bot_info.username}")
    
    # Now pre-fetch channel info for faster future responses
    try:
        # Use a temporary context-like object (just the bot)
        promo_info = await get_channel_promo_info(telegram_bot_app)
        logger.info(f"Pre-fetched {len(promo_info)} support channels info")
    except Exception as e:
        logger.error(f"Error pre-fetching channel info: {e}")
    
    # Test channel link generation and get channel titles
    support_channels = get_support_channels()
    if support_channels:
        logger.info(f"Support channels: {support_channels}")
        for channel in support_channels:
            try:
                invite_link = await get_channel_invite_link(telegram_bot_app, channel)
                # Try to get channel title
                try:
                    if channel.startswith('@'):
                        chat_id = channel
                    else:
                        chat_id = int(channel)
                    
                    chat = await telegram_bot_app.bot.get_chat(chat_id)
                    logger.info(f"Support channel: {chat.title or channel} - Invite: {invite_link}")
                except:
                    logger.info(f"Support channel: {channel} - Invite: {invite_link}")
            except Exception as e:
                logger.error(f"Failed to generate channel link for {channel}: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    """Stop bot."""
    logger.info("Stopping bot...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("Bot stopped")

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Telegram webhook."""
    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")
    
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    
    return Response(status_code=200)

@app.get("/health")
async def health_check():
    """Lightweight health check for uptime monitoring."""
    try:
        # Quick MongoDB ping
        client.admin.command('ismaster')
        return {"status": "ok", "service": "LinkShield Pro", "database": "connected"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Database not available")

@app.get("/")
async def root():
    """Root endpoint with basic stats (lightweight)."""
    try:
        # Use estimated counts for speed
        total_users = users_collection.estimated_document_count()
        active_links = links_collection.count_documents({"active": True})  # filter count
        total_ads = ad_impressions_collection.estimated_document_count()
        
        return {
            "status": "ok",
            "service": "LinkShield Pro",
            "version": "2.1.0",
            "time": datetime.datetime.now().isoformat(),
            "database": "connected",
            "contact": "https://t.me/team_secret_cont_bot",
            "tutorial": "https://t.me/team_secret_tutorial_video/5",
            "stats": {
                "total_users": total_users,
                "active_links": active_links,
                "total_ad_impressions": total_ads
            }
        }
    except Exception as e:
        logger.error(f"Root endpoint error: {e}")
        return {"status": "error", "message": str(e)}

# Include all other endpoints (verify, check_membership, etc.) from the original code.
# They remain unchanged.

# Note: For brevity, I've omitted the full code for other endpoints.
# In a real deployment, you would keep them exactly as before.