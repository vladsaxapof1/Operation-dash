# Установка PULSE на Windows. Запускается из INSTALL.bat или установкой одной строкой (scripts\get-windows.ps1).
# Права администратора не нужны: всё ставится в профиль пользователя.
# PULSE_UNATTENDED=1 — без вопросов (проверка установки в GitHub Actions): на все вопросы ответ «нет».
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "    OK  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "    !   $t" -ForegroundColor Yellow }
function Ask($q) {
    if ($env:PULSE_UNATTENDED -eq "1") { return $false }
    return (Read-Host "    $q (Y/N)") -match '^[YyДд]'
}
# Внешняя программа: что она пишет в stderr (uv — ход загрузки), не ошибка — судим по коду выхода
function Native {
    $exe = $args[0]
    $rest = @(); if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @rest } finally { $ErrorActionPreference = $old }
}
function Under($path, $base) {
    if (-not $base) { return $false }
    return ($path.TrimEnd('\') + '\').StartsWith($base.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

Write-Host "Установка «PULSE» в $Root" -ForegroundColor White

# 1. Папка: не из архива, не в OneDrive — иначе база может пропасть с диска
Step "1/7 Папка PULSE"
if (-not [Environment]::Is64BitOperatingSystem) { throw "PULSE работает только на 64-битной Windows 10/11." }
if ((Under $Root $env:TEMP) -or ($Root -like "*\AppData\Local\Temp\*")) {
    throw "INSTALL.bat запущен прямо из архива. Сначала распакуйте архив (правый клик → «Извлечь все»), перенесите папку в $env:USERPROFILE\Pult и запустите INSTALL.bat оттуда."
}
$where = $null
foreach ($od in $env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer) { if (Under $Root $od) { $where = "в OneDrive" } }
if (-not $where -and (Under $Root ([Environment]::GetFolderPath("Desktop")))) { $where = "на рабочем столе" }
if (-not $where -and (Under $Root (Join-Path $env:USERPROFILE "Downloads"))) { $where = "в «Загрузках»" }
if ($where) {
    Warn "Папка PULSE лежит $where. OneDrive может убрать базу с диска или заблокировать её во время синхронизации, а «Загрузки» Windows иногда чистит сама."
    Warn "Надёжнее перенести папку в $env:USERPROFILE\Pult и запустить INSTALL.bat оттуда."
    if (-not (Ask "Всё равно продолжить здесь?")) { exit 1 }
}
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    Warn "Процессор ARM (Snapdragon): PULSE проверен на Intel и AMD, расшифровка встреч может не заработать."
}
# файлы из скачанного архива Windows помечает «из интернета» и переспрашивает перед запуском
Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin ".venv", "data", ".git" } | ForEach-Object {
    if ($_.PSIsContainer) { Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force } else { $_ }
} | Unblock-File -ErrorAction SilentlyContinue
Ok $Root

# 2. uv — ставит нужный Python и библиотеки, не трогая системный Python
Step "2/7 Менеджер Python (uv)"
$uvPath = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $uvPath = $cmd.Source }
elseif (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") { $uvPath = "$env:USERPROFILE\.local\bin\uv.exe" }
elseif (Test-Path "$env:USERPROFILE\.cargo\bin\uv.exe") { $uvPath = "$env:USERPROFILE\.cargo\bin\uv.exe" }
else {
    Write-Host "    Скачиваю uv..."
    Native powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uvPath = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uvPath)) { throw "Не удалось установить uv. Проверьте интернет или прокси (см. docs\05-обслуживание.md)." }
}
Ok $uvPath

# 3. Python и библиотеки
Step "3/7 Python и библиотеки (первый раз — 3-10 минут)"
Native $uvPath sync --no-dev
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить библиотеки (uv sync). Подробности выше." }
# расшифровке и разметке спикеров нужны библиотеки Microsoft Visual C++ — на чистой Windows их может не быть
Native "$Root\.venv\Scripts\python.exe" -c "import ctranslate2, onnxruntime, sherpa_onnx, av"
if ($LASTEXITCODE -ne 0) {
    Warn "Не загрузились библиотеки распознавания речи — скорее всего, на компьютере нет Microsoft Visual C++ Redistributable."
    Warn "Установите его: https://aka.ms/vs/17/release/vc_redist.x64.exe (нужны права администратора), затем запустите INSTALL.bat ещё раз."
    Warn "До этого не будет работать расшифровка встреч; остальное работает."
} else { Ok "установлено в .venv" }

