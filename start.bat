@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск бота UYDA Импорт (Python 3.12). Не закрывайте это окно.
echo Остановить бота: Ctrl+C
py -3.12 bot.py
pause
