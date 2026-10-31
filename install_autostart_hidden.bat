@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Бот будет сам запускаться В ФОНЕ при входе в Windows (без чёрного окна).
schtasks /Create /TN "UYDA Import Bot" /TR "wscript.exe \"%~dp0start_hidden.vbs\" /silent" /SC ONLOGON /F
if errorlevel 1 (
  echo Не получилось. Нажмите на этот файл правой кнопкой - "Запуск от имени администратора".
  pause
  exit /b
)
echo Готово. После следующего входа в Windows бот запустится сам, окно не появится.
echo Проверить: check_bot.bat   Остановить: stop_bot.bat
pause
