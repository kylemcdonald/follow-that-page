import sys
import time
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from google import genai

from botlib import (
    DEFAULT_JOBS_PATH,
    DEFAULT_OFFSET_PATH,
    RedditPostConfig,
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
    set_job_reddit_post,
    sleep_with_backoff,
    validate_follow_target,
)


CHECK_INTERVAL_SECONDS = 300
DEFAULT_CONFIG_PATH = Path(".bot_config.json")
REDDIT_SETUP_CONFIG_KEY = "pending_reddit_setup"


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
        if job.reddit_post is None:
            reddit_status = "Reddit: off"
        elif job.reddit_post.posted_at is not None:
            reddit_status = f"Reddit: {job.reddit_post.target}, posted"
        else:
            reddit_status = f"Reddit: {job.reddit_post.target}, pending"
        lines.append(f"{index}. {job.url} [{job.selector}] ({reddit_status})")
        buttons.append(
            [
                {
                    "text": f"Configure Reddit {index}",
                    "callback_data": f"reddit:{job.id}",
                },
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
        "/reddit <number-or-id> - configure Reddit posting for a followed page.\n"
        "/list - show followed links and delete buttons.\n"
        "/interval - show the current check interval.\n"
        "/interval <seconds> - set the check interval, minimum 30.\n"
        "/cancel - cancel an in-progress Reddit setup.\n"
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
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "Configure Reddit posting",
                        "callback_data": f"reddit:{job.id}",
                    }
                ]
            ]
        },
    )


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


def normalize_reddit_target(raw_target: str) -> str:
    target = raw_target.strip()
    if re.match(r"^[uUrR]/", target):
        target = f"/{target}"

    match = re.fullmatch(r"/([uUrR])/([A-Za-z0-9][A-Za-z0-9_-]{0,30})", target)
    if not match:
        raise ValueError("Send a target like /u/kcimc or /r/UFOs.")

    return f"/{match.group(1).lower()}/{match.group(2)}"


def get_reddit_setup(config_path: Path = DEFAULT_CONFIG_PATH) -> Optional[Dict[str, Any]]:
    pending = load_runtime_config(config_path).get(REDDIT_SETUP_CONFIG_KEY)
    if not isinstance(pending, dict):
        return None
    if not isinstance(pending.get("job_id"), str):
        return None
    if not isinstance(pending.get("step"), str):
        return None
    return pending


def save_reddit_setup(
    pending: Dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH
) -> None:
    config = load_runtime_config(config_path)
    config[REDDIT_SETUP_CONFIG_KEY] = pending
    save_runtime_config(config, config_path)


def clear_reddit_setup(config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config = load_runtime_config(config_path)
    if REDDIT_SETUP_CONFIG_KEY in config:
        del config[REDDIT_SETUP_CONFIG_KEY]
        save_runtime_config(config, config_path)


def find_job_by_id(job_id: str) -> Optional[Any]:
    for job in load_jobs(DEFAULT_JOBS_PATH):
        if job.id == job_id:
            return job
    return None


def find_job_by_reference(reference: str) -> Optional[Any]:
    token = reference.strip()
    jobs = load_jobs(DEFAULT_JOBS_PATH)
    if token.isdigit():
        index = int(token) - 1
        if 0 <= index < len(jobs):
            return jobs[index]

    for job in jobs:
        if job.id == token or job.url == token:
            return job
    return None


def start_reddit_setup(bot_token: str, chat_id: str, job_id: str) -> None:
    job = find_job_by_id(job_id)
    if job is None:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="Could not find that followed page. Use /list to refresh.",
        )
        return

    save_reddit_setup({"job_id": job.id, "step": "target"})
    prompt_lines = [
        "Where should updates post?",
        "Send a target like /u/kcimc or /r/UFOs.",
        "",
        f"Page: {job.url}",
    ]
    if job.reddit_post is not None:
        prompt_lines.extend(["", f"Current target: {job.reddit_post.target}"])

    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body="\n".join(prompt_lines),
    )


