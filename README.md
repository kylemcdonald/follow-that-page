# follow-that-page

Simple webpage change checker that monitors specific CSS selectors on pages, summarizes relevant changes with OpenAI, sends notifications via Telegram, and lets the bot owner manage followed pages directly from Telegram.

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
OPENAI_API_KEY=
OPENAI_MODEL=gpt-6-luna
OPENAI_REASONING_EFFORT=minimal
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_OWNER_USERNAME=kcimc
CHECK_INTERVAL_SECONDS=300
```

The app reads `.env`.

## Usage

- One-off check for a single page section:

```bash
uv run follow_that_page.py "https://example.com" "#main"
```

- Compare only normalized text (ignore scripts and HTML attributes):

```bash
uv run follow_that_page.py "https://example.com" "#main" text
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
  {
    "url": "https://www.apple.com/ca/shop/refurbished/mac/macbook-air",
    "selector": "body",
    "mode": "robust",
    "change_criteria": "Notify only when refurbished MacBook Air inventory changes: a listing is added, removed, or its availability, price, or listed configuration changes."
  }
]
```

## Notes

- A `.cache/` directory is created to store the last-seen HTML snippets for diffs.
- In standard mode, HTTP status transitions trigger notifications, including cases like `404 -> 200`.
- Transport failures such as invalid SSL certificates, timeouts, and connection errors are tracked in standard mode, so transitions like `ssl_error -> HTTP 200` trigger notifications.
- Notifications are sent through the Telegram Bot API with an OpenAI-generated summary followed by the page URL.
- The OpenAI client uses `OPENAI_MODEL`, defaulting to `gpt-6-luna`, and `OPENAI_REASONING_EFFORT`, defaulting to `minimal`.
- Jobs use `standard` mode by default, which notifies on every detected selector or state change. A `robust` job asks the model to evaluate each content diff against its required `change_criteria`. It sends an update only when the model finds clear evidence that the criterion was met; errors, incomplete output, state-only changes, and uncertain results are suppressed.
- Set `"mode": "text"` for text-only comparison, with no criteria required. This mode strips scripts, styles, templates, noscript content, comments, and HTML attributes, and normalizes whitespace before comparing text. It suppresses status-only alerts and retains the last successful baseline across outages. Existing HTML caches work immediately when switching modes. Only changed text is sent for summarization.
- Text mode compares the fetched HTML, without running JavaScript or evaluating CSS visibility. It can include hidden or unrelated text and miss changes represented only in attributes or scripts. For example, Apple's MacBook Air URL includes other Mac inventory in its source HTML; text mode removes rotating URL-token noise but does not filter that inventory to MacBook Air. Use a narrower selector or robust criteria when needed.
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

- `/follow <url> [selector]` offers Standard, Robust, and Text only modes before saving the page. Standard and Text only save immediately; Robust asks for the changes that should trigger notifications. The mode and criteria are saved for that link. If no selector is provided, it uses `body`. Non-200 pages can still be followed; standard mode reports status transitions when they go live. A new `/follow` replaces any unfinished follow question; following an existing URL and selector updates its mode.
- `/list` shows all followed pages, their monitoring mode, and delete buttons.
- `/interval` shows the current check interval.
- `/interval <seconds>` sets the check interval. Minimum is `30`.
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
