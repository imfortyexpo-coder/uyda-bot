@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot\.py' }) { 'Бот РАБОТАЕТ.' } else { 'Бот НЕ работает. Запустите start_hidden.vbs' }; ''; 'Последние строки журнала bot.log:'; if (Test-Path bot.log) { Get-Content bot.log -Tail 25 -Encoding UTF8 } else { 'журнала пока нет' }"
pause
