import os
import logging
import asyncpg
import re
import difflib
import unicodedata
from urllib.parse import urlparse
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

def _can_reach_telegram_direct(timeout: float = 6.0) -> bool:
    """Return True if api.telegram.org is reachable without a proxy."""
    try:
        import httpx
        with httpx.Client(timeout=timeout) as c:
            c.get("https://api.telegram.org")
        return True
    except Exception:
        return False

_proxy_url_configured = None
if _proxy_host and _proxy_port:
    if _proxy_user and _proxy_pass:
        _proxy_url_configured = (
            f"{_proxy_scheme}://{_proxy_user}:{_proxy_pass}@{_proxy_host}:{_proxy_port}"
        )
    else:
        _proxy_url_configured = f"{_proxy_scheme}://{_proxy_host}:{_proxy_port}"

_proxy_status = "Direct (no proxy configured)"
_proxy_url = None
if _proxy_url_configured:
    if _can_reach_telegram_direct():
        _proxy_url = None
        _proxy_status = "Direct (proxy bypassed -- Telegram reachable from server)"
    else:
        _proxy_url = _proxy_url_configured
        _proxy_status = f"Proxy active ({_proxy_scheme}://{_proxy_host}:{_proxy_port})"

db_pool = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= TEXT NORMALIZATION (anti-homoglyph) =================
#
# "Safe-mode" bots replace Latin characters with visually identical
# Unicode lookalikes (Cyrillic, Greek, Fullwidth) so the text doesn't
# appear in Telegram search — and bypasses naive regex blacklists.
# We normalize every text string BEFORE checking it.

# 1. Invisible / zero-width characters that are silently inserted
_INVISIBLE_RE = re.compile(
    '['
    '­'            # SOFT HYPHEN
    '͏'            # COMBINING GRAPHEME JOINER
    '؜'            # ARABIC LETTER MARK
    'ᅟᅠ'      # HANGUL FILLER
    '឴឵'      # KHMER VOWEL INHERENT AQ / AA
    '᠋-᠍'     # MONGOLIAN FREE VARIATION SELECTORS
    '​-‏'     # ZERO WIDTH SPACE … RIGHT-TO-LEFT MARK
    '‪-‮'     # LTR / RTL embedding controls
    '⁠-⁤'     # WORD JOINER, INVISIBLE PLUS …
    '⁦-⁯'     # DIRECTIONAL ISOLATES, INHIBIT …
    'ㅤ'            # HANGUL FILLER
    '︀-️'     # VARIATION SELECTORS
    '﻿'            # BOM / ZERO WIDTH NO-BREAK SPACE
    ']',
    re.UNICODE
)

# 2. Homoglyph → ASCII translation table
#    Covers the most common Cyrillic, Greek, and Fullwidth lookalikes.
_HOMOGLYPH_TABLE = str.maketrans({
    # ── Cyrillic ──────────────────────────────
    'а': 'a', 'А': 'A',
    'в': 'b', 'В': 'B',
    'с': 'c', 'С': 'C',
    'е': 'e', 'Е': 'E',
    'і': 'i', 'І': 'I',
    'ӏ': 'l',                # Cyrillic palochka
    'о': 'o', 'О': 'O',
    'р': 'p', 'Р': 'P',
    'к': 'k', 'К': 'K',
    'т': 't', 'Т': 'T',
    'м': 'm', 'М': 'M',
    'н': 'h', 'Н': 'H',
    'у': 'y', 'У': 'Y',
    'х': 'x', 'Х': 'X',
    'ѕ': 's', 'Ѕ': 'S',
    'ј': 'j', 'Ј': 'J',
    'ч': '4',
    # ── Greek ─────────────────────────────────
    'α': 'a', 'Α': 'A',
    'β': 'b', 'Β': 'B',
    'ε': 'e', 'Ε': 'E',
    'ζ': 'z', 'Ζ': 'Z',
    'η': 'h', 'Η': 'H',
    'ι': 'i', 'Ι': 'I',
    'κ': 'k', 'Κ': 'K',
    'μ': 'u', 'Μ': 'M',
    'ν': 'v', 'Ν': 'N',
    'ο': 'o', 'Ο': 'O',
    'ρ': 'p', 'Ρ': 'P',
    'τ': 't', 'Τ': 'T',
    'υ': 'u', 'Υ': 'Y',
    'χ': 'x', 'Χ': 'X',
    # ── Fullwidth ASCII (！ … ～) ───────────────
    **{chr(0xFF01 + i): chr(0x21 + i) for i in range(94)},
    # ── Lookalike digits ──────────────────────
    '０': '0', '１': '1', '２': '2', '３': '3', '４': '4',
    '５': '5', '６': '6', '７': '7', '８': '8', '９': '9',
})


