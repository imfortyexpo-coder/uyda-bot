@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist bot.db (echo Файл bot.db не найден в этой папке & pause & exit /b)
if not exist backups mkdir backups
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm"') do set STAMP=%%i
copy /y bot.db "backups\bot_%STAMP%.db" >nul
echo Копия создана: backups\bot_%STAMP%.db
echo Скопируйте папку backups на флешку или в облако.
pause
