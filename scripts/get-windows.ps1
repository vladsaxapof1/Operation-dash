# Установка PULSE на Windows одной строкой — вставить в PowerShell:
#   irm https://raw.githubusercontent.com/vladsaxapof1/Operation-dash/claude/pult-v1/scripts/get-windows.ps1 | iex
# Скачивает PULSE с GitHub в папку %USERPROFILE%\Pult и запускает установщик (scripts\install.ps1).
# Повторный запуск ничего не скачивает — снова запускает установщик (настройки и данные остаются).
# Для проверки установки (.github/workflows/windows.yml): PULSE_REPO, PULSE_REF — ветка или коммит, PULSE_DIR — папка.
# Файл без BOM: его читает irm (кодировку берёт из ответа GitHub), а не Windows PowerShell с диска.
& {
    $ErrorActionPreference = "Stop"
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $Repo = if ($env:PULSE_REPO) { $env:PULSE_REPO } else { "vladsaxapof1/Operation-dash" }
    $Ref = if ($env:PULSE_REF) { $env:PULSE_REF } else { "claude/pult-v1" }
    $Dest = if ($env:PULSE_DIR) { $env:PULSE_DIR } else { Join-Path $env:USERPROFILE "Pult" }

    if (Test-Path (Join-Path $Dest "scripts\install.ps1")) {
        Write-Host "PULSE уже есть в $Dest — запускаю установку ещё раз (настройки и данные останутся)." -ForegroundColor Cyan
    } else {
        if ((Test-Path $Dest) -and (Get-ChildItem -LiteralPath $Dest -Force | Select-Object -First 1)) {
            throw "Папка $Dest уже занята другими файлами. Переименуйте её и запустите команду снова."
        }
        $url = "https://github.com/$Repo/archive/refs/heads/$Ref.zip"
        if ($Ref -match '^[0-9a-f]{40}$') { $url = "https://github.com/$Repo/archive/$Ref.zip" }  # коммит
        $parent = Split-Path -Parent $Dest
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        # временная папка рядом с Dest, а не в TEMP: папку потом переносим, а между дисками Move-Item не умеет
        $tmp = Join-Path $parent (".pulse-download-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            Write-Host "Скачиваю PULSE с GitHub..." -ForegroundColor Cyan
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $ProgressPreference = "SilentlyContinue"  # с полоской прогресса Windows PowerShell качает в разы медленнее
            Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp "pulse.zip") -UseBasicParsing
            Expand-Archive -LiteralPath (Join-Path $tmp "pulse.zip") -DestinationPath (Join-Path $tmp "x")
            $inner = Get-ChildItem -LiteralPath (Join-Path $tmp "x") -Directory | Select-Object -First 1
            if (Test-Path $Dest) { Remove-Item -LiteralPath $Dest -Force }  # пустая — проверили выше
            Move-Item -LiteralPath $inner.FullName -Destination $Dest
        } finally {
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "PULSE скачан в $Dest" -ForegroundColor Green
    }
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dest "scripts\install.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Установка не закончилась — причина выше. Запустить её снова: $Dest\INSTALL.bat" }
}
