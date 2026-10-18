@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - OK.ru Downloader Setup

cd /d "%~dp0"

REM Общее виртуальное окружение находится в корне репозитория:
REM loaders\ok -> loaders -> корень FaceSearch -> .venv
set "VENV_DIR=%~dp0..\..\.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Не найдено виртуальное окружение FaceSearch:
    echo         "%PYTHON%"
    echo.
    echo Сначала запустите setup.bat в корне репозитория FaceSearch.
    pause
    exit /b 1
)

if not exist "%~dp0requirements.txt" (
    echo [ERROR] Не найден requirements.txt в loaders\ok.
    pause
    exit /b 1
)

echo.
echo [STEP] Установка зависимостей загрузчика в общее окружение FaceSearch...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :failed

echo.
echo [OK] Зависимости загрузчика OK.ru установлены.
echo Используется общее окружение: "%VENV_DIR%"
pause
exit /b 0

:failed
echo.
echo [ERROR] Не удалось установить зависимости загрузчика.
pause
exit /b 1
