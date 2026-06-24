import os
import asyncio
import logging
import asyncpg
import re
from telegram import Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is missing.")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing.")

# ── Proxy config (optional, with auto-detect) ─────────────────────────────────
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

# ================= PROXY AUTO-DETECT =================

async def _telegram_reachable_direct() -> bool:
    """Return True if Telegram DC2 is reachable without a proxy (5 s timeout)."""
    import ssl
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                "149.154.167.51", 443,
                ssl=ssl.create_default_context()
            ),
            timeout=5.0,
        )
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _resolve_proxy_url():
    """Return the effective proxy URL (None = connect directly)."""
    if not _proxy_url:
        logger.info("No proxy configured — connecting directly.")
        return None
    logger.info("Proxy configured (%s) — checking direct Telegram connectivity...", _proxy_url)
    if await _telegram_reachable_direct():
        logger.info("Telegram reachable directly — skipping proxy for lower latency.")
        return None
    logger.info("Telegram not reachable directly — using proxy.")
    return _proxy_url

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
                next_msg_force_delete BOOLEAN DEFAULT FALSE,
                last_below_id         BIGINT
            );
        """)

        # --- Migrations ---
        for col, definition in [
            ("poster_text",           "TEXT"),
            ("next_msg_id",           "BIGINT"),
            ("next_msg_text",         "TEXT"),
            ("next_msg_force_delete", "BOOLEAN DEFAULT FALSE"),
            ("last_below_id",         "BIGINT"),
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
                url = text[ent.offset : ent.offset + ent.length]
                if _is_external_url(url):
                    return True
            elif ent.type == "text_link":
                if _is_external_url(ent.url or ""):
                    return True
    return False


def is_poster(message) -> bool:
    """A poster = photo or video with at least one external (non-Telegram) URL."""
    if not (message.photo or message.video):
        return False
    return contains_external_link(message)


def has_blacklisted_words(text: str) -> bool:
    if not text:
        return False
    return bool(BLACKLIST_REGEX.search(text))


def should_force_delete(message) -> bool:
    """
    True if this message must be deleted IMMEDIATELY on arrival (don't wait for next poster).
    Conditions (OR): voice note, document/APK, or external link.
    """
    if message.voice:
        return True
    if message.document:
        return True
    if contains_external_link(message):
        return True
    return False


def is_likely_safe_mode_resent(message, stored_poster_text: str = "") -> bool:
    """
    True if this photo/video is a safe-mode re-sent version of a poster.
    Deliberately strict to avoid false positives:
      - Must be photo or video
      - Must NOT have any external link (real poster still has URL entity)
      - Caption must have >8% Cyrillic characters OR be empty after a known poster
    The loose length-ratio fallback was removed (caused false positives).
    """
    if not (message.photo or message.video):
        return False
    if contains_external_link(message):
        return False

    caption = message.caption or ""

    # Empty-caption photo/video right after a tracked poster → likely safe-mode re-sent
    if not caption and stored_poster_text:
        return True

    # Must have a meaningful share of Cyrillic characters
    if caption:
        cyrillic = sum(1 for c in caption if 'Ѐ' <= c <= 'ӿ')
        if cyrillic / len(caption) > 0.08:
            return True

    return False

# ================= DELETE HELPERS =================

async def _safe_delete(bot, channel_id: int, msg_id: int, label: str = "msg") -> bool:
    """Delete a single message. Returns True on success."""
    try:
        await bot.delete_message(chat_id=channel_id, message_id=msg_id)
        logger.info("Deleted %s (channel=%s, msg=%s)", label, channel_id, msg_id)
        return True
    except (BadRequest, Forbidden) as e:
        err = str(e).lower()
        if "message to delete not found" in err or "message can't be deleted" in err:
            logger.warning("Already gone — %s (channel=%s, msg=%s): %s", label, channel_id, msg_id, e)
        elif "not enough rights" in err or "forbidden" in err or "need administrator" in err or "chat_admin_required" in err:
            logger.error(
                "BOT NEEDS ADMIN RIGHTS in channel=%s — make the bot an administrator "
                "with 'Delete messages' permission.", channel_id
            )
        else:
            logger.warning("Could not delete %s (channel=%s, msg=%s): %s", label, channel_id, msg_id, e)
        return False
    except RetryAfter as e:
        logger.warning("Rate-limited deleting %s — retrying after %ss", label, e.retry_after)
        await asyncio.sleep(e.retry_after + 1)
        return await _safe_delete(bot, channel_id, msg_id, label)
    except TimedOut:
        logger.warning("Timeout deleting %s (channel=%s, msg=%s) — retrying once", label, channel_id, msg_id)
        await asyncio.sleep(2)
        return await _safe_delete(bot, channel_id, msg_id, label)
    except Exception as e:
        logger.error("Unexpected error deleting %s (channel=%s, msg=%s): %s", label, channel_id, msg_id, e)
        return False


async def _safe_delete_range(bot, channel_id: int, first_id: int, last_id: int) -> None:
    """
    Delete all messages with IDs in [first_id, last_id] using deleteMessages
    (up to 100 per call). Telegram silently ignores non-existent IDs.
    """
    if not first_id or not last_id or first_id > last_id:
        return

    # Guard against accidental huge ranges (>500 messages)
    if last_id - first_id > 500:
        logger.warning(
            "Range %s-%s is >500 msgs — clamping to last 500 (channel=%s)",
            first_id, last_id, channel_id
        )
        first_id = last_id - 499

    all_ids = list(range(first_id, last_id + 1))
    for i in range(0, len(all_ids), 100):
        chunk = all_ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id=channel_id, message_ids=chunk)
            logger.info(
                "Bulk-deleted msgs %s-%s (channel=%s, count=%s)",
                chunk[0], chunk[-1], channel_id, len(chunk)
            )
        except (BadRequest, Forbidden) as e:
            err = str(e).lower()
            if "not enough rights" in err or "forbidden" in err or "need administrator" in err or "chat_admin_required" in err:
                logger.error(
                    "BOT NEEDS ADMIN RIGHTS in channel=%s — make the bot an administrator "
                    "with 'Delete messages' permission.", channel_id
                )
                return  # No point retrying each chunk if no rights
            else:
                logger.warning("Bulk delete failed (channel=%s, %s-%s): %s",
                               channel_id, chunk[0], chunk[-1], e)
        except RetryAfter as e:
            logger.warning("Rate-limited on bulk delete — sleeping %ss", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.error("Unexpected error on bulk delete (channel=%s): %s", channel_id, e)


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

        # ── Case 1: New poster arrived ────────────────────────────────────────
        if is_poster(message):
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, last_below_id "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row and row["poster_msg_id"]:
                old_poster_id = row["poster_msg_id"]
                first_below   = row["next_msg_id"]
                last_below    = row["last_below_id"] or first_below

                # Always delete the old poster
                await _safe_delete(context.bot, channel_id, old_poster_id, "old poster")

                # Always delete ALL messages tracked below the old poster
                # (regardless of content — any message between two posters is unwanted)
                if first_below:
                    if first_below == last_below:
                        await _safe_delete(context.bot, channel_id, first_below, "msg below poster")
                    else:
                        await _safe_delete_range(context.bot, channel_id, first_below, last_below)
                else:
                    logger.info(
                        "No below-poster msg tracked for old poster (channel=%s, poster=%s)",
                        channel_id, old_poster_id
                    )

            # Store the new poster
            new_poster_text = (message.caption or message.text or "")[:500]
            await conn.execute("""
                INSERT INTO tracked_msgs(
                    channel_id, poster_msg_id, poster_text,
                    next_msg_id, next_msg_text, next_msg_force_delete, last_below_id
                )
                VALUES($1, $2, $3, NULL, NULL, FALSE, NULL)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id         = EXCLUDED.poster_msg_id,
                    poster_text           = EXCLUDED.poster_text,
                    next_msg_id           = NULL,
                    next_msg_text         = NULL,
                    next_msg_force_delete = FALSE,
                    last_below_id         = NULL
            """, channel_id, msg_id, new_poster_text)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        # ── Case 2: Non-poster message ────────────────────────────────────────
        else:
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, last_below_id "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )
            if not row or not row["poster_msg_id"]:
                return  # No poster tracked yet

            stored_poster_text = row["poster_text"] or ""

            # ── Safe-mode re-sent poster detection ───────────────────────────
            if is_likely_safe_mode_resent(message, stored_poster_text):
                await conn.execute("""
                    UPDATE tracked_msgs
                    SET poster_msg_id=$2, next_msg_id=NULL,
                        next_msg_text=NULL, next_msg_force_delete=FALSE,
                        last_below_id=NULL
                    WHERE channel_id=$1
                """, channel_id, msg_id)
                logger.info(
                    "Safe-mode re-sent poster detected — updated tracker "
                    "(channel=%s, old_poster=%s, new_poster=%s)",
                    channel_id, row["poster_msg_id"], msg_id
                )
                return  # explicit return — do NOT track this as a below-poster msg

            # ── Force-delete: act immediately, don't wait for next poster ────
            if should_force_delete(message):
                await _safe_delete(
                    context.bot, channel_id, msg_id, "force-delete msg after poster"
                )
                # Still extend tracked range so if more msgs appear they're in range
                if not row["next_msg_id"]:
                    text = (message.text or message.caption or "")[:500]
                    await conn.execute("""
                        UPDATE tracked_msgs
                        SET next_msg_id=$2, next_msg_text=$3,
                            next_msg_force_delete=TRUE, last_below_id=$2
                        WHERE channel_id=$1
                    """, channel_id, msg_id, text)
                else:
                    await conn.execute(
                        "UPDATE tracked_msgs SET last_below_id=$2 WHERE channel_id=$1",
                        channel_id, msg_id
                    )
                return

            # ── Regular message below the poster ─────────────────────────────
            # Track first msg as next_msg_id; extend last_below_id on every msg.
            # All of [next_msg_id, last_below_id] are deleted when next poster arrives.
            if not row["next_msg_id"]:
                text = (message.text or message.caption or "")[:500]
                await conn.execute("""
                    UPDATE tracked_msgs
                    SET next_msg_id=$2, next_msg_text=$3,
                        next_msg_force_delete=FALSE, last_below_id=$2
                    WHERE channel_id=$1
                """, channel_id, msg_id, text)
                logger.info(
                    "Stored first msg below poster (channel=%s, msg=%s)", channel_id, msg_id
                )
            else:
                await conn.execute(
                    "UPDATE tracked_msgs SET last_below_id=$2 WHERE channel_id=$1",
                    channel_id, msg_id
                )
                logger.info(
                    "Extended below-poster range to msg=%s (channel=%s)", msg_id, channel_id
                )


# ================= ENTRY POINT =================

def main():
    async def _build_and_run():
        effective_proxy = await _resolve_proxy_url()

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
        await application.run_polling(allowed_updates=Update.ALL_TYPES)

    asyncio.run(_build_and_run())


if __name__ == "__main__":
    main()
