@echo off
rem Mantém o Clipador rodando: se o processo cair (erro, falta de energia + logon), reinicia em 20 s.
rem Uso: scripts\run_forever.bat   (ou instalado no logon por scripts\install_autostart.ps1)
cd /d "%~dp0.."
:loop
echo [%date% %time%] iniciando clipador run >> data\logs\run_forever.log
".venv\Scripts\python.exe" -m clipador run
echo [%date% %time%] clipador saiu com codigo %errorlevel%; reiniciando em 20 s >> data\logs\run_forever.log
timeout /t 20 /nobreak > nul
goto loop
