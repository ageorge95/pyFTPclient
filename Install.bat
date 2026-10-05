@echo off

set python=%1
IF "%python%"=="" set python="python"

:: installation

%python% -m venv venv
:: Windows doesn't allow the creation of symlinks without special priviledges, so hardlinks are created instead.
mklink /h activate.bat venv\Scripts\activate.bat

call activate.bat

python install_helper.py || goto :error
goto :all_good

: error
pause
exit

: all_good

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

:: post-installation message

echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
echo.
echo pyFTPclient install complete.
echo.
echo Run 'activate' to activate pyFTPclient's Python virtual environment and
echo 'deactivate' to, well, deactivate it.
echo.
echo @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@

pause

deactivate
