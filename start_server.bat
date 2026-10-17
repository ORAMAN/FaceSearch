@echo off
setlocal
chcp 65001 >nul
title Face Search Launcher

REM Переходим в папку, где лежит сам .bat
cd /d "%~dp0"

REM Проверяем, что GPU-venv существует
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Не найдено окружение .venv
    echo Запустите сначала setup.bat
    pause
    exit /b 1
)

REM Проверяем GPU-версию программы
if not exist "server_http.py" (
    echo [ERROR] Не найден server_http.py
    pause
    exit /b 1
)

REM Запускаем GUI как независимый процесс
start "" ".venv\Scripts\python.exe" server_http.py --host 0.0.0.0 --port 8765

REM Закрываем консоль лаунчера сразу
exit /b 0
