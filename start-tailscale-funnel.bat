@echo off
cd /d "%~dp0"
set TS="C:\Program Files\Tailscale\tailscale.exe"
echo Publishing Context Layer with Tailscale Funnel. Log: tailscale-funnel.log
echo ===== %date% %time% > tailscale-funnel.log
%TS% status --json > tailscale-status.json 2>&1
%TS% funnel --bg 8080 >> tailscale-funnel.log 2>&1
echo exit %errorlevel% >> tailscale-funnel.log
%TS% funnel status >> tailscale-funnel.log 2>&1
echo DONE >> tailscale-funnel.log
type tailscale-funnel.log
pause
