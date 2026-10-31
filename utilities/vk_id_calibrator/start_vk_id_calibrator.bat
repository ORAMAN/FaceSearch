@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title VK ID Calibrator

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установите Python 3.10 или новее и добавьте его в PATH.
    pause
    exit /b 1
)

echo Проверка Python...
python --version

python -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo Устанавливается Playwright...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить Playwright. Проверьте подключение к Интернету и pip.
        pause
        exit /b 1
    )
)

python -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=Path(p.chromium.executable_path).is_file(); p.stop(); raise SystemExit(0 if ok else 1)" >nul 2>nul
if errorlevel 1 (
    echo Chromium Playwright не найден. Устанавливается браузер...
    python -m playwright install chromium
    if errorlevel 1 (
        echo Не удалось установить Chromium для Playwright.
        echo Проверьте подключение к Интернету и повторите запуск.
        pause
        exit /b 1
    )
)

python vk_date_ranges_app.py
if errorlevel 1 pause
endlocal
