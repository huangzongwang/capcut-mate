@echo off
setlocal
cd /d "%~dp0.."

set "ENV_NAME=capcut-mate"
set "DEFAULT_DRAFT_PATH=%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft"
if not defined DRAFT_URL set "DRAFT_URL=http://127.0.0.1:30000/openapi/capcut-mate/v1/get_draft"
if not defined DOWNLOAD_URL set "DOWNLOAD_URL=http://127.0.0.1:30000/"

where conda >nul 2>&1
if errorlevel 1 (
    echo Conda was not found. Run this script from Anaconda Prompt or add Conda to PATH.
    pause
    exit /b 1
)

rem CALL is required when Conda resolves to condabin\conda.bat.
call conda run -n %ENV_NAME% python --version >nul 2>&1
if errorlevel 1 (
    echo Creating Conda environment %ENV_NAME% with Python 3.11...
    call conda create -y -n %ENV_NAME% python=3.11
    if errorlevel 1 goto :failed
)

echo Installing CapCut Mate and Windows desktop automation dependencies...
call conda run --no-capture-output -n %ENV_NAME% python -m pip install -e ".[windows]"
if errorlevel 1 goto :failed

if not defined DRAFT_SAVE_PATH set "DRAFT_SAVE_PATH=%DEFAULT_DRAFT_PATH%"
if not exist "%DRAFT_SAVE_PATH%\" (
    echo Jianying draft folder was not found:
    echo "%DRAFT_SAVE_PATH%"
    set /p "DRAFT_SAVE_PATH=Enter Jianying draft folder path: "
)
if not exist "%DRAFT_SAVE_PATH%\" (
    echo The configured Jianying draft folder does not exist.
    echo Install and open Jianying once, then rerun this script.
    pause
    exit /b 1
)

echo.
echo Starting CapCut Mate API at http://localhost:30000/docs
echo Keep this window open. Press Ctrl+C to stop the server.
echo.
call conda run --no-capture-output -n %ENV_NAME% python main.py
if errorlevel 1 goto :failed
echo The API server has stopped.
pause
exit /b 0

:failed
echo.
echo Deployment or startup failed. Review the error above.
pause
exit /b 1
