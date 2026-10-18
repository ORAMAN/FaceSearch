@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - OK.ru Downloader

cd /d "%~dp0"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
set "SCRIPT=%~dp0ok_downloader.py"
set "SETUP=%~dp0setup.bat"

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

REM Проверяем обязательную зависимость до запуска, чтобы не показывать traceback.
"%PYTHON%" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 goto :playwright_missing
goto :run_downloader

:playwright_missing
echo.
echo [WARN] Не найдена обязательная библиотека Playwright.
echo Загрузчик OK.ru не сможет запуститься без этой зависимости.
echo Рекомендуется установить зависимости через setup.bat:
echo     "%SETUP%"
echo.

if not exist "%SETUP%" (
    echo [ERROR] Не найден setup.bat в папке загрузчика.
    echo Установите зависимости вручную или восстановите этот файл.
    pause
    exit /b 1
)

choice /C YN /N /M "Запустить setup.bat сейчас? [Y/N]: "
if errorlevel 2 goto :setup_skipped
if errorlevel 1 goto :run_setup

:run_setup
echo.
echo [INFO] Запускаю установку зависимостей...
call "%SETUP%"
if errorlevel 1 goto :setup_failed

REM Повторно проверяем Playwright после установки.
"%PYTHON%" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 goto :playwright_still_missing

echo.
echo [OK] Playwright доступен. Запускаю загрузчик OK.ru...
goto :run_downloader

:setup_skipped
echo.
echo [INFO] Запуск отменён. Для установки зависимостей запустите:
echo     "%SETUP%"
pause
exit /b 1

:setup_failed
echo.
echo [ERROR] Установка зависимостей завершилась с ошибкой.
echo Проверьте сообщения setup.bat и повторите попытку.
pause
exit /b 1

:playwright_still_missing
echo.
echo [ERROR] Playwright по-прежнему недоступен после установки.
echo Проверьте вывод setup.bat и путь к общему окружению:
echo     "%PYTHON%"
pause
exit /b 1

:run_downloader
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
