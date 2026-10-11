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
if not exist "face_search_gui.py" (
    echo [ERROR] Не найден face_search_gui.py
    pause
    exit /b 1
)

REM Запускаем GUI как независимый процесс
start "" ".venv\Scripts\python.exe" "face_search_gui.py"

REM Закрываем консоль лаунчера сразу
exit /b 0
