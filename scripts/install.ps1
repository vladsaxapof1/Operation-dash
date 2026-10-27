# Установка PULSE на Windows. Запускается из INSTALL.bat.
# Права администратора не нужны: всё ставится в профиль пользователя.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "    OK  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    !   $t" -ForegroundColor Yellow }

Write-Host "Установка «PULSE» в $Root" -ForegroundColor White

# 1. uv — ставит нужный Python и библиотеки, не трогая системный Python
Step "1/6 Менеджер Python (uv)"
$uvPath = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $uvPath = $cmd.Source }
elseif (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") { $uvPath = "$env:USERPROFILE\.local\bin\uv.exe" }
else {
    Write-Host "    Скачиваю uv..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uvPath = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uvPath)) { throw "Не удалось установить uv. Проверьте интернет или прокси (см. docs\05-обслуживание.md)." }
}
Ok $uvPath

# 2. Python и библиотеки
Step "2/6 Python и библиотеки (первый раз — 3-10 минут)"
& $uvPath sync --no-dev
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить библиотеки (uv sync). Подробности выше." }
Ok "установлено в .venv"

# 3. Папки и настройки
Step "3/6 Папки и настройки"
New-Item -ItemType Directory -Force -Path config, secrets, data\recordings, data\meetings, data\logs | Out-Null
if (-not (Test-Path config\settings.yaml)) {
    Copy-Item config\settings.example.yaml config\settings.yaml
    Ok "создан config\settings.yaml"
} else { Ok "настройки уже есть — оставляю как есть" }
# папка с паролями и сессией Telegram — доступ только у текущего пользователя
icacls secrets /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
Ok "доступ к secrets — только у $($env:USERNAME)"

# 4. Git for Windows и Claude Code
Step "4/6 Git for Windows и Claude Code"
if (Get-Command git -ErrorAction SilentlyContinue) { Ok "Git найден" }
else { Warn "Git for Windows не найден — он нужен Claude Code. Установите: https://git-scm.com/download/win (все параметры по умолчанию), затем запустите INSTALL.bat ещё раз." }

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude -and (Test-Path "$env:USERPROFILE\.local\bin\claude.exe")) { $claude = "$env:USERPROFILE\.local\bin\claude.exe" }
if ($claude) { Ok "Claude Code найден" }
else {
    $answer = Read-Host "    Claude Code не найден. Установить сейчас? (Y/N)"
    if ($answer -match '^[YyДд]') {
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
        Warn "После установки откройте новое окно PowerShell, выполните команду claude и войдите в свой аккаунт Claude (один раз)."
    } else {
        Warn "Без Claude Code будут работать сбор данных, расшифровка и дашборд, но не разбор встреч и сообщений."
    }
}

# 5. Ярлыки: рабочий стол и автозапуск
Step "5/6 Ярлык на рабочем столе и автозапуск"
$shell = New-Object -ComObject WScript.Shell
$vbs = Join-Path $Root "scripts\start-hidden.vbs"
$icon = Join-Path $Root "scripts\pult.ico"
function MakeShortcut($path, $arguments) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = "$env:SystemRoot\System32\wscript.exe"
    $s.Arguments = "`"$vbs`" $arguments"
    $s.WorkingDirectory = $Root
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Description = "PULSE"
    $s.Save()
}
foreach ($dir in "Desktop", "Startup") {  # ярлыки до переименования в PULSE
    Remove-Item (Join-Path ([Environment]::GetFolderPath($dir)) "Пульт директора.lnk") -ErrorAction SilentlyContinue
}
MakeShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "PULSE.lnk") "open"
Ok "ярлык «PULSE» на рабочем столе"
MakeShortcut (Join-Path ([Environment]::GetFolderPath("Startup")) "PULSE.lnk") ""
Ok "PULSE будет запускаться при входе в Windows"

# 6. Запуск
Step "6/6 Запускаю PULSE"
Start-Process "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$vbs`" open"
Write-Host ""
Write-Host "Готово! Через несколько секунд откроется http://localhost:8765" -ForegroundColor Green
Write-Host "Дальше — подключения: docs\02-подключения.md (или раздел «Справка» в PULSE)."
