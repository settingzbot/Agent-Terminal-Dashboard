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
python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
pause
