# Обновление PULSE. Данные (data), настройки (config) и пароли (secrets) не затрагиваются.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$uvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uvPath) { $uvPath = "$env:USERPROFILE\.local\bin\uv.exe" }

Write-Host "==> Останавливаю PULSE" -ForegroundColor Cyan
& "$Root\ops.cmd" stop
Start-Sleep -Seconds 2

if (Test-Path "$Root\.git") {
    Write-Host "==> Скачиваю новую версию (git pull)" -ForegroundColor Cyan
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull не удался — возможно, файлы PULSE меняли вручную." }
} else {
    # папка из архива: PULSE сам скачает новую версию с GitHub (Настройки → Основное → «Обновления»)
    Write-Host "==> Скачиваю новую версию с GitHub" -ForegroundColor Cyan
    & "$Root\ops.cmd" update --no-restart
    if ($LASTEXITCODE -ne 0) { throw "не удалось скачать новую версию. Можно распаковать новый архив поверх этой папки, НЕ удаляя config, secrets и data, и запустить UPDATE.bat снова." }
}

Write-Host "==> Обновляю библиотеки" -ForegroundColor Cyan
& $uvPath sync --no-dev
if ($LASTEXITCODE -ne 0) { throw "uv sync завершился с ошибкой" }

Write-Host "==> Запускаю PULSE" -ForegroundColor Cyan
Start-Process "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$Root\scripts\start-hidden.vbs`" open"
Write-Host "Готово." -ForegroundColor Green
