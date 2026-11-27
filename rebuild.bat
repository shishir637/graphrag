@echo off
cd /d "%~dp0"
echo Building Context Layer (log: rebuild.log)...
powershell -NoProfile -Command "docker compose up -d --build 2>&1 | Tee-Object -FilePath rebuild.log; docker compose ps 2>&1 | Tee-Object -FilePath rebuild.log -Append; 'DONE' | Out-File rebuild.log -Append"
pause
