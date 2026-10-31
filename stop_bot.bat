@echo off
chcp 65001 >nul
echo Останавливаю бота, который работает в фоне...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'bot_service\.bat|bot\.py' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; 'Бот остановлен.' } else { 'Бот не был запущен.' }"
pause
