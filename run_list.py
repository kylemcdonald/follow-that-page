import sys
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai

from botlib import monitor_jobs_once, resolve_telegram_config


def main() -> int:
    load_dotenv(override=True)

    try:
        gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception as exc:
        print(f"Gemini client init error: {exc}", file=sys.stderr)
        return 2

    if len(sys.argv) == 1:
        jobs_path = Path("jobs.json")
    elif len(sys.argv) == 2:
        jobs_path = Path(sys.argv[1])
    else:
        print("Usage: python run_list.py [jobs.json]", file=sys.stderr)
        return 2

    try:
        telegram_bot_token, telegram_chat_id, _ = resolve_telegram_config()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    monitor_jobs_once(
        gemini_client=gemini_client,
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        jobs_path=jobs_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
