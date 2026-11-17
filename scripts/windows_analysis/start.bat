@echo off
setlocal
cd /d "%~dp0..\.."
if not defined API_ENV_NAME set "API_ENV_NAME=capcut-mate"
if not defined ANALYSIS_ENV_NAME set "ANALYSIS_ENV_NAME=capcut-analysis"
where conda >nul 2>&1
if errorlevel 1 goto :failed
if defined SCENE_PYTHON goto :show_python
for /f "delims=" %%P in ('call conda run -n "%ANALYSIS_ENV_NAME%" python -c "import sys; print(sys.executable)"') do set "SCENE_PYTHON=%%P"
if not defined SCENE_PYTHON goto :failed
:show_python
echo.
echo API Conda environment: %API_ENV_NAME%
echo Analysis Conda environment: %ANALYSIS_ENV_NAME%
echo SCENE_PYTHON: %SCENE_PYTHON%
echo Checking analysis Python and OpenCV...
"%SCENE_PYTHON%" -c "import cv2,sys; print('Analysis Python:',sys.executable); print('OpenCV:',cv2.__version__)"
if errorlevel 1 goto :failed
for /f "delims=" %%P in ('call conda run -n "%ANALYSIS_ENV_NAME%" python -c "import sys; print(sys.executable)"') do set "EXPECTED_SCENE_PYTHON=%%P"
if /i not "%SCENE_PYTHON%"=="%EXPECTED_SCENE_PYTHON%" (
    echo ERROR: SCENE_PYTHON does not match the selected analysis environment.
    echo Expected: %EXPECTED_SCENE_PYTHON%
    goto :failed
)
echo Analysis Python check passed.
echo Starting API...
call conda run --no-capture-output -n "%API_ENV_NAME%" python scripts\windows_analysis\launch.py %*
if errorlevel 1 goto :failed
pause
exit /b 0
:failed
echo Analysis startup failed. Run install.bat first and check the error above.
pause
exit /b 1
