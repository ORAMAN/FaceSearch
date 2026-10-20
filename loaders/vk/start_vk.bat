@echo off
setlocal EnableExtensions
chcp 65001 >nul
title FaceSearch - VK Avatar Downloader

cd /d "%~dp0"
set "PYTHON=%~dp0..\..\.venv\Scripts\python.exe"
set "SCRIPT=%~dp0vk_abatars_downloader.py"
set "SETUP=%~dp0setup.bat"

if not exist "%PYTHON%" (
    echo [ERROR] Не найдено общее окружение FaceSearch:
    echo         "%PYTHON%"
    echo Сначала запустите setup.bat в корне FaceSearch.
    pause
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo [ERROR] Не найден vk_abatars_downloader.py:
    echo         "%SCRIPT%"
    pause
    exit /b 1
)

"%PYTHON%" -c "import requests" >nul 2>&1
if errorlevel 1 goto :missing_dependency
goto :run_downloader

:missing_dependency
echo.
echo [WARN] Не найдена обязательная библиотека requests.
echo Рекомендуется запустить установщик зависимостей:
echo     "%SETUP%"
if not exist "%SETUP%" (
    echo [ERROR] Не найден setup.bat.
    pause
    exit /b 1
)
choice /C YN /N /M "Запустить setup.bat сейчас? [Y/N]: "
if errorlevel 2 goto :cancelled
call "%SETUP%"
if errorlevel 1 goto :setup_failed
"%PYTHON%" -c "import requests" >nul 2>&1
if errorlevel 1 goto :still_missing

echo.
echo [OK] Зависимость requests доступна.
goto :run_downloader

:cancelled
echo Запуск отменён. Для установки зависимостей запустите setup.bat вручную.
pause
exit /b 1

:setup_failed
echo [ERROR] Установка зависимостей завершилась с ошибкой.
pause
exit /b 1

:still_missing
echo [ERROR] requests по-прежнему недоступен в общем окружении.
echo Проверьте вывод setup.bat и путь:
echo     "%PYTHON%"
pause
exit /b 1

:run_downloader
echo Запуск загрузчика аватарок VK...
echo Диагностика и ход загрузки отображаются в окне программы.
echo.
"%PYTHON%" "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] загрузчик аватарок VK завершился с кодом %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