def handle_reddit_command(bot_token: str, chat_id: str, text: str) -> None:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="Usage: /reddit <number-or-id>. Use /list to see followed pages.",
        )
        return

    job = find_job_by_reference(parts[1])
    if job is None:
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="Could not find that followed page. Use /list to see the numbers.",
        )
        return

    start_reddit_setup(bot_token, chat_id, job.id)


def handle_pending_reddit_setup(bot_token: str, chat_id: str, text: str) -> bool:
    pending = get_reddit_setup()
    if pending is None:
        return False

    if text.strip() == "/cancel":
        clear_reddit_setup()
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="Canceled Reddit setup.",
        )
        return True

    job = find_job_by_id(pending["job_id"])
    if job is None:
        clear_reddit_setup()
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="The followed page for this Reddit setup no longer exists.",
        )
        return True

    step = pending["step"]
    if step == "target":
        try:
            target = normalize_reddit_target(text)
        except ValueError as exc:
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body=str(exc),
            )
            return True

        save_reddit_setup({"job_id": job.id, "step": "title", "target": target})
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="What title should the Reddit post use?",
        )
        return True

    if step == "title":
        title = text.strip()
        if not title:
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body="Title cannot be empty.",
            )
            return True

        target = pending.get("target")
        if not isinstance(target, str):
            clear_reddit_setup()
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body="Reddit setup state was incomplete. Start again with /reddit.",
            )
            return True

        save_reddit_setup(
            {"job_id": job.id, "step": "body", "target": target, "title": title}
        )
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=(
                "What body text should the Reddit post use?\n"
                "Use {summary} to insert the generated change summary."
            ),
        )
        return True

    if step == "body":
        target = pending.get("target")
        title = pending.get("title")
        if not isinstance(target, str) or not isinstance(title, str):
            clear_reddit_setup()
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body="Reddit setup state was incomplete. Start again with /reddit.",
            )
            return True

        body = text.strip()
        updated = set_job_reddit_post(
            job.id,
            RedditPostConfig(target=target, title=title, body=body),
            DEFAULT_JOBS_PATH,
        )
        clear_reddit_setup()
        if updated is None:
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body="Could not save Reddit posting for that followed page.",
            )
            return True

        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=(
                "Reddit posting configured for updates:\n"
                f"{updated.url}\n"
                f"Target: {target}\n"
                f"Title: {title}\n"
                f"Body: {body}"
            ),
        )
        return True

    clear_reddit_setup()
    send_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        body="Reddit setup state was invalid. Start again with /reddit.",
    )
    return True


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

    if handle_pending_reddit_setup(bot_token, chat_id, text):
        return None

    command = text.split(maxsplit=1)[0].split("@", 1)[0]
    if command in {"/start", "/help"}:
        send_help(bot_token, chat_id)
        return None

    if command == "/follow":
        handle_follow(bot_token, chat_id, text)
        return None

    if command == "/reddit":
        handle_reddit_command(bot_token, chat_id, text)
        return None

    if command == "/cancel":
        send_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body="No Reddit setup is in progress.",
        )
        return None

    if command == "/list":
        handle_list(bot_token, chat_id)
        return None

    if command == "/interval":
        return handle_interval(bot_token, chat_id, text)

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
    if data.startswith("reddit:"):
        job_id = data.split(":", 1)[1]
        try_answer_callback_query("Starting Reddit setup.")
        start_reddit_setup(bot_token, chat_id, job_id)
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
    gemini_client: genai.Client,
    bot_token: str,
    chat_id: str,
) -> None:
    started_at = time.monotonic()
    print("[daemon] Monitor pass started")
    try:
        change_count = monitor_jobs_once(
            gemini_client=gemini_client,
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
    gemini_client: genai.Client,
    bot_token: str,
    chat_id: str,
) -> threading.Thread:
    thread = threading.Thread(
        target=run_monitor_pass,
        kwargs={
            "gemini_client": gemini_client,
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
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
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
                    gemini_client=gemini_client,
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
