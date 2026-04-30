import difflib
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class PageState:
    status_code: int
    selected_html: Optional[str]


@dataclass
class ChangeResult:
    previous_status_code: Optional[int]
    current_status_code: int
    diff_text: Optional[str]
    selected_html: Optional[str]

    @property
    def status_changed(self) -> bool:
        return (
            self.previous_status_code is not None
            and self.previous_status_code != self.current_status_code
        )

    @property
    def content_changed(self) -> bool:
        return self.diff_text is not None

    @property
    def changed(self) -> bool:
        return self.status_changed or self.content_changed


def get_cache_path(url: str, selector: str) -> Path:
    script_dir = Path(__file__).resolve().parent
    cache_dir = script_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{url}\n{selector}"
    cache_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return cache_dir / cache_hash


def _request_page(url: str) -> requests.Response:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    return resp


def fetch_page_state(url: str, selector: str) -> PageState:
    resp = _request_page(url)

    if resp.status_code != 200:
        return PageState(status_code=resp.status_code, selected_html=None)

    soup = BeautifulSoup(resp.text, "html.parser")
    element = soup.select_one(selector)
    if element is None:
        raise ValueError(f"Selector not found: {selector}")

    return PageState(status_code=resp.status_code, selected_html=str(element))


def fetch_selected_html(url: str, selector: str) -> str:
    state = fetch_page_state(url, selector)
    if state.status_code != 200:
        raise ValueError(f"Expected HTTP 200 but got HTTP {state.status_code}")
    if state.selected_html is None:
        raise ValueError(f"Selector not found: {selector}")
    return state.selected_html


def _load_previous_state(cache_path: Path) -> Optional[PageState]:
    if not cache_path.exists():
        return None

    raw = cache_path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return PageState(status_code=200, selected_html=raw)

    if not isinstance(data, dict):
        return None

    status_code = data.get("status_code")
    selected_html = data.get("selected_html")
    if not isinstance(status_code, int):
        return None
    if selected_html is not None and not isinstance(selected_html, str):
        return None

    return PageState(status_code=status_code, selected_html=selected_html)


def _save_state(cache_path: Path, state: PageState) -> None:
    payload = {
        "status_code": state.status_code,
        "selected_html": state.selected_html,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")


def _build_diff(previous_html: str, current_html: str) -> Optional[str]:
    if previous_html == current_html:
        return None

    try:
        previous_lines = BeautifulSoup(previous_html, "html.parser").prettify().splitlines()
        current_lines = BeautifulSoup(current_html, "html.parser").prettify().splitlines()
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


def check_page_for_changes(url: str, selector: str) -> ChangeResult:
    current_state = fetch_page_state(url, selector)
    cache_path = get_cache_path(url, selector)
    previous_state = _load_previous_state(cache_path)

    _save_state(cache_path, current_state)

    previous_status_code = None if previous_state is None else previous_state.status_code
    diff_text: Optional[str] = None

    if (
        previous_state is not None
        and previous_state.selected_html is not None
        and current_state.selected_html is not None
    ):
        diff_text = _build_diff(previous_state.selected_html, current_state.selected_html)

    return ChangeResult(
        previous_status_code=previous_status_code,
        current_status_code=current_state.status_code,
        diff_text=diff_text,
        selected_html=current_state.selected_html,
    )


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python follow-that-page.py <url> <selector>", file=sys.stderr)
        sys.exit(2)

    url = sys.argv[1]
    selector = sys.argv[2]

    try:
        result = check_page_for_changes(url, selector)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(3)
    except requests.RequestException as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        sys.exit(4)

    if result.status_changed:
        print(
            f"HTTP status changed: {result.previous_status_code} -> {result.current_status_code}"
        )
    if result.diff_text:
        print(result.diff_text)
    sys.exit(0)


if __name__ == "__main__":
    main()
