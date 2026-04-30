# follow-that-page

Simple webpage change checker that monitors specific CSS selectors on pages. It can summarize detected changes with Gemini, send notifications via a Telegram bot, and let the bot owner manage followed pages directly from Telegram.

## Quick start

- Requirements: Python 3.9+, `uv` package manager.

```bash
# Install dependencies
uv sync

# Run against a single URL/selector
uv run follow_that_page.py "https://example.com" "#main"

# Run one monitoring pass against the current job list
uv run run_list.py

# Run the Telegram bot daemon
uv run python telegram_daemon.py
```

## Environment variables

Loaded from `.env` via `python-dotenv`.

```
GEMINI_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_OWNER_USERNAME=kcimc
CHECK_INTERVAL_SECONDS=300
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

- Long-running Telegram bot daemon:

```bash
uv run python telegram_daemon.py
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
- HTTP status transitions also trigger notifications, including cases like `404 -> 200`.
- Notifications are sent through the Telegram Bot API with a short Gemini-generated summary followed by the page URL.
- The summarization uses model `gemini-2.5-flash` via the `google-genai` Python SDK.
- The Telegram bot accepts commands only from the configured owner username and chat ID.

## Telegram setup

1. Create a bot with `@BotFather` in Telegram using `/newbot`.
2. Save the bot token that BotFather returns.
3. Start a chat with your bot and send it any message.
4. Open:

```text
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

5. Find the chat ID in the JSON response under `message.chat.id`.
6. Set these values in `.env`:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_OWNER_USERNAME=kcimc
CHECK_INTERVAL_SECONDS=300
```

## Telegram commands

- `/follow <url> [selector]` adds a page to monitor. If no selector is provided, it uses `body`. Non-200 pages can still be followed so you can detect when they go live later.
- `/list` shows all followed pages and includes delete buttons.
- `/help` shows command help.

## systemd

Install and start the user service:

```bash
./install_systemd_user_service.sh
```

Useful commands:

```bash
systemctl --user status follow-that-page.service
journalctl --user -u follow-that-page.service -f
systemctl --user restart follow-that-page.service
```
