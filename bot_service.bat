@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem Работает в фоне: если бот упал — через 30 секунд запускается снова. Журнал — bot.log
:loop
for %%A in (bot.log) do if %%~zA GTR 10485760 move /y bot.log bot.old.log >nul
echo ===== %date% %time% запуск бота ===== >> bot.log
py -3.12 bot.py >> bot.log 2>&1
echo ===== %date% %time% бот остановился, перезапуск через 30 секунд ===== >> bot.log
timeout /t 30 /nobreak >nul
goto loop
