import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from google import genai

from follow_that_page import ChangeResult, check_page_for_changes, fetch_page_state


DEFAULT_JOBS_PATH = Path("jobs.json")
DEFAULT_OFFSET_PATH = Path(".telegram_offset")


@dataclass
class Job:
    id: str
    url: str
    selector: str


def load_jobs(json_path: Path = DEFAULT_JOBS_PATH) -> List[Job]:
    if not json_path.exists():
        return []

    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of objects with 'url' and 'selector'")

    jobs: List[Job] = []
    needs_save = False
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item at index {idx} is not an object")

        url = item.get("url")
        selector = item.get("selector")
        job_id = item.get("id")
        if not isinstance(url, str) or not isinstance(selector, str):
            raise ValueError(
                f"Item at index {idx} must contain string 'url' and 'selector'"
            )
        if not isinstance(job_id, str) or not job_id:
            job_id = uuid.uuid4().hex[:12]
            needs_save = True

        jobs.append(Job(id=job_id, url=url, selector=selector))

    if needs_save:
        save_jobs(jobs, json_path)

    return jobs


def save_jobs(jobs: List[Job], json_path: Path = DEFAULT_JOBS_PATH) -> None:
    payload = [
        {"id": job.id, "url": job.url, "selector": job.selector}
        for job in jobs
    ]
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def add_job(url: str, selector: str, json_path: Path = DEFAULT_JOBS_PATH) -> Job:
    jobs = load_jobs(json_path)

    for job in jobs:
        if job.url == url and job.selector == selector:
            return job

    new_job = Job(id=uuid.uuid4().hex[:12], url=url, selector=selector)
    jobs.append(new_job)
    save_jobs(jobs, json_path)
    return new_job


def delete_job(job_id: str, json_path: Path = DEFAULT_JOBS_PATH) -> Optional[Job]:
    jobs = load_jobs(json_path)
    kept_jobs: List[Job] = []
    deleted: Optional[Job] = None

    for job in jobs:
        if job.id == job_id and deleted is None:
            deleted = job
        else:
            kept_jobs.append(job)

    if deleted is not None:
        save_jobs(kept_jobs, json_path)

    return deleted


def summarize_diff_with_gemini(
    client: genai.Client, url: str, selector: str, diff_text: str
) -> str:
    prompt = (
        "Summarize the following unified diff for a web page section. "
        "For example, if the diff describes the addition or removal of an item in a marketplace, "
        "briefly describe the item that was added or removed. "
        "Return plain text, in 140 characters or less.\n\n"
        f"URL: {url}\nSelector: {selector}\n\n"
        "Diff:\n"
        f"{diff_text}"
    )
    print(f"[gemini] Generating summary for {url} ({selector})...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    summary_text = response.text or ""
    if summary_text:
        print(f"[gemini] Summary generated for {url} ({selector}):")
        print(summary_text)
    else:
        print(f"[gemini] Empty summary returned for {url} ({selector})")
    return summary_text.strip()


def resolve_telegram_config() -> Tuple[str, str, str]:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    owner_username = os.getenv("TELEGRAM_OWNER_USERNAME", "kcimc").strip().lstrip("@")

    if not bot_token or not chat_id:
        raise ValueError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment/.env"
        )

    if not owner_username:
        raise ValueError("Missing TELEGRAM_OWNER_USERNAME in environment/.env")

    return bot_token, chat_id, owner_username


def send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    body: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": body,
        "disable_web_page_preview": False,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    print(f"[telegram] POST {api_url}")
    resp = requests.post(api_url, json=payload, timeout=30)
    print(f"[telegram] status={resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def telegram_api_post(
    *, bot_token: str, method: str, payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    api_url = f"https://api.telegram.org/bot{bot_token}/{method}"
    resp = requests.post(api_url, json=payload or {}, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Telegram API error from {method}: {data}")
    return data


def get_updates(bot_token: str, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        payload["offset"] = offset
    data = telegram_api_post(bot_token=bot_token, method="getUpdates", payload=payload)
    return data.get("result", [])


def answer_callback_query(
    bot_token: str, callback_query_id: str, text: str = ""
) -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    telegram_api_post(bot_token=bot_token, method="answerCallbackQuery", payload=payload)


def load_offset(offset_path: Path = DEFAULT_OFFSET_PATH) -> Optional[int]:
    if not offset_path.exists():
        return None

    raw = offset_path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    return int(raw)


def save_offset(offset: int, offset_path: Path = DEFAULT_OFFSET_PATH) -> None:
    offset_path.write_text(f"{offset}\n", encoding="utf-8")


def validate_follow_target(url: str, selector: str) -> None:
    state = fetch_page_state(url, selector)
    if state.status_code == 200 and state.selected_html is None:
        raise ValueError(f"Selector not found: {selector}")


def build_notification_message(
    *,
    url: str,
    selector: str,
    change: ChangeResult,
    gemini_client: genai.Client,
) -> Optional[str]:
    parts: List[str] = []

    if change.status_changed:
        parts.append(
            f"HTTP status changed: {change.previous_status_code} -> {change.current_status_code}"
        )

    if change.diff_text:
        print(f"=== Diff for {url} ({selector}) ===")
        print("\n".join(change.diff_text.splitlines()[:10]))
        print(f"({len(change.diff_text.splitlines())} lines)")
        print()

        try:
            summary = summarize_diff_with_gemini(
                client=gemini_client,
                url=url,
                selector=selector,
                diff_text=change.diff_text,
            )
        except Exception as exc:
            print(f"[{url}] Gemini summarization error: {exc}")
            summary = ""

        if summary:
            parts.append(summary)

    if not parts:
        return None

    parts.append(url)
    return "\n".join(parts)


def monitor_jobs_once(
    *,
    gemini_client: genai.Client,
    bot_token: str,
    chat_id: str,
    jobs_path: Path = DEFAULT_JOBS_PATH,
) -> int:
    change_count = 0
    jobs = load_jobs(jobs_path)

    for job in jobs:
        try:
            change = check_page_for_changes(job.url, job.selector)
        except ValueError as exc:
            print(f"[{job.url}] selector error: {exc}")
            continue
        except requests.RequestException as exc:
            print(f"[{job.url}] network error: {exc}")
            continue

        if not change.changed:
            continue

        change_count += 1
        message_body = build_notification_message(
            url=job.url,
            selector=job.selector,
            change=change,
            gemini_client=gemini_client,
        )
        if not message_body:
            continue

        try:
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body=message_body,
            )
        except Exception as exc:
            print(f"[{job.url}] Telegram send error: {exc}")

    return change_count


def sleep_with_backoff(seconds: float) -> None:
    time.sleep(max(seconds, 0.0))
