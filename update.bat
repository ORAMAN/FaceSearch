@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Face Search - Update Repository

REM ============================================================
REM Face Search: обновление существующей копии или клонирование.
REM Git-клон обновляется через fetch + fast-forward merge.
REM ZIP-копия обновляется без удаления пользовательских данных.
REM ============================================================

cd /d "%~dp0"
set "PROJECT_ROOT=%CD%"
set "REPO_URL=https://github.com/ORAMAN/FaceSearch.git"
set "BRANCH=main"
set "ZIP_URL=https://github.com/ORAMAN/FaceSearch/archive/refs/heads/main.zip"
set "TMP_DIR=%TEMP%\FaceSearch_update_%RANDOM%_%RANDOM%"

REM ------------------------------------------------------------
REM КРИТИЧНО: Git использует переменные GIT_DIR / GIT_WORK_TREE /
REM GIT_INDEX_FILE / GIT_COMMON_DIR как переопределение путей к
REM репозиторию. Если они случайно окажутся заданы (например,
REM унаследованы от родительского процесса), git начнёт искать
REM .git не там и упадёт с "fatal: not a git repository".
REM Принудительно очищаем.
REM ------------------------------------------------------------
set "GIT_DIR="
set "GIT_WORK_TREE="
set "GIT_INDEX_FILE="
set "GIT_COMMON_DIR="
set "GIT_OBJECT_DIRECTORY="
set "GIT_ALTERNATE_OBJECT_DIRECTORIES="

echo.
echo ============================================================
echo Face Search - обновление репозитория
echo ============================================================
echo [INFO] Папка: %PROJECT_ROOT%
echo.
echo [STEP] Проверка наличия Git for Windows...
call :ensure_git
if errorlevel 1 goto :error
echo.

REM Git-репозиторий: обновляем только ветку main и только fast-forward.
if exist ".git" goto :git_update

REM ZIP-распаковка проекта: обновляем файлы, сохраняя локальные данные.
if exist "face_search_gui.py" if exist "setup.bat" if exist "requirements.txt" goto :zip_update

REM Репозитория/проекта в текущей папке нет: клонируем в подпапку FaceSearch.
goto :clone_repo

:git_update
echo [STEP] Найден Git-репозиторий.

git rev-parse --is-inside-work-tree >nul 2>&1
if not errorlevel 1 goto :git_ok

echo [WARN] Git не распознаёт репозиторий. Пробую починить safe.directory...
git config --global --add safe.directory "%PROJECT_ROOT%" >nul 2>&1
git rev-parse --is-inside-work-tree >nul 2>&1
if not errorlevel 1 goto :git_ok

echo [ERROR] Каталог .git найден, но репозиторий Git не распознаётся.
echo -------- вывод git rev-parse --------
git rev-parse --is-inside-work-tree
echo -------------------------------------
echo Если выше "dubious ownership" - выполните вручную:
echo     git config --global --add safe.directory "%PROJECT_ROOT%"
echo Если выше "not a git repository" - .git повреждён.
echo Удалите или переименуйте папку .git и запустите update.bat снова,
echo тогда файлы обновятся из ZIP-архива без Git.
goto :error

:git_ok
set "CURRENT_BRANCH="
for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "CURRENT_BRANCH=%%B"
if /I not "!CURRENT_BRANCH!"=="%BRANCH%" (
    echo [ERROR] Текущая ветка: "!CURRENT_BRANCH!", ожидается "%BRANCH%".
    echo Переключите репозиторий на main и повторите обновление.
    goto :error
)

set "ORIGIN="
for /f "delims=" %%R in ('git config --get remote.origin.url 2^>nul') do set "ORIGIN=%%R"
if /I "!ORIGIN!"=="https://github.com/ORAMAN/FaceSearch.git" goto :origin_ok
if /I "!ORIGIN!"=="https://github.com/ORAMAN/FaceSearch" goto :origin_ok
if /I "!ORIGIN!"=="git@github.com:ORAMAN/FaceSearch.git" goto :origin_ok
if /I "!ORIGIN!"=="git@github.com:ORAMAN/FaceSearch" goto :origin_ok

