#!/bin/bash
# Установка «PULSE» на macOS. Запускается из INSTALL.command
# (или в Терминале: bash INSTALL.command). Права администратора не нужны.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/mac-common.sh
source "$ROOT/scripts/mac-common.sh"

echo "Установка «PULSE» в $ROOT"
check_macos

step "1/6 Папка PULSE"
case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*|"$HOME/Library/Mobile Documents"*)
    warn "Папка лежит в «Документах», на «Рабочем столе», в «Загрузках» или в iCloud."
    warn "macOS защищает эти места отдельно, а iCloud может убрать файлы базы с диска."
    warn "Надёжнее перенести папку в $HOME/Pult и запустить установку оттуда."
    read -r -p "    Всё равно продолжить здесь? (y/N) " answer
    case "$answer" in y|Y|yes|д|Д|да|Да) ;; *) exit 1 ;; esac
    ;;
esac
# файлы из скачанного архива macOS помечает «из интернета» и не даёт запускать
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
chmod +x "$ROOT/ops" "$ROOT"/*.command "$ROOT"/scripts/*.sh
ok "$ROOT"

step "2/6 Менеджер Python (uv)"
if ! UV="$(find_uv)"; then
  echo "    Скачиваю uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || die "не удалось установить uv — проверьте интернет (docs/05-обслуживание.md)"
fi
ok "$UV"

step "3/6 Python и библиотеки (первый раз — 3-10 минут)"
# shellcheck disable=SC2046
"$UV" sync --no-dev $(uv_extras)
if [ -n "$(uv_extras)" ]; then ok "установлено, включено ускорение расшифровки на Apple Silicon (MLX)"; else ok "установлено"; fi

step "4/6 Папки и настройки"
mkdir -p config secrets data/recordings data/meetings data/logs
if [ ! -f config/settings.yaml ]; then
  cp config/settings.example.yaml config/settings.yaml
  ok "создан config/settings.yaml"
else
  ok "настройки уже есть — оставляю как есть"
fi
chmod 700 secrets
ok "папка secrets доступна только вам"

step "5/6 Claude Code"
if CLAUDE="$(find_claude)"; then
  ok "найден: $CLAUDE"
else
  read -r -p "    Claude Code не найден. Установить сейчас? (y/N) " answer
  case "$answer" in
    y|Y|yes|д|Д|да|Да)
      curl -fsSL https://claude.ai/install.sh | bash
      warn "Откройте новое окно Терминала, выполните команду claude и войдите в свой аккаунт Claude (один раз)."
      ;;
    *) warn "Без Claude Code будут работать сбор данных, расшифровка и дашборд, но не разбор встреч и сообщений." ;;
  esac
fi

step "6/6 Автозапуск и приложение «PULSE»"
"$ROOT/ops" mac install

echo
"$ROOT/ops" mac permissions
"$ROOT/ops" open
printf '\n\033[32mГотово! PULSE открыт в браузере: http://localhost:8765\033[0m\n'
echo "Дальше — подключения: docs/02-подключения.md (или раздел «Справка» в PULSE)."
echo "Открывать PULSE: Программы → «PULSE» (можно перетащить в Dock)."