def normalize_text(text: str) -> str:
    """
    Convert safe-mode / homoglyph text back to plain ASCII-like text so
    the blacklist regex can match it.

    Steps:
      1. NFKC  — collapses compatibility variants (ﬁ→fi, ² →2, ａ→a, …)
      2. Remove invisible / zero-width characters
      3. Translate Cyrillic / Greek / Fullwidth homoglyphs → ASCII
      4. Lowercase (so the caller doesn't need re.IGNORECASE)
    """
    if not text:
        return text
    text = unicodedata.normalize('NFKC', text)
    text = _INVISIBLE_RE.sub('', text)
    text = text.translate(_HOMOGLYPH_TABLE)
    return text.lower()


# ================= BLACKLIST =================

BLACKLIST = [
    "casino", "stakeid", "stake", "bharosa", "punters",
    "download", "bonus", "bet",
    "exclusive", "registed",
    "khelo", "betting", "guaranteed", "guranteed", "apk", "aviator",
]

# After normalize_text() the input is already lowercase, so IGNORECASE is
# a safety net only (costs nothing but avoids surprises).
BLACKLIST_REGEX = re.compile(
    r'\b(?:' + '|'.join(re.escape(w) for w in BLACKLIST) + r')\b',
    re.IGNORECASE
)

# ================= DATABASE =================

