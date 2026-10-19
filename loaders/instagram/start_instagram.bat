@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - Instagram Downloader
cd /d "%~dp0"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
set "SCRIPT=%~dp0instagram_downloader.py"
set "SETUP=%~dp0setup.bat"
if not exist "%PYTHON%" (
  echo [ERROR] Не найдено общее виртуальное окружение FaceSearch: "%PYTHON%"
  echo Сначала запустите setup.bat в корне FaceSearch.
  pause
  exit /b 1
)
if not exist "%SCRIPT%" (
  echo [ERROR] Не найден файл загрузчика: "%SCRIPT%"
  pause
  exit /b 1
)
"%PYTHON%" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 (
  echo [WARN] Playwright не установлен.
  if not exist "%SETUP%" (
    echo [ERROR] Не найден setup.bat.
    pause
    exit /b 1
  )
  choice /C YN /N /M "Запустить setup.bat сейчас? [Y/N]: "
  if errorlevel 2 exit /b 1
  call "%SETUP%"
  if errorlevel 1 (
    echo [ERROR] Установка зависимостей завершилась ошибкой.
    pause
    exit /b 1
  )
)
"%PYTHON%" "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
