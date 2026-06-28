@echo off
cd /d "%~dp0.."
echo === Claude Dashboard ===
echo.
echo Installing Python dependencies...
pip install -r requirements.txt --quiet
echo.
echo Starting server on http://localhost:8080
echo Frontend dev server: cd web ^&^& npm run dev
echo.
rem Relaunch loop: the in-app Settings -> "Restart Dashboard" button exits with
rem code 42 (RESTART_EXIT_CODE in server/app.py); we relaunch in this same
rem window so a rebuilt frontend / edited backend takes effect. Any other exit
rem code ends the loop.
:restart
python -m uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8080
if "%ERRORLEVEL%"=="42" (
    echo.
    echo Restarting dashboard ^(picking up rebuild^)...
    echo.
    goto restart
)
pause