async def init_postgres(application: Application):
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        # Recycle idle connections every 60 s so stale server-side
        # connections (closed by Postgres after hours of inactivity)
        # never silently break the pool.
        max_inactive_connection_lifetime=60.0,
        # Avoid prepared-statement cache conflicts after a reconnect.
        statement_cache_size=0,
        command_timeout=30,
    )

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_msgs (
                channel_id            BIGINT PRIMARY KEY,
                poster_msg_id         BIGINT,
                poster_text           TEXT,
                next_msg_id           BIGINT,
                next_msg_text         TEXT,
                next_msg_force_delete BOOLEAN DEFAULT FALSE,
                next_msg_has_link     BOOLEAN DEFAULT FALSE
            );
        """)

        # --- Migrations ---
        for col, definition in [
            ("poster_text",           "TEXT"),
            ("next_msg_id",           "BIGINT"),
            ("next_msg_text",         "TEXT"),
            ("next_msg_force_delete", "BOOLEAN DEFAULT FALSE"),
            ("next_msg_has_link",     "BOOLEAN DEFAULT FALSE"),
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
_TELEGRAM_DOMAINS = ("t.me", "telegram.me", "telegram.dog", "t.dog", "telegra.ph", "telegram.org")


def _extract_host(url: str) -> str:
    """Return the hostname of a URL ('' if unparseable)."""
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    try:
        return urlparse(u).hostname or ""
    except Exception:
        return ""


def _is_telegram_host(host: str) -> bool:
    """Exact-domain match (incl. subdomains) — substring tricks like
    'nott.me-scam.com' or 'evil.com/t.me' do NOT count as Telegram."""
    return any(host == d or host.endswith("." + d) for d in _TELEGRAM_DOMAINS)


def _is_external_url(url: str) -> bool:
    """Return True if url is a real external link (not a Telegram link)."""
    host = _extract_host(url)
    return bool(host) and not _is_telegram_host(host)


# Plain-text URL fallback — catches obfuscated links that Telegram did NOT
# turn into url entities (e.g. homoglyph 'ｗｗｗ.ｓｉｔｅ.ｃｏｍ' after
# normalization). Kept conservative: scheme / www. / bare domain with a
# common TLD only.
_URL_IN_TEXT_RE = re.compile(
    r'(?:https?://|www\.)\S+'
    r'|\b[a-z0-9][a-z0-9.-]*\.(?:com|net|org|in|io|co|me|app|xyz|site|club|'
    r'online|top|live|win|vip|bet|link|store|pro|fun|cc|dog|ph)\b(?:/\S*)?'
)


def contains_external_link(message) -> bool:
    """True if the message contains at least one external (non-Telegram) URL.

    Checks (a) real URL entities, then (b) URL-looking strings inside the
    NORMALIZED text so homoglyph-obfuscated links are also caught.
    """
    text = message.text or message.caption or ""
    for entities in filter(None, [message.entities, message.caption_entities]):
        for ent in entities:
            if ent.type == "url":
                url = text[ent.offset : ent.offset + ent.length]
                if _is_external_url(url):
                    return True
            elif ent.type == "text_link":
                if _is_external_url(ent.url or ""):
                    return True
    # Fallback: obfuscated / plain-text URLs (safe-mode homoglyph evasion)
    normalized = normalize_text(text)
    for m in _URL_IN_TEXT_RE.finditer(normalized):
        if _is_external_url(m.group(0)):
            return True
    return False


def is_poster(message) -> bool:
    """
    A poster = photo or video message that has ALL of:
      1. A non-empty caption
      2. At least ONE blacklisted word in the caption (homoglyph-normalized)
      3. At least ONE external (non-Telegram) link in the caption

    ALL three must hold — a normal channel photo with a link but no promo
    words, or promo words but no external link, is NOT a poster and is
    never touched.
    """
    if not (message.photo or message.video):
        return False
    if not message.caption:                          # must have a caption
        return False
    if not has_blacklisted_words(message.caption):   # must have promo words
        return False
    return contains_external_link(message)           # must have external link


def has_blacklisted_words(text: str) -> bool:
    """
    True if text contains ANY blacklisted word (case-insensitive).
    Even a single match triggers deletion.

    The text is normalized FIRST to defeat homoglyph / safe-mode evasion:
    Cyrillic 'а' → 'a', invisible chars removed, fullwidth → ASCII, etc.
    """
    if not text:
        return False
    normalized = normalize_text(text)
    matched = bool(BLACKLIST_REGEX.search(normalized))
    if matched:
        logger.debug("Blacklist match in normalized text (raw=%r, norm=%r)", text[:80], normalized[:80])
    return matched


def should_force_delete(message) -> bool:
    """
    Returns True if the message type alone warrants deletion (independent
    of blacklist words or external links).

    Conditions:
      - audio file (message.audio)           — mp3/m4a/etc.
      - any document / APK (message.document)
      - voice note WITH a caption (message.voice + message.caption)
        A plain voice note with no caption is kept; one with a caption
        is almost always a spam promo.
    """
    if message.audio:                               # audio file
        return True
    if message.document:                            # APK or any document
        return True
    if message.voice and message.caption:           # voice note with caption
        return True
    return False


def _is_cyrillic_heavy(s: str) -> bool:
    """True if >8% of chars are Cyrillic — the fingerprint of safe-mode encoding."""
    if not s:
        return False
    return sum(1 for c in s if 'Ѐ' <= c <= 'ӿ') / len(s) > 0.08


def _norm_similarity(a: str, b: str) -> float:
    """Similarity ratio (0..1) between two already-normalized strings."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_likely_safe_mode_resent(message, stored_poster_text: str = "") -> bool:
    """
    True if this photo/video message looks like a safe-mode re-sent version
    of the TRACKED poster (Latin chars replaced with Cyrillic homoglyphs, so
    URL entities are gone but the message is still a photo/video).

    STRICT — ALL must hold:
      1. The normalized caption contains at least one BLACKLISTED word
         (after homoglyph decoding — this is the real promo fingerprint).
      2. The caption is visibly Cyrillic-heavy (>8% Cyrillic chars).
      3. The normalized caption is SIMILAR (>70%) to the tracked poster's
         normalized text — a safe-mode resend is the SAME text re-encoded,
         so similarity is ~1.0. A different spam photo below the poster will
         NOT match and therefore can never delete the active poster early.

    Requiring blacklist + similarity means an innocent Russian/Ukrainian
    channel photo can NEVER trigger this path — Cyrillic alone is not enough.
    """
    if not (message.photo or message.video):
        return False
    if contains_external_link(message):
        return False   # still has a recoverable URL -- handled by the poster path
    caption = message.caption or ""
    if not caption:
        # A photo/video with no caption at all is NOT treated as a safe-mode
        # resend -- it is far more likely to be a regular channel photo.
        return False
    # Must contain promo words once homoglyphs are decoded.
    if not has_blacklisted_words(caption):
        return False
    # And must be visibly homoglyph-encoded (Cyrillic-heavy).
    if not _is_cyrillic_heavy(caption):
        return False
    # And must be the SAME text as the tracked poster (re-encoded).
    if not stored_poster_text:
        return False
    return _norm_similarity(
        normalize_text(caption)[:500],
        normalize_text(stored_poster_text)[:500]
    ) > 0.7


# ================= DB HELPER =================

