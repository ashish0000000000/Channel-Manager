import os
import logging
import asyncpg
import re
import httpx
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

# -- Proxy config (optional) --------------------------------------------------
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

_TELEGRAM_DOMAINS = ("t.me", "telegram.me", "telegram.dog", "t.dog", "telegra.ph")


def _is_external_url(url: str) -> bool:
    url = url.lower().strip()
    return bool(url) and not any(d in url for d in _TELEGRAM_DOMAINS)


def contains_external_link(message) -> bool:
    for entities in filter(None, [message.entities, message.caption_entities]):
        for ent in entities:
            if ent.type == "url":
                text = message.text or message.caption or ""
                url = text[ent.offset: ent.offset + ent.length]
                if _is_external_url(url):
                    return True
            elif ent.type == "text_link":
                if _is_external_url(ent.url or ""):
                    return True
    return False


def is_poster(message) -> bool:
    if not (message.photo or message.video):
        return False
    return contains_external_link(message)


def has_blacklisted_words(text: str) -> bool:
    if not text:
        return False
    return bool(BLACKLIST_REGEX.search(text))


def should_force_delete(message) -> bool:
    if message.voice:
        return True
    if message.document:
        return True
    if contains_external_link(message):
        return True
    return False


def _cyrillic_ratio(text: str) -> float:
    """Fraction of characters that are Cyrillic (safe-mode homoglyphs)."""
    if not text:
        return 0.0
    return sum(1 for c in text if 'Ѐ' <= c <= 'ӿ') / len(text)


def is_likely_safe_mode_resent_poster(message, stored_poster_text: str = "") -> bool:
    """
    True if this photo/video looks like a safe-mode re-sent version of the
    tracked poster (URL stripped, Latin chars replaced with Cyrillic homoglyphs).

    Reliable indicators only — no broad length-ratio guessing which causes
    false positives and breaks poster tracking.
    """
    if not (message.photo or message.video):
        return False
    if contains_external_link(message):
        return False   # still has a URL entity -> real poster, not a re-send

    caption = message.caption or ""

    # Indicator 1: Cyrillic homoglyphs in caption (safe-mode signature)
    if caption and _cyrillic_ratio(caption) > 0.05:
        return True

    # Indicator 2: Caption is completely blank and stored poster also had blank caption
    # (some forwarding bots strip everything including caption in safe mode)
    if not caption and not stored_poster_text:
        return True

    return False


def is_likely_safe_mode_resent_next(message, stored_text: str) -> bool:
    """
    True if this message looks like a safe-mode re-sent version of the stored
    next_msg (same content, Cyrillic homoglyphs replacing Latin chars).
    Used to update next_msg_id when the original spam is deleted + resent.
    """
    if not stored_text:
        return False
    new_text = message.text or message.caption or ""
    if not new_text:
        return False
    # Must have Cyrillic homoglyphs — that's safe-mode's signature
    if _cyrillic_ratio(new_text) < 0.05:
        return False
    # Similar content length confirms it's the same message resent
    ratio = len(new_text) / max(len(stored_text), 1)
    return 0.6 <= ratio <= 1.6


# ================= PROXY AUTO-DETECT =================

