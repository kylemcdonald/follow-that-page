import hashlib
import difflib
import sys
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup


def get_cache_path(url: str) -> Path:
    script_dir = Path(__file__).resolve().parent
    cache_dir = script_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / url_hash


def fetch_selected_html(url: str, selector: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    element = soup.select_one(selector)
    if element is None:
        raise ValueError(f"Selector not found: {selector}")

    return str(element)


def check_page_for_changes(url: str, selector: str) -> Optional[str]:
    """Return a unified diff string if page section changed, otherwise None."""
    current_html = fetch_selected_html(url, selector)
    cache_path = get_cache_path(url)

    previous_html: Optional[str] = None
    if cache_path.exists():
        previous_html = cache_path.read_text(encoding="utf-8", errors="ignore")

    cache_path.write_text(current_html, encoding="utf-8")

    if previous_html is None:
        return None

    if previous_html != current_html:
        try:
            previous_lines = (
                BeautifulSoup(previous_html, "html.parser").prettify().splitlines()
            )
            current_lines = (
                BeautifulSoup(current_html, "html.parser").prettify().splitlines()
            )
        except Exception:
            previous_lines = previous_html.splitlines()
            current_lines = current_html.splitlines()

        diff_iter = difflib.unified_diff(
            previous_lines,
            current_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
        return "\n".join(diff_iter)

    return None


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python follow-that-page.py <url> <selector>", file=sys.stderr)
        sys.exit(2)

    url = sys.argv[1]
    selector = sys.argv[2]

    try:
        diff_text = check_page_for_changes(url, selector)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(3)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        sys.exit(4)

    if diff_text:
        print(diff_text)
    sys.exit(0)


if __name__ == "__main__":
    main()


