#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$SCRIPT_DIR/systemd/follow-that-page.service"
UNIT_DST="$HOME/.config/systemd/user/follow-that-page.service"

mkdir -p "$HOME/.config/systemd/user"
cp "$UNIT_SRC" "$UNIT_DST"
if systemctl --user list-unit-files | grep -q '^follow-that-page.timer'; then
  systemctl --user disable --now follow-that-page.timer || true
  rm -f "$HOME/.config/systemd/user/follow-that-page.timer"
fi
systemctl --user daemon-reload
systemctl --user enable --now follow-that-page.service
systemctl --user status --no-pager follow-that-page.service
