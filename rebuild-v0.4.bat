@echo off
cd /d "%~dp0"
echo Building Context Layer v0.4 (log: rebuild-v0.4.log)...
powershell -NoProfile -Command "docker compose up -d --build 2>&1 | Tee-Object -FilePath rebuild-v0.4.log; docker compose ps 2>&1 | Tee-Object -FilePath rebuild-v0.4.log -Append; 'DONE' | Out-File rebuild-v0.4.log -Append"
pause
