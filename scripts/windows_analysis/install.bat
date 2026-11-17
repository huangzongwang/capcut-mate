@echo off
setlocal
cd /d "%~dp0..\.."
if not defined ANALYSIS_ENV_NAME set "ANALYSIS_ENV_NAME=capcut-analysis"
if not defined API_ENV_NAME set "API_ENV_NAME=capcut-mate"
where conda >nul 2>&1
if errorlevel 1 goto :no_conda
call conda run -n "%ANALYSIS_ENV_NAME%" python --version >nul 2>&1
if not errorlevel 1 goto :install
call conda create -y -n "%ANALYSIS_ENV_NAME%" python=3.11 pip
if errorlevel 1 goto :failed
:install
rem Use an environment-owned FFmpeg; an older system copy may lack -fps_mode.
call conda install -y -n "%ANALYSIS_ENV_NAME%" -c conda-forge "ffmpeg>=6"
if errorlevel 1 goto :failed
call conda run --no-capture-output -n "%ANALYSIS_ENV_NAME%" python -m pip install -r scripts\windows_analysis\requirements.txt
if errorlevel 1 goto :failed
:done
call conda run --no-capture-output -n "%API_ENV_NAME%" python -m pip install -r scripts\windows_analysis\requirements-api.txt
if errorlevel 1 goto :failed
echo Analysis worker and API-side model directory dependencies installed.
echo Next: scripts\windows_analysis\start.bat --public-host YOUR_LAN_IP
echo CUDA/cuDNN and optional OCR are NOT installed by this script.
pause
exit /b 0
:no_conda
echo Conda not found. Open Anaconda Prompt and run this script again.
:failed
echo Installation failed. Review the error above.
pause
exit /b 1
