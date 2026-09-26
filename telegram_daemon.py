import sys
import time
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

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
DEFAULT_CONFIG_PATH = Path(".bot_config.json")


def parse_check_interval() -> int:
    import os

    raw = os.getenv("CHECK_INTERVAL_SECONDS", str(CHECK_INTERVAL_SECONDS)).strip()
    return max(30, int(raw))


def load_runtime_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def save_runtime_config(config: Dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def get_check_interval_seconds(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    config = load_runtime_config(config_path)
    value = config.get("check_interval_seconds")
    if isinstance(value, int):
        return max(30, value)
    return parse_check_interval()


def set_check_interval_seconds(seconds: int, config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    config = load_runtime_config(config_path)
    config["check_interval_seconds"] = max(30, seconds)
    save_runtime_config(config, config_path)
    return config["check_interval_seconds"]


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
        mode_status = "text only" if job.mode == "text" else job.mode
        lines.append(f"{index}. {job.url} [{job.selector}] ({mode_status})")
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
        "/follow <url> [selector] - choose standard, robust, or text-only mode. Default selector is body.\n"
        "/list - show followed links and delete buttons.\n"
        "/interval - show the current check interval.\n"
        "/interval <seconds> - set the check interval, minimum 30.\n"
        "/help - show this message."
    )
    send_telegram_message(bot_token=bot_token, chat_id=chat_id, body=help_text)


def handle_follow(bot_token: str, chat_id: str, text: str) -> None:
    try:
        url, selector = normalize_follow_command(text)
        validate_follow_target(url, selector)
    except Exception as exc:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=f"Could not add follow target.\n{exc}",
        )
        return

    config = load_runtime_config()
    request_id = uuid.uuid4().hex[:12]
    config["pending_follow"] = {
        "id": request_id, "chat_id": chat_id, "url": url,
        "selector": selector, "stage": "mode",
    }
    save_runtime_config(config)
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=(f"Choose a monitoring mode for this link:\n{url}\nSelector: {selector}\n"
              "Robust mode only notifies about changes matching your criteria. "
              "Standard mode notifies about every detected change. "
              "Text only ignores scripts, markup, and status-only changes."),
        reply_markup={"inline_keyboard": [[
            {"text": "Standard", "callback_data": f"follow:{request_id}:standard"},
            {"text": "Robust", "callback_data": f"follow:{request_id}:robust"},
            {"text": "Text only", "callback_data": f"follow:{request_id}:text"},
        ]]},
    )


def finish_follow(bot_token: str, chat_id: str, config: Dict[str, Any],
                  mode: str, criteria: Optional[str] = None) -> None:
    pending = config["pending_follow"]
    job = add_job(pending["url"], pending["selector"], DEFAULT_JOBS_PATH,
                  mode=mode, change_criteria=criteria)
    del config["pending_follow"]
    save_runtime_config(config)
    body = f"Following:\n{job.url}\nSelector: {job.selector}\nMode: {job.mode}"
    if job.change_criteria:
        body += f"\nNotify when: {job.change_criteria}"
    send_telegram_message(bot_token=bot_token, chat_id=chat_id, body=body)


def handle_list(bot_token: str, chat_id: str) -> None:
    text, reply_markup = build_jobs_reply()
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=text,
        reply_markup=reply_markup,
    )


def handle_interval(bot_token: str, chat_id: str, text: str) -> Optional[int]:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        current = get_check_interval_seconds()
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=f"Current check interval: {current} seconds.",
        )
        return None

    try:
        requested = int(parts[1].strip())
    except ValueError:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="Usage: /interval <seconds>. Minimum is 30.",
        )
        return None

    current = set_check_interval_seconds(requested)
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body=f"Check interval set to {current} seconds.",
    )
    return current


