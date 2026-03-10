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
    """Get or create an invite link for a channel."""
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

async def get_channel_info_for_user(user_id: int) -> Dict[str, Any]:
    """Get channel information including membership status, invite links, channel titles, and logos."""
    support_channels = get_support_channels()
    if not support_channels:
        return {
            "is_member": True,
            "channels": [],
            "channel_count": 0,
            "invite_link": None
        }
    
    from telegram import Bot
    
    try:
        bot_token = os.environ.get("TELEGRAM_TOKEN")
        if not bot_token:
            return {
                "is_member": False,
                "channels": [],
                "channel_count": len(support_channels),
                "invite_link": None
            }
        
        bot = Bot(token=bot_token)
        channels_info = []
        is_member = True
        
        for channel in support_channels:
            try:
                try:
                    chat_id = int(channel)
                except ValueError:
                    if channel.startswith('@'):
                        chat_id = channel
                    else:
                        chat_id = f"@{channel}"
                
                # Get chat info and title
                try:
                    chat = await bot.get_chat(chat_id)
                    chat_title = chat.title or format_channel_name(channel)
                    chat_username = getattr(chat, 'username', None)
                    
                    # Get channel photo URL (using our proxy)
                    logo_url = await get_channel_photo_url(bot, channel)
                    
                    # Get or create invite link
                    invite_link = None
                    if chat.invite_link:
                        invite_link = chat.invite_link
                    elif chat_username:
                        invite_link = f"https://t.me/{chat_username}"
                    else:
                        # Try to create one
                        try:
                            invite = await bot.create_chat_invite_link(
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
                    
                    # Update channel info in database
                    channels_collection.update_one(
                        {"channel_id": channel},
                        {"$set": {
                            "title": chat_title,
                            "username": chat_username,
                            "last_updated": datetime.datetime.now()
                        }},
                        upsert=True
                    )
                    
                except Exception as e:
                    logger.error(f"Failed to get chat info for {channel}: {e}")
                    chat_title = format_channel_name(channel)
                    # Generate fallback link
                    if channel.startswith('-100'):
                        invite_link = f"https://t.me/c/{channel[4:]}"
                    elif channel.startswith('@'):
                        invite_link = f"https://t.me/{channel[1:]}"
                    else:
                        invite_link = f"https://t.me/{channel}"
                
                # Check membership with detailed logging
                is_channel_member = False
                try:
                    chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                    logger.info(f"DEBUG get_channel_info: User {user_id} status in {channel}: {chat_member.status}")
                    
                    if chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                        is_channel_member = True
                        logger.info(f"✅ User {user_id} is member of {channel}")
                    else:
                        logger.info(f"❌ User {user_id} is NOT member of {channel}. Status: {chat_member.status}")
                        
                except BadRequest as e:
                    error_msg = str(e).lower()
                    if "user not found" in error_msg:
                        logger.warning(f"User {user_id} not found in {channel}")
                    elif "chat not found" in error_msg:
                        logger.warning(f"Chat {channel} not found")
                    elif "user not participant" in error_msg:
                        logger.info(f"User {user_id} is not participant in {channel}")
                    elif "bot was kicked" in error_msg or "bot is not a member" in error_msg:
                        logger.warning(f"Bot cannot access {channel}")
                    else:
                        logger.error(f"Error checking membership for {channel}: {e}")
                except Exception as e:
                    logger.error(f"Failed to check membership for {channel}: {e}")
                
                if not is_channel_member:
                    is_member = False
                
                # Try to get existing photo URL from database
                if not logo_url:
                    channel_data = channels_collection.find_one({"channel_id": channel})
                    if channel_data and channel_data.get("photo_id"):
                        logo_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/channel_photo/{channel}"
                
                channels_info.append({
                    "channel": channel,
                    "channel_title": chat_title,
                    "invite_link": invite_link,
                    "is_member": is_channel_member,
                    "display_name": chat_title,
                    "username": chat_username if 'chat_username' in locals() else None,
                    "logo_url": logo_url
                })
                
            except Exception as e:
                logger.error(f"Error processing channel {channel}: {e}")
                # Fallback with basic info
                chat_title = format_channel_name(channel)
                
                channels_info.append({
                    "channel": channel,
                    "channel_title": chat_title,
                    "invite_link": f"https://t.me/{channel[1:]}" if channel.startswith('@') else f"https://t.me/c/{channel[4:]}" if channel.startswith('-100') else f"https://t.me/{channel}",
                    "is_member": False,
                    "display_name": chat_title,
                    "logo_url": None
                })
                is_member = False
        
        # Get the first channel's invite link as the primary one
        primary_invite_link = channels_info[0]["invite_link"] if channels_info else None
        
        return {
            "is_member": is_member,
            "channels": channels_info,
            "channel_count": len(support_channels),
            "invite_link": primary_invite_link
        }
        
    except Exception as e:
        logger.error(f"Bot initialization error: {e}")
        # Fallback response
        fallback_channels = []
        for channel in support_channels:
            chat_title = format_channel_name(channel)
            
            fallback_channels.append({
                "channel": channel,
                "channel_title": chat_title,
                "invite_link": f"https://t.me/{channel[1:]}" if channel.startswith('@') else f"https://t.me/c/{channel[4:]}" if channel.startswith('-100') else f"https://t.me/{channel}",
                "is_member": False,
                "display_name": chat_title,
                "logo_url": None
            })
        
        return {
            "is_member": False,
            "channels": fallback_channels,
            "channel_count": len(support_channels),
            "invite_link": fallback_channels[0]["invite_link"] if fallback_channels else None
        }

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    user_id = update.effective_user.id
    
    # Store user
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
    
    # First check channel membership regardless of args
    support_channels = get_support_channels()
    if support_channels:
        logger.info(f"Checking membership for user {user_id} in channels: {support_channels}")
        
        is_member = await check_channel_membership(user_id, context)
        if not is_member:
            # Get channel info and invite links
            channel_info = await get_channel_info_for_user(user_id)
            
            # If there's a protected link argument, include it in callback data
            if context.args:
                encoded_id = context.args[0]
                callback_data = f"check_join_{encoded_id}"
            else:
                callback_data = "check_join"
            
            # Create keyboard with buttons only for channels user hasn't joined
            keyboard = []

            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]

            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}  # blue
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data=callback_data,
                    api_kwargs={'style': 'success'}  # green
                )])
                
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
            else:
                # User has joined all channels - this shouldn't happen but as a fallback
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data=callback_data,
                    api_kwargs={'style': 'success'}
                )])
                
                # Add tutorial and contact buttons
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
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Update message text
            if context.args:
                message_text = (
                    f"🔐 *This is a Protected Link*\n\n"
                    f"Join the channel(s) below first to access this link.\n"
                    f"Then click 'Check Membership' below."
                )
            else:
                message_text = (
                    f"🔐 Join the channel(s) below first to use this bot.\n"
                    "Then click 'Check Membership' below."
                )
            
            # Send message with link preview disabled
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN if context.args else None,
                disable_web_page_preview=True  # Turn off link preview
            )
            return
    
    # User is in all channels or no channels required
    
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
    
    # If no args, show beautiful welcome message
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
    
    # Create keyboard with support channel button
    keyboard = []
    
    support_channels = get_support_channels()
    if support_channels:
        # Get channel info and create individual buttons
        channel_info = await get_channel_info_for_user(user_id)
        
        # Show all channels for promotion (not just unjoined ones)
        for i in range(0, len(channel_info["channels"]), 2):
            row_buttons = []
            for j in range(2):
                if i + j < len(channel_info["channels"]):
                    channel = channel_info["channels"][i + j]
                    button_text = f"🌟 {channel['display_name'][:15]}"  # Limit text length
                    row_buttons.append(InlineKeyboardButton(
                        button_text,
                        url=channel["invite_link"],
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        logger.info(f"User {query.from_user.id} clicked 'Check Membership'")
        
        # Get updated channel info
        channel_info = await get_channel_info_for_user(query.from_user.id)
        
        if channel_info["is_member"]:
            await query.message.edit_text(
                "✅ Verified!\n"
                "You can now use the bot.\n\n"
                "Use /help for commands.",
                disable_web_page_preview=True
            )
        else:
            # Create updated keyboard with only unjoined channels
            keyboard = []
            
            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]
            
            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
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
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.message.edit_text(
                    f"🔐 Join the channel(s) below to use this bot.\n"
                    "Then click 'Check Membership' below.",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                # This shouldn't happen but handle gracefully
                await query.answer("❌ Not joined yet. Please join channel(s) first.", show_alert=True)
    
    elif query.data.startswith("check_join_"):
        # Handle check join for protected links
        encoded_id = query.data.replace("check_join_", "")
        logger.info(f"User {query.from_user.id} checking membership for protected link {encoded_id}")
        
        # Get updated channel info
        channel_info = await get_channel_info_for_user(query.from_user.id)
        
        if channel_info["is_member"]:
            # User has joined, show protected link
            link_data = links_collection.find_one({"_id": encoded_id, "active": True})
            
            if link_data:
                # Updated: Include user_id in the WebApp URL for ad tracking
                web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/verify?token={encoded_id}&user_id={query.from_user.id}"
                
                keyboard = [[InlineKeyboardButton(
                    "🔗 Join Group",
                    web_app=WebAppInfo(url=web_app_url),
                    api_kwargs={'style': 'primary'}
                )]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.message.edit_text(
                    "✅ Verified!\n\n"
                    "You can now access the protected link.",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked", disable_web_page_preview=True)
        else:
            # Create updated keyboard with only unjoined channels
            keyboard = []
            
            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]
            
            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button with the same encoded_id (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data=f"check_join_{encoded_id}",
                    api_kwargs={'style': 'success'}
                )])
                
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
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Update message text to reflect current status
                if encoded_id:
                    message_text = (
                        f"🔐 *This is a Protected Link*\n\n"
                        f"Join the channel(s) below first to access this link.\n"
                        f"Then click 'Check Membership' below."
                    )
                else:
                    message_text = (
                        f"🔐 Join the channel(s) below to use this bot.\n"
                        "Then click 'Check Membership' below."
                    )
                
                await query.message.edit_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN if encoded_id else None,
                    disable_web_page_preview=True
                )
            else:
                await query.answer("❌ Not joined yet. Please join channel(s) first.", show_alert=True)
    
    elif query.data == "create_link":
        await query.message.reply_text(
            "To create a protected link, use:\n\n"
            "`/protect https://t.me/yourchannel`\n\n"
            "Replace with your actual channel link.\n\n"
            "*Tutorial:* https://t.me/team\\_secret\\_tutorial\\_video/5\n"
            "*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled", disable_web_page_preview=True)
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create protected link for ANY Telegram link (group or channel)."""
    # Check channel membership
    support_channels = get_support_channels()
    if support_channels:
        logger.info(f"Checking membership for user {update.effective_user.id} in protect command")
        is_member = await check_channel_membership(update.effective_user.id, context)
        if not is_member:
            # Get channel info and invite links
            channel_info = await get_channel_info_for_user(update.effective_user.id)
            
            # Create keyboard with buttons only for channels user hasn't joined
            keyboard = []

            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]

            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
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
            else:
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
                # Add tutorial and contact buttons
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
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🔐 Join the channel(s) below to use this bot.\n"
                "Then click 'Check Membership' below.",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Usage: `/protect https://t.me/yourchannel`\n\n"
            "This works for:\n"
            "• Channels (public/private)\n"
            "• Groups (public/private)\n"
            "• Supergroups\n\n"
            "*Tutorial:* https://t.me/team\\_secret\\_tutorial\\_video/5\n"
            "*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return

    telegram_link = context.args[0]
    
    # Validate the link (basic check)
    if not telegram_link.startswith("https://t.me/"):
        await update.message.reply_text("❌ Invalid link. Must start with https://t.me/", disable_web_page_preview=True)
        return
    
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    short_id = encoded_id[:8].upper()

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "link_type": "channel" if "/c/" in telegram_link or "/s/" in telegram_link or telegram_link.count('/') == 1 else "group",
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Simple buttons with tutorial and contact
    keyboard = [
        [
            InlineKeyboardButton(
                "📤 Share",
                url=f"https://t.me/share/url?url={protected_link}&text=🔐 Protected Link - Join via secure invitation",
                api_kwargs={'style': 'primary'}  # blue
            ),
            InlineKeyboardButton(
                "❌ Revoke",
                callback_data=f"revoke_{encoded_id}",
                api_kwargs={'style': 'danger'}  # red
            )
        ],
        [
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
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Formatted message with markdown for easy copying
    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔑 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n"
        f"🔗 *Original Link:* `{telegram_link}`\n"
        f"📝 *Type:* {'Channel' if 'channel' in telegram_link else 'Group'}\n"
        f"⏰ *Created:* {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"🔐 *Your Protected Link:*\n"
        f"`{protected_link}`\n\n"
        f"📋 *Quick Actions:*\n"
        f"• Copy the link above\n"
        f"• Share with your audience\n"
        f"• Revoke anytime with `/revoke {short_id}`\n\n"
        f"💡 *Need Help?*\n"
        f"• *Tutorial:* https://t.me/team\\_secret\\_tutorial\\_video/5\n"
        f"• *Contact:* https://t.me/team\\_secret\\_cont\\_bot",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a link."""
    # Check channel membership
    support_channels = get_support_channels()
    if support_channels:
        is_member = await check_channel_membership(update.effective_user.id, context)
        if not is_member:
            # Get channel info and invite links
            channel_info = await get_channel_info_for_user(update.effective_user.id)
            
            # Create keyboard with buttons only for channels user hasn't joined
            keyboard = []

            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]

            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
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
            else:
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
                # Add tutorial and contact buttons
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
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🔐 Join the channel(s) below to use this bot.\n"
                "Then click 'Check Membership' below.",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            return
    
    if not context.args:
        # Show user's active links
        user_id = update.effective_user.id
        active_links = list(links_collection.find(
            {"created_by": user_id, "active": True},
            sort=[("created_at", -1)],
            limit=10
        ))
        
        if not active_links:
            await update.message.reply_text("📭 No active links", disable_web_page_preview=True)
            return
        
        message = "🔐 *Your Active Links:*\n\n"
        keyboard = []
        
        for link in active_links:
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
            
            message += f"• `{short_id}` - {clicks} clicks - {created}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Revoke {short_id}",
                callback_data=f"revoke_{link['_id']}",
                api_kwargs={'style': 'danger'}  # red
            )])
        
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
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message += "\nClick a button below to revoke."
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    # Revoke by ID
    link_id = context.args[0].upper()
    
    # Find link
    query = {
        "$or": [
            {"short_id": link_id},
            {"_id": link_id}
        ],
        "created_by": update.effective_user.id,
        "active": True
    }
    
    link_data = links_collection.find_one(query)
    
    if not link_data:
        await update.message.reply_text("❌ Link not found", disable_web_page_preview=True)
        return
    
    # Revoke
    links_collection.update_one(
        {"_id": link_data['_id']},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await update.message.reply_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
        f"⚠️ All future access attempts will be blocked.\n\n"
        f"*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle revoke button."""
    query = update.callback_query
    await query.answer()
    
    link_data = links_collection.find_one({"_id": link_id, "active": True})
    
    if not link_data:
        await query.message.edit_text(
            "❌ Link not found or already revoked.",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    if link_data['created_by'] != query.from_user.id:
        await query.message.edit_text(
            "❌ You don't have permission to revoke this link.",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    # Revoke
    links_collection.update_one(
        {"_id": link_id},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await query.message.edit_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"👥 Final Clicks: {link_data.get('clicks', 0)}\n\n"
        f"⚠️ All access has been permanently blocked.\n\n"
        f"*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin broadcast."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.\n\n"
            "*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 *Broadcast System*\n\n"
            "To broadcast a message:\n"
            "1. Send any message\n"
            "2. Reply to it with `/broadcast`\n"
            "3. Confirm the action\n\n"
            "✨ *Features:*\n"
            "• Supports all media types\n"
            "• Preserves formatting\n"
            "• Tracks delivery\n"
            "• No rate limiting\n\n"
            "*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    total_users = users_collection.count_documents({})
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirm Broadcast",
                callback_data="confirm_broadcast",
                api_kwargs={'style': 'success'}  # green
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="cancel_broadcast",
                api_kwargs={'style': 'danger'}  # red
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Safely get content_type with default fallback
    content_type = getattr(update.message.reply_to_message, 'content_type', 'text')
    
    await update.message.reply_text(
        f"⚠️ *Broadcast Confirmation*\n\n"
        f"📊 *Delivery Stats:*\n"
        f"• 📨 Recipients: `{total_users}` users\n"
        f"• 📝 Type: {content_type}\n"
        f"• ⚡ Delivery: Instant\n\n"
        f"Are you sure you want to proceed?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 *Broadcasting...*\n\nPlease wait, this may take a moment.", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    
    users = list(users_collection.find({}))
    total_users = len(users)
    successful = 0
    failed = 0
    
    message_to_broadcast = context.user_data.get('broadcast_message')
    
    for user in users:
        try:
            await message_to_broadcast.copy(chat_id=user['user_id'])
            successful += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed: {user['user_id']}: {e}")
            failed += 1
    
    broadcast_collection.insert_one({
        "admin_id": query.from_user.id,
        "date": datetime.datetime.now(),
        "total_users": total_users,
        "successful": successful,
        "failed": failed
    })
    
    success_rate = (successful / total_users * 100) if total_users > 0 else 0
    
    await query.message.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📊 *Delivery Report:*\n"
        f"• 📨 Total Recipients: `{total_users}`\n"
        f"• ✅ Successful: `{successful}`\n"
        f"• ❌ Failed: `{failed}`\n"
        f"• 📈 Success Rate: `{success_rate:.1f}%`\n"
        f"• ⏰ Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n\n"
        f"✨ Broadcast logged in system.",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show stats."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.\n\n"
            "*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})
    
    # Calculate total clicks
    total_clicks_result = links_collection.aggregate([
        {"$group": {"_id": None, "total_clicks": {"$sum": "$clicks"}}}
    ])
    total_clicks = 0
    for result in total_clicks_result:
        total_clicks = result.get('total_clicks', 0)
    
    # Get ad statistics
    total_ad_impressions = ad_impressions_collection.count_documents({})
    today_ads = ad_impressions_collection.count_documents({
        "timestamp": {"$gte": today}
    })
    
    await update.message.reply_text(
        f"📊 *System Analytics Dashboard*\n\n"
        f"👥 *User Statistics*\n"
        f"• 📈 Total Users: `{total_users}`\n"
        f"• 🆕 New Today: `{new_users_today}`\n\n"
        f"🔗 *Link Statistics*\n"
        f"• 🔢 Total Links: `{total_links}`\n"
        f"• 🟢 Active Links: `{active_links}`\n"
        f"• 🆕 Created Today: `{new_links_today}`\n"
        f"• 👆 Total Clicks: `{total_clicks}`\n\n"
        f"💰 *Ad Revenue Statistics*\n"
        f"• 📱 Total Ad Impressions: `{total_ad_impressions}`\n"
        f"• 📈 Today's Ads: `{today_ads}`\n\n"
        f"⚙️ *System Status*\n"
        f"• 🗄️ Database: 🟢 Operational\n"
        f"• 🤖 Bot: 🟢 Online\n"
        f"• ⚡ Uptime: 100%\n"
        f"• 🕐 Last Update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    user_id = update.effective_user.id
    
    # Check channel membership
    support_channels = get_support_channels()
    if support_channels:
        logger.info(f"Checking membership for user {user_id} in help command")
        is_member = await check_channel_membership(user_id, context)
        if not is_member:
            # Get channel info and invite links
            channel_info = await get_channel_info_for_user(user_id)
            
            # Create keyboard with buttons only for channels user hasn't joined
            keyboard = []

            # Filter channels to show only those not joined
            not_joined_channels = [ch for ch in channel_info["channels"] if not ch['is_member']]

            if not_joined_channels:
                # Show channels user needs to join (blue primary)
                for i in range(0, len(not_joined_channels), 2):
                    row_buttons = []
                    for j in range(2):
                        if i + j < len(not_joined_channels):
                            channel = not_joined_channels[i + j]
                            button_text = f"📢 {channel['display_name'][:15]}"  # Limit text length
                            row_buttons.append(InlineKeyboardButton(
                                button_text,
                                url=channel["invite_link"],
                                api_kwargs={'style': 'primary'}
                            ))
                    if row_buttons:
                        keyboard.append(row_buttons)
                
                # Add check button (green success)
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
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
            else:
                keyboard.append([InlineKeyboardButton(
                    "✅ Check Membership",
                    callback_data="check_join",
                    api_kwargs={'style': 'success'}
                )])
                
                # Add tutorial and contact buttons
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
                
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🔐 Join the channel(s) below to use this bot.\n"
                "Then click 'Check Membership' below.\n\n"
                f"*Tutorial:* https://t.me/team\\_secret\\_tutorial\\_video/5\n"
                f"*Contact:* https://t.me/team\\_secret\\_cont\\_bot",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
            return
    
    keyboard = []
    
    support_channels = get_support_channels()
    if support_channels:
        # Get channel info and create individual buttons
        channel_info = await get_channel_info_for_user(user_id)
        
        # Show all channels for promotion (not just unjoined ones)
        for i in range(0, len(channel_info["channels"]), 2):
            row_buttons = []
            for j in range(2):
                if i + j < len(channel_info["channels"]):
                    channel = channel_info["channels"][i + j]
                    button_text = f"🌟 {channel['display_name'][:15]}"  # Limit text length
                    row_buttons.append(InlineKeyboardButton(
                        button_text,
                        url=channel["invite_link"],
                        api_kwargs={'style': 'primary'}
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
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await update.message.reply_text(
        "🛡️ *LinkShield Pro - Help Center*\n\n"
        "✨ *What I Can Protect:*\n"
        "• 🔗 Telegram Channels\n"
        "• 👥 Telegram Groups\n"
        "• 🛡️ Private/Public links\n"
        "• 🔒 Supergroups\n\n"
        "📋 *Available Commands:*\n"
        "• `/start` - Start the bot\n"
        "• `/protect https://t.me/channel` - Create secure link\n"
        "• `/revoke` - Revoke access\n"
        "• `/broadcast` - Broadcast message (Admin)\n"
        "• `/stats` - View statistics (Admin)\n"
        "• `/help` - This message\n\n"
        "🔒 *How to Use:*\n"
        "1. Use `/protect https://t.me/yourchannel`\n"
        "2. Share the generated link\n"
        "3. Users join via verification\n"
        "4. Manage with `/revoke`\n\n"
        "💡 *Pro Tips:*\n"
        "• Works with any t.me link\n"
        "• Monitor link analytics\n"
        "• Revoke unused links\n"
        "• Join our support channels\n\n"
        "*Tutorial Video:*\n"
        "https://t.me/team\\_secret\\_tutorial\\_video/5\n\n"
        "*Contact Owner:*\n"
        "https://t.me/team\\_secret\\_cont\\_bot",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store user activity."""
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback handler
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

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
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook: {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"Bot: @{bot_info.username}")
    
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

@app.get("/verify")
async def verify_page(request: Request, token: str, user_id: Optional[int] = None):
    """Verification page with ad support."""
    # Validate token
    link_data = links_collection.find_one({"_id": token, "active": True})
    if not link_data:
        raise HTTPException(status_code=404, detail="Link not found or expired")
    
    # NEW: read ADS_ENABLED environment variable
    ads_enabled = os.environ.get("ADS_ENABLED", "false").lower() == "true"
    
    return templates.TemplateResponse(
        "verify.html", 
        {
            "request": request, 
            "token": token,
            "user_id": user_id,  # Pass user_id to template for tracking
            "ads_enabled": ads_enabled,          # ← added
            "tutorial_link": "https://t.me/team_secret_tutorial_video/5",
            "contact_bot": "https://t.me/team_secret_cont_bot"
        }
    )

@app.post("/track_ad/{user_id}")
async def track_ad_impression(user_id: int, ad_type: str = "inApp"):
    """Track ad impressions for analytics."""
    try:
        ad_impressions_collection.insert_one({
            "user_id": user_id,
            "ad_type": ad_type,
            "timestamp": datetime.datetime.now(),
            "revenue_estimate": 0.01  # Estimated revenue per impression
        })
        
        # Update user's last ad impression time
        users_collection.update_one(
            {"user_id": user_id},
            {"$set": {"last_ad_impression": datetime.datetime.now()}},
            upsert=True
        )
        
        logger.info(f"Ad impression tracked for user {user_id}, type: {ad_type}")
        return {"status": "success", "message": "Ad impression tracked"}
    except Exception as e:
        logger.error(f"Failed to track ad impression: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/check_membership/{token}")
async def check_membership_api(token: str, user_id: int):
    """API to check if user is member of support channels."""
    # First check if token is valid
    link_data = links_collection.find_one({"_id": token, "active": True})
    if not link_data:
        raise HTTPException(status_code=404, detail="Link not found")
    
    # Track that user is checking membership (potential ad view)
    try:
        ad_impressions_collection.insert_one({
            "user_id": user_id,
            "ad_type": "verification_page_view",
            "timestamp": datetime.datetime.now(),
            "page": "verify"
        })
    except Exception as e:
        logger.error(f"Failed to track page view: {e}")
    
    # Get channel membership info WITH CHANNEL TITLES AND LOGOS
    channel_info = await get_channel_info_for_user(user_id)
    
    return {
        "is_member": channel_info["is_member"],
        "channels": channel_info["channels"],  # Now includes channel_title and logo_url
        "channel_count": channel_info["channel_count"],
        "invite_link": channel_info["invite_link"],
        "tutorial_link": "https://t.me/team_secret_tutorial_video/5",
        "contact_bot": "https://t.me/team_secret_cont_bot"
    }

@app.get("/channel_photo/{channel_id}")
async def get_channel_photo(channel_id: str):
    """Proxy endpoint to serve channel photos."""
    try:
        # Get channel data from database
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if not channel_data or not channel_data.get("photo_id"):
            # Return default Telegram logo
            default_url = "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg"
            import requests
            response = requests.get(default_url)
            return StreamingResponse(
                io.BytesIO(response.content),
                media_type="image/svg+xml",
                headers={
                    "Cache-Control": "public, max-age=3600",
                    "Content-Disposition": f"inline; filename=default_channel.svg"
                }
            )
        
        # Get bot instance
        from telegram import Bot
        bot_token = os.environ.get("TELEGRAM_TOKEN")
        bot = Bot(token=bot_token)
        
        # Download the photo
        photo_file = await bot.get_file(channel_data["photo_id"])
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Return as image
        return StreamingResponse(
            io.BytesIO(photo_bytes),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Disposition": f"inline; filename=channel_{channel_id}.jpg"
            }
        )
    except Exception as e:
        logger.error(f"Failed to get channel photo for {channel_id}: {e}")
        # Fallback to default
        default_url = "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg"
        import requests
        response = requests.get(default_url)
        return StreamingResponse(
            io.BytesIO(response.content),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Disposition": "inline; filename=default_channel.svg"
            }
        )

@app.get("/join")
async def join_page(request: Request, token: str, user_id: int):
    """Join page after verification."""
    # Check if token is valid
    link_data = links_collection.find_one({"_id": token, "active": True})
    if not link_data:
        raise HTTPException(status_code=404, detail="Link not found")
    
    # Check membership
    is_member = await verify_user_membership(user_id)
    if not is_member:
        # Track failed join attempt (potential ad revenue lost)
        try:
            ad_impressions_collection.insert_one({
                "user_id": user_id,
                "ad_type": "failed_join_attempt",
                "timestamp": datetime.datetime.now(),
                "reason": "not_member"
            })
        except Exception as e:
            logger.error(f"Failed to track failed join: {e}")
        
        # Redirect to verification page
        raise HTTPException(status_code=303, detail="Not a member of support channels")
    
    # Track successful join attempt (ad will be shown)
    try:
        ad_impressions_collection.insert_one({
            "user_id": user_id,
            "ad_type": "join_page_view",
            "timestamp": datetime.datetime.now(),
            "page": "join"
        })
    except Exception as e:
        logger.error(f"Failed to track join page view: {e}")
    
    return templates.TemplateResponse("join.html", {
        "request": request, 
        "token": token,
        "user_id": user_id,  # Pass user_id for tracking
        "tutorial_link": "https://t.me/team_secret_tutorial_video/5",
        "contact_bot": "https://t.me/team_secret_cont_bot"
    })

@app.get("/getgrouplink/{token}")
async def get_group_link(token: str):
    """Get real group/channel link."""
    link_data = links_collection.find_one({"_id": token, "active": True})
    
    if link_data:
        links_collection.update_one(
            {"_id": token},
            {"$inc": {"clicks": 1}}
        )
        
        # Track successful link access (after ad view)
        return {"url": link_data.get("telegram_link") or link_data.get("group_link")}
    else:
        raise HTTPException(status_code=404, detail="Link not found")

@app.get("/ad_stats")
async def get_ad_stats():
    """Get ad statistics (admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    
    # You would need to implement authentication here
    # For now, return basic stats
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    stats = {
        "total_impressions": ad_impressions_collection.count_documents({}),
        "today_impressions": ad_impressions_collection.count_documents({"timestamp": {"$gte": today}}),
        "impressions_by_type": list(ad_impressions_collection.aggregate([
            {"$group": {"_id": "$ad_type", "count": {"$sum": 1}}}
        ])),
        "estimated_revenue": ad_impressions_collection.count_documents({}) * 0.01,  # $0.01 per impression
        "top_users": list(ad_impressions_collection.aggregate([
            {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ])),
        "contact_bot": "https://t.me/team_secret_cont_bot",
        "tutorial_link": "https://t.me/team_secret_tutorial_video/5"
    }
    
    return stats

@app.get("/")
async def root():
    """Health check."""
    try:
        # Check MongoDB connection
        client.admin.command('ismaster')
        db_status = "🟢 Connected"
    except:
        db_status = "🔴 Disconnected"
    
    # Get basic stats
    total_users = users_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    total_ads = ad_impressions_collection.count_documents({})
    
    return {
        "status": "ok",
        "service": "LinkShield Pro",
        "version": "2.1.0",
        "time": datetime.datetime.now().isoformat(),
        "database": db_status,
        "contact": "https://t.me/team_secret_cont_bot",
        "tutorial": "https://t.me/team_secret_tutorial_video/5",
        "stats": {
            "total_users": total_users,
            "active_links": active_links,
            "total_ad_impressions": total_ads
        }
    }