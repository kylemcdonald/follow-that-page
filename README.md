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
GEMINI_MODEL=gemini-flash-latest
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_OWNER_USERNAME=kcimc
CHECK_INTERVAL_SECONDS=300
VIBECHECK_API_BASE_URL=http://vibecheck.local:8765
VIBECHECK_API_TOKEN=
VIBECHECK_TIMEOUT_SECONDS=180
```

The app reads `.env`.

`VIBECHECK_API_TOKEN` is optional and is sent as `Authorization: Bearer <token>`
when configured.

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
  {
    "url": "https://en.wikipedia.org/wiki/Special:RecentChanges",
    "selector": "#mw-content-text",
    "reddit_post": {
      "target": "/u/kcimc",
      "title": "Wikipedia recent changes",
      "body": "Screenshot of Special:RecentChanges from Wikipedia."
    }
  }
]
```

## Notes

- A `.cache/` directory is created to store the last-seen HTML snippets for diffs.
- HTTP status transitions trigger notifications, including cases like `404 -> 200`.
- Transport failures such as invalid SSL certificates, timeouts, and connection errors are also tracked, so transitions like `ssl_error -> HTTP 200` trigger notifications.
- Notifications are sent through the Telegram Bot API with a short Gemini-generated summary followed by the page URL.
- The summarization uses `GEMINI_MODEL`, defaulting to `gemini-flash-latest`, via the `google-genai` Python SDK.
- Jobs with `reddit_post` configured call the Vibecheck API after a detected update and submit a screenshot image post to the configured Reddit target. If the configured Reddit body contains `{summary}`, it is replaced with the generated change summary before posting. After a successful Vibecheck response, the job records `reddit_post.posted_at` and will not post that link to Reddit again unless Reddit posting is reconfigured for the job.
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
- `/reddit <number-or-id>` starts a prompt flow to configure Reddit posting for a followed page.
- `/list` shows all followed pages and includes delete buttons.
- `/interval` shows the current check interval.
- `/interval <seconds>` sets the check interval. Minimum is `30`.
- `/cancel` cancels an in-progress Reddit setup.
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
