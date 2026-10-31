@echo off
chcp 65001 >nul
cd /d "%~dp0"
title UYDA: первая загрузка кода в GitHub
echo.
echo  Первая загрузка кода бота в GitHub (imfortyexpo-coder/uyda-bot)
echo  Токен, база, копии базы, файлы партий и таблица НЕ загружаются.
echo.
where git >nul 2>nul || (echo  [!] Git не установлен. Установите: https://git-scm.com/download/win & echo      Потом запустите этот файл снова. & pause & exit /b 1)
if not exist bot.py (echo  [!] Положите этот файл в папку бота, рядом с bot.py & pause & exit /b 1)
if not exist .gitignore (echo  [!] Нет файла .gitignore рядом с bot.py — скопируйте все файлы из архива & pause & exit /b 1)
if not exist .git (
  git init -q
  git branch -M main
)
git config user.name >nul 2>nul || git config user.name "imfortyexpo-coder"
git config user.email >nul 2>nul || git config user.email "imforty.expo@gmail.com"
git add -A
rem ── защита: проверяем, что секретные файлы не попали в список ──
git diff --cached --name-only > "%TEMP%\uyda_files.txt"
findstr /R /I /C:"^env$" /C:"\.env$" /C:"\.db$" /C:"^backups/" /C:"^docs/" /C:"service_account" /C:"^tok$" /C:"\.xlsx$" "%TEMP%\uyda_files.txt" >nul
if not errorlevel 1 (
  echo  [!] СТОП: в список попал секретный файл. Ничего не отправлено.
  findstr /R /I /C:"^env$" /C:"\.env$" /C:"\.db$" /C:"^backups/" /C:"^docs/" /C:"service_account" /C:"^tok$" /C:"\.xlsx$" "%TEMP%\uyda_files.txt"
  git reset -q
  pause & exit /b 1
)
echo  Будут загружены файлы:
type "%TEMP%\uyda_files.txt"
echo.
git commit -q -m "Версия 14 (31.10.2026): документы по этапам, доступы, дашборд" || echo  (новых изменений нет)
git remote get-url origin >nul 2>nul || git remote add origin https://github.com/imfortyexpo-coder/uyda-bot.git
echo  Отправляю в GitHub... (при первом разе откроется окно входа в GitHub — войдите)
git push -u origin main
if errorlevel 1 (echo. & echo  [!] Не удалось отправить. Пришлите снимок этого окна. & pause & exit /b 1)
echo.
echo  Готово! Код в https://github.com/imfortyexpo-coder/uyda-bot
pause
