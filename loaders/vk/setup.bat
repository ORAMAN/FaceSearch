@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title FaceSearch - VK Avatar Downloader Setup

cd /d "%~dp0"
for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "VENV_DIR=%ROOT_DIR%\.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "UV_REQUIRED=0.12.23"

if not exist "%~dp0requirements.txt" (
    echo [ERROR] Не найден requirements.txt в папке загрузчика аватарок VK.
    pause
    exit /b 1
)
if not exist "%PYTHON%" (
    echo [ERROR] Не найдено общее окружение FaceSearch:
    echo         "%VENV_DIR%"
    echo Сначала запустите setup.bat в корне FaceSearch.
    pause
    exit /b 1
)

REM Поиск uv — аналогично корневому setup.bat FaceSearch.
set "UV_EXE="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV_EXE set "UV_EXE=%%U"
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_EXE if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"

set "UV_INSTALLED="
if defined UV_EXE for /f "tokens=2" %%V in ('"!UV_EXE!" --version 2^>nul') do set "UV_INSTALLED=%%V"
if "!UV_INSTALLED!"=="%UV_REQUIRED%" goto :uv_ready

if defined UV_EXE (
    echo [INFO] Найден uv !UV_INSTALLED!, требуется %UV_REQUIRED%. Устанавливаю...
) else (
    echo [INFO] uv не найден. Устанавливаю %UV_REQUIRED%...
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/%UV_REQUIRED%/install.ps1 | iex"
if errorlevel 1 (
    echo [ERROR] Не удалось установить uv.
    pause
    exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not exist "!UV_EXE!" (
    set "UV_EXE2="
    for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV_EXE2 set "UV_EXE2=%%U"
    if defined UV_EXE2 set "UV_EXE=!UV_EXE2!"
)

:uv_ready
if not defined UV_EXE (
    echo [ERROR] uv не найден.
    pause
    exit /b 1
)
set "UV_INSTALLED="
for /f "tokens=2" %%V in ('"!UV_EXE!" --version 2^>nul') do set "UV_INSTALLED=%%V"
echo [INFO] uv: !UV_INSTALLED!
if not "!UV_INSTALLED!"=="%UV_REQUIRED%" (
    echo [ERROR] Требуется uv %UV_REQUIRED%, обнаружена версия !UV_INSTALLED!.
    pause
    exit /b 1
)

echo.
echo [STEP] Установка зависимостей загрузчика аватарок VK в общее окружение FaceSearch...
"!UV_EXE!" pip install --python "%PYTHON%" -r "%~dp0requirements.txt"
if errorlevel 1 goto :failed

echo.
echo [OK] Зависимости загрузчика аватарок VK установлены.
echo Общее окружение: "%VENV_DIR%"
pause
exit /b 0

:failed
echo.
echo [ERROR] Не удалось установить зависимости через uv.
pause
exit /b 1
