# Убрать автозапуск и ярлык (данные и сама папка остаются).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
& "$Root\ops.cmd" stop
Remove-Item (Join-Path ([Environment]::GetFolderPath("Startup")) "PULSE.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "PULSE.lnk") -ErrorAction SilentlyContinue
# ярлыки до переименования в PULSE
Remove-Item (Join-Path ([Environment]::GetFolderPath("Startup")) "Пульт директора.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "Пульт директора.lnk") -ErrorAction SilentlyContinue
Write-Host "Автозапуск и ярлык удалены. Папку $Root можно удалить вручную (в data — ваши встречи и база)."