echo [ERROR] Удалённый origin не соответствует ORAMAN/FaceSearch:
echo         !ORIGIN!
echo Обновление отменено во избежание изменения другого репозитория.
goto :error

:origin_ok
REM Не затираем пользовательские изменения в отслеживаемых Git-файлах.
git diff --quiet
if errorlevel 1 goto :local_changes
git diff --cached --quiet
if errorlevel 1 goto :local_changes

echo [STEP] Получение последних изменений с GitHub...
git fetch --prune origin
if errorlevel 1 (
    echo [ERROR] Не удалось получить изменения с GitHub.
    goto :error
)

echo [STEP] Применение обновления без принудительного сброса локальных файлов...
git merge --ff-only origin/%BRANCH%
if errorlevel 1 (
    echo [ERROR] Обновление невозможно выполнить автоматически.
    echo Локальная ветка могла разойтись с origin/%BRANCH%.
    echo Локальные изменения не сбрасывались.
    goto :error
)

echo.
echo [OK] Репозиторий успешно обновлён.
goto :post_update

:local_changes
echo [ERROR] В отслеживаемых файлах есть локальные изменения.
echo Обновление остановлено, чтобы не перезаписать вашу работу.
echo Сохраните изменения в коммите или сделайте резервную копию, затем повторите запуск.
echo Примечание: неотслеживаемый update.bat сам по себе обновлению не мешает.
goto :error

:zip_update
echo [INFO] Git-клон не найден. Обнаружена папка с файлами проекта.
echo [INFO] Загружаю актуальный архив main и обновляю файлы проекта.
echo [INFO] .venv, настройки, базы данных, индексы и CROP-каталоги сохраняются.

call :download_archive
if errorlevel 1 (
    echo [ERROR] Не удалось скачать или распаковать архив GitHub.
    call :cleanup_temp
    goto :error
)

robocopy "%TMP_DIR%\FaceSearch-main" "%PROJECT_ROOT%" /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /XD ".git" ".venv" "venv" "__pycache__" "models" "indexes" "face_images" "face_crops" "CROP" "CROPS" /XF "settings.xml" "database_profiles.json" "*.db" "*.sqlite" "*.sqlite3" "*.sqlite-wal" "*.sqlite-shm" "*.faiss" "*.index" "*.log" ".env" "vk_token.txt"
set "ROBOCOPY_RC=!ERRORLEVEL!"
call :cleanup_temp
if !ROBOCOPY_RC! GEQ 8 (
    echo [ERROR] Robocopy завершился с кодом !ROBOCOPY_RC!.
    goto :error
)

echo.
echo [OK] Файлы проекта обновлены из GitHub.
echo [INFO] Устаревшие файлы, отсутствующие в репозитории, автоматически не удалялись.
goto :post_update

:clone_repo
echo [INFO] Файлы проекта в текущей папке не найдены.
set "TARGET_DIR=%~dp0FaceSearch"
if exist "!TARGET_DIR!" (
    echo [ERROR] Папка назначения уже существует:
    echo         !TARGET_DIR!
    echo Укажите другую папку или переместите её, затем повторите запуск.
    goto :error
)

where git >nul 2>&1
if not errorlevel 1 goto :clone_with_git

echo [INFO] Git не найден. Выполняю первоначальное скачивание ZIP-архива.
call :download_archive
if errorlevel 1 (
    echo [ERROR] Не удалось скачать или распаковать архив GitHub.
    call :cleanup_temp
    goto :error
)

