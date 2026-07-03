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
        # One row per channel: the currently tracked promo poster.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_msgs (
                channel_id    BIGINT PRIMARY KEY,
                poster_msg_id BIGINT,
                poster_text   TEXT
            );
        """)

        # EVERY qualifying spam message below the current poster.
        # (Multiple rows per channel — fixes the old single-slot design that
        # let intermediate safe-mode resends survive the cleanup.)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS below_msgs (
                channel_id BIGINT NOT NULL,
                msg_id     BIGINT NOT NULL,
                reason     TEXT,
                PRIMARY KEY (channel_id, msg_id)
            );
        """)

        # --- Migrations ---
        for col, definition in [
            ("poster_msg_id", "BIGINT"),
            ("poster_text",   "TEXT"),
        ]:
            exists = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if not exists:
                await conn.execute(f"ALTER TABLE tracked_msgs ADD COLUMN {col} {definition};")
                logger.info("Migration: added column '%s'", col)

        # Move any still-tracked single below-msg into the new table, then
        # drop all legacy columns.
        has_next_id = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name='tracked_msgs' AND column_name='next_msg_id'
        """)
        if has_next_id:
            await conn.execute("""
                INSERT INTO below_msgs(channel_id, msg_id, reason)
                SELECT channel_id, next_msg_id, 'migrated'
                FROM tracked_msgs WHERE next_msg_id IS NOT NULL
                ON CONFLICT DO NOTHING
            """)
            logger.info("Migration: moved legacy next_msg_id values into below_msgs")

        for col in ("next_msg_id", "next_msg_text", "next_msg_force_delete",
                    "next_msg_has_link", "next_msg_is_audio", "msg_id",
                    "candidate_id", "candidate_text"):
            has_col = await conn.fetchval("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name='tracked_msgs' AND column_name=$1
            """, col)
            if has_col:
                await conn.execute(f"ALTER TABLE tracked_msgs DROP COLUMN {col};")
                logger.info("Migration: dropped legacy column '%s'", col)

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


def _is_apk(document) -> bool:
    """True only for real APK files (by filename or mime type) — a normal
    document (PDF, schedule, image file) is NOT deleted on its own."""
    if not document:
        return False
    name = (getattr(document, "file_name", "") or "").lower()
    mime = (getattr(document, "mime_type", "") or "").lower()
    return name.endswith(".apk") or mime == "application/vnd.android.package-archive"


def get_delete_reason(message):
    """
    Returns a human-readable reason string if the message meets ANY of the
    five delete conditions for a message below the poster, else None:

      1. one or more blacklisted words (homoglyph-normalized)
      2. any external (non-Telegram) link
      3. audio file
      4. APK file
      5. voice note WITH a caption

    A message matching NONE of these is never stored and never deleted.
    """
    text = message.text or message.caption or ""
    reasons = []
    if has_blacklisted_words(text):
        reasons.append("blacklist word(s)")
    if contains_external_link(message):
        reasons.append("external link")
    if message.audio:
        reasons.append("audio file")
    if _is_apk(message.document):
        reasons.append("apk file")
    if message.voice and message.caption:
        reasons.append("voice note with caption")
    return " + ".join(reasons) if reasons else None


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


async def _delete_msg(bot, channel_id, msg_id, what, reason=""):
    """Delete one message; downgrade 'already gone' to a warning."""
    try:
        await bot.delete_message(chat_id=channel_id, message_id=msg_id)
        logger.info(
            "Deleted %s (channel=%s, msg=%s%s)",
            what, channel_id, msg_id, f", reason={reason}" if reason else ""
        )
    except BadRequest as e:
        logger.warning("%s already gone (msg=%s): %s", what, msg_id, e)
    except Exception as e:
        logger.error("Could not delete %s (msg=%s): %s", what, msg_id, e)


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
                "SELECT poster_msg_id FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )

            # --- Delete the old poster ---
            if row and row["poster_msg_id"]:
                await _delete_msg(
                    context.bot, channel_id, row["poster_msg_id"], "old poster"
                )

            # --- Delete EVERY recorded qualifying message below the old poster ---
            below = await conn.fetch(
                "SELECT msg_id, reason FROM below_msgs WHERE channel_id=$1 ORDER BY msg_id",
                channel_id
            )
            for b in below:
                await _delete_msg(
                    context.bot, channel_id, b["msg_id"],
                    "msg below poster", b["reason"] or ""
                )
            if below:
                await conn.execute(
                    "DELETE FROM below_msgs WHERE channel_id=$1", channel_id
                )

            # --- Track the new poster ---
            new_poster_text = (message.caption or message.text or "")[:500]
            await conn.execute("""
                INSERT INTO tracked_msgs(channel_id, poster_msg_id, poster_text)
                VALUES($1, $2, $3)
                ON CONFLICT(channel_id) DO UPDATE SET
                    poster_msg_id = EXCLUDED.poster_msg_id,
                    poster_text   = EXCLUDED.poster_text
            """, channel_id, msg_id, new_poster_text)

            logger.info("New poster tracked (channel=%s, msg=%s)", channel_id, msg_id)

        else:
            row = await conn.fetchrow(
                "SELECT poster_msg_id, poster_text FROM tracked_msgs WHERE channel_id=$1",
                channel_id
            )
            if not (row and row["poster_msg_id"]):
                return   # no poster tracked in this channel — never touch anything

            stored_poster_text = row["poster_text"] or ""

            # ── Safe-mode re-sent poster detection ───────────────────────────
            # The safe-mode bot deletes the original poster and re-sends it
            # with Cyrillic homoglyphs (no URL entity). That re-sent message
            # looks like a non-poster photo/video. Move the tracker to it so
            # the next cleanup deletes the message actually in the channel.
            if is_likely_safe_mode_resent(message, stored_poster_text):
                old_poster_id = row["poster_msg_id"]

                # Delete the original poster — the safe-mode bot usually
                # already did; if not, clean it up ourselves.
                await _delete_msg(
                    context.bot, channel_id, old_poster_id,
                    "original poster (safe-mode resend)"
                )

                await conn.execute("""
                    UPDATE tracked_msgs
                    SET poster_msg_id=$2, poster_text=$3
                    WHERE channel_id=$1
                """, channel_id, msg_id, (message.caption or "")[:500])
                logger.info(
                    "Safe-mode re-sent poster detected — tracker updated "
                    "(channel=%s, old_id=%s, new_id=%s)",
                    channel_id, old_poster_id, msg_id
                )
                return

            # ── Message below the poster ─────────────────────────────────────
            # Record EVERY message that meets a delete condition. Innocent
            # messages are never recorded, so they can never be deleted.
            # Safe-mode resends of spam qualify again on arrival and are
            # simply recorded as additional rows — nothing slips through.
            reason = get_delete_reason(message)
            if reason:
                await conn.execute("""
                    INSERT INTO below_msgs(channel_id, msg_id, reason)
                    VALUES($1, $2, $3)
                    ON CONFLICT DO NOTHING
                """, channel_id, msg_id, reason)
                logger.info(
                    "Recorded msg below poster for deletion (channel=%s, msg=%s, reason=%s, text_preview=%r)",
                    channel_id, msg_id, reason,
                    normalize_text(message.text or message.caption or "")[:60]
                )
            else:
                logger.info(
                    "Msg below poster ignored — no delete condition met "
                    "(channel=%s, msg=%s)", channel_id, msg_id
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
