import os
import logging
import asyncpg
from html import escape
import re
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("CRITICAL: BOT_TOKEN environment variable is missing.")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing.")

LOG_CHAT_ID = int(os.environ.get("LOG_CHAT_ID", "0"))
if not LOG_CHAT_ID:
    raise ValueError("LOG_CHAT_ID environment variable is missing.")

db_pool = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= BLACKLIST =================

BLACKLIST = [
    "casino", "stakeid", "stake", "bharosa", "punters", "service",
    "download", "bonus", "circle", "red", "bet",
    "exclusive", "platform", "registed",
    "khelo", "safe", "betting", "book", "Guranteed", "apk", "aviator", "Cricket", "Luck"
]

BLACKLIST_REGEX = re.compile(
    r'\b(?:' + '|'.join(re.escape(w) for w in BLACKLIST) + r')\b',
    re.IGNORECASE
)

TOSS_REGEX = re.compile(r'toss winner', re.IGNORECASE)

# ================= DATABASE =================

async def init_postgres(application: Application):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        # Create table if it doesn't exist at all
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_msgs (
                channel_id       BIGINT PRIMARY KEY,
                poster_msg_id    BIGINT,
                candidate_id     BIGINT,
                candidate_text   TEXT
            );
        """)

        # Auto-migration: add missing columns if table existed with old schema
        for column, definition in [
            ("poster_msg_id",  "BIGINT"),
            ("candidate_id",   "BIGINT"),
            ("candidate_text", "TEXT"),
        ]:
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, column)
            if not exists:
                await conn.execute(
                    f"ALTER TABLE tracked_msgs ADD COLUMN {column} {definition};"
                )
                logger.info("Migration: added column '%s' to tracked_msgs", column)

        # Old schema had msg_id — migrate its data into poster_msg_id then drop it
        old_col_exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='msg_id'
        """)
        if old_col_exists:
            await conn.execute("""
                UPDATE tracked_msgs SET poster_msg_id = msg_id WHERE poster_msg_id IS NULL;
            """)
            await conn.execute("ALTER TABLE tracked_msgs DROP COLUMN msg_id;")
            logger.info("Migration: moved msg_id -> poster_msg_id and dropped old column")

        # Drop channel_state table if it exists from old versions
        await conn.execute("DROP TABLE IF EXISTS channel_state;")

    logger.info("PostgreSQL connected and tables ready.")

# ================= HELPERS =================

def is_poster(message) -> bool:
    """
    Only treat as poster if it has photo/video AND a link.
    No link = not a poster, nothing gets tracked or deleted.
    """
    has_media = bool(message.photo or message.video)
    if not has_media:
        return False
    return contains_link(message)

def is_spam_text(text: str) -> bool:
    """
    Check spam from stored text. Called only when a NEW poster arrives.
    Spam is NEVER checked on the message after a new poster — only after old poster.
    """
    if not text:
        return False
    if text in ("[APK_FILE]", "[AUDIO_SPAM]", "[LINK_SPAM]"):
        return True
    return bool(BLACKLIST_REGEX.search(text))


def extract_candidate_text(message) -> str | None:
    """
    Extract spam fingerprint from message to store as candidate.
    Returns None if message is clearly not spam (don't store).
    """
    if message.document and message.document.file_name:
        if message.document.file_name.lower().endswith('.apk'):
            return "[APK_FILE]"

    if (message.audio or message.voice) and message.caption:
        return "[AUDIO_SPAM]"

    if contains_link(message):
        return "[LINK_SPAM]"

    text = message.text or message.caption or ""
    if text and BLACKLIST_REGEX.search(text):
        return text

    return None


def contains_link(message) -> bool:
    entities = message.caption_entities if message.caption else message.entities
    if entities and any(ent.type in ['url', 'text_link'] for ent in entities):
        return True
    text = message.text or message.caption or ""
    if "http://" in text.lower() or "https://" in text.lower() or "t.me" in text.lower():
        return True
    return False

# ================= TOSS FINISH =================

