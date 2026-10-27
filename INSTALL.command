#!/bin/bash
# macOS: двойной щелчок — установка PULSE
cd "$(dirname "$0")" || exit 1
bash scripts/install-mac.sh
echo
read -r -p "Нажмите Enter, чтобы закрыть окно…" _