async def _get_db_conn():
    """
    Acquire a connection from the pool.
    If the pool has gone stale (e.g. after a Postgres restart), reinitialise it
    once and retry so the bot self-heals without needing a manual restart.
    """
    global db_pool
    try:
        return await db_pool.acquire()
    except Exception as first_err:
        logger.warning("DB pool acquire failed (%s) -- reinitialising pool...", first_err)
        try:
            await db_pool.close()
        except Exception:
            pass
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            max_inactive_connection_lifetime=60.0,
            statement_cache_size=0,
            command_timeout=30,
        )
        return await db_pool.acquire()


# ================= MAIN HANDLER =================

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Wrap the entire handler so a single bad message never silently kills
    # processing for all subsequent messages.
    try:
        await _handle_channel_post_inner(update, context)
    except Exception as exc:
        logger.error(
            "Unhandled error in handle_channel_post: %s", exc, exc_info=True
        )


async def _handle_channel_post_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                "SELECT poster_msg_id, poster_text, next_msg_id, next_msg_text, "
                "next_msg_force_delete, next_msg_has_link "
                "FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            if row and row["poster_msg_id"]:
                old_poster_id         = row["poster_msg_id"]
                next_msg_id           = row["next_msg_id"]
                next_msg_text         = row["next_msg_text"] or ""
                next_msg_force_delete = row["next_msg_force_delete"] or False
                next_msg_has_link     = row["next_msg_has_link"] or False

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

                # --- Delete msg below old poster if ANY condition is met (OR logic) ---
                #
                # Condition 1: text/caption contains ANY blacklist word
                # Condition 2: message has an external (non-Telegram) link
                # Condition 3: message is an audio file / document / APK
                # Condition 4: message is a voice note with a caption
                #
                # One match is enough — delete immediately.
                blacklisted  = has_blacklisted_words(next_msg_text)
                force_delete = next_msg_force_delete  # audio file or document/APK

                should_delete = blacklisted or next_msg_has_link or force_delete

                if next_msg_id and should_delete:
                    reasons = []
                    if blacklisted:        reasons.append("blacklist word(s)")
                    if next_msg_has_link:  reasons.append("external link")
                    if force_delete:       reasons.append("audio/apk/voice-with-caption")
                    reason = " + ".join(reasons)

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
                        "Msg below poster kept — no delete condition met "
                        "(blacklisted=%s, has_link=%s, force_delete=%s, channel=%s, msg=%s)",
                        blacklisted, next_msg_has_link, force_delete, channel_id, next_msg_id
                    )

            # Store the new poster
            new_poster_text = (message.caption or message.text or "")[:500]
            await conn.execute("""
                INSERT INTO tracked_msgs(
                    channel_id, poster_msg_id, poster_text,
                    next_msg_id, next_msg_text, next_msg_force_delete,
                    next_msg_has_link
                )
                VALUES($1, $2, $3, NULL, NULL, FALSE, FALSE)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id         = EXCLUDED.poster_msg_id,
                    poster_text           = EXCLUDED.poster_text,
                    next_msg_id           = NULL,
                    next_msg_text         = NULL,
                    next_msg_force_delete = FALSE,
                    next_msg_has_link     = FALSE
            """, channel_id, msg_id, new_poster_text)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        else:
            # Record the message right below the current poster.
            # No deletion here — decision is made when the next poster arrives.
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text, next_msg_id, next_msg_text, "
                "next_msg_force_delete FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )
            if row and row["poster_msg_id"]:
                stored_poster_text = row["poster_text"] or ""

                # ── Safe-mode re-sent poster detection ───────────────────────
                # When the safe-mode bot fires it deletes the original poster
                # and re-sends it with Cyrillic homoglyphs (no URL entity).
                # That re-sent message looks like a non-poster photo/video.
                # We must NOT store it as next_msg — instead update
                # poster_msg_id so the real spam after it is caught.
                if is_likely_safe_mode_resent(message, stored_poster_text):
                    old_poster_id   = row["poster_msg_id"]
                    old_next_msg_id = row["next_msg_id"]

                    # Delete the original poster — the safe-mode bot may have
                    # already deleted it, but if it didn't we must clean it up
                    # ourselves so it doesn't linger in the channel.
                    try:
                        await context.bot.delete_message(
                            chat_id=channel_id, message_id=old_poster_id
                        )
                        logger.info(
                            "Deleted original poster on safe-mode resend "
                            "(channel=%s, msg=%s)", channel_id, old_poster_id
                        )
                    except BadRequest as e:
                        logger.warning(
                            "Original poster already gone on safe-mode resend "
                            "(msg=%s): %s", old_poster_id, e
                        )
                    except Exception as e:
                        logger.error(
                            "Could not delete original poster (msg=%s): %s",
                            old_poster_id, e
                        )

                    # Also delete any stored below-message (it's now orphaned).
                    if old_next_msg_id:
                        try:
                            await context.bot.delete_message(
                                chat_id=channel_id, message_id=old_next_msg_id
                            )
                            logger.info(
                                "Deleted orphaned below-msg on safe-mode resend "
                                "(channel=%s, msg=%s)", channel_id, old_next_msg_id
                            )
                        except Exception:
                            pass

                    await conn.execute("""
                        UPDATE tracked_msgs
                        SET poster_msg_id=$2, next_msg_id=NULL,
                            next_msg_text=NULL, next_msg_force_delete=FALSE,
                            next_msg_has_link=FALSE
                        WHERE channel_id=$1
                    """, channel_id, msg_id)
                    logger.info(
                        "Safe-mode re-sent poster detected — updated tracker "
                        "(channel=%s, old_id=%s, new_id=%s)",
                        channel_id, old_poster_id, msg_id
                    )

                # ── Regular message below the poster ─────────────────────────
                # Accept any non-poster message that arrives after the poster
                # while no next_msg is stored yet.
                elif not row["next_msg_id"]:
                    raw_text     = (message.text or message.caption or "")
                    # Normalize before storing so the blacklist check at
                    # deletion time works even if text used homoglyphs.
                    text         = normalize_text(raw_text)[:500]
                    force_delete = should_force_delete(message)
                    has_link     = contains_external_link(message)

                    await conn.execute("""
                        UPDATE tracked_msgs
                        SET next_msg_id=$2, next_msg_text=$3,
                            next_msg_force_delete=$4, next_msg_has_link=$5
                        WHERE channel_id=$1
                    """, channel_id, msg_id, text, force_delete, has_link)

                    logger.info(
                        "Stored msg below poster (channel=%s, msg=%s, force_delete=%s, text_preview=%r)",
                        channel_id, msg_id, force_delete, text[:60]
                    )

                # ── Safe-mode resend of the BELOW message ─────────────────────
                # The safe-mode bot deletes every admin message and re-sends it
                # homoglyph-encoded. If we already stored the ORIGINAL below-msg,
                # its resend arrives while the slot is filled and would be
                # ignored — the stored msg_id points at an already-deleted
                # message, so the resend would survive the next cleanup.
                #
                # We overwrite the slot ONLY when the new message:
                #   (a) itself meets a delete condition (blacklist word,
                #       external link, or apk/audio/voice-with-caption), AND
                #   (b) carries a resend fingerprint: Cyrillic-heavy text, OR
                #       >70% similar to the stored below-msg text, OR same
                #       force-delete media type as the stored one.
                # (a) guarantees an innocent message is never tracked here.
                else:
                    raw  = message.text or message.caption or ""
                    norm = normalize_text(raw)[:500]
                    force_delete = should_force_delete(message)
                    has_link     = contains_external_link(message)
                    qualifies = (
                        bool(BLACKLIST_REGEX.search(norm))
                        or has_link
                        or force_delete
                    )
                    fingerprint = (
                        _is_cyrillic_heavy(raw)
                        or _norm_similarity(norm, row["next_msg_text"] or "") > 0.7
                        or (force_delete and (row["next_msg_force_delete"] or False))
                    )
                    if qualifies and fingerprint:
                        await conn.execute("""
                            UPDATE tracked_msgs
                            SET next_msg_id=$2, next_msg_text=$3,
                                next_msg_force_delete=$4, next_msg_has_link=$5
                            WHERE channel_id=$1
                        """, channel_id, msg_id, norm, force_delete, has_link)
                        logger.info(
                            "Below-msg safe-mode resend — slot updated "
                            "(channel=%s, old_id=%s, new_id=%s)",
                            channel_id, row["next_msg_id"], msg_id
                        )

# ================= ENTRY POINT =================

def main():
    builder = Application.builder().token(BOT_TOKEN).post_init(init_postgres)
    logger.info("Proxy status: %s", _proxy_status)
    if _proxy_url:
        builder = (
            builder
            .request(HTTPXRequest(proxy=_proxy_url))
            .get_updates_request(HTTPXRequest(proxy=_proxy_url))
        )
    application = builder.build()
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL, handle_channel_post)
    )
    logger.info("Bot started successfully.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        # Never throw away updates that arrived while the bot was restarting.
        drop_pending_updates=False,
        # Reconnect quickly if the long-poll connection drops.
        poll_interval=0.0,
        timeout=30,
    )

if __name__ == "__main__":
    main()
