#!/bin/bash
# Обновление PULSE на macOS. Данные (data), настройки (config) и пароли (secrets) не затрагиваются.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/mac-common.sh
source "$ROOT/scripts/mac-common.sh"
check_macos

step "Останавливаю PULSE"
"$ROOT/ops" stop || true

if [ -d "$ROOT/.git" ]; then
  step "Скачиваю новую версию (git pull)"
  git pull --ff-only || die "git pull не удался — возможно, файлы PULSE меняли вручную"
else
  # папка из архива: PULSE сам скачает новую версию с GitHub (Настройки → Основное → «Обновления»)
  step "Скачиваю новую версию с GitHub"
  "$ROOT/ops" update --no-restart || die "не удалось скачать новую версию (подробности выше). Можно распаковать новый архив поверх этой папки, НЕ удаляя config, secrets и data, и запустить UPDATE.command снова."
fi
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
chmod +x "$ROOT/ops" "$ROOT"/*.command "$ROOT"/scripts/*.sh

step "Обновляю библиотеки"
UV="$(find_uv)" || die "uv не найден — запустите INSTALL.command"
# shellcheck disable=SC2046
"$UV" sync --no-dev $(uv_extras)

step "Перезапускаю PULSE"
"$ROOT/ops" mac install
"$ROOT/ops" open
printf '\n\033[32mГотово.\033[0m\n'
