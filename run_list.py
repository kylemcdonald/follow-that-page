import json
import sys
import os
import html
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from google import genai

from follow_that_page import check_page_for_changes


def load_jobs(json_path: Path) -> List[Dict[str, Any]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of objects with 'url' and 'selector'")
    jobs: List[Dict[str, Any]] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item at index {idx} is not an object")
        url = item.get("url")
        selector = item.get("selector")
        if not isinstance(url, str) or not isinstance(selector, str):
            raise ValueError(f"Item at index {idx} must contain string 'url' and 'selector'")
        jobs.append({"url": url, "selector": selector})
    return jobs


def summarize_diff_with_gemini(client: genai.Client, url: str, selector: str, diff_text: str) -> str:
    prompt = (
        "Summarize the following unified diff for a web page section. ",
        "For example, if the diff describes the addition or removal of an item in a marketplace, ",
        "briefly describe the item that was added or removed. ",
        "Return plain text, in 140 characters or less.\n\n"
        f"URL: {url}\nSelector: {selector}\n\n"
        "Diff:\n"
        f"{diff_text}"
    )
    # Debug: indicate we're calling Gemini
    print(f"[gemini] Generating summary for {url} ({selector})...")
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    summary_text = response.text or ""
    # Debug: show the produced summary (may be empty)
    if summary_text:
        print(f"[gemini] Summary generated for {url} ({selector}):")
        print(summary_text)
    else:
        print(f"[gemini] Empty summary returned for {url} ({selector})")
    return summary_text


def send_ntfy_message(*, ntfy_url: str, body: str) -> None:
    print(f"[ntfy] POST {ntfy_url}")
    resp = requests.post(ntfy_url, data=body.encode("utf-8"), timeout=30)
    print(f"[ntfy] status={resp.status_code}")


def main() -> int:
    # Load environment variables from .env if present
    load_dotenv()

    # Initialize Gemini client (uses GEMINI_API_KEY from env)
    try:
        gemini_client = genai.Client()
    except Exception as exc:
        print(f"Gemini client init error: {exc}", file=sys.stderr)
        return 2

    # Allow optional argument; default to './jobs.json' when not provided
    if len(sys.argv) == 1:
        json_path = Path("jobs.json")
    elif len(sys.argv) == 2:
        json_path = Path(sys.argv[1])
    else:
        print("Usage: python run_list.py [jobs.json]", file=sys.stderr)
        return 2

    if not json_path.exists():
        print(f"File not found: {json_path}", file=sys.stderr)
        return 2

    try:
        jobs = load_jobs(json_path)
    except Exception as exc:
        print(f"Invalid jobs file: {exc}", file=sys.stderr)
        return 2

    # ntfy.sh configuration
    ntfy_url: Optional[str] = os.getenv("NTFY_URL") or os.getenv("NTFY_TOPIC_URL")
    if not ntfy_url:
        topic_id = os.getenv("NTFY_TOPIC_ID")
        if topic_id:
            base_url = os.getenv("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
            ntfy_url = f"{base_url}/{topic_id}"
    if not ntfy_url:
        print(
            "Missing NTFY_URL or NTFY_TOPIC_ID in environment/.env (set NTFY_URL=https://ntfy.sh/<topic> or NTFY_TOPIC_ID=<topic>)",
            file=sys.stderr,
        )
        return 2

    any_changes = False
    for job in jobs:
        url = job["url"]
        selector = job["selector"]
        try:
            diff_text = check_page_for_changes(url, selector)
        except ValueError as exc:
            print(f"[{url}] selector error: {exc}", file=sys.stderr)
            continue
        except requests.RequestException as exc:
            print(f"[{url}] network error: {exc}", file=sys.stderr)
            continue

        if diff_text:
            any_changes = True
            print(f"=== Diff for {url} ({selector}) ===")
            # only print the first 10 lines
            print("\n".join(diff_text.splitlines()[:10]))
            print(f"({len(diff_text.splitlines())} lines)")
            print()

            # Summarize the diff using Gemini 2.5 Flash
            try:
                summary = summarize_diff_with_gemini(
                    client=gemini_client,
                    url=url,
                    selector=selector,
                    diff_text=diff_text,
                )
                if summary:
                    # Already printed inside summarize_diff_with_gemini
                    print()

                    # ntfy notification: summary followed by link
                    message_body = f"{summary}\n{url}"
                    try:
                        send_ntfy_message(ntfy_url=ntfy_url, body=message_body)
                    except Exception as exc:
                        print(f"[{url}] ntfy error: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[{url}] Gemini summarization error: {exc}", file=sys.stderr)

    return 0 if any_changes else 0


if __name__ == "__main__":
    raise SystemExit(main())


