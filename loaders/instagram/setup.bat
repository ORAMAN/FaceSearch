@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - Instagram Downloader Setup
cd /d "%~dp0"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [ERROR] Не найдено общее виртуальное окружение FaceSearch: "%PYTHON%"
  echo Создайте .venv в корне FaceSearch или измените путь в setup.bat.
  pause
  exit /b 1
)
echo Установка Python-зависимостей загрузчика Instagram...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo [ERROR] Не удалось установить Python-зависимости.
  pause
  exit /b 1
)
echo [OK] Зависимости установлены.
pause