mkdir "!TARGET_DIR!" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Не удалось создать папку назначения.
    call :cleanup_temp
    goto :error
)
robocopy "%TMP_DIR%\FaceSearch-main" "!TARGET_DIR!" /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /XD ".git" ".venv" "venv" "__pycache__" "models" "indexes" "face_images" "face_crops" "CROP" "CROPS" /XF "settings.xml" "database_profiles.json" "*.db" "*.sqlite" "*.sqlite3" "*.sqlite-wal" "*.sqlite-shm" "*.faiss" "*.index" "*.log" ".env" "vk_token.txt"
set "ROBOCOPY_RC=!ERRORLEVEL!"
call :cleanup_temp
if !ROBOCOPY_RC! GEQ 8 (
    echo [ERROR] Не удалось скопировать файлы проекта. Код: !ROBOCOPY_RC!.
    goto :error
)
copy /Y "%~f0" "!TARGET_DIR!\update.bat" >nul 2>&1
echo.
echo [OK] Проект скачан в папку:
echo      !TARGET_DIR!
echo [NEXT] Перейдите в эту папку и запустите setup.bat для установки окружения.
goto :success

:clone_with_git
echo [STEP] Клонирование %REPO_URL% ...
git clone --branch "%BRANCH%" "%REPO_URL%" "!TARGET_DIR!"
if errorlevel 1 (
    echo [ERROR] Не удалось клонировать репозиторий.
    goto :error
)
copy /Y "%~f0" "!TARGET_DIR!\update.bat" >nul 2>&1
echo.
echo [OK] Репозиторий склонирован в папку:
echo      !TARGET_DIR!
echo [NEXT] Перейдите в эту папку и запустите setup.bat для установки окружения.
goto :success

REM ============================================================
REM Проверка/установка Git for Windows. Сначала winget, затем официальный
REM установщик из релиза git-for-windows/git на GitHub.
REM ============================================================
:ensure_git
call :locate_git
if not errorlevel 1 goto :git_ready

echo [WARN] Git не найден. Пытаюсь установить Git for Windows...
where winget >nul 2>&1
if errorlevel 1 goto :git_direct_install

echo [STEP] Установка Git через winget...
winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements
call :locate_git
if not errorlevel 1 goto :git_ready
echo [WARN] Установка через winget не завершила настройку Git. Пробую официальный установщик...

:git_direct_install
set "GIT_INSTALLER=%TEMP%\FaceSearch_GitSetup_%RANDOM%.exe"
echo [STEP] Загрузка официального установщика Git for Windows...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $h=@{'User-Agent'='FaceSearch-Updater'}; $r=Invoke-RestMethod -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest' -Headers $h; $a=$r.assets | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1; if (-not $a) { throw 'Не найден x64 установщик в последнем релизе Git for Windows' }; Invoke-WebRequest -UseBasicParsing -Uri $a.browser_download_url -OutFile $env:GIT_INSTALLER; if (-not (Test-Path -LiteralPath $env:GIT_INSTALLER)) { throw 'Установщик не загружен' }"
if errorlevel 1 (
    echo [ERROR] Не удалось скачать установщик Git for Windows.
    exit /b 1
)

echo [STEP] Установка Git for Windows. Может появиться запрос UAC...
start /wait "" "%GIT_INSTALLER%" /VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS
del /q "%GIT_INSTALLER%" >nul 2>&1
call :locate_git
if not errorlevel 1 goto :git_ready
echo [ERROR] Git не найден даже после установки. Проверьте права и завершение установщика.
exit /b 1

:git_ready
echo [INFO] Git доступен: "!GIT_EXE!"
"!GIT_EXE!" --version
exit /b 0

:locate_git
REM Ищем git.exe. Имя переменной GIT_CMD_DIR (а НЕ GIT_DIR!)
REM выбрано намеренно: GIT_DIR — зарезервированная переменная Git,
REM её установка ломает все последующие вызовы git.
set "GIT_EXE="
set "GIT_CMD_DIR="
for /f "delims=" %%G in ('where git 2^>nul') do if not defined GIT_EXE set "GIT_EXE=%%G"
if not defined GIT_EXE if exist "%ProgramFiles%\Git\cmd\git.exe" set "GIT_EXE=%ProgramFiles%\Git\cmd\git.exe"
if not defined GIT_EXE if exist "%ProgramFiles(x86)%\Git\cmd\git.exe" set "GIT_EXE=%ProgramFiles(x86)%\Git\cmd\git.exe"
if not defined GIT_EXE if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" set "GIT_EXE=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
if not defined GIT_EXE exit /b 1
"!GIT_EXE!" --version >nul 2>&1
if errorlevel 1 (
    set "GIT_EXE="
    exit /b 1
)
for %%D in ("!GIT_EXE!") do set "GIT_CMD_DIR=%%~dpD"
set "PATH=!GIT_CMD_DIR!;!PATH!"
exit /b 0

