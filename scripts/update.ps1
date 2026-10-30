# Обновление PULSE. Данные (data), настройки (config) и пароли (secrets) не затрагиваются.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$uvPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uvPath) { $uvPath = "$env:USERPROFILE\.local\bin\uv.exe" }

Write-Host "==> Останавливаю PULSE" -ForegroundColor Cyan
& "$Root\ops.cmd" stop
# ждём, пока PULSE закроется и отпустит файлы библиотек: пока он работает, он держит блокировку data\pult.lock
$lock = Join-Path $Root "data\pult.lock"
for ($i = 0; $i -lt 60 -and (Test-Path $lock); $i++) {
    $f = $null
    try { $f = [IO.File]::Open($lock, "Open", "ReadWrite", "ReadWrite"); $f.Lock(0, 1); $f.Unlock(0, 1); break }
    catch { Start-Sleep -Seconds 1 }
    finally { if ($f) { $f.Close() } }
}

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
# в папке из git (у разработчика) оставляем и инструменты разработки: тесты — как при обновлении кнопкой
if (Test-Path "$Root\.git") { & $uvPath sync } else { & $uvPath sync --no-dev }
if ($LASTEXITCODE -ne 0) { throw "uv sync завершился с ошибкой" }

Write-Host "==> Запускаю PULSE" -ForegroundColor Cyan
Start-Process "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$Root\scripts\start-hidden.vbs`" open"
Write-Host "Готово." -ForegroundColor Green
