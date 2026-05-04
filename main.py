import os
import logging
import asyncpg
import re
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is missing.")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing.")

db_pool = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= BLACKLIST =================

BLACKLIST = [
    "casino", "stakeid", "stake", "bharosa", "punters",
    "download", "bonus", "bet",
    "exclusive", "registed",
    "khelo", "betting", "Guranteed", "apk", "aviator", "arrow", "back", "wazirwin",
]

BLACKLIST_REGEX = re.compile(
    r'\b(?:' + '|'.join(re.escape(w) for w in BLACKLIST) + r')\b',
    re.IGNORECASE
)

# ================= DATABASE =================

async def init_postgres(application: Application):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        # Schema: one row per channel.
        # poster_text       — caption of the stored poster (checked for blacklist on next poster)
        # next_msg_id       — message_id of the message right below the poster
        # next_msg_text     — text of that message (checked for blacklist on next poster)
        # next_msg_is_audio — whether that message is an audio with a caption
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_msgs (
                channel_id        BIGINT PRIMARY KEY,
                poster_msg_id     BIGINT,
                poster_text       TEXT,
                next_msg_id       BIGINT,
                next_msg_text     TEXT,
                next_msg_is_audio BOOLEAN DEFAULT FALSE
            );
        """)

        # --- Migrations from older schemas ---

        # Ensure all expected columns exist
        for col, definition in [
            ("poster_text",        "TEXT"),
            ("next_msg_id",        "BIGINT"),
            ("next_msg_text",      "TEXT"),
            ("next_msg_is_audio",  "BOOLEAN DEFAULT FALSE"),
        ]:
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if not exists:
                await conn.execute(f"ALTER TABLE tracked_msgs ADD COLUMN {col} {definition};")
                logger.info("Migration: added column '%s'", col)

        # Ensure poster_msg_id exists (very old schema used msg_id)
        has_poster_col = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='poster_msg_id'
        """)
        if not has_poster_col:
            await conn.execute("ALTER TABLE tracked_msgs ADD COLUMN poster_msg_id BIGINT;")
            logger.info("Migration: added poster_msg_id column")

        # Old schema used msg_id — copy its data then drop it
        has_msg_id = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='msg_id'
        """)
        if has_msg_id:
            await conn.execute("""
                UPDATE tracked_msgs SET poster_msg_id = msg_id WHERE poster_msg_id IS NULL
            """)
            await conn.execute("ALTER TABLE tracked_msgs DROP COLUMN msg_id;")
            logger.info("Migration: moved msg_id -> poster_msg_id")

        # Drop stale single-candidate columns from previous versions
        for col in ("candidate_id", "candidate_text"):
            has_col = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if has_col:
                await conn.execute(f"ALTER TABLE tracked_msgs DROP COLUMN {col};")
                logger.info("Migration: dropped stale column '%s'", col)

        # Drop tables from old versions
        await conn.execute("DROP TABLE IF EXISTS spam_candidates;")
        await conn.execute("DROP TABLE IF EXISTS channel_state;")

    logger.info("PostgreSQL connected and tables ready.")

# ================= HELPERS =================

def is_poster(message) -> bool:
    """A poster is a photo/video message that also contains a link."""
    if not (message.photo or message.video):
        return False
    return contains_link(message)


def contains_link(message) -> bool:
    """True if the message contains a URL or Telegram link (entity or raw text)."""
    for entities in filter(None, [message.entities, message.caption_entities]):
        if any(ent.type in ("url", "text_link") for ent in entities):
            return True
    text = message.text or message.caption or ""
    return any(token in text.lower() for token in ("http://", "https://", "t.me"))


def has_blacklisted_words(text: str) -> bool:
    """True if the text contains any blacklisted word."""
    if not text:
        return False
    return bool(BLACKLIST_REGEX.search(text))


# ================= MAIN HANDLER =================

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    channel_id = message.chat_id
    msg_id     = message.message_id

    if not db_pool:
        logger.error("Database pool not initialized.")
        return

    async with db_pool.acquire() as conn:

        if is_poster(message):
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, next_msg_text, next_msg_is_audio "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row and row["poster_msg_id"]:
                old_poster_id   = row["poster_msg_id"]
                old_poster_text = row["poster_text"] or ""
                next_msg_id     = row["next_msg_id"]
                next_msg_text   = row["next_msg_text"] or ""
                next_msg_is_audio = row["next_msg_is_audio"] or False

                # Delete old poster ONLY IF its caption has blacklisted words.
                if has_blacklisted_words(old_poster_text):
                    try:
                        await context.bot.delete_message(
                            chat_id=channel_id,
                            message_id=old_poster_id
                        )
                        logger.info(
                            "Deleted old poster (channel=%s, msg=%s)", channel_id, old_poster_id
                        )
                    except BadRequest as e:
                        logger.warning(
                            "Old poster already gone (msg=%s): %s", old_poster_id, e
                        )
                    except Exception as e:
                        logger.error(
                            "Could not delete old poster (msg=%s): %s", old_poster_id, e
                        )
                else:
                    logger.info(
                        "Old poster kept — no blacklisted words (channel=%s, msg=%s)",
                        channel_id, old_poster_id
                    )

                # Delete message right below old poster if:
                #   - it has blacklisted words, OR
                #   - it is an audio message with a caption
                should_delete_next = (
                    has_blacklisted_words(next_msg_text) or next_msg_is_audio
                )

                if next_msg_id and should_delete_next:
                    reason = "blacklisted words" if has_blacklisted_words(next_msg_text) else "audio with caption"
                    try:
                        await context.bot.delete_message(
                            chat_id=channel_id,
                            message_id=next_msg_id
                        )
                        logger.info(
                            "Deleted msg below poster (channel=%s, msg=%s, reason=%s)",
                            channel_id, next_msg_id, reason
                        )
                    except BadRequest as e:
                        logger.warning(
                            "Msg below poster already gone (msg=%s): %s", next_msg_id, e
                        )
                    except Exception as e:
                        logger.error(
                            "Could not delete msg below poster (msg=%s): %s", next_msg_id, e
                        )
                elif next_msg_id:
                    logger.info(
                        "Msg below poster kept — no blacklisted words and not audio with caption "
                        "(channel=%s, msg=%s)", channel_id, next_msg_id
                    )

            # Store the new poster (caption stored so we can check it when next poster arrives)
            new_poster_text = (message.caption or message.text or "")[:500]
            await conn.execute("""
                INSERT INTO tracked_msgs(
                    channel_id, poster_msg_id, poster_text,
                    next_msg_id, next_msg_text, next_msg_is_audio
                )
                VALUES($1, $2, $3, NULL, NULL, FALSE)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id     = EXCLUDED.poster_msg_id,
                    poster_text       = EXCLUDED.poster_text,
                    next_msg_id       = NULL,
                    next_msg_text     = NULL,
                    next_msg_is_audio = FALSE
            """, channel_id, msg_id, new_poster_text)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        else:
            # If this is the message right below the current poster, record it.
            # No deletion here — decision is made when the next poster arrives.
            row = await conn.fetchrow(
                "SELECT poster_msg_id, next_msg_id FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )
            if (
                row
                and row["poster_msg_id"]
                and msg_id == row["poster_msg_id"] + 1
                and not row["next_msg_id"]
            ):
                text = (message.text or message.caption or "")[:500]
                is_audio_with_caption = bool(message.audio and message.caption)

                await conn.execute("""
                    UPDATE tracked_msgs
                    SET next_msg_id=$2, next_msg_text=$3, next_msg_is_audio=$4
                    WHERE channel_id=$1
                """, channel_id, msg_id, text, is_audio_with_caption)

                logger.info(
                    "Stored msg below poster (channel=%s, msg=%s, audio_with_caption=%s)",
                    channel_id, msg_id, is_audio_with_caption
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