:download_archive
if not exist "%TMP_DIR%" mkdir "%TMP_DIR%" >nul 2>&1
set "FS_UPDATE_TMP=%TMP_DIR%"
set "FS_UPDATE_URL=%ZIP_URL%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $zip=Join-Path $env:FS_UPDATE_TMP 'repo.zip'; Invoke-WebRequest -UseBasicParsing -Uri $env:FS_UPDATE_URL -OutFile $zip; Expand-Archive -LiteralPath $zip -DestinationPath $env:FS_UPDATE_TMP -Force"
if errorlevel 1 exit /b 1
if not exist "%TMP_DIR%\FaceSearch-main\face_search_gui.py" exit /b 1
exit /b 0

:cleanup_temp
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%" >nul 2>&1
exit /b 0

REM ------------------------------------------------------------
REM Устанавливает только новую зависимость drag-and-drop в уже
REM существующее окружение .venv. Не переустанавливает PyTorch/ORT.
REM ------------------------------------------------------------
:ensure_tkinterdnd
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] .venv не найден. tkinterdnd2 будет установлен через setup.bat.
    exit /b 0
)
".venv\Scripts\python.exe" -c "import tkinterdnd2" >nul 2>&1
if not errorlevel 1 exit /b 0

set "UV_EXE="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV_EXE set "UV_EXE=%%U"
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_EXE if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"
if not defined UV_EXE (
    echo [WARN] tkinterdnd2 не установлен, а uv не найден.
    echo [WARN] Установите зависимость вручную или запустите setup.bat.
    exit /b 1
)

echo [STEP] Установка tkinterdnd2 для drag-and-drop ...
"!UV_EXE!" pip install --python ".venv\Scripts\python.exe" "tkinterdnd2>=0.4.2"
if errorlevel 1 (
    echo [WARN] Не удалось автоматически установить tkinterdnd2.
    echo [WARN] Запустите setup.bat или выполните установку вручную.
    exit /b 1
)
".venv\Scripts\python.exe" -c "import tkinterdnd2" >nul 2>&1
if errorlevel 1 (
    echo [WARN] tkinterdnd2 не прошёл проверку импорта.
    exit /b 1
)
echo [OK] Drag-and-drop зависимость установлена.
exit /b 0

:post_update
call :ensure_tkinterdnd
if errorlevel 1 echo [WARN] Автоматическая настройка drag-and-drop не завершена.
echo.
echo Для обновления/проверки Python-зависимостей можно запустить setup.bat.
echo Он может переустановить PyTorch/ONNX Runtime в соответствии с обнаруженным GPU.
set "RUN_SETUP="
set /p "RUN_SETUP=Запустить setup.bat сейчас? [y/N]: "
if /I "!RUN_SETUP!"=="Y" (
    if exist "%PROJECT_ROOT%\setup.bat" (
        call "%PROJECT_ROOT%\setup.bat"
        if errorlevel 1 (
            echo [ERROR] setup.bat завершился с ошибкой.
            goto :error
        )
    ) else (
        echo [WARN] setup.bat не найден.
    )
)
goto :success

:success
echo.
echo ============================================================
echo [OK] Операция завершена.
echo ============================================================
pause
exit /b 0

:error
echo.
echo ============================================================
echo [ERROR] Операция не завершена. См. сообщения выше.
echo ============================================================
pause
exit /b 1
