@echo off
cd /d "%~dp0"

:: --- Configuration ---
set "PYTHON_UTIL_SCRIPT=install_helper.py"
set "DEFAULT_PYTHON_CMD=python"

:: Determine the Python command to use for running the utility script
:: If first argument is --headless skip and continue with the next one
if "%~1"=="--headless" (
    set "HELPER_PYTHON=%~2"
) else (
    set "HELPER_PYTHON=%~1"
)
if "%HELPER_PYTHON%"=="" set "HELPER_PYTHON=%DEFAULT_PYTHON_CMD%"
echo Using helper Python: %HELPER_PYTHON%

:: Find the highest available Python version using the utility script
echo.
echo --- Searching for the highest suitable Python installation ---
set "TARGET_PYTHON_EXE="
for /f "tokens=1,*" %%A in ('""%HELPER_PYTHON%" "%PYTHON_UTIL_SCRIPT%" find_highest"') do (
    if "%%A"=="Path:" set "TARGET_PYTHON_EXE=%%B"
)

:: Decide which Python executable to use for creating the venv
if defined TARGET_PYTHON_EXE (
    echo Found recommended Python: %TARGET_PYTHON_EXE%
) else (
    echo No specific Python version found by the script.
    echo Falling back to the helper Python: %HELPER_PYTHON%
    set "TARGET_PYTHON_EXE=%HELPER_PYTHON%"
)

:: Check if the TARGET_PYTHON_EXE version is valid using the utility script
echo.
echo --- Validating Python version (%TARGET_PYTHON_EXE%) ---
"%TARGET_PYTHON_EXE%" "%PYTHON_UTIL_SCRIPT%" check_current
if errorlevel 1 (
    echo ERROR: The Python version %TARGET_PYTHON_EXE% is invalid.
    echo Please check the output from %PYTHON_UTIL_SCRIPT% above.
    echo Installation cannot proceed.
    goto :error_exit
)
echo Python version %TARGET_PYTHON_EXE% is acceptable.
echo Using Python for venv: %TARGET_PYTHON_EXE%

:: installation
"%TARGET_PYTHON_EXE%" -m venv venv || goto :error_exit
:: Windows doesn't allow the creation of symlinks without special priviledges, so hardlinks are created instead.
if not exist activate.bat mklink /h activate.bat venv\Scripts\activate.bat

call activate.bat

python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :error_exit

:: post-installation message

echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
echo.
echo pyFTPclient install complete.
echo.
echo Run 'activate' to activate pyFTPclient's Python virtual environment and
echo 'deactivate' to, well, deactivate it.
echo.
echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@

:: skip pause if first argument is --headless
if not "%~1"=="--headless" pause
call deactivate
exit /b 0

:error_exit
echo.
echo !!!!! INSTALLATION FAILED !!!!!
:: skip pause if first argument is --headless
if not "%~1"=="--headless" pause
exit /b 1
