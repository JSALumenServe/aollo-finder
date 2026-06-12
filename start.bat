@echo off
echo === Apollo Contact Finder ===
echo.

REM Check if .env exists
if not exist .env (
    echo Creating .env from template...
    copy .env.example .env
    echo.
    echo IMPORTANT: Open .env and fill in your Apollo API key and email credentials.
    echo Then run this script again.
    pause
    exit /b
)

REM Install dependencies if needed
if not exist venv (
    echo Setting up Python virtual environment...
    python -m venv venv
    call venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate
)

echo Starting Apollo Contact Finder on http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python app.py
pause