def _can_reach_telegram_direct(timeout: float = 6.0) -> bool:
    """
    Return True if api.telegram.org is reachable without a proxy.
    Uses a synchronous httpx call so it runs before the event loop starts.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            client.get("https://api.telegram.org")
            return True
    except Exception:
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
            # ----------------------------------------------------------------
            # NEW POSTER ARRIVED
            # ----------------------------------------------------------------
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, "
                "next_msg_text, next_msg_force_delete "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row and row["poster_msg_id"]:
                old_poster_id         = row["poster_msg_id"]
                next_msg_id           = row["next_msg_id"]
                next_msg_text         = row["next_msg_text"] or ""
                next_msg_force_delete = row["next_msg_force_delete"] or False

                # Always delete old poster
                try:
                    await context.bot.delete_message(
                        chat_id=channel_id, message_id=old_poster_id
                    )
                    logger.info("Deleted old poster (ch=%s, msg=%s)", channel_id, old_poster_id)
                except BadRequest as e:
                    logger.warning("Old poster already gone (msg=%s): %s", old_poster_id, e)
                except Exception as e:
                    logger.error("Could not delete old poster (msg=%s): %s", old_poster_id, e)

                # Delete msg below old poster if it meets any condition
                blacklisted  = has_blacklisted_words(next_msg_text)
                force_delete = next_msg_force_delete

                if next_msg_id and (blacklisted or force_delete):
                    reason = (
                        "blacklisted+force" if blacklisted and force_delete
                        else "blacklisted" if blacklisted
                        else "voice/doc/link"
                    )
                    try:
                        await context.bot.delete_message(
                            chat_id=channel_id, message_id=next_msg_id
                        )
                        logger.info(
                            "Deleted next_msg (ch=%s, msg=%s, reason=%s)",
                            channel_id, next_msg_id, reason
                        )
                    except BadRequest as e:
                        logger.warning("next_msg already gone (msg=%s): %s", next_msg_id, e)
                    except Exception as e:
                        logger.error("Could not delete next_msg (msg=%s): %s", next_msg_id, e)
                elif next_msg_id:
                    logger.info(
                        "next_msg kept — clean (ch=%s, msg=%s)", channel_id, next_msg_id
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

            logger.info("New poster tracked (ch=%s, msg=%s)", channel_id, msg_id)

        else:
            # ----------------------------------------------------------------
            # NON-POSTER MESSAGE
            # ----------------------------------------------------------------
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, next_msg_text "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if not (row and row["poster_msg_id"]):
                return  # No poster tracked yet — nothing to do

            stored_poster_text = row["poster_text"] or ""
            stored_next_id     = row["next_msg_id"]
            stored_next_text   = row["next_msg_text"] or ""

            # -- Case 1: safe-mode re-sent poster (Cyrillic, no URL) ---------
            # The forwarding bot deleted the original poster and resent it
            # with Cyrillic homoglyphs so URL entities are gone.
            # We update poster_msg_id to keep tracking correct.
            if is_likely_safe_mode_resent_poster(message, stored_poster_text):
                await conn.execute("""
                    UPDATE tracked_msgs
                    SET poster_msg_id=$2,
                        next_msg_id=NULL, next_msg_text=NULL, next_msg_force_delete=FALSE
                    WHERE channel_id=$1
                """, channel_id, msg_id)
                logger.info(
                    "Safe-mode poster resent — updated tracker "
                    "(ch=%s, old_id=%s -> new_id=%s)",
                    channel_id, row["poster_msg_id"], msg_id
                )
                return

            # -- Case 2: safe-mode re-sent next_msg (Cyrillic, next_msg set) -
            # The spam below the poster was deleted+resent with Cyrillic.
            # Update next_msg_id so we delete the resent version later.
            if stored_next_id and is_likely_safe_mode_resent_next(message, stored_next_text):
                text         = (message.text or message.caption or "")[:500]
                force_delete = should_force_delete(message)
                await conn.execute("""
                    UPDATE tracked_msgs
                    SET next_msg_id=$2, next_msg_text=$3, next_msg_force_delete=$4
                    WHERE channel_id=$1
                """, channel_id, msg_id, text, force_delete)
                logger.info(
                    "Safe-mode next_msg resent — updated tracker "
                    "(ch=%s, old_id=%s -> new_id=%s)",
                    channel_id, stored_next_id, msg_id
                )
                return

            # -- Case 3: first message below the poster ----------------------
            if not stored_next_id:
                text         = (message.text or message.caption or "")[:500]
                force_delete = should_force_delete(message)
                await conn.execute("""
                    UPDATE tracked_msgs
                    SET next_msg_id=$2, next_msg_text=$3, next_msg_force_delete=$4
                    WHERE channel_id=$1
                """, channel_id, msg_id, text, force_delete)
                logger.info(
                    "Stored next_msg (ch=%s, msg=%s, force_delete=%s)",
                    channel_id, msg_id, force_delete
                )

# ================= ENTRY POINT =================

def main():
    # -- Auto-proxy detection -------------------------------------------------
    # If proxy env vars are set, first check if Telegram is reachable directly.
    # If yes (e.g. server in India where Telegram is accessible), skip proxy
    # to avoid unnecessary latency routing through a foreign proxy server.
    effective_proxy = _proxy_url
    if _proxy_url:
        logger.info("Proxy configured (%s). Checking direct connectivity...", _proxy_url)
        if _can_reach_telegram_direct():
            logger.info("Telegram reachable directly — skipping proxy for lower latency.")
            effective_proxy = None
        else:
            logger.info("Telegram not reachable directly — using proxy.")
    else:
        logger.info("No proxy configured — connecting directly.")

    builder = Application.builder().token(BOT_TOKEN).post_init(init_postgres)
    if effective_proxy:
        builder = (
            builder
            .request(HTTPXRequest(proxy=effective_proxy))
            .get_updates_request(HTTPXRequest(proxy=effective_proxy))
        )

    application = builder.build()
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )
    logger.info("Bot started successfully.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
