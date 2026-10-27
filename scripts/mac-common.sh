# Общие функции установщика для macOS (подключается через source).

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mOK\033[0m  %s\n' "$1"; }
warn() { printf '    \033[33m!\033[0m   %s\n' "$1"; }
die()  { printf '\n\033[31mОшибка: %s\033[0m\n' "$1"; exit 1; }

# Версия macOS (основной номер) и архитектура: arm64 — Apple Silicon, x86_64 — Intel
MAC_MAJOR="$(sw_vers -productVersion 2>/dev/null | cut -d. -f1)"
ARCH="$(uname -m)"

check_macos() {
  [ "$(uname -s)" = "Darwin" ] || die "этот установщик — для macOS. На Windows запустите INSTALL.bat"
  [ "${MAC_MAJOR:-0}" -ge 13 ] || die "нужна macOS 13 Ventura или новее (сейчас $(sw_vers -productVersion))"
}

# Ускорение расшифровки (MLX) — только Apple Silicon и macOS 14+
uv_extras() {
  if [ "$ARCH" = "arm64" ] && [ "${MAC_MAJOR:-0}" -ge 14 ]; then
    echo "--extra mlx"
  fi
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$p" ]; then echo "$p"; return 0; fi
  done
  return 1
}

find_claude() {
  if command -v claude >/dev/null 2>&1; then command -v claude; return 0; fi
  for p in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if [ -x "$p" ]; then echo "$p"; return 0; fi
  done
  return 1
}