async def trigger_toss_finish(context, channel_id, reply_id, original_text):
    try:
        await context.bot.delete_message(chat_id=channel_id, message_id=reply_id)
    except Exception as e:
        logger.warning("Could not delete toss reply (msg_id=%s): %s", reply_id, e)

    safe_text = escape(original_text)
    final_message = (
        f"<b>{safe_text}</b>"
        "<b> Loss ❌</b>\n\n"
        "<b>As I Said Toss Normal Limit Se Hi Khelna Hota Hai</b>\n\n"
        "<b>10% Amount Hi Loss Hua Hai Overall Hum Same Limit Se Play Krte He Hai "
        "Toh Profit Me Nikalte He Hai.</b>\n\n"
        "<b>Baaki Session Me Cover Krte Hai...❤️</b>"
    )
    try:
        await context.bot.send_message(
            chat_id=channel_id,
            text=final_message,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Failed to send toss finish message: %s", e)

# ================= TOSS DELETION CHECK =================

async def check_single_toss(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    channel_id = data["channel_id"]
    original_id = data["original_id"]
    reply_id = data["reply_id"]
    original_text = data["original_text"]

    try:
        temp = await context.bot.copy_message(
            chat_id=LOG_CHAT_ID,
            from_chat_id=channel_id,
            message_id=original_id
        )
        await context.bot.delete_message(chat_id=LOG_CHAT_ID, message_id=temp.message_id)
        logger.info("Toss message still exists (channel=%s, msg=%s)", channel_id, original_id)

    except BadRequest as e:
        error_text = str(e).lower()
        if any(x in error_text for x in ["not found", "message_id_invalid", "message to copy not found"]):
            logger.info("Toss deleted — sending loss message (channel=%s)", channel_id)
            await trigger_toss_finish(context, channel_id, reply_id, original_text)

    except Exception as e:
        logger.error("Unexpected error in check_single_toss: %s", e)

# ================= MAIN HANDLER =================

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    channel_id = message.chat_id
    msg_id = message.message_id
    text = message.text or message.caption or ""

    # ---------- TOSS CHECK ----------
    if TOSS_REGEX.search(text):
        reply_text = (
            "<b>Always Play Toss In Small Limits</b>\n\n"
            "<b>Agr ID Me 10K Hai Toh Toss 1K Se Khelo Only...👆</b>"
        )
        try:
            reply_msg = await message.reply_text(reply_text, parse_mode=ParseMode.HTML)
            context.job_queue.run_once(
                check_single_toss,
                when=20,
                data={
                    "channel_id": channel_id,
                    "original_id": msg_id,
                    "reply_id": reply_msg.message_id,
                    "original_text": text
                }
            )
        except Exception as e:
            logger.error("Failed to handle toss message: %s", e)
        return

    # ---------- MODERATION ----------
    if not db_pool:
        logger.error("Database pool not initialized.")
        return

    msg_is_poster = is_poster(message)

    async with db_pool.acquire() as conn:

        if msg_is_poster:
            row = await conn.fetchrow(
                "SELECT poster_msg_id, candidate_id, candidate_text FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row:
                old_poster_id = row["poster_msg_id"]
                candidate_id  = row["candidate_id"]
                candidate_text = row["candidate_text"]

                # ── Step 1: Delete spam from OLD poster's window ──
                # This is the ONLY place spam gets deleted.
                # candidate was stored when a message arrived at old_poster_id+1.
                # We check it NOW (on new poster arrival), NOT when it originally arrived.
                # So the new poster's next message is NEVER touched.
                if (
                    candidate_id
                    and old_poster_id
                    and candidate_id == old_poster_id + 1
                    and is_spam_text(candidate_text)
                ):
                    try:
                        await context.bot.delete_message(
                            chat_id=channel_id,
                            message_id=candidate_id
                        )
                        logger.info("Deleted spam (channel=%s, msg=%s)", channel_id, candidate_id)
                    except BadRequest as e:
                        logger.warning("Spam already gone (msg=%s): %s", candidate_id, e)
                    except Exception as e:
                        logger.error("Could not delete spam (msg=%s): %s", candidate_id, e)

                # ── Step 2: Delete the OLD poster ──
                try:
                    await context.bot.delete_message(
                        chat_id=channel_id,
                        message_id=old_poster_id
                    )
                    logger.info("Deleted old poster (channel=%s, msg=%s)", channel_id, old_poster_id)
                except BadRequest as e:
                    logger.warning("Old poster already gone (msg=%s): %s", old_poster_id, e)
                except Exception as e:
                    logger.error("Could not delete old poster (msg=%s): %s", old_poster_id, e)

            # ── Step 3: Store new poster, clear candidate window ──
            await conn.execute("""
                INSERT INTO tracked_msgs(channel_id, poster_msg_id, candidate_id, candidate_text)
                VALUES($1, $2, NULL, NULL)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id  = EXCLUDED.poster_msg_id,
                    candidate_id   = NULL,
                    candidate_text = NULL
            """, channel_id, msg_id)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        else:
            # ── Store as spam candidate if it's the immediate next message after poster ──
            # We NEVER delete here. Only record. Decision is made when next poster arrives.
            row = await conn.fetchrow(
                "SELECT poster_msg_id, candidate_id FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if (
                row
                and row["poster_msg_id"]
                and msg_id == row["poster_msg_id"] + 1
                and not row["candidate_id"]
            ):
                candidate_text = extract_candidate_text(message)
                if candidate_text:
                    await conn.execute("""
                        UPDATE tracked_msgs
                        SET candidate_id=$2, candidate_text=$3
                        WHERE channel_id=$1
                    """, channel_id, msg_id, candidate_text)
                    logger.info(
                        "Stored spam candidate (channel=%s, msg=%s)",
                        channel_id, msg_id
                    )

# ================= ENTRY POINT =================

def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(init_postgres)
        .build()
    )
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )
    logger.info("Bot started successfully.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
