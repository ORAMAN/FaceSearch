@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Face Search - Environment Setup (Auto GPU/CPU)

REM ============================================================
REM Face Search setup for Windows x64
REM uv 0.12.23 + CPython 3.13.14
REM Detects NVIDIA GPU, driver, compute capability.
REM Chooses between CUDA 13.2, CUDA 12.6 and CPU wheels.
REM ============================================================

cd /d "%~dp0"

set "UV_REQUIRED=0.12.23"
set "PYTHON_REQUIRED=3.13.14"
set "TORCH_VERSION_CPU=2.14.0"
set "MIN_DRIVER_MAJOR=580"

REM Torch/ORT per GPU branch
set "TORCH_CU13=2.14.0+cu132"
set "TORCH_CU12=2.14.0+cu126"
set "ORT_CU13=1.29.0"
set "ORT_CU12=1.22.0"

if not exist "requirements.txt" (
    echo [ERROR] Не найден requirements.txt
    pause
    exit /b 1
)
if not exist "face_search_gui.py" (
    echo [ERROR] Не найден face_search_gui.py
    pause
    exit /b 1
)

REM ============================================================
REM STEP 0. Detect GPU / driver / compute capability
REM ============================================================
echo.
echo [STEP] Определение GPU ...
set "GPU_MODE=CPU"
set "GPU_NAME="
set "GPU_DRIVER="
set "GPU_CC="
set "DRV_MAJOR="

where nvidia-smi >nul 2>&1
if errorlevel 1 (
    echo [DEBUG] nvidia-smi не найден.
    goto :gpu_decided
)

for /f "usebackq tokens=*" %%G in (`nvidia-smi --query-gpu^=name --format^=csv^,noheader 2^>nul`) do if not defined GPU_NAME set "GPU_NAME=%%G"
for /f "usebackq tokens=*" %%D in (`nvidia-smi --query-gpu^=driver_version --format^=csv^,noheader 2^>nul`) do if not defined GPU_DRIVER set "GPU_DRIVER=%%D"
for /f "usebackq tokens=*" %%C in (`nvidia-smi --query-gpu^=compute_cap --format^=csv^,noheader 2^>nul`) do if not defined GPU_CC set "GPU_CC=%%C"

echo [DEBUG] GPU_NAME="!GPU_NAME!"
echo [DEBUG] GPU_DRIVER="!GPU_DRIVER!"
echo [DEBUG] GPU_CC="!GPU_CC!"

if not defined GPU_NAME goto :gpu_decided
if not defined GPU_DRIVER goto :gpu_decided
if not defined GPU_CC goto :gpu_decided

for /f "tokens=1 delims=." %%V in ("!GPU_DRIVER!") do set "DRV_MAJOR=%%V"
set "DRV_MAJOR=!DRV_MAJOR: =!"
echo !DRV_MAJOR!| findstr /R /C:"^[0-9][0-9]*$" >nul
if errorlevel 1 goto :gpu_decided

REM Compute capability as integer x10
for /f "tokens=1,2 delims=." %%A in ("!GPU_CC!") do (
    set "CC_MAJ=%%A"
    set "CC_MIN=%%B"
)
set "CC_MAJ=!CC_MAJ: =!"
set "CC_MIN=!CC_MIN: =!"
if not defined CC_MIN set "CC_MIN=0"
set /a "CC_NUM=CC_MAJ*10+CC_MIN"
echo [DEBUG] DRV_MAJOR=!DRV_MAJOR!  CC_NUM=!CC_NUM!

REM -- Pascal / Volta: CC 6.1 .. 7.4 -> CUDA 12.6
if !CC_NUM! GEQ 61 if !CC_NUM! LSS 75 (
    if !DRV_MAJOR! LSS 525 (
        echo [WARN] Драйвер !GPU_DRIVER! старше 525, CUDA 12.6 недоступна. Режим: CPU.
        goto :gpu_decided
    )
    set "GPU_MODE=GPU_12"
    echo [INFO] NVIDIA !GPU_NAME! CC !GPU_CC! драйвер !GPU_DRIVER!
    echo [INFO] Режим: GPU CUDA 12.6.
    goto :gpu_decided
)

REM -- Turing и новее: CC >= 7.5 -> CUDA 13.2
if !CC_NUM! GEQ 75 (
    if !DRV_MAJOR! LSS %MIN_DRIVER_MAJOR% (
        echo [WARN] Драйвер !GPU_DRIVER! старше %MIN_DRIVER_MAJOR%, CUDA 13.2 недоступна. Режим: CPU.
        goto :gpu_decided
    )
    set "GPU_MODE=GPU_13"
    echo [INFO] NVIDIA !GPU_NAME! CC !GPU_CC! драйвер !GPU_DRIVER!
    echo [INFO] Режим: GPU CUDA 13.2.
    goto :gpu_decided
)

