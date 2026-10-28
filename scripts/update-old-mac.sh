#!/bin/bash
# Обновить PULSE на macOS, где в настройках ещё нет кнопки «Обновить» (версии до 27.10.2026).
# Запуск — одной строкой в Терминале (файл, присланный через Telegram, macOS открыть не даст):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/vladsaxapof1/Operation-dash/claude/pult-v1/scripts/update-old-mac.sh)"
# Данные (data), настройки (config/settings.yaml) и пароли (secrets) не трогает. Дальше PULSE
# обновляется сам: Настройки → Основное → «Обновления PULSE» → «Обновить».
set -uo pipefail
REPO="vladsaxapof1/Operation-dash"
BRANCH="claude/pult-v1"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*"; exit 1; }
is_pulse() { [ -n "$1" ] && [ -d "$1/opsdash" ] && [ -f "$1/ops" ]; }

# где стоит PULSE: папка, из которой запускается его фоновая служба; иначе — спросить
ROOT=""
PLIST="$HOME/Library/LaunchAgents/local.pult.director.plist"
[ -f "$PLIST" ] && ROOT="$(/usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$PLIST" 2>/dev/null || true)"
if ! is_pulse "$ROOT"; then
  ROOT="$(osascript -e 'POSIX path of (choose folder with prompt "Выберите папку PULSE (в ней лежит START.command)")' 2>/dev/null || true)"
  ROOT="${ROOT%/}"
fi
is_pulse "$ROOT" || fail "Не нашла папку PULSE. Запустите ещё раз и выберите папку, в которой лежит START.command."
say "Папка PULSE: $ROOT"

if [ ! -d "$ROOT/.git" ]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  say "Скачиваю новую версию PULSE…"
  curl -fsSL "https://codeload.github.com/$REPO/zip/refs/heads/$BRANCH" -o "$TMP/pulse.zip" \
    || fail "Не удалось скачать новую версию — проверьте интернет и запустите ещё раз."
  # ditto, а не unzip: unzip из macOS ломается на русских именах файлов
  ditto -x -k "$TMP/pulse.zip" "$TMP/new" || fail "Скачанный архив повреждён — запустите ещё раз."
  SRC="$(find "$TMP/new" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  is_pulse "$SRC" || fail "В скачанном архиве нет PULSE."

  say "Останавливаю PULSE…"
  "$ROOT/ops" stop >/dev/null 2>&1 || true
  if [ -f "$ROOT/data/ops.db" ]; then  # на всякий случай — копия базы рядом с ней
    cp -c "$ROOT/data/ops.db" "$ROOT/data/ops.db.before-update" 2>/dev/null \
      || cp "$ROOT/data/ops.db" "$ROOT/data/ops.db.before-update" || fail "Не удалось сделать копию базы — обновление отменено."
  fi

  say "Кладу новую версию (данные, настройки и пароли остаются как были)…"
  rsync -a --exclude '/data/' --exclude '/secrets/' --exclude '/config/settings.yaml' --exclude '/.venv/' \
    --exclude '/.git/' --exclude '/dist/' --exclude '/.claude/settings.local.json' "$SRC/" "$ROOT/" \
    || fail "Не удалось разложить файлы новой версии."
fi

# дальше — обычное обновление уже новой версии: библиотеки, отметка о версии, перезапуск
bash "$ROOT/scripts/update-mac.sh" || fail "Обновление не завершилось — подробности выше."
say "PULSE обновлён. Следующие обновления — кнопкой: Настройки → Основное → «Обновления PULSE» → «Обновить»."
