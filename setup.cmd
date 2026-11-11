@echo off
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.11 or newer from python.org and select Add Python to PATH.
  pause
  exit /b 1
)
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m mealapp.speech --setup
if errorlevel 1 goto failed
echo.
echo App setup complete. Open Ollama, then run these commands once:
echo   ollama pull qwen2.5:1.5b
echo   ollama pull llama3.2:1b
echo   ollama pull qwen3.5:0.8b
echo Then double-click start.cmd.
pause
exit /b 0
:failed
echo Setup failed. Read the error above. Manual recipes still work with python app.py.
pause
exit /b 1