echo [WARN] GPU !GPU_NAME! CC !GPU_CC! слишком старая. Режим: CPU.

:gpu_decided
echo [INFO] GPU_MODE=!GPU_MODE!
echo.

REM ============================================================
REM Find uv
REM ============================================================
set "UV_EXE="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV_EXE set "UV_EXE=%%U"
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_EXE if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.cargo\bin\uv.exe"

set "UV_INSTALLED="
if defined UV_EXE for /f "tokens=2" %%V in ('"!UV_EXE!" --version 2^>nul') do set "UV_INSTALLED=%%V"
if "!UV_INSTALLED!"=="%UV_REQUIRED%" goto :uv_ready

if defined UV_EXE (
    echo [INFO] Найден uv !UV_INSTALLED!, требуется %UV_REQUIRED%. Устанавливаю...
) else (
    echo [INFO] uv не найден. Устанавливаю %UV_REQUIRED%...
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/%UV_REQUIRED%/install.ps1 | iex"
if errorlevel 1 (
    echo [ERROR] Не удалось установить uv.
    pause
    exit /b 1
)
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
if not exist "!UV_EXE!" for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV_EXE2 set "UV_EXE2=%%U"
if not exist "!UV_EXE!" if defined UV_EXE2 set "UV_EXE=!UV_EXE2!"

:uv_ready
if not defined UV_EXE (
    echo [ERROR] uv не найден.
    pause
    exit /b 1
)
set "UV_INSTALLED="
for /f "tokens=2" %%V in ('"!UV_EXE!" --version 2^>nul') do set "UV_INSTALLED=%%V"
echo [INFO] uv: !UV_INSTALLED!
if not "!UV_INSTALLED!"=="%UV_REQUIRED%" (
    echo [ERROR] Не та версия uv: !UV_INSTALLED!
    pause
    exit /b 1
)

REM ============================================================
REM Managed Python + venv
REM ============================================================
echo.
echo [STEP] Проверка CPython %PYTHON_REQUIRED% ...
"!UV_EXE!" python install %PYTHON_REQUIRED%
if errorlevel 1 (
    echo [ERROR] Не удалось установить Python.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version.split()[0]=='%PYTHON_REQUIRED%' else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] .venv создан с другим Python. Пересоздаю...
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [STEP] Создание .venv ...
    "!UV_EXE!" venv --python %PYTHON_REQUIRED% ".venv"
    if errorlevel 1 (
        echo [ERROR] Не удалось создать .venv.
        pause
        exit /b 1
    )
) else (
    echo [INFO] .venv уже использует Python %PYTHON_REQUIRED%.
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Не удалось активировать .venv.
    pause
    exit /b 1
)
python -c "import sys; print('[INFO] Python:', sys.version)"

REM ============================================================
REM Common deps
REM ============================================================
echo.
echo [STEP] Установка зависимостей из requirements.txt ...
"!UV_EXE!" pip install --python ".venv\Scripts\python.exe" -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Ошибка установки requirements.txt.
    pause
    exit /b 1
)

REM ============================================================
REM PyTorch
REM ============================================================
if "!GPU_MODE!"=="GPU_13" goto :torch_cu13
if "!GPU_MODE!"=="GPU_12" goto :torch_cu12
goto :torch_cpu

:torch_cu13
echo.
echo [STEP] PyTorch %TORCH_CU13% ...
"!UV_EXE!" pip install --python ".venv\Scripts\python.exe" --index-url https://download.pytorch.org/whl/cu132 "torch==%TORCH_CU13%"
if errorlevel 1 (
    echo [ERROR] Не удалось установить PyTorch CUDA 13.2.
    pause
    exit /b 1
)
goto :ort_step

:torch_cu12
echo.
echo [STEP] PyTorch %TORCH_CU12% (CUDA 12.6, для Pascal/Volta) ...
"!UV_EXE!" pip install --python ".venv\Scripts\python.exe" --index-url https://download.pytorch.org/whl/cu126 "torch==%TORCH_CU12%"
if errorlevel 1 (
    echo [ERROR] Не удалось установить PyTorch CUDA 12.6.
    pause
    exit /b 1
)
goto :ort_step

:torch_cpu
echo.
echo [STEP] PyTorch %TORCH_VERSION_CPU% (CPU) ...
"!UV_EXE!" pip install --python ".venv\Scripts\python.exe" "torch==%TORCH_VERSION_CPU%"
if errorlevel 1 (
    echo [ERROR] Не удалось установить PyTorch CPU.
    pause
    exit /b 1
)

:ort_step
REM ============================================================
REM ONNX Runtime
REM ============================================================
if "!GPU_MODE!"=="CPU" goto :ort_skip

