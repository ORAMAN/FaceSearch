@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - OK.ru Downloader

cd /d "%~dp0"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
set "SCRIPT=%~dp0ok_downloader.py"

if not exist "%PYTHON%" (
    echo [ERROR] Не найдено общее виртуальное окружение FaceSearch:
    echo         "%PYTHON%"
    echo Сначала запустите setup.bat в корне репозитория FaceSearch.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] Не найден файл загрузчика:
    echo         "%SCRIPT%"
    pause
    exit /b 1
)

echo Запуск загрузчика OK.ru...
echo Для просмотра диагностических сообщений оставьте это окно открытым.
echo.
"%PYTHON%" "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Загрузчик завершился с кодом %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
