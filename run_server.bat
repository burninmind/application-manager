@echo off
cd /d "%~dp0"
call conda activate app_m || (
    echo Failed to activate conda env "app_m".
    pause
    exit /b 1
)
python -m app.main
pause