# 4. Папки и настройки
Step "4/7 Папки и настройки"
New-Item -ItemType Directory -Force -Path config, secrets, data\recordings, data\meetings, data\logs | Out-Null
if (-not (Test-Path config\settings.yaml)) {
    Copy-Item config\settings.example.yaml config\settings.yaml
    Ok "создан config\settings.yaml"
} else { Ok "настройки уже есть — оставляю как есть" }
# папка с паролями и сессией Telegram — доступ только у текущего пользователя (по SID: имя бывает и не латиницей,
# и с доменом — а с SID icacls не промахнётся)
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Native icacls secrets /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "доступ к secrets — только у $($env:USERNAME)" }
else { Warn "не удалось закрыть папку secrets от других пользователей компьютера — PULSE работает и так" }

# 5. Git for Windows и Claude Code
Step "5/7 Git for Windows и Claude Code"
$gitFound = [bool](Get-Command git -ErrorAction SilentlyContinue)
foreach ($p in "$env:ProgramFiles\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe") { if (Test-Path $p) { $gitFound = $true } }
if ($gitFound) { Ok "Git найден" }
else { Warn "Git for Windows не найден — без него Claude Code не работает. Установите: https://git-scm.com/download/win (все параметры по умолчанию), затем запустите INSTALL.bat ещё раз." }

$claude = $null
$cmd = Get-Command claude -ErrorAction SilentlyContinue
if ($cmd) { $claude = $cmd.Source }
elseif (Test-Path "$env:USERPROFILE\.local\bin\claude.exe") { $claude = "$env:USERPROFILE\.local\bin\claude.exe" }
if ($claude) { Ok "Claude Code найден" }
elseif (Ask "Claude Code не найден. Установить сейчас?") {
    Native powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
    if (Test-Path "$env:USERPROFILE\.local\bin\claude.exe") { $claude = "$env:USERPROFILE\.local\bin\claude.exe"; Ok "Claude Code установлен" }
    else { Warn "Claude Code не установился — подробности выше. Установить можно и позже: PULSE → Настройки → Claude." }
} else {
    Warn "Без Claude Code будут работать сбор данных, расшифровка и дашборд, но не разбор встреч и сообщений."
}
if ($claude) {
    $status = $null
    try { $status = Native $claude auth status --json | Out-String | ConvertFrom-Json } catch { }
    if ($status.loggedIn) { Ok "вход в Claude выполнен" }
    elseif ($status -and (Ask "Войти в Claude сейчас? Откроется браузер — войдите аккаунтом Claude.ai")) {
        $ErrorActionPreference = "Continue"
        & $claude auth login --claudeai  # напрямую, не через Native: вход ведёт диалог в этом же окне
        $ErrorActionPreference = "Stop"
    } else { Warn "Войти в Claude можно позже: PULSE → Настройки → Claude → «Войти в Claude»." }
}

# 6. Ярлыки: рабочий стол, меню «Пуск» и автозапуск
Step "6/7 Ярлыки и автозапуск"
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
MakeShortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "PULSE.lnk") "open"
Ok "ярлык «PULSE» на рабочем столе и в меню «Пуск»"
MakeShortcut (Join-Path ([Environment]::GetFolderPath("Startup")) "PULSE.lnk") ""
Ok "PULSE будет запускаться при входе в Windows и перезапускаться после сбоя"

# 7. Запуск
Step "7/7 Запускаю PULSE"
Start-Process "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$vbs`" open"
Write-Host ""
Write-Host "Готово! Через несколько секунд откроется http://localhost:8765" -ForegroundColor Green
Write-Host "Дальше — подключения: docs\02-подключения.md (или раздел «Справка» в PULSE)."
Write-Host "Открывать PULSE: ярлык «PULSE» на рабочем столе или в меню «Пуск»."
