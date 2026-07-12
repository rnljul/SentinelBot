import html
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telegram import Message, Update, User
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
LOGGER = logging.getLogger("media_guard_bot")


ADMIN_STATUSES = {"administrator", "creator", "owner"}
GROUP_TYPES = {"group", "supergroup"}
DURATION_PATTERN = re.compile(r"^(?P<amount>\d+)(?P<unit>[mhd])$")
DEFAULT_RESTRICTION_DURATION = "4h"
DEFAULT_RESTRICTION_REASON = "irrelevant content"


@dataclass(frozen=True)
class Restriction:
    chat_id: int
    user_id: int
    until_ts: int
    reason: str
    display_name: str
    restricted_by: int
    created_ts: int


@dataclass(frozen=True)
class KnownUser:
    chat_id: int
    user_id: int
    username: str
    display_name: str
    last_seen_ts: int


class RestrictionStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_restrictions (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    until_ts INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    restricted_by INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS known_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    username_key TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    last_seen_ts INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_known_users_username
                ON known_users (chat_id, username_key)
                """
            )

    def restrict(
        self,
        chat_id: int,
        user_id: int,
        until_ts: int,
        reason: str,
        display_name: str,
        restricted_by: int,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_restrictions (
                    chat_id, user_id, until_ts, reason, display_name, restricted_by, created_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    until_ts = excluded.until_ts,
                    reason = excluded.reason,
                    display_name = excluded.display_name,
                    restricted_by = excluded.restricted_by,
                    created_ts = excluded.created_ts
                """,
                (chat_id, user_id, until_ts, reason, display_name, restricted_by, now),
            )

    def remove(self, chat_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM media_restrictions WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            return cursor.rowcount > 0

    def get(self, chat_id: int, user_id: int) -> Optional[Restriction]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT chat_id, user_id, until_ts, reason, display_name, restricted_by, created_ts
                FROM media_restrictions
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()

        if row is None:
            return None

        restriction = Restriction(**dict(row))
        if restriction.until_ts <= int(time.time()):
            self.remove(chat_id, user_id)
            return None

        return restriction

    def list_active(self, chat_id: int) -> list[Restriction]:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM media_restrictions WHERE chat_id = ? AND until_ts <= ?",
                (chat_id, now),
            )
            rows = connection.execute(
                """
                SELECT chat_id, user_id, until_ts, reason, display_name, restricted_by, created_ts
                FROM media_restrictions
                WHERE chat_id = ?
                ORDER BY until_ts ASC
                """,
                (chat_id,),
            ).fetchall()
        return [Restriction(**dict(row)) for row in rows]

    def remember_user(self, chat_id: int, user: User) -> None:
        if user.is_bot:
            return

        now = int(time.time())
        username = user.username or ""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO known_users (
                    chat_id, user_id, username, username_key, display_name, last_seen_ts
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    username_key = excluded.username_key,
                    display_name = excluded.display_name,
                    last_seen_ts = excluded.last_seen_ts
                """,
                (chat_id, user.id, username, username.lower(), user.full_name, now),
            )

    def get_known_user_by_username(self, chat_id: int, username: str) -> Optional[KnownUser]:
        username_key = username.lower().lstrip("@")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT chat_id, user_id, username, display_name, last_seen_ts
                FROM known_users
                WHERE chat_id = ? AND username_key = ?
                """,
                (chat_id, username_key),
            ).fetchone()

        return KnownUser(**dict(row)) if row else None

    def search_known_users(self, chat_id: int, query: str, limit: int = 10) -> list[KnownUser]:
        query_key = f"%{query.lower().lstrip('@')}%"
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, user_id, username, display_name, last_seen_ts
                FROM known_users
                WHERE chat_id = ?
                  AND (username_key LIKE ? OR lower(display_name) LIKE ? OR CAST(user_id AS TEXT) = ?)
                ORDER BY last_seen_ts DESC
                LIMIT ?
                """,
                (chat_id, query_key, query_key, query, limit),
            ).fetchall()

        return [KnownUser(**dict(row)) for row in rows]


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_duration(value: str) -> int:
    match = DURATION_PATTERN.fullmatch(value.lower().strip())
    if not match:
        raise ValueError("Use a duration like 30m, 4h, or 2d.")

    amount = int(match.group("amount"))
    unit = match.group("unit")
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")

    multiplier = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}[unit]
    return amount * multiplier


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    days, seconds = divmod(seconds, 24 * 60 * 60)
    hours, seconds = divmod(seconds, 60 * 60)
    minutes, _ = divmod(seconds, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def mention_user(user_id: int, display_name: str) -> str:
    safe_name = html.escape(display_name or str(user_id))
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def display_known_user(user: KnownUser) -> str:
    username = f"@{html.escape(user.username)}" if user.username else "no username"
    return f"{mention_user(user.user_id, user.display_name)} ({username}, id: <code>{user.user_id}</code>)"


def target_from_reply(message: Message) -> Optional[User]:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    return None


def is_image_or_video(message: Message) -> bool:
    if message.photo or message.video or message.animation or message.video_note:
        return True

    document = message.document
    if document and document.mime_type:
        return document.mime_type.startswith(("image/", "video/"))

    return False


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError as exc:
        LOGGER.warning("Could not check admin status: %s", exc)
        return False

    return member.status in ADMIN_STATUSES


async def is_admin_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError as exc:
        LOGGER.warning("Could not check admin status in %s: %s", chat_id, exc)
        return False

    return member.status in ADMIN_STATUSES


def configured_moderation_chat_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    return context.bot_data.get("moderation_chat_id")


async def require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return False

    if chat.type not in GROUP_TYPES:
        await message.reply_text("Use this bot in a group or supergroup.")
        return False

    if not await is_admin(update, context):
        await message.reply_text("Only chat admins can use this command.")
        return False

    return True


async def get_moderation_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None:
        return None

    if chat.type in GROUP_TYPES:
        if not await require_group_admin(update, context):
            return None
        return chat.id

    if chat.type != "private":
        await message.reply_text("Use this command in a group or in a private chat with the bot.")
        return None

    moderation_chat_id = configured_moderation_chat_id(context)
    if moderation_chat_id is None:
        await message.reply_text(
            "Private admin commands need MODERATION_CHAT_ID in .env. "
            "Run /chat_id in the target group, then set that value."
        )
        return None

    if not await is_admin_in_chat(context, moderation_chat_id, user.id):
        await message.reply_text("Only admins of the configured moderation group can use this command.")
        return None

    return moderation_chat_id


def resolve_target(chat_id: int, store: RestrictionStore, target_text: str) -> tuple[int, str]:
    if target_text.startswith("@"):
        known_user = store.get_known_user_by_username(chat_id, target_text)
        if known_user is None:
            raise ValueError(
                f"I do not know {target_text} yet. Use /find_user {target_text} or ask them "
                "to send one message in the group first."
            )
        return known_user.user_id, known_user.display_name

    try:
        user_id = int(target_text)
    except ValueError as exc:
        raise ValueError("Use a reply, @username that the bot has seen, or a numeric user ID.") from exc

    return user_id, str(user_id)


def parse_target_and_duration(
    message: Message,
    args: list[str],
    chat_id: int,
    store: RestrictionStore,
) -> tuple[int, str, int, str]:
    target = target_from_reply(message)
    if target:
        if not args:
            raise ValueError("Reply with /restrict_media 4h [reason].")
        duration_seconds = parse_duration(args[0])
        reason = " ".join(args[1:]).strip()
        return target.id, target.full_name, duration_seconds, reason

    if len(args) < 2:
        if not args:
            raise ValueError(
                "Reply to a user's message with /restrict_media 4h [reason], "
                "or use /restrict_media @username [duration] [reason]."
            )
        user_id, display_name = resolve_target(chat_id, store, args[0])
        return (
            user_id,
            display_name,
            parse_duration(DEFAULT_RESTRICTION_DURATION),
            DEFAULT_RESTRICTION_REASON,
        )

    user_id, display_name = resolve_target(chat_id, store, args[0])
    duration_seconds = parse_duration(args[1])
    reason = " ".join(args[2:]).strip() or DEFAULT_RESTRICTION_REASON
    return user_id, display_name, duration_seconds, reason


def parse_unrestrict_target(
    message: Message,
    args: list[str],
    chat_id: int,
    store: RestrictionStore,
) -> tuple[int, str]:
    target = target_from_reply(message)
    if target:
        return target.id, target.full_name

    if not args:
        raise ValueError("Reply with /unrestrict_media, or use /unrestrict_media @username.")

    return resolve_target(chat_id, store, args[0])


async def restrict_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await get_moderation_chat_id(update, context)
    if chat_id is None:
        return

    message = update.effective_message
    admin = update.effective_user
    assert message is not None and admin is not None
    store: RestrictionStore = context.bot_data["store"]

    try:
        user_id, display_name, duration_seconds, reason = parse_target_and_duration(
            message,
            list(context.args),
            chat_id,
            store,
        )
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    until_ts = int(time.time()) + duration_seconds
    store.restrict(
        chat_id=chat_id,
        user_id=user_id,
        until_ts=until_ts,
        reason=reason,
        display_name=display_name,
        restricted_by=admin.id,
    )

    await message.reply_html(
        f"{mention_user(user_id, display_name)} cannot post images or videos for "
        f"{format_duration(duration_seconds)}."
        + (f"\nReason: {html.escape(reason)}" if reason else "")
    )


async def unrestrict_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await get_moderation_chat_id(update, context)
    if chat_id is None:
        return

    message = update.effective_message
    assert message is not None
    store: RestrictionStore = context.bot_data["store"]

    try:
        user_id, display_name = parse_unrestrict_target(message, list(context.args), chat_id, store)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    removed = store.remove(chat_id, user_id)
    if removed:
        await message.reply_html(f"{mention_user(user_id, display_name)} can post images and videos again.")
    else:
        await message.reply_text("That user is not currently media-restricted in this chat.")


async def list_restrictions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await get_moderation_chat_id(update, context)
    if chat_id is None:
        return

    message = update.effective_message
    assert message is not None

    store: RestrictionStore = context.bot_data["store"]
    restrictions = store.list_active(chat_id)
    if not restrictions:
        await message.reply_text("No active media restrictions in this chat.")
        return

    now = int(time.time())
    lines = ["Active media restrictions:"]
    for restriction in restrictions:
        remaining = format_duration(restriction.until_ts - now)
        name = mention_user(restriction.user_id, restriction.display_name)
        reason = f" - {html.escape(restriction.reason)}" if restriction.reason else ""
        lines.append(f"- {name}: {remaining} remaining{reason}")

    await message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def find_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = await get_moderation_chat_id(update, context)
    if chat_id is None:
        return

    message = update.effective_message
    assert message is not None

    query = " ".join(context.args).strip()
    if not query:
        await message.reply_text("Use /find_user username_or_name.")
        return

    store: RestrictionStore = context.bot_data["store"]
    matches = store.search_known_users(chat_id, query)
    if not matches:
        await message.reply_text("No known users found. The bot learns users after they post in the group.")
        return

    lines = ["Known users:"]
    lines.extend(f"- {display_known_user(match)}" for match in matches)
    await message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if chat.type not in GROUP_TYPES:
        await message.reply_text("Use /chat_id in the group you want to moderate.")
        return

    if not await require_group_admin(update, context):
        return

    await message.reply_html(f"Set this in .env:\n<code>MODERATION_CHAT_ID={chat.id}</code>")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    store: RestrictionStore = context.bot_data["store"]
    if chat.type in GROUP_TYPES:
        store.remember_user(chat.id, user)

    if chat.type not in GROUP_TYPES or not is_image_or_video(message):
        return

    restriction = store.get(chat.id, user.id)
    if restriction is None:
        return

    remaining = restriction.until_ts - int(time.time())
    reason = f"\nReason: {html.escape(restriction.reason)}" if restriction.reason else ""
    notice = (
        f"{mention_user(user.id, user.full_name)}, you are blocked from posting images "
        f"or videos for {format_duration(remaining)} because of the channel rules violation."
        f"{reason}"
    )

    try:
        await message.delete()
    except TelegramError as exc:
        LOGGER.warning("Could not delete restricted media message: %s", exc)
        notice += "\n\nBot could not delete the message. Check bot admin permissions."

    await context.bot.send_message(
        chat_id=chat.id,
        text=notice,
        parse_mode=ParseMode.HTML,
        message_thread_id=message.message_thread_id,
        disable_web_page_preview=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "Media Guard is running.\n\n"
        "Admin commands:\n"
        "/restrict_media 4h [reason] - reply to a user's message\n"
        "/restrict_media @username [duration] [reason] - private chat, defaults to 4h irrelevant content\n"
        "/unrestrict_media - reply to a user's message\n"
        "/find_user username_or_name - search known users\n"
        "/chat_id - show the current group id\n"
        "/media_restrictions - list active restrictions"
    )


def main() -> None:
    load_env_file()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot.")

    database_path = os.getenv("DATABASE_PATH", "data/media_restrictions.sqlite3")
    moderation_chat_id = os.getenv("MODERATION_CHAT_ID")
    store = RestrictionStore(database_path)

    application = Application.builder().token(token).build()
    application.bot_data["store"] = store
    application.bot_data["moderation_chat_id"] = int(moderation_chat_id) if moderation_chat_id else None

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("restrict_media", restrict_media))
    application.add_handler(CommandHandler("unrestrict_media", unrestrict_media))
    application.add_handler(CommandHandler("media_restrictions", list_restrictions))
    application.add_handler(CommandHandler("find_user", find_user))
    application.add_handler(CommandHandler("chat_id", chat_id))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    LOGGER.info("Starting media guard bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