echo.
echo [STEP] Удаление CPU ONNX Runtime ...
"!UV_EXE!" pip uninstall --python ".venv\Scripts\python.exe" onnxruntime

if "!GPU_MODE!"=="GPU_13" (
    echo [STEP] ONNX Runtime GPU %ORT_CU13% ^(CUDA 13^) ...
    "!UV_EXE!" pip install --python ".venv\Scripts\python.exe" --upgrade "onnxruntime-gpu[cuda,cudnn]==%ORT_CU13%"
) else (
    echo [STEP] ONNX Runtime GPU %ORT_CU12% ^(CUDA 12^) ...
    "!UV_EXE!" pip install --python ".venv\Scripts\python.exe" --upgrade "onnxruntime-gpu==%ORT_CU12%"
)
if errorlevel 1 (
    echo [ERROR] Не удалось установить ONNX Runtime GPU.
    pause
    exit /b 1
)
goto :verify

:ort_skip
echo [INFO] ONNX Runtime CPU остаётся.

:verify
REM ============================================================
REM Verification
REM ============================================================
if "!GPU_MODE!"=="CPU" goto :verify_cpu

echo.
echo [STEP] Проверка GPU-стека и InsightFace ^(!GPU_MODE!^) ...
python -c "import sys, torch, onnxruntime as ort, faiss; print('[CHECK] Torch', torch.__version__); print('[CHECK] Torch CUDA', torch.version.cuda); print('[CHECK] CUDA available', torch.cuda.is_available()); print('[CHECK] GPU', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'); print('[CHECK] ORT', ort.__version__); print('[CHECK] ORT providers', ort.get_available_providers()); assert torch.cuda.is_available(), 'PyTorch CUDA недоступен'; assert 'CUDAExecutionProvider' in ort.get_available_providers(), 'CUDAExecutionProvider недоступен'"
if errorlevel 1 (
    echo [ERROR] GPU-проверка не пройдена.
    pause
    exit /b 1
)

python -c "from insightface.app import FaceAnalysis; app=FaceAnalysis(name='buffalo_l', providers=['CUDAExecutionProvider','CPUExecutionProvider']); app.prepare(ctx_id=0, det_size=(640,640), det_thresh=0.5); print('[CHECK] InsightFace CUDA OK')"
if errorlevel 1 (
    echo [ERROR] InsightFace CUDA verification failed.
    pause
    exit /b 1
)
goto :syntax

:verify_cpu
echo.
echo [STEP] Проверка CPU-стека и InsightFace ...

REM === Автоматическая установка VC Redistributable ===
echo [INFO] Проверяем наличие Microsoft Visual C++ Redistributable...
"%SystemRoot%\System32\reg.exe" query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v "Installed" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] VC Redistributable не найден. Устанавливаю x64...
    call :install_vc_redist
)
if errorlevel 1 (
    echo [ERROR] Не удалось установить VC Redistributable.
    pause
    exit /b 1
)

echo [STEP] Запуск проверки...

python -c "import sys, torch, onnxruntime as ort, faiss; print('[CHECK] Torch', torch.__version__); print('[CHECK] ORT', ort.__version__); print('[CHECK] ORT providers', ort.get_available_providers()); assert 'CPUExecutionProvider' in ort.get_available_providers()"

if errorlevel 1 (
    echo [ERROR] CPU-проверка не пройдена.
    pause
    exit /b 1
)

python -c "from insightface.app import FaceAnalysis; app=FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider']); app.prepare(ctx_id=-1, det_size=(640,640), det_thresh=0.5); print('[CHECK] InsightFace CPU OK')"

if errorlevel 1 (
    echo [ERROR] InsightFace CPU verification failed.
    pause
    exit /b 1
)
goto :syntax

:install_vc_redist
echo [INFO] Скачиваем VC Redistributable x64...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "irm https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile vc_redist.x64.exe"

echo [INFO] Запускаем установку (может потребоваться перезагрузка)...
start /wait "" vc_redist.x64.exe /install /quiet /norestart

if %errorlevel% equ 0 (
    echo [OK] VC Redistributable успешно установлен.
    del vc_redist.x64.exe
    exit /b 0
) else (
    echo [WARN] Установка VC Redistributable завершилась с кодом %errorlevel%.
    del vc_redist.x64.exe
    exit /b 1
)

:syntax
python -m py_compile face_search_gui.py
if errorlevel 1 (
    echo [ERROR] Ошибка синтаксиса face_search_gui.py.
    pause
    exit /b 1
)

"!UV_EXE!" pip list --python ".venv\Scripts\python.exe" | findstr /I "insightface onnxruntime faiss torch numpy opencv-python Pillow psutil"

echo.
echo ============================================================
echo [OK] Окружение готово. Режим: !GPU_MODE!
echo [OK] Запуск: start_gui.bat
echo ============================================================
pause
exit /b 0
