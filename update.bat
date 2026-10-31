@echo off
chcp 65001 >nul
cd /d "%~dp0"
title UYDA: обновление бота из GitHub
where git >nul 2>nul || (echo  [!] Git не установлен: https://git-scm.com/download/win & pause & exit /b 1)
if not exist .git (echo  [!] Сначала запустите github_setup.bat & pause & exit /b 1)
for /f %%i in ('git rev-parse --short HEAD') do set OLD=%%i
echo  Текущая версия: %OLD%
git pull --ff-only origin main
if errorlevel 1 (echo. & echo  [!] Обновить не удалось. Пришлите снимок этого окна. & pause & exit /b 1)
for /f %%i in ('git rev-parse --short HEAD') do set NEW=%%i
echo.
if "%OLD%"=="%NEW%" (echo  Обновлений нет — у вас последняя версия.) else (
  echo  Обновлено: %OLD% -^> %NEW%
  git log --oneline %OLD%..%NEW%
  echo.
  echo  Вернуть прежнюю версию при проблемах: git reset --hard %OLD%
)
echo.
echo  Теперь перезапустите бота: закройте его окно (Ctrl+C) и запустите start.bat
pause
