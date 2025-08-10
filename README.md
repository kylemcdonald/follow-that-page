# follow-that-page

Simple webpage change checker that monitors specific CSS selectors on pages. It can summarize detected changes with Gemini and send notifications via ntfy.

## Quick start

- Requirements: Python 3.9+, `uv` package manager.

```bash
# Install dependencies
uv sync

# Copy environment template and fill in your values
cp .env.local .env

# Run against a single URL/selector
uv run follow_that_page.py "https://example.com" "#main"

# Or run a list of jobs (default: jobs.json)
uv run run_list.py
```

## Environment variables

Loaded from `.env` via `python-dotenv`.

```
GEMINI_API_KEY=
NTFY_TOPIC_ID=
```

By default the app reads `.env`. You can keep local values in `.env.local`; copy it to `.env` for the app to load.

## Usage

- One-off check for a single page section:

```bash
uv run follow_that_page.py "https://example.com" "#main"
```

- Batch mode using a JSON job list (see `jobs.json`):

```bash
uv run run_list.py jobs.json
```

`jobs.json` example:

```json
[
  { "url": "https://swappa.com/listings/macbook-air-2025-m4-15", "selector": "#section_main" },
  { "url": "https://en.wikipedia.org/wiki/Special:RecentChanges", "selector": "#mw-content-text" }
]
```

## Notes

- A `.cache/` directory is created to store the last-seen HTML snippets for diffs.
- Notifications are sent to the resolved ntfy topic URL with a short Gemini-generated summary followed by the page URL.
- The summarization uses model `gemini-2.5-flash` via the `google-genai` Python SDK.


