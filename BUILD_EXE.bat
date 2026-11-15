@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
 set "PY=py -3"
) else (
 set "PY=python"
)
%PY% -m pip install -r requirements.txt "pyinstaller>=6,<7" || goto :error
rem Don't rebuild into your live portable folder: PyInstaller deletes dist.
if exist "dist\LEGO Collection Manager\data" (
 echo ERROR: Move/backup existing dist\LEGO Collection Manager\data before building.
 pause
 exit /b 1
)
%PY% -m PyInstaller --noconfirm --clean --windowed --onedir --name "LEGO Collection Manager" --icon collector_icon.ico main.py || goto :error
%PY% -m PyInstaller --noconfirm --clean --windowed --onefile --name "LEGO Updater" --icon collector_icon.ico --paths . updater.py || goto :error
copy /Y "dist\LEGO Updater.exe" "dist\LEGO Collection Manager\LEGO Updater.exe" >nul || goto :error
copy /Y "dist\LEGO Updater.exe" "dist\LEGO Collection Manager\_internal\updater_next.exe" >nul || goto :error
copy /Y collector_icon.ico "dist\LEGO Collection Manager\collector_icon.ico" >nul || goto :error
copy /Y version.json "dist\LEGO Collection Manager\version.json" >nul || goto :error
copy /Y updates.json "dist\LEGO Collection Manager\updates.json" >nul || goto :error
if not exist "dist\LEGO Collection Manager\data" mkdir "dist\LEGO Collection Manager\data"
echo.
echo SUCCESS: dist\LEGO Collection Manager\
echo Backup and copy your existing data folder into this folder AFTER the build.
pause
exit /b 0
:error
echo BUILD FAILED. See messages above.
pause
exit /b 1
