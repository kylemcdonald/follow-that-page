import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from google import genai

from botlib import (
    DEFAULT_JOBS_PATH,
    DEFAULT_OFFSET_PATH,
    add_job,
    answer_callback_query,
    delete_job,
    get_updates,
    load_jobs,
    load_offset,
    monitor_jobs_once,
    resolve_telegram_config,
    save_offset,
    send_telegram_message,
    sleep_with_backoff,
    validate_follow_target,
)


CHECK_INTERVAL_SECONDS = 300


def parse_check_interval() -> int:
    import os

    raw = os.getenv("CHECK_INTERVAL_SECONDS", str(CHECK_INTERVAL_SECONDS)).strip()
    return max(30, int(raw))


def is_authorized_actor(actor: Dict[str, Any], expected_username: str) -> bool:
    username = (actor.get("username") or "").lstrip("@")
    return username == expected_username


def is_authorized_chat(chat: Dict[str, Any], expected_chat_id: str) -> bool:
    return str(chat.get("id")) == expected_chat_id


def is_authorized_message(
    message: Dict[str, Any], expected_chat_id: str, expected_username: str
) -> bool:
    actor = message.get("from") or {}
    chat = message.get("chat") or {}
    return is_authorized_actor(actor, expected_username) and is_authorized_chat(
        chat, expected_chat_id
    )


def is_authorized_callback(
    callback_query: Dict[str, Any], expected_chat_id: str, expected_username: str
) -> bool:
    actor = callback_query.get("from") or {}
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    return is_authorized_actor(actor, expected_username) and is_authorized_chat(
        chat, expected_chat_id
    )


def normalize_follow_command(text: str) -> Tuple[str, str]:
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("Usage: /follow <url> [selector]. Default selector is body.")

    url = parts[1].strip()
    selector = parts[2].strip() if len(parts) >= 3 else "body"

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must start with http:// or https://")

    if not selector:
        raise ValueError("Selector cannot be empty")

    return url, selector


def build_jobs_reply() -> Tuple[str, Optional[Dict[str, Any]]]:
    jobs = load_jobs(DEFAULT_JOBS_PATH)
    if not jobs:
        return "No links are being followed.", None

    lines: List[str] = ["Currently followed links:"]
    buttons: List[List[Dict[str, str]]] = []

    for index, job in enumerate(jobs, start=1):
        lines.append(f"{index}. {job.url} [{job.selector}]")
        buttons.append(
            [
                {
                    "text": f"Delete {index}",
                    "callback_data": f"delete:{job.id}",
                }
            ]
        )

    reply_markup = {"inline_keyboard": buttons}
    return "\n".join(lines), reply_markup


def send_help(bot_token: str, chat_id: str) -> None:
    help_text = (
        "Commands:\n"
        "/follow <url> [selector] - add a page to monitor. Default selector is body.\n"
        "/list - show followed links and delete buttons.\n"
        "/help - show this message."
    )
    send_telegram_message(bot_token=bot_token, chat_id=chat_id, body=help_text)


def handle_follow(bot_token: str, chat_id: str, text: str) -> None:
    try:
        url, selector = normalize_follow_command(text)
        validate_follow_target(url, selector)
        job = add_job(url, selector, DEFAULT_JOBS_PATH)
    except Exception as exc:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=f"Could not add follow target.\n{exc}",
        )
        return

    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=f"Following:\n{job.url}\nSelector: {job.selector}",
    )


def handle_list(bot_token: str, chat_id: str) -> None:
    text, reply_markup = build_jobs_reply()
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=text,
        reply_markup=reply_markup,
    )


def handle_message(
    *,
    bot_token: str,
    chat_id: str,
    owner_username: str,
    message: Dict[str, Any],
) -> None:
    if not is_authorized_message(message, chat_id, owner_username):
        print("[telegram] Ignoring unauthorized message")
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    command = text.split(maxsplit=1)[0].split("@", 1)[0]
    if command in {"/start", "/help"}:
        send_help(bot_token, chat_id)
        return

    if command == "/follow":
        handle_follow(bot_token, chat_id, text)
        return

    if command == "/list":
        handle_list(bot_token, chat_id)
        return

    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body="Unknown command. Use /help.",
    )


def handle_callback_query(
    *,
    bot_token: str,
    chat_id: str,
    owner_username: str,
    callback_query: Dict[str, Any],
) -> None:
    callback_id = callback_query.get("id")
    if not callback_id:
        return

    if not is_authorized_callback(callback_query, chat_id, owner_username):
        answer_callback_query(bot_token, callback_id, "Not authorized.")
        return

    data = callback_query.get("data") or ""
    if not data.startswith("delete:"):
        answer_callback_query(bot_token, callback_id, "Unknown action.")
        return

    job_id = data.split(":", 1)[1]
    deleted = delete_job(job_id, DEFAULT_JOBS_PATH)
    if deleted is None:
        answer_callback_query(bot_token, callback_id, "Item already deleted.")
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="That item was already removed.",
        )
        return

    answer_callback_query(bot_token, callback_id, "Deleted.")
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=f"Deleted:\n{deleted.url}\nSelector: {deleted.selector}",
    )


def process_updates(
    *,
    bot_token: str,
    chat_id: str,
    owner_username: str,
    offset_path: Path,
) -> Optional[int]:
    offset = load_offset(offset_path)
    updates = get_updates(bot_token, offset=offset, timeout=20)
    latest_offset: Optional[int] = None

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            latest_offset = update_id + 1

        if "message" in update:
            handle_message(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                message=update["message"],
            )

        if "callback_query" in update:
            handle_callback_query(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                callback_query=update["callback_query"],
            )

    if latest_offset is not None:
        save_offset(latest_offset, offset_path)

    return latest_offset


def main() -> int:
    load_dotenv()

    try:
        gemini_client = genai.Client()
        bot_token, chat_id, owner_username = resolve_telegram_config()
    except Exception as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    interval_seconds = parse_check_interval()
    next_check_at = time.monotonic()

    print("[daemon] Telegram bot daemon started")
    print(f"[daemon] Owner username: @{owner_username}")
    print(f"[daemon] Chat ID: {chat_id}")
    print(f"[daemon] Check interval: {interval_seconds}s")

    while True:
        try:
            process_updates(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                offset_path=DEFAULT_OFFSET_PATH,
            )

            now = time.monotonic()
            if now >= next_check_at:
                monitor_jobs_once(
                    gemini_client=gemini_client,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    jobs_path=DEFAULT_JOBS_PATH,
                )
                next_check_at = now + interval_seconds

        except KeyboardInterrupt:
            print("[daemon] Stopping")
            return 0
        except Exception as exc:
            print(f"[daemon] Loop error: {exc}", file=sys.stderr)
            sleep_with_backoff(5)


if __name__ == "__main__":
    raise SystemExit(main())
