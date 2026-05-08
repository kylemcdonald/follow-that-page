import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from google import genai
from google.genai import types

from follow_that_page import ChangeResult, check_page_for_changes, fetch_page_state


DEFAULT_JOBS_PATH = Path("jobs.json")
DEFAULT_OFFSET_PATH = Path(".telegram_offset")


@dataclass
class RedditPostConfig:
    target: str
    title: str
    body: str
    posted_at: Optional[str] = None
    posted_request_id: Optional[str] = None


@dataclass
class Job:
    id: str
    url: str
    selector: str
    reddit_post: Optional[RedditPostConfig] = None


def _parse_reddit_post_config(
    item: Dict[str, Any], index: int
) -> Optional[RedditPostConfig]:
    value = item.get("reddit_post")
    if value is None:
        return None

    if not isinstance(value, dict):
        raise ValueError(f"Item at index {index} has invalid 'reddit_post'")

    target = value.get("target")
    title = value.get("title")
    body = value.get("body", "")
    if not isinstance(target, str) or not target.strip():
        raise ValueError(
            f"Item at index {index} must contain string 'reddit_post.target'"
        )
    if not isinstance(title, str) or not title.strip():
        raise ValueError(
            f"Item at index {index} must contain string 'reddit_post.title'"
        )
    if not isinstance(body, str):
        raise ValueError(
            f"Item at index {index} must contain string 'reddit_post.body'"
        )
    posted_at = value.get("posted_at")
    if posted_at is not None and not isinstance(posted_at, str):
        raise ValueError(
            f"Item at index {index} must contain string 'reddit_post.posted_at'"
        )
    posted_request_id = value.get("posted_request_id")
    if posted_request_id is not None and not isinstance(posted_request_id, str):
        raise ValueError(
            f"Item at index {index} must contain string 'reddit_post.posted_request_id'"
        )

    return RedditPostConfig(
        target=target.strip(),
        title=title.strip(),
        body=body,
        posted_at=posted_at,
        posted_request_id=posted_request_id,
    )


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

        jobs.append(
            Job(
                id=job_id,
                url=url,
                selector=selector,
                reddit_post=_parse_reddit_post_config(item, idx),
            )
        )

    if needs_save:
        save_jobs(jobs, json_path)

    return jobs


def save_jobs(jobs: List[Job], json_path: Path = DEFAULT_JOBS_PATH) -> None:
    payload: List[Dict[str, Any]] = []
    for job in jobs:
        item: Dict[str, Any] = {
            "id": job.id,
            "url": job.url,
            "selector": job.selector,
        }
        if job.reddit_post is not None:
            item["reddit_post"] = {
                "target": job.reddit_post.target,
                "title": job.reddit_post.title,
                "body": job.reddit_post.body,
            }
            if job.reddit_post.posted_at is not None:
                item["reddit_post"]["posted_at"] = job.reddit_post.posted_at
            if job.reddit_post.posted_request_id is not None:
                item["reddit_post"][
                    "posted_request_id"
                ] = job.reddit_post.posted_request_id
        payload.append(item)

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


def set_job_reddit_post(
    job_id: str,
    reddit_post: RedditPostConfig,
    json_path: Path = DEFAULT_JOBS_PATH,
) -> Optional[Job]:
    jobs = load_jobs(json_path)
    updated: Optional[Job] = None

    for job in jobs:
        if job.id == job_id:
            job.reddit_post = reddit_post
            updated = job
            break

    if updated is not None:
        save_jobs(jobs, json_path)

    return updated


def mark_job_reddit_post_success(
    *,
    job_id: str,
    posted_config: RedditPostConfig,
    reddit_result: Dict[str, Any],
    json_path: Path = DEFAULT_JOBS_PATH,
) -> Optional[Job]:
    jobs = load_jobs(json_path)
    updated: Optional[Job] = None

    request_id = reddit_result.get("request_id")
    posted_request_id = request_id if isinstance(request_id, str) else None
    posted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for job in jobs:
        if job.id != job_id or job.reddit_post is None:
            continue
        if (
            job.reddit_post.target != posted_config.target
            or job.reddit_post.title != posted_config.title
            or job.reddit_post.body != posted_config.body
        ):
            return None

        job.reddit_post.posted_at = posted_at
        job.reddit_post.posted_request_id = posted_request_id
        updated = job
        break

    if updated is not None:
        save_jobs(jobs, json_path)

    return updated


def get_job_by_id(job_id: str, json_path: Path = DEFAULT_JOBS_PATH) -> Optional[Job]:
    for job in load_jobs(json_path):
        if job.id == job_id:
            return job
    return None


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
    model_name = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()
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
    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)],
        )
    ]
    config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="HIGH")
    )
    pieces: List[str] = []
    for chunk in client.models.generate_content_stream(
        model=model_name,
        contents=contents,
        config=config,
    ):
        if chunk.text:
            pieces.append(chunk.text)

    summary_text = "".join(pieces).strip()
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


