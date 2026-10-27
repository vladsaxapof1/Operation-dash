#!/bin/bash
# macOS: обновить PULSE
cd "$(dirname "$0")" || exit 1
bash scripts/update-mac.sh
echo
read -r -p "Нажмите Enter, чтобы закрыть окно…" _
