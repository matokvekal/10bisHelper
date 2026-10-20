@echo off
rem Builds dist\MaBaLi\MaBaLi.exe (+ dist\MaBaLi.zip) - runs on PCs without Python.
rem Personal files (.env, config.json, browser_profile, data.*) are NOT included.
cd /d "%~dp0"

rem Isolated venv so only this app's packages get bundled (not everything in global Python).
if not exist ".build-venv\Scripts\python.exe" python -m venv .build-venv || goto :fail
set PY=.build-venv\Scripts\python.exe
%PY% -m pip install -q -r requirements.txt pyinstaller || goto :fail

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
%PY% -m PyInstaller --noconfirm --clean --windowed --onedir --name MaBaLi ^
  --add-data "index.html;." ^
  --collect-submodules uvicorn ^
  --exclude-module tkinter ^
  server.py || goto :fail

copy /y "READ-ME-FIRST.txt" "dist\MaBaLi\" >nul
if exist "dist\MaBaLi.zip" del "dist\MaBaLi.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\MaBaLi' -DestinationPath 'dist\MaBaLi.zip'" || goto :fail

echo.
echo Done: dist\MaBaLi.zip  - send this file.
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