def _format_reddit_api_error(data: Dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
    return json.dumps(data, sort_keys=True)[:500]


def post_reddit_update(job: Job, body: Optional[str] = None) -> Dict[str, Any]:
    if job.reddit_post is None:
        raise ValueError("Job does not have Reddit posting configured")

    base_url = os.getenv("VIBECHECK_API_BASE_URL", "http://vibecheck.local:8765").strip()
    if not base_url:
        raise ValueError("VIBECHECK_API_BASE_URL cannot be empty")

    timeout_seconds = int(os.getenv("VIBECHECK_TIMEOUT_SECONDS", "180").strip())
    api_url = f"{base_url.rstrip('/')}/posts"
    payload = {
        "target": job.reddit_post.target,
        "page_url": job.url,
        "title": job.reddit_post.title,
        "body": job.reddit_post.body if body is None else body,
    }
    headers = {"Content-Type": "application/json"}
    api_token = os.getenv("VIBECHECK_API_TOKEN", "").strip()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    print(f"[reddit] POST {api_url} target={job.reddit_post.target} url={job.url}")
    resp = requests.post(api_url, json=payload, headers=headers, timeout=timeout_seconds)
    print(f"[reddit] status={resp.status_code}")
    resp.raise_for_status()

    data = resp.json()
    if not data.get("ok"):
        raise ValueError(_format_reddit_api_error(data))
    return data


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
    if state.state_kind == "http" and state.status_code == 200 and state.selected_html is None:
        raise ValueError(f"Selector not found: {selector}")


def build_change_summary(
    *,
    url: str,
    selector: str,
    change: ChangeResult,
    gemini_client: genai.Client,
) -> Optional[str]:
    parts: List[str] = []

    if change.status_changed:
        parts.append(
            f"State changed: {change.previous_state_label} -> {change.current_state_label}"
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
        else:
            parts.append(f"Content changed for selector {selector}")

    if not parts:
        return None

    return "\n".join(parts)


def build_notification_message(*, change_summary: str, url: str) -> str:
    return "\n".join([change_summary, url])


def render_reddit_post_body(template: str, change_summary: str) -> str:
    return template.replace("{summary}", change_summary)


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

        if not change.changed:
            continue

        change_count += 1
        change_summary = build_change_summary(
            url=job.url,
            selector=job.selector,
            change=change,
            gemini_client=gemini_client,
        )
        if not change_summary:
            continue

        message_body = build_notification_message(
            change_summary=change_summary,
            url=job.url,
        )

        try:
            send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                body=message_body,
            )
        except Exception as exc:
            print(f"[{job.url}] Telegram send error: {exc}")

        current_job = get_job_by_id(job.id, jobs_path)
        if current_job is None:
            print(f"[{job.url}] Job was deleted before Reddit post step; skipping")
            continue

        if current_job.reddit_post is not None:
            if current_job.reddit_post.posted_at is not None:
                print(
                    f"[{job.url}] Reddit post already succeeded at "
                    f"{current_job.reddit_post.posted_at}; skipping"
                )
                continue

            try:
                posted_config = current_job.reddit_post
                reddit_body = render_reddit_post_body(
                    current_job.reddit_post.body,
                    change_summary,
                )
                reddit_result = post_reddit_update(current_job, body=reddit_body)
            except Exception as exc:
                print(f"[{job.url}] Reddit post error: {exc}")
                try:
                    send_telegram_message(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        body=(
                            "Reddit post failed for updated page:\n"
                            f"{job.url}\n"
                            f"Target: {current_job.reddit_post.target}\n"
                            f"Error: {exc}"
                        ),
                    )
                except Exception as telegram_exc:
                    print(f"[{job.url}] Telegram send error: {telegram_exc}")
                continue

            marked = mark_job_reddit_post_success(
                job_id=current_job.id,
                posted_config=posted_config,
                reddit_result=reddit_result,
                json_path=jobs_path,
            )
            if marked is None:
                print(
                    f"[{job.url}] Reddit post succeeded, but success marker was not saved"
                )

            request_id = reddit_result.get("request_id")
            submitted = reddit_result.get("submitted")
            result_lines = [
                "Reddit post submitted for updated page:"
                if submitted
                else "Reddit post prepared for updated page:",
                job.url,
                f"Target: {current_job.reddit_post.target}",
                "Future Reddit posts for this link will be skipped.",
            ]
            if isinstance(request_id, str) and request_id:
                result_lines.append(f"Request ID: {request_id}")

            try:
                send_telegram_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    body="\n".join(result_lines),
                )
            except Exception as exc:
                print(f"[{job.url}] Telegram send error: {exc}")

    return change_count


def sleep_with_backoff(seconds: float) -> None:
    time.sleep(max(seconds, 0.0))
