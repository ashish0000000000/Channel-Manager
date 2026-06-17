import os
import logging
import asyncpg
import re
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is missing.")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing.")

# ── Proxy config (optional) ──────────────────────────────────────────────────
_proxy_host   = os.environ.get("PROXY_HOST")
_proxy_port   = os.environ.get("PROXY_PORT")
_proxy_user   = os.environ.get("PROXY_USER")
_proxy_pass   = os.environ.get("PROXY_PASS")
_proxy_scheme = os.environ.get("PROXY_SCHEME", "socks5")

_proxy_url = None
if _proxy_host and _proxy_port:
    if _proxy_user and _proxy_pass:
        _proxy_url = f"{_proxy_scheme}://{_proxy_user}:{_proxy_pass}@{_proxy_host}:{_proxy_port}"
    else:
        _proxy_url = f"{_proxy_scheme}://{_proxy_host}:{_proxy_port}"

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
    "khelo", "betting", "Guranteed", "apk", "aviator",
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_msgs (
                channel_id            BIGINT PRIMARY KEY,
                poster_msg_id         BIGINT,
                poster_text           TEXT,
                next_msg_id           BIGINT,
                next_msg_text         TEXT,
                next_msg_force_delete BOOLEAN DEFAULT FALSE
            );
        """)

        # --- Migrations ---
        for col, definition in [
            ("poster_text",           "TEXT"),
            ("next_msg_id",           "BIGINT"),
            ("next_msg_text",         "TEXT"),
            ("next_msg_force_delete", "BOOLEAN DEFAULT FALSE"),
        ]:
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if not exists:
                await conn.execute(f"ALTER TABLE tracked_msgs ADD COLUMN {col} {definition};")
                logger.info("Migration: added column '%s'", col)

        # Rename old next_msg_is_audio -> next_msg_force_delete if it exists
        has_old_flag = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='next_msg_is_audio'
        """)
        if has_old_flag:
            await conn.execute("""
                UPDATE tracked_msgs
                SET next_msg_force_delete = next_msg_is_audio
                WHERE next_msg_force_delete IS NULL OR next_msg_force_delete = FALSE
            """)
            await conn.execute("ALTER TABLE tracked_msgs DROP COLUMN next_msg_is_audio;")
            logger.info("Migration: renamed next_msg_is_audio -> next_msg_force_delete")

        has_poster_col = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='poster_msg_id'
        """)
        if not has_poster_col:
            await conn.execute("ALTER TABLE tracked_msgs ADD COLUMN poster_msg_id BIGINT;")
            logger.info("Migration: added poster_msg_id column")

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

        for col in ("candidate_id", "candidate_text"):
            has_col = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if has_col:
                await conn.execute(f"ALTER TABLE tracked_msgs DROP COLUMN {col};")
                logger.info("Migration: dropped stale column '%s'", col)

        await conn.execute("DROP TABLE IF EXISTS spam_candidates;")
        await conn.execute("DROP TABLE IF EXISTS channel_state;")

    logger.info("PostgreSQL connected and tables ready.")

# ================= HELPERS =================

# Telegram-owned domains — links to these are NOT considered external
_TELEGRAM_DOMAINS = ("t.me", "telegram.me", "telegram.dog", "t.dog", "telegra.ph")


def _is_external_url(url: str) -> bool:
    """Return True if url is a real external link (not a Telegram link)."""
    url = url.lower().strip()
    return bool(url) and not any(d in url for d in _TELEGRAM_DOMAINS)


def contains_external_link(message) -> bool:
    """True if the message contains at least one external (non-Telegram) URL."""
    for entities in filter(None, [message.entities, message.caption_entities]):
        for ent in entities:
            if ent.type == "url":
                text = message.text or message.caption or ""
                url = text[ent.offset : ent.offset + ent.length]
                if _is_external_url(url):
                    return True
            elif ent.type == "text_link":
                if _is_external_url(ent.url or ""):
                    return True
    return False


def is_poster(message) -> bool:
    """
    A poster = photo or video message whose caption contains
    at least one external link (not t.me / telegram.me / telegram.dog).
    """
    if not (message.photo or message.video):
        return False
    return contains_external_link(message)


def has_blacklisted_words(text: str) -> bool:
    """True if the text contains any blacklisted word."""
    if not text:
        return False
    return bool(BLACKLIST_REGEX.search(text))


def should_force_delete(message) -> bool:
    """
    True if the message below a poster must be deleted regardless of text.
    Conditions (OR):
      - voice note
      - any file/document (APK, etc.)
      - contains an external link
    """
    if message.voice:
        return True
    if message.document:
        return True
    if contains_external_link(message):
        return True
    return False


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
                "SELECT poster_msg_id, poster_text, next_msg_id, next_msg_text, next_msg_force_delete "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row and row["poster_msg_id"]:
                old_poster_id         = row["poster_msg_id"]
                next_msg_id           = row["next_msg_id"]
                next_msg_text         = row["next_msg_text"] or ""
                next_msg_force_delete = row["next_msg_force_delete"] or False

                # --- Always delete old poster when a new one arrives ---
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

                # --- Delete msg below old poster ---
                # Delete if ANY ONE of these is true (OR logic):
                #   1. voice note
                #   2. document / APK
                #   3. contains external link
                #   4. contains blacklisted word
                blacklisted  = has_blacklisted_words(next_msg_text)
                force_delete = next_msg_force_delete  # voice / doc / external link (set at storage time)

                if next_msg_id and (blacklisted or force_delete):
                    if blacklisted and force_delete:
                        reason = "blacklisted words + voice/doc/external link"
                    elif blacklisted:
                        reason = "blacklisted words"
                    else:
                        reason = "voice/doc/external link"

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
                        "Msg below poster kept — clean message (channel=%s, msg=%s)",
                        channel_id, next_msg_id
                    )

            # Store the new poster
            new_poster_text = (message.caption or message.text or "")[:500]
            await conn.execute("""
                INSERT INTO tracked_msgs(
                    channel_id, poster_msg_id, poster_text,
                    next_msg_id, next_msg_text, next_msg_force_delete
                )
                VALUES($1, $2, $3, NULL, NULL, FALSE)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id         = EXCLUDED.poster_msg_id,
                    poster_text           = EXCLUDED.poster_text,
                    next_msg_id           = NULL,
                    next_msg_text         = NULL,
                    next_msg_force_delete = FALSE
            """, channel_id, msg_id, new_poster_text)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        else:
            # Record the message right below the current poster.
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
                text         = (message.text or message.caption or "")[:500]
                force_delete = should_force_delete(message)

                await conn.execute("""
                    UPDATE tracked_msgs
                    SET next_msg_id=$2, next_msg_text=$3, next_msg_force_delete=$4
                          WHERE channel_id=$1
                """, channel_id, msg_id, text, force_delete)

                logger.info(
                    "Stored msg below poster (channel=%s, msg=%s, force_delete=%s)",
                    channel_id, msg_id, force_delete
                )

# ================= ENTRY POINT =================

def main():
    builder = Application.builder().token(BOT_TOKEN).post_init(init_postgres)
    if _proxy_url:
        logging.getLogger(__name__).info("🌐 Proxy enabled: %s", _proxy_url)
        # Both request objects must use the proxy:
        # - request()            → regular API calls (sendMessage, deleteMessage, etc.)
        # - get_updates_request() → long-polling getUpdates (must also go through proxy)
        builder = (
            builder
            .request(HTTPXRequest(proxy=_proxy_url))
            .get_updates_request(HTTPXRequest(proxy=_proxy_url))
        )
    else:
        logging.getLogger(__name__).info("🌐 No proxy configured — connecting directly.")
    application = builder.build()
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )
    logger.info("Bot started successfully.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
