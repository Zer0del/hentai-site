@echo off
chcp 65001 >nul 2>&1
echo Starting Hentach...
echo.
echo Upgrading pip...
python -m pip install --upgrade pip

echo Installing / updating dependencies (Flask, Pillow, requests, werkzeug)...
python -m pip install -r requirements.txt
echo.
echo ================================================
echo IMPORTANT:
echo 1. To stop the server press Ctrl+C in THIS window
echo    and wait for "Shutting down..."
echo 2. Do NOT just close the window - the process may keep running!
echo 3. After stopping you can close the window.
echo ================================================
echo.
python app.py
echo.
echo Server stopped.
pause