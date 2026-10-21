@echo off
cd /d "C:\Users\kaive\crazyfiles"

docker compose run --rm attendance-monitor

exit /b %errorlevel%