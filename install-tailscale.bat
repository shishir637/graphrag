@echo off
cd /d "%~dp0"
echo Installing Tailscale (official package via winget). Click Yes if Windows asks. Log: tailscale-setup.log
echo ===== %date% %time% install > tailscale-setup.log
winget install -e --id Tailscale.Tailscale --accept-source-agreements --accept-package-agreements >> tailscale-setup.log 2>&1
echo exit %errorlevel% >> tailscale-setup.log
set TS="C:\Program Files\Tailscale\tailscale.exe"
echo ===== up >> tailscale-setup.log
echo.
echo A browser window opens: sign in to Tailscale there.
%TS% up --timeout=600s >> tailscale-setup.log 2>&1
echo exit %errorlevel% >> tailscale-setup.log
%TS% status >> tailscale-setup.log 2>&1
echo DONE >> tailscale-setup.log
echo Done. You can close this window.
pause
