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
    state_kind: str
    status_code: Optional[int]
    error_kind: Optional[str]
    error_detail: Optional[str]
    selected_html: Optional[str]


@dataclass
class ChangeResult:
    previous_state_label: Optional[str]
    current_state_label: str
    diff_text: Optional[str]
    selected_html: Optional[str]

    @property
    def status_changed(self) -> bool:
        return (
            self.previous_state_label is not None
            and self.previous_state_label != self.current_state_label
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
    try:
        resp = _request_page(url)
    except requests.exceptions.SSLError as exc:
        return PageState(
            state_kind="transport_error",
            status_code=None,
            error_kind="ssl_error",
            error_detail=str(exc),
            selected_html=None,
        )
    except requests.exceptions.Timeout as exc:
        return PageState(
            state_kind="transport_error",
            status_code=None,
            error_kind="timeout",
            error_detail=str(exc),
            selected_html=None,
        )
    except requests.exceptions.ConnectionError as exc:
        return PageState(
            state_kind="transport_error",
            status_code=None,
            error_kind="connection_error",
            error_detail=str(exc),
            selected_html=None,
        )
    except requests.RequestException as exc:
        return PageState(
            state_kind="transport_error",
            status_code=None,
            error_kind="request_error",
            error_detail=str(exc),
            selected_html=None,
        )

    if resp.status_code != 200:
        return PageState(
            state_kind="http",
            status_code=resp.status_code,
            error_kind=None,
            error_detail=None,
            selected_html=None,
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    element = soup.select_one(selector)
    if element is None:
        raise ValueError(f"Selector not found: {selector}")

    return PageState(
        state_kind="http",
        status_code=resp.status_code,
        error_kind=None,
        error_detail=None,
        selected_html=str(element),
    )


def fetch_selected_html(url: str, selector: str) -> str:
    state = fetch_page_state(url, selector)
    if state.state_kind != "http":
        raise ValueError(
            f"Expected HTTP 200 but got transport error: {state.error_kind}"
        )
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
        return PageState(
            state_kind="http",
            status_code=200,
            error_kind=None,
            error_detail=None,
            selected_html=raw,
        )

    if not isinstance(data, dict):
        return None

    state_kind = data.get("state_kind")
    status_code = data.get("status_code")
    error_kind = data.get("error_kind")
    error_detail = data.get("error_detail")
    selected_html = data.get("selected_html")
    if state_kind is None:
        if isinstance(status_code, int):
            state_kind = "http"
        else:
            return None
    if not isinstance(state_kind, str):
        return None
    if status_code is not None and not isinstance(status_code, int):
        return None
    if error_kind is not None and not isinstance(error_kind, str):
        return None
    if error_detail is not None and not isinstance(error_detail, str):
        return None
    if selected_html is not None and not isinstance(selected_html, str):
        return None

    return PageState(
        state_kind=state_kind,
        status_code=status_code,
        error_kind=error_kind,
        error_detail=error_detail,
        selected_html=selected_html,
    )


def _save_state(cache_path: Path, state: PageState) -> None:
    payload = {
        "state_kind": state.state_kind,
        "status_code": state.status_code,
        "error_kind": state.error_kind,
        "error_detail": state.error_detail,
        "selected_html": state.selected_html,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")


def describe_state(state: PageState) -> str:
    if state.state_kind == "http":
        return f"HTTP {state.status_code}"
    if state.error_kind:
        return state.error_kind
    return state.state_kind


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

    previous_state_label = None if previous_state is None else describe_state(previous_state)
    diff_text: Optional[str] = None

    if (
        previous_state is not None
        and previous_state.selected_html is not None
        and current_state.selected_html is not None
    ):
        diff_text = _build_diff(previous_state.selected_html, current_state.selected_html)

    return ChangeResult(
        previous_state_label=previous_state_label,
        current_state_label=describe_state(current_state),
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
    if result.status_changed:
        print(
            f"State changed: {result.previous_state_label} -> {result.current_state_label}"
        )
    if result.diff_text:
        print(result.diff_text)
    sys.exit(0)


if __name__ == "__main__":
    main()