def handle_message(
    *,
    bot_token: str,
    chat_id: str,
    owner_username: str,
    message: Dict[str, Any],
) -> Optional[int]:
    if not is_authorized_message(message, chat_id, owner_username):
        print("[telegram] Ignoring unauthorized message")
        return None

    text = (message.get("text") or "").strip()
    if not text:
        return None

    command = text.split(maxsplit=1)[0].split("@", 1)[0]
    if command in {"/start", "/help"}:
        send_help(bot_token, chat_id)
        return None

    if command == "/follow":
        handle_follow(bot_token, chat_id, text)
        return None

    if command == "/list":
        handle_list(bot_token, chat_id)
        return None

    if command == "/interval":
        return handle_interval(bot_token, chat_id, text)

    config = load_runtime_config()
    pending = config.get("pending_follow", {})
    if (not text.startswith("/") and pending.get("chat_id") == chat_id
            and pending.get("stage") == "criteria"):
        finish_follow(bot_token, chat_id, config, "robust", text)
        return None

    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body="Unknown command. Use /help.",
    )
    return None


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

    def try_answer_callback_query(text: str = "") -> None:
        try:
            answer_callback_query(bot_token, callback_id, text)
        except Exception as exc:
            print(f"[telegram] answerCallbackQuery error: {exc}", file=sys.stderr)

    if not is_authorized_callback(callback_query, chat_id, owner_username):
        try_answer_callback_query("Not authorized.")
        return

    data = callback_query.get("data") or ""
    if data.startswith("follow:"):
        config = load_runtime_config()
        pending = config.get("pending_follow", {})
        parts = data.split(":")
        if (len(parts) != 3 or pending.get("id") != parts[1]
                or pending.get("chat_id") != chat_id
                or pending.get("stage") != "mode"
                or parts[2] not in {"standard", "robust", "text"}):
            try_answer_callback_query("This question is no longer active. Use /follow again.")
            return
        try_answer_callback_query()
        if parts[2] in {"standard", "text"}:
            finish_follow(bot_token, chat_id, config, parts[2])
        else:
            pending["stage"] = "criteria"
            save_runtime_config(config)
            send_telegram_message(
                bot_token=bot_token, chat_id=chat_id,
                body=(f"What changes should trigger a notification for {pending['url']}?\n"
                      "Reply with your criteria, such as price or availability changes."),
            )
        return

    if not data.startswith("delete:"):
        try_answer_callback_query("Unknown action.")
        return

    job_id = data.split(":", 1)[1]
    deleted = delete_job(job_id, DEFAULT_JOBS_PATH)
    if deleted is None:
        try_answer_callback_query("Item already deleted.")
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="That item was already removed.",
        )
        return

    try_answer_callback_query("Deleted.")
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
) -> Tuple[Optional[int], Optional[int]]:
    offset = load_offset(offset_path)
    updates = get_updates(bot_token, offset=offset, timeout=20)
    latest_offset: Optional[int] = None
    interval_override: Optional[int] = None

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            latest_offset = update_id + 1

        if "message" in update:
            maybe_interval = handle_message(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                message=update["message"],
            )
            if maybe_interval is not None:
                interval_override = maybe_interval

        if "callback_query" in update:
            handle_callback_query(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                callback_query=update["callback_query"],
            )

    if latest_offset is not None:
        save_offset(latest_offset, offset_path)

    return latest_offset, interval_override


def run_monitor_pass(
    *,
    openai_client: OpenAI,
    bot_token: str,
    chat_id: str,
) -> None:
    started_at = time.monotonic()
    print("[daemon] Monitor pass started")
    try:
        change_count = monitor_jobs_once(
            openai_client=openai_client,
            bot_token=bot_token,
            chat_id=chat_id,
            jobs_path=DEFAULT_JOBS_PATH,
        )
    except Exception as exc:
        print(f"[daemon] Monitor pass error: {exc}", file=sys.stderr)
        return

    elapsed_seconds = time.monotonic() - started_at
    print(
        "[daemon] Monitor pass finished: "
        f"changes={change_count} elapsed={elapsed_seconds:.1f}s"
    )


def start_monitor_thread(
    *,
    openai_client: OpenAI,
    bot_token: str,
    chat_id: str,
) -> threading.Thread:
    thread = threading.Thread(
        target=run_monitor_pass,
        kwargs={
            "openai_client": openai_client,
            "bot_token": bot_token,
            "chat_id": chat_id,
        },
        name="follow-that-page-monitor",
        daemon=True,
    )
    thread.start()
    return thread


def main() -> int:
    load_dotenv(override=True)

    try:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("Missing OPENAI_API_KEY in environment/.env")
        openai_client = OpenAI(api_key=api_key)
        bot_token, chat_id, owner_username = resolve_telegram_config()
    except Exception as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    interval_seconds = get_check_interval_seconds()
    next_check_at = time.monotonic()
    monitor_thread: Optional[threading.Thread] = None

    print("[daemon] Telegram bot daemon started")
    print(f"[daemon] Owner username: @{owner_username}")
    print(f"[daemon] Chat ID: {chat_id}")
    print(f"[daemon] Check interval: {interval_seconds}s")

    while True:
        try:
            if monitor_thread is not None and not monitor_thread.is_alive():
                monitor_thread = None
                next_check_at = time.monotonic() + interval_seconds

            now = time.monotonic()
            if monitor_thread is None and now >= next_check_at:
                monitor_thread = start_monitor_thread(
                    openai_client=openai_client,
                    bot_token=bot_token,
                    chat_id=chat_id,
                )

            _, maybe_interval = process_updates(
                bot_token=bot_token,
                chat_id=chat_id,
                owner_username=owner_username,
                offset_path=DEFAULT_OFFSET_PATH,
            )
            if maybe_interval is not None and maybe_interval != interval_seconds:
                interval_seconds = maybe_interval
                if monitor_thread is None:
                    next_check_at = time.monotonic() + interval_seconds
                print(f"[daemon] Check interval updated: {interval_seconds}s")

        except KeyboardInterrupt:
            print("[daemon] Stopping")
            return 0
        except Exception as exc:
            print(f"[daemon] Loop error: {exc}", file=sys.stderr)
            sleep_with_backoff(5)


if __name__ == "__main__":
    raise SystemExit(main())
