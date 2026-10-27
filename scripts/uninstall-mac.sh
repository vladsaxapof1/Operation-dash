#!/bin/bash
# Убрать автозапуск и приложение «PULSE» (данные и сама папка остаются).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/ops" stop || true
"$ROOT/ops" mac uninstall
echo "Папку $ROOT можно удалить вручную (в data — ваши встречи и база)."
