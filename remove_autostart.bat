@echo off
chcp 65001 >nul
schtasks /Delete /TN "UYDA Import Bot" /F
echo Автозапуск отключён.
pause
