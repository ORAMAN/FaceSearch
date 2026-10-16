# Зависимости для окружения устанавливаются через setup.bat / requirements.txt.

# Менеджер UV / PowerShell:             powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.23/install.ps1 | iex"
# Менеджер UV / winget:                 winget install --id=astral-sh.uv -e

# Общие зависимости
# PowerShell:                           uv pip install "psycopg[binary]>=3.2" "PyMySQL>=1.1" "pyodbc>=5.1" insightface==2.1 faiss-cpu==1.15.1 "numpy>=2.0,<3" "opencv-python>=4.10" "Pillow>=10.0" "psutil>=6.0" --system
# CMD:                                  uv pip install "psycopg[binary]>=3.2" "PyMySQL>=1.1" "pyodbc>=5.1" insightface==2.1 faiss-cpu==1.15.1 "numpy>=2.0,<3" "opencv-python>=4.10" "Pillow>=10.0" "psutil>=6.0" --system

# Удаление старых пакетов:              uv pip uninstall --system torch torchvision torchaudio onnxruntime onnxruntime-gpu

# torch и CUDA для старых GPU:          uv pip install --system "torch==2.14.0+cu126" "torchvision" "torchaudio" --index-url https://download.pytorch.org/whl/cu126
# torch и CUDA для старых GPU:          uv pip install --system "onnxruntime-gpu==1.22.0"

# torch и CUDA для новых GPU:           uv pip install "torch==2.14.0+cu132" --torch-backend=cu132
# torch и CUDA для новых GPU:           uv pip install --system "onnxruntime-gpu==1.29.0"

# ВАЖНО для Windows + CUDA:
# PyTorch загружается раньше ONNX Runtime. Это позволяет ONNX Runtime
# использовать совместимые CUDA/cuDNN DLL из установленного PyTorch не ставим импорт ниже.

from __future__ import annotations

import argparse
import concurrent.futures
from collections import deque
from itertools import islice
import ctypes
import gc
import hashlib
import html
import importlib
import json
import os
import shutil
import ssl
import queue
import sqlite3
import re
import sys
import subprocess
import threading
import time
import traceback
import webbrowser
import xml.etree.ElementTree as ET
import tkinter as tk
try:
    import psutil
except ImportError:
    psutil = None
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse
from urllib.request import Request, url2pathname, urlopen
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk


def _iter_image_batches(
    directory: Path,
    size: int,
    *,
    status_callback: Callable[[int, int], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    report_every: int = 5000,
):
    """Потоково обходит дерево через scandir и выдаёт ограниченные пакеты изображений.

    В отличие от Path.rglob/os.walk, не материализует весь список содержимого
    отдельного каталога. Поэтому большая плоская папка с миллионом файлов не
    требует предварительно прочитать все записи до получения первых изображений.
    """
    size = max(1, int(size))
    report_every = max(1, int(report_every))
    batch: list[Path] = []
    entries_seen = 0
    images_found = 0
    stack = []

    try:
        # Сразу сообщаем GUI, что именно сейчас начался обход каталога.
        # Это важно для очень больших или медленных сетевых папок: первый пакет
        # ещё не собран, но пользователь уже видит текущий этап.
        if status_callback is not None:
            status_callback(entries_seen, images_found)

        stack.append(os.scandir(os.fspath(directory)))
        while stack:
            if stop_check is not None and stop_check():
                return

            current = stack[-1]
            try:
                entry = next(current)
            except StopIteration:
                try:
                    current.close()
                finally:
                    stack.pop()
                continue
            except OSError:
                try:
                    current.close()
                finally:
                    stack.pop()
                continue

            entries_seen += 1
            try:
                # Не следуем за ссылками на каталоги, чтобы избежать циклов.
                # Для файлов сохраняем прежнее поведение: file-symlink на фото
                # может быть включён в обработку.
                if entry.is_dir(follow_symlinks=False):
                    try:
                        stack.append(os.scandir(entry.path))
                    except OSError:
                        pass
                elif (
                    os.path.splitext(entry.name)[1].lower() in IMAGE_EXTENSIONS
                    and entry.is_file()
                ):
                    batch.append(Path(entry.path))
                    images_found += 1
            except OSError:
                # Удалённые/недоступные во время обхода записи пропускаем.
                pass

            chunk_boundary = entries_seen % report_every == 0
            if chunk_boundary and status_callback is not None:
                status_callback(entries_seen, images_found)

            # Отдаём неполный пакет также после каждых нескольких тысяч
            # записей каталога. Иначе при малом числе изображений среди
            # множества других файлов первый пакет мог бы собираться слишком долго.
            if len(batch) >= size or (chunk_boundary and batch):
                yield batch
                batch = []

        if batch and not (stop_check is not None and stop_check()):
            yield batch
    finally:
        for iterator in reversed(stack):
            try:
                iterator.close()
            except Exception:
                pass


# Разделённые модули конфигурации, настроек, профилей БД и телеметрии.
from facesearch.config import *  # noqa: F401,F403 - constants are public app configuration
from facesearch.settings import (
    _load_gui_settings, _save_gui_settings_data, COLLAPSIBLE_SECTION_DEFAULTS,
)
from facesearch.database_profiles import (
    _profile_storage_slug, _profile_storage_paths, _normalize_profile_storage_paths,
    _default_profile, _load_db_profiles, _save_db_profiles,
)
from facesearch.database.connection import (
    DBResult, DatabaseConnection, _safe_db_identifier, _quote_identifier,
)
from facesearch.database.schema import (
    ensure_columns as _ensure_columns,
    backfill_import_image_urls as _backfill_import_image_urls,
    table_exists as _table_exists,
    initialize_schema as _initialize_schema_impl,
)
from facesearch.processing.telemetry import (
    _auto_worker_limits, _find_nvidia_smi, _refresh_cpu_ram_metrics,
    _read_cpu_ram_metrics, _read_gpu_system_metrics, _gpu_telemetry_worker,
    _windows_drive_to_disk_map, _path_drive_letter, _disk_keys_for_paths,
    _disk_busy_percent, _read_system_metrics, AdaptiveWorkerController,
    AdaptiveConcurrencyLimiter,
)
from facesearch.export.html_report import (
    _safe_file_href, _local_file_href_for_html, vk_last_seen_text,
    vk_verified_text, _site_display_data,
    generate_html_gallery as _generate_html_gallery_impl,
)

# Текущие пути выбранного профиля изменяются при подключении к БД.
DB_FILE = str(APP_DIR / "faces.db")
INDEX_FILE = str(APP_DIR / "indexes" / "legacy_unselected.faiss")
CROP_DIR = APP_DIR / "face_images" / "legacy_unselected"
# HTTP(S)-корень CROP для HTML-галереи; пустая строка означает локальные файлы.
CROP_URL = ""

# Тяжёлые ML/vision-библиотеки загружаются лениво: окно GUI должно появляться
# до импорта PyTorch, ONNX Runtime, OpenCV, NumPy, FAISS и InsightFace.
# Порядок PyTorch -> ONNX Runtime сохранён для совместимости CUDA DLL в Windows.
np = None
cv2 = None
faiss = None
torch = None
ort = None
FaceAnalysis = None
_RUNTIME_IMPORT_LOCK = threading.RLock()
_RUNTIME_IMPORT_ATTEMPTED: dict[str, bool] = {}
_RUNTIME_IMPORT_ERRORS: dict[str, BaseException] = {}


def _ensure_runtime_dependencies(
    *,
    need_numpy: bool = False,
    need_cv2: bool = False,
    need_faiss: bool = False,
    need_ml: bool = False,
    need_insightface: bool = False,
) -> None:
    """Загружает тяжёлые зависимости только в момент первой реальной необходимости."""
    global np, cv2, faiss, torch, ort, FaceAnalysis

    with _RUNTIME_IMPORT_LOCK:
        def load_module(module_name: str, global_name: str, *, required: bool) -> None:
            current = globals().get(global_name)
            if current is None and not _RUNTIME_IMPORT_ATTEMPTED.get(module_name, False):
                _RUNTIME_IMPORT_ATTEMPTED[module_name] = True
                try:
                    globals()[global_name] = importlib.import_module(module_name)
                    _RUNTIME_IMPORT_ERRORS.pop(module_name, None)
                except BaseException as exc:
                    _RUNTIME_IMPORT_ERRORS[module_name] = exc
            current = globals().get(global_name)
            if required and current is None:
                error = _RUNTIME_IMPORT_ERRORS.get(module_name)
                raise RuntimeError(
                    f"Не удалось загрузить обязательную зависимость {module_name!r}: {error or 'модуль недоступен'}"
                ) from error

        if need_numpy or need_cv2 or need_faiss or need_ml or need_insightface:
            load_module("numpy", "np", required=True)
        if need_cv2 or need_insightface:
            load_module("cv2", "cv2", required=True)
        if need_faiss:
            load_module("faiss", "faiss", required=True)
        if need_ml or need_insightface:
            # PyTorch обязательно пытаемся загрузить раньше ONNX Runtime.
            load_module("torch", "torch", required=False)
            load_module("onnxruntime", "ort", required=False)
        if need_insightface and FaceAnalysis is None:
            if not _RUNTIME_IMPORT_ATTEMPTED.get("insightface.app", False):
                _RUNTIME_IMPORT_ATTEMPTED["insightface.app"] = True
                try:
                    FaceAnalysis = getattr(importlib.import_module("insightface.app"), "FaceAnalysis")
                except BaseException as exc:
                    _RUNTIME_IMPORT_ERRORS["insightface.app"] = exc
            if FaceAnalysis is None:
                error = _RUNTIME_IMPORT_ERRORS.get("insightface.app")
                raise RuntimeError(
                    "Не удалось загрузить InsightFace. Проверьте установку insightface и onnxruntime: "
                    f"{error or 'модуль недоступен'}"
                ) from error


# ============================================================
# НАСТРОЙКИ
# ============================================================



# ============================================================
# INSIGHTFACE / CUDA
# ============================================================

_CUDA_DLL_DIR_HANDLES: list[Any] = []
_CUDA_PROVIDER_DLL_HANDLE: Any = None
_CUDA_RUNTIME_ERROR: str | None = None
_CUDA_RUNTIME_PREPARED = False


def _torch_cuda_version() -> str:
    if torch is None:
        return ""
    try:
        return str(torch.version.cuda or "").strip()
    except Exception:
        return ""


def _ort_cuda_build_version() -> str:
    """Возвращает CUDA-версию, с которой собран текущий ONNX Runtime."""
    if ort is None:
        return ""

    try:
        from onnxruntime.capi import build_and_package_info

        value = getattr(build_and_package_info, "cuda_version", "")
        return str(value or "").strip()
    except Exception:
        pass

    try:
        info = str(ort.get_build_info() or "")
        match = re.search(r"CUDA\s+([0-9]+\.[0-9]+)", info, re.IGNORECASE)
        return match.group(1) if match else ""
    except Exception:
        return ""


def _cuda_version_diagnostic() -> str:
    """Формирует понятную диагностику несовместимости CUDA."""
    torch_cuda = _torch_cuda_version()
    ort_cuda = _ort_cuda_build_version()

    if not torch_cuda and not ort_cuda:
        return "CUDA-версию PyTorch/ONNX Runtime определить не удалось."
    if torch_cuda and not ort_cuda:
        return f"PyTorch CUDA {torch_cuda}; CUDA-версия сборки ONNX Runtime не определена."
    if ort_cuda and not torch_cuda:
        return f"ONNX Runtime CUDA {ort_cuda}; PyTorch CUDA не определена."

    try:
        t_major, t_minor = [int(x) for x in torch_cuda.split(".")[:2]]
        o_major, o_minor = [int(x) for x in ort_cuda.split(".")[:2]]
    except Exception:
        return f"PyTorch CUDA {torch_cuda}; ONNX Runtime CUDA {ort_cuda}."

    if t_major != o_major:
        return (
            f"Несовместимый major CUDA: PyTorch {torch_cuda}, "
            f"ONNX Runtime {ort_cuda}."
        )

    # Для CUDA 12.x современные ORT GPU wheels могут требовать CUDA 12.8+.
    # Если ORT собран с более новой minor-версией, чем runtime PyTorch,
    # явно предупреждаем: именно это часто приводит к WinError 127.
    if o_major == 12 and o_minor > t_minor:
        return (
            f"Версия CUDA не совпадает: PyTorch содержит CUDA {torch_cuda}, "
            f"а ONNX Runtime собран под CUDA {ort_cuda}. "
            "Для этой конфигурации используйте совместимую сборку ONNX Runtime "
            "или выровняйте версии CUDA."
        )

    return f"PyTorch CUDA {torch_cuda}; ONNX Runtime CUDA {ort_cuda}: версии совместимы по major/minor."


def _add_cuda_dll_directory(path: Path) -> None:
    """Добавляет каталог DLL в Windows search path."""
    global _CUDA_DLL_DIR_HANDLES

    if os.name != "nt" or not path.is_dir():
        return

    try:
        resolved = str(path.resolve())
    except OSError:
        return

    try:
        handle = os.add_dll_directory(resolved)
    except (AttributeError, OSError):
        return

    _CUDA_DLL_DIR_HANDLES.append((resolved, handle))


def _prepare_cuda_dll_paths() -> None:
    """Настраивает DLL search path для PyTorch/ONNX Runtime на Windows."""
    if os.name != "nt":
        return

    # torch/lib — основной источник CUDA/cuDNN DLL для PyTorch wheels.
    if torch is not None:
        try:
            torch_lib = Path(torch.__file__).resolve().parent / "lib"
            _add_cuda_dll_directory(torch_lib)
        except Exception:
            pass

    # NVIDIA PyPI packages, если они установлены рядом с Python.
    try:
        site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        nvidia_root = site_packages / "nvidia"
        if nvidia_root.is_dir():
            for package_dir in nvidia_root.iterdir():
                bin_dir = package_dir / "bin"
                if bin_dir.is_dir():
                    _add_cuda_dll_directory(bin_dir)
                # Некоторые Linux-style layouts или альтернативные wheels
                # используют lib вместо bin.
                lib_dir = package_dir / "lib"
                if lib_dir.is_dir():
                    _add_cuda_dll_directory(lib_dir)
    except Exception:
        pass

    # Системный CUDA Toolkit, если он установлен.
    for env_name in ("CUDA_PATH", "CUDA_HOME"):
        value = os.environ.get(env_name, "").strip()
        if value:
            cuda_bin = Path(value) / "bin"
            _add_cuda_dll_directory(cuda_bin)


def _preload_cuda_dlls() -> bool:
    """Подготавливает CUDA/cuDNN DLL и проверяет базовую загрузку CUDA EP DLL."""
    global _CUDA_RUNTIME_ERROR, _CUDA_RUNTIME_PREPARED, _CUDA_PROVIDER_DLL_HANDLE

    if _CUDA_RUNTIME_PREPARED:
        return _CUDA_RUNTIME_ERROR is None

    _CUDA_RUNTIME_PREPARED = True

    if ort is None:
        _CUDA_RUNTIME_ERROR = "ONNX Runtime не установлен."
        return False

    _prepare_cuda_dll_paths()

    try:
        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            # Если torch совместим по CUDA/cuDNN, ONNX Runtime может
            # использовать уже загруженные DLL PyTorch. Если не совместим,
            # следующая проверка DLL даст точную ошибку.
            preload(cuda=True, cudnn=True, msvc=True)
    except Exception as exc:
        print(f"ONNX Runtime: preload CUDA DLL warning: {exc}")

    # Не доверяем только get_available_providers(): он может показать CUDA EP,
    # даже когда сама provider DLL не может загрузиться.
    try:
        capi_dir = Path(ort.__file__).resolve().parent / "capi"
        provider_dll = capi_dir / "onnxruntime_providers_cuda.dll"
        if os.name == "nt" and provider_dll.is_file():
            _CUDA_PROVIDER_DLL_HANDLE = ctypes.WinDLL(str(provider_dll))
    except OSError as exc:
        _CUDA_RUNTIME_ERROR = (
            "Не удалось загрузить onnxruntime_providers_cuda.dll: "
            f"{exc}. {_cuda_version_diagnostic()}"
        )
        print(f"ONNX Runtime CUDA DLL error: {_CUDA_RUNTIME_ERROR}")
        return False
    except Exception as exc:
        _CUDA_RUNTIME_ERROR = str(exc)
        print(f"ONNX Runtime CUDA DLL check error: {_CUDA_RUNTIME_ERROR}")
        return False

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        _CUDA_RUNTIME_ERROR = (
            "CUDAExecutionProvider отсутствует в ONNX Runtime. "
            f"{_cuda_version_diagnostic()}"
        )
        return False

    return True


def _ort_cuda_available() -> bool:
    if ort is None:
        return False

    if not _preload_cuda_dlls():
        return False

    try:
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _configure_torch_cuda() -> bool:
    if torch is None:
        return False

    try:
        if not torch.cuda.is_available():
            return False

        torch.cuda.set_device(GPU_INDEX_DEVICE)
        # TF32 не влияет на GTX 1050, но безопасен для современных GPU.
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"PyTorch CUDA setup warning: {exc}")
        return False


def _face_app_provider_report(app: FaceAnalysis) -> tuple[bool, list[str]]:
    """Проверяет реальные providers созданных ONNX Runtime sessions."""
    models = getattr(app, "models", None)
    if not isinstance(models, dict) or not models:
        return False, ["InsightFace не вернул список ONNX-моделей."]

    report: list[str] = []
    all_cuda = True

    for name, model in models.items():
        session = getattr(model, "session", None)
        if session is None:
            all_cuda = False
            report.append(f"{name}: session отсутствует")
            continue

        try:
            providers = list(session.get_providers())
        except Exception as exc:
            all_cuda = False
            report.append(f"{name}: не удалось получить providers: {exc}")
            continue

        report.append(f"{name}: {providers}")
        if "CUDAExecutionProvider" not in providers:
            all_cuda = False

    return all_cuda, report


def _warmup_face_app(app: FaceAnalysis) -> str | None:
    """Первый inference прогревает CUDA context / kernels заранее."""
    if not GPU_WARMUP:
        return None

    try:
        dummy = np.zeros(
            (DET_SIZE[1], DET_SIZE[0], 3),
            dtype=np.uint8,
        )
        app.get(dummy)
        return None
    except Exception as exc:
        return str(exc)


BACKEND_GPU = "gpu"
BACKEND_CPU = "cpu"


def cuda_runtime_available() -> bool:
    """Проверяет наличие PyTorch CUDA и возможность загрузки ONNX Runtime CUDA EP."""
    _ensure_runtime_dependencies(need_numpy=True, need_ml=True)
    torch_ok = _configure_torch_cuda()
    ort_ok = _ort_cuda_available()
    return bool(torch_ok and ort_ok)


def face_app_backend(app: FaceAnalysis | None) -> str:
    """Возвращает фактически использованный backend созданной модели."""
    if app is None:
        return ""
    return str(getattr(app, "_face_backend", BACKEND_CPU))


def create_face_app(backend: str = BACKEND_GPU) -> FaceAnalysis:
    """Создаёт одну модель InsightFace и проверяет реальное использование CUDA."""
    _ensure_runtime_dependencies(need_numpy=True, need_cv2=True, need_ml=True, need_insightface=True)
    backend = BACKEND_CPU if backend == BACKEND_CPU else BACKEND_GPU

    if backend == BACKEND_CPU:
        app = FaceAnalysis(
            name=MODEL_NAME,
            providers=["CPUExecutionProvider"],
        )
        app.prepare(
            ctx_id=-1,
            det_size=DET_SIZE,
            det_thresh=DET_THRESHOLD,
        )
        app._face_backend = BACKEND_CPU
        print("InsightFace: CPUExecutionProvider (forced)")
        return app

    # Сначала проверяем именно PyTorch CUDA и загрузку CUDA EP DLL.
    if not _configure_torch_cuda() or not _preload_cuda_dlls():
        reason = _CUDA_RUNTIME_ERROR or "CUDA runtime недоступен."
        print("InsightFace: CUDA недоступна, переход на CPU")
        print(f"Причина: {reason}")
        print(_cuda_version_diagnostic())
        return create_face_app(BACKEND_CPU)

    try:
        provider_options = {
            "device_id": GPU_INDEX_DEVICE,
            "arena_extend_strategy": "kNextPowerOfTwo",
            "cudnn_conv_algo_search": "HEURISTIC",
            "do_copy_in_default_stream": True,
        }

        app = FaceAnalysis(
            name=MODEL_NAME,
            providers=[
                ("CUDAExecutionProvider", provider_options),
                "CPUExecutionProvider",
            ],
        )
        app.prepare(
            ctx_id=GPU_INDEX_DEVICE,
            det_size=DET_SIZE,
            det_thresh=DET_THRESHOLD,
        )

        cuda_ok, provider_report = _face_app_provider_report(app)
        for line in provider_report:
            print(f"InsightFace providers: {line}")

        if not cuda_ok:
            raise RuntimeError(
                "ONNX Runtime создал InsightFace без CUDAExecutionProvider.\n"
                + "\n".join(provider_report)
                + f"\n{_cuda_version_diagnostic()}"
            )

        warmup_error = _warmup_face_app(app)
        if warmup_error:
            raise RuntimeError(
                "InsightFace CUDA warm-up завершился ошибкой: "
                f"{warmup_error}"
            )

        app._face_backend = BACKEND_GPU

        print("InsightFace: CUDAExecutionProvider")
        try:
            props = torch.cuda.get_device_properties(GPU_INDEX_DEVICE)
            print(
                f"GPU: {props.name}; VRAM: {props.total_memory / 1024**3:.2f} GB"
            )
        except Exception:
            pass
        if ort is not None:
            try:
                print("ONNX Runtime providers:", ort.get_available_providers())
            except Exception:
                pass
        return app

    except Exception as exc:
        print("Ошибка инициализации InsightFace CUDA:")
        print(exc)
        # Полный FAISS может занимать VRAM, которая нужна InsightFace.
        # Не отключаем GPU сразу: переносим индекс в RAM и ищем GPU-чанками,
        # после чего один раз повторяем создание CUDA-модели.
        try:
            if _is_gpu_memory_error(exc) and get_gpu_search_status().get("mode") == "full":
                try:
                    del app
                except UnboundLocalError:
                    pass
                gc.collect()
                if _downgrade_full_gpu_index_to_streaming(INDEX_FILE, str(exc)):
                    return create_face_app(BACKEND_GPU)
        except Exception as downgrade_exc:
            print(f"Восстановление GPU после нехватки VRAM не удалось: {downgrade_exc}")
        if _CUDA_RUNTIME_ERROR:
            print(f"CUDA runtime: {_CUDA_RUNTIME_ERROR}")
        print(_cuda_version_diagnostic())
        print("InsightFace переходит на CPU; GPU search/index status сохраняется отдельно.")

    app = FaceAnalysis(
        name=MODEL_NAME,
        providers=["CPUExecutionProvider"],
    )
    app.prepare(
        ctx_id=-1,
        det_size=DET_SIZE,
        det_thresh=DET_THRESHOLD,
    )
    app._face_backend = BACKEND_CPU
    print("InsightFace: CPUExecutionProvider (fallback)")
    return app

# ============================================================
# SQLITE
# ============================================================

# ============================================================
# DATABASE ABSTRACTION
# ============================================================







def _raw_server_connection(profile: dict[str, Any], database: str | None = None):
    backend = str(profile.get("type") or DB_TYPE_SQLITE)
    host = str(profile.get("host") or "localhost")
    user = str(profile.get("user") or "")
    password = str(profile.get("password") or "")
    port = int(profile.get("port") or DB_DEFAULT_PORTS.get(backend, 0) or 0)
    # Важно: database=None означает "использовать БД из профиля",
    # а database="" означает "подключиться к серверу без выбора БД".
    # Последнее необходимо для MySQL при CREATE DATABASE: если передать
    # profile["database"] здесь, MySQL сначала пытается открыть ещё
    # несуществующую БД и возвращает ошибку 1049 Unknown database.
    db_value = profile.get("database") if database is None else database
    db_name = str(db_value or "").strip()

    if backend == DB_TYPE_POSTGRESQL:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Для PostgreSQL требуется пакет psycopg. "
                "Установите: python -m pip install \"psycopg[binary]\""
            ) from exc

        kwargs = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "dbname": db_name or "postgres",
            "connect_timeout": 15,
        }
        return psycopg.connect(**kwargs)

    if backend == DB_TYPE_MYSQL:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError(
                "Для MySQL требуется PyMySQL. "
                "Установите: python -m pip install PyMySQL"
            ) from exc

        kwargs = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "charset": "utf8mb4",
            "autocommit": False,
            "connect_timeout": 15,
        }
        if db_name:
            kwargs["database"] = db_name
        return pymysql.connect(**kwargs)

    if backend == DB_TYPE_MSSQL:
        try:
            import pyodbc
        except ImportError as exc:
            raise RuntimeError(
                "Для Microsoft SQL Server требуется pyodbc. "
                "Установите: python -m pip install pyodbc"
            ) from exc

        driver = str(profile.get("driver") or "").strip()
        if not driver:
            drivers = list(pyodbc.drivers())
            candidates = [
                d for d in drivers
                if "ODBC Driver 18 for SQL Server" in d
                or "ODBC Driver 17 for SQL Server" in d
            ]
            driver = candidates[-1] if candidates else ""
        if not driver:
            raise RuntimeError(
                "Не найден ODBC-драйвер SQL Server. "
                "Установите Microsoft ODBC Driver 18 for SQL Server."
            )

        server = f"{host},{port}" if port else host
        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={db_name or 'master'};"
            f"UID={user};PWD={password};"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;"
        )
        return pyodbc.connect(conn_str, timeout=15)

    raise ValueError(f"Неизвестный тип БД: {backend}")


def _database_exists(profile: dict[str, Any]) -> bool:
    backend = str(profile.get("type") or "")
    db_name = _safe_db_identifier(profile.get("database"))
    if backend == DB_TYPE_SQLITE:
        return Path(profile.get("path", "")).is_file()

    raw = _raw_server_connection(profile, {
        DB_TYPE_POSTGRESQL: "postgres",
        DB_TYPE_MYSQL: "",
        DB_TYPE_MSSQL: "master",
    }.get(backend))
    try:
        if backend == DB_TYPE_POSTGRESQL:
            cur = raw.cursor()
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,),
            )
        elif backend == DB_TYPE_MYSQL:
            cur = raw.cursor()
            cur.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
                (db_name,),
            )
        else:
            cur = raw.cursor()
            cur.execute(
                "SELECT 1 FROM sys.databases WHERE name = ?",
                (db_name,),
            )
        return cur.fetchone() is not None
    finally:
        raw.close()


def _create_server_database(profile: dict[str, Any]) -> None:
    backend = str(profile.get("type") or "")
    db_name = _safe_db_identifier(profile.get("database"))

    if backend == DB_TYPE_POSTGRESQL:
        raw = _raw_server_connection(profile, "postgres")
        try:
            raw.autocommit = True
            cur = raw.cursor()
            cur.execute(
                f"SELECT 1 FROM pg_database WHERE datname = %s",
                (db_name,),
            )
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {_quote_identifier(db_name, backend)}")
        finally:
            raw.close()
        return

    if backend == DB_TYPE_MYSQL:
        raw = _raw_server_connection(profile, "")
        try:
            cur = raw.cursor()
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(db_name, backend)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            raw.commit()
        finally:
            raw.close()
        return

    if backend == DB_TYPE_MSSQL:
        raw = _raw_server_connection(profile, "master")
        try:
            raw.autocommit = True
            cur = raw.cursor()
            cur.execute(
                "IF DB_ID(?) IS NULL "
                f"BEGIN CREATE DATABASE {_quote_identifier(db_name, backend)} END",
                (db_name,),
            )
        finally:
            raw.close()
        return

    raise ValueError(f"Создание БД не поддерживается: {backend}")


def create_database_for_profile(
    profile: dict[str, Any],
    *,
    maintenance: bool = True,
    create_if_missing: bool = True,
    activate_storage: bool = True,
    initialize: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    allow_thread_handoff: bool = False,
    check_crop_migration: bool = True,
) -> DatabaseConnection:
    """
    Подключает выбранный профиль и гарантирует наличие таблиц/индексов.
    Для серверных БД при create_if_missing=True сама БД создаётся при отсутствии.
    """
    global DB_FILE, INDEX_FILE, CROP_DIR, CROP_URL

    profile = _normalize_profile_storage_paths(profile)
    backend = str(profile.get("type") or DB_TYPE_SQLITE)

    if backend == DB_TYPE_SQLITE:
        db_path = Path(str(profile.get("path") or APP_DIR / "faces.db")).expanduser()
        db_path = db_path.resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(
            str(db_path),
            timeout=60,
            # При старте GUI соединение может быть создано в worker-потоке и
            # передано Tk-потоку после полной остановки worker-а. Одновременного
            # использования нет; для обычных соединений поведение не меняется.
            check_same_thread=not allow_thread_handoff,
        )
        conn = DatabaseConnection(raw, backend)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 60000")
        if activate_storage:
            DB_FILE = str(db_path)
    else:
        if create_if_missing:
            if not _database_exists(profile):
                _create_server_database(profile)
        raw = _raw_server_connection(profile)
        conn = DatabaseConnection(raw, backend)
        if activate_storage:
            DB_FILE = (
                f"{backend}://{profile.get('host')}:{profile.get('port')}/"
                f"{profile.get('database')}"
            )

    index_path = Path(str(profile["index_path"])).expanduser().resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)

    crop_path = Path(str(profile["crop_path"])).expanduser().resolve()
    crop_path.mkdir(parents=True, exist_ok=True)

    if activate_storage:
        INDEX_FILE = str(index_path)
        CROP_DIR = crop_path
        CROP_URL = str(profile.get("crop_url") or "").strip().rstrip("/")

    if initialize:
        if progress_callback:
            progress_callback("Подготовка таблиц и индексов базы данных...")
        _initialize_schema(conn, maintenance=maintenance)

        # Миграция проверяется при обычном подключении приложения один раз.
        # Потоки восстановления CROP открывают собственные соединения и явно
        # пропускают повторную миграционную проверку: иначе она запускалась бы
        # заново для каждой группы исходных изображений и засоряла журнал.
        if check_crop_migration:
            try:
                if _crop_path_migration_needed(conn):
                    if progress_callback:
                        progress_callback("Обновление устаревших путей CROP...")
                    changed = _migrate_crop_paths(conn)
                    print(f"[CROP] Проверка завершена; исправлено путей: {changed}")
                else:
                    print("[CROP] Пути актуальны; миграция не требуется.")
            except Exception as exc:
                print(f"[CROP] Не удалось выполнить миграцию путей: {exc}")

    return conn


# backward-compatible entry point used throughout the existing program
def create_database(
    *,
    maintenance: bool = True,
    initialize: bool = True,
    check_crop_migration: bool = True,
) -> DatabaseConnection:
    profiles, active = _load_db_profiles()
    profile = next((p for p in profiles if str(p.get("name")) == active), None)
    if profile is None:
        profile = _default_profile()
    return create_database_for_profile(
        profile,
        maintenance=maintenance,
        create_if_missing=True,
        initialize=initialize,
        check_crop_migration=check_crop_migration,
    )










# Все функции ниже получают DatabaseConnection, а не DatabaseConnection.
# Это сохраняет существующую архитектуру проекта и позволяет переключать СУБД
# без переписывания логики сканирования/поиска.


def _initialize_schema(conn: DatabaseConnection, *, maintenance: bool = True):
    """Совместимая точка входа; реализация схемы находится в facesearch.database.schema."""
    return _initialize_schema_impl(
        conn,
        maintenance=maintenance,
        cleanup_duplicate_images_fn=cleanup_duplicate_images,
    )


# ============================================================
# FILE HASH / IMAGE
# ============================================================

def calculate_sha256(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def load_image(path: Path):
    try:
        _ensure_runtime_dependencies(need_numpy=True, need_cv2=True)
        data = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return image
    except Exception as e:
        print(f"Ошибка чтения {path}: {e}")
        return None


def embedding_to_blob(embedding: np.ndarray) -> bytes:
    _ensure_runtime_dependencies(need_numpy=True)
    return np.asarray(embedding, dtype=np.float32).tobytes()


def blob_to_embedding(blob: bytes, dimension: int) -> np.ndarray:
    _ensure_runtime_dependencies(need_numpy=True)
    return np.frombuffer(
        blob,
        dtype=np.float32,
        count=dimension,
    ).copy()


# ============================================================
# DATABASE IMAGE PATH / DEDUPLICATION
# ============================================================

def normalize_image_path(path: Path) -> str:
    return _relative_db_path(path)


def legacy_image_path(path: Path) -> str:
    resolved = str(path.resolve())
    cwd = str(Path.cwd().resolve())
    try:
        return resolved.replace(cwd, ".", 1)
    except Exception:
        return resolved


def cleanup_duplicate_images(conn: DatabaseConnection) -> int:
    """Сводит старые дубли по SHA-256, сохраняя faces."""
    groups = conn.execute("""
        SELECT sha256, COUNT(*)
        FROM images
        WHERE sha256 IS NOT NULL AND sha256 != ''
        GROUP BY sha256
        HAVING COUNT(*) > 1
    """).fetchall()

    removed = 0
    for sha256, _count in groups:
        rows = conn.execute("""
            SELECT id, processed
            FROM images
            WHERE sha256 = ?
            ORDER BY processed DESC, id ASC
        """, (sha256,)).fetchall()

        if len(rows) < 2:
            continue

        keep_id = int(rows[0][0])
        keep_processed = int(rows[0][1])

        for duplicate_id_raw, duplicate_processed_raw in rows[1:]:
            duplicate_id = int(duplicate_id_raw)
            duplicate_processed = int(duplicate_processed_raw)

            if not keep_processed and duplicate_processed:
                conn.execute(
                    "UPDATE faces SET image_id = ? WHERE image_id = ?",
                    (keep_id, duplicate_id),
                )
                conn.execute(
                    "UPDATE images SET processed = 1 WHERE id = ?",
                    (keep_id,),
                )
                keep_processed = 1

            conn.execute("DELETE FROM images WHERE id = ?", (duplicate_id,))
            removed += 1

    conn.commit()
    return removed


def cleanup_missing_images(conn: DatabaseConnection) -> int:
    rows = conn.execute(
        "SELECT id, path, source_type, url_image, source_url FROM images"
    ).fetchall()
    missing = []

    for image_id, path, source_type, url_image, source_url in rows:
        try:
            # Пути CROP в БД хранятся относительно активного CROP_DIR,
            # а исходные локальные изображения — относительно APP_DIR.
            # Используем общий resolver, иначе CROP-путь ошибочно проверяется
            # только внутри APP_DIR и валидные записи удаляются из БД.
            exists = _resolve_db_storage_path(path).is_file()
        except Exception:
            exists = False

        # Импортированные JSON-изображения можно восстановить прямо в HTML
        # по сохранённому url_image, поэтому запись и face/embedding сохраняем.
        has_remote_fallback = (
            str(source_type or "") == "import"
            and bool(str(url_image or "").strip())
        )
        # Записи, у которых CROP намеренно отложен, должны переживать обычную
        # очистку при открытии БД, пока исходная локальная фотография доступна.
        try:
            has_local_source = _source_url_to_local_path(source_url) is not None
        except Exception:
            has_local_source = False
        if not exists and not has_remote_fallback and not has_local_source:
            missing.append(int(image_id))

    if not missing:
        return 0

    conn.executemany(
        "DELETE FROM images WHERE id = ?",
        [(image_id,) for image_id in missing],
    )
    conn.commit()
    return len(missing)


def get_or_create_image(
    conn: DatabaseConnection,
    path: Path,
    image: np.ndarray,
    *,
    source_type: str = "local",
    source_url: str | None = None,
    profile_id: int | None = None,
) -> int:
    path = path.resolve()
    path_str = normalize_image_path(path)
    legacy_path = legacy_image_path(path)
    sha256 = calculate_sha256(path)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    row = conn.execute(
        """
        SELECT id, sha256, path, processed
        FROM images
        WHERE path IN (?, ?)
        LIMIT 1
        """,
        (path_str, legacy_path),
    ).fetchone()

    if row:
        image_id = int(row[0])
        conn.execute(
            """
            UPDATE images
            SET path = ?, filename = ?, sha256 = ?,
                width = ?, height = ?, source_type = ?,
                source_url = ?, profile_id = ?, source_updated_at = ?
            WHERE id = ?
            """,
            (
                path_str, path.name, sha256,
                int(image.shape[1]), int(image.shape[0]),
                source_type, source_url, profile_id, timestamp,
                image_id,
            ),
        )
        conn.commit()
        return image_id

    hash_row = conn.execute(
        "SELECT id, path, processed FROM images WHERE sha256 = ? LIMIT 1",
        (sha256,),
    ).fetchone()

    if hash_row:
        image_id = int(hash_row[0])
        old_path = Path(hash_row[1])
        if not (APP_DIR / old_path).resolve().is_file():
            conn.execute(
                """
                UPDATE images
                SET path = ?, filename = ?, width = ?, height = ?,
                    source_type = ?, source_url = ?, profile_id = ?,
                    source_updated_at = ?
                WHERE id = ?
                """,
                (
                    path_str, path.name,
                    int(image.shape[1]), int(image.shape[0]),
                    source_type, source_url, profile_id, timestamp,
                    image_id,
                ),
            )
            conn.commit()
        return image_id

    cursor = conn.execute(
        """
        INSERT INTO images (
            path, filename, sha256, width, height, processed,
            source_type, source_url, profile_id, source_updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            path_str, path.name, sha256,
            int(image.shape[1]), int(image.shape[0]),
            source_type, source_url, profile_id, timestamp,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


# ============================================================
# IMPORTED SOURCE METADATA / FACE CROPS
# ============================================================

def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_crop(image: np.ndarray, bbox: np.ndarray, margin: float = CROP_MARGIN):
    """Безопасно вырезает лицо и не допускает вырожденных crop.

    Особенно важно после детекции на padded-изображении: детектор может
    вернуть bbox, который частично попал в добавленную рамку. Такой bbox
    после обратного пересчёта нельзя превращать в crop размером 1-2 пикселя.
    """
    height, width = image.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox]

    if width < 2 or height < 2:
        return None

    bw = x2 - x1
    bh = y2 - y1
    if bw < 2.0 or bh < 2.0:
        return None

    mx = bw * margin
    my = bh * margin

    left = max(0, int(np.floor(x1 - mx)))
    top = max(0, int(np.floor(y1 - my)))
    right = min(width, int(np.ceil(x2 + mx)))
    bottom = min(height, int(np.ceil(y2 + my)))

    if right <= left or bottom <= top:
        return None

    crop_w = right - left
    crop_h = bottom - top

    # Защита от случайного bbox, который после округления/обрезки
    # превратился в тонкую полоску.
    if crop_w < 8 or crop_h < 8:
        return None

    return image[top:bottom, left:right].copy(), left, top


def _relative_db_path(path: Path) -> str:
    """Хранит обычные пути относительно APP_DIR."""
    try:
        return Path(os.path.relpath(path.resolve(), APP_DIR)).as_posix()
    except Exception:
        return path.as_posix()


def _relative_crop_db_path(path: Path) -> str:
    """Хранит путь CROP только относительно активного CROP_DIR."""
    try:
        return Path(os.path.relpath(path.resolve(), CROP_DIR)).as_posix()
    except Exception:
        return path.name


def _allocate_new_crop_path(conn: DatabaseConnection, image_id: int) -> Path:
    """Выбирает свободный относительный путь для новой CROP-записи.

    После миграции БД images.id может отличаться от ID, зашитого в сохранённом
    пути CROP: миграция сохраняет путь/файл без переименования. Поэтому нельзя
    предполагать, что CROP_DIR/<bucket>/<new image_id>.jpg всегда свободен.
    """
    image_id = int(image_id)
    bucket = ((image_id - 1) // 1000 + 1) * 1000
    crop_dir = CROP_DIR / str(bucket)

    # Обычно сохраняем компактную схему bucket/id.jpg. Если этот путь остался
    # от записи, перенесённой из другой БД, выбираем уникальное альтернативное
    # имя в том же каталоге и не затрагиваем существующий файл или DB-row.
    for attempt in range(100):
        if attempt == 0:
            candidate = crop_dir / f"{image_id}.jpg"
        else:
            candidate = crop_dir / (
                f"{image_id}_{threading.get_ident()}_{time.time_ns()}_{attempt}.jpg"
            )

        relative_path = _relative_crop_db_path(candidate)
        existing = conn.execute(
            "SELECT id FROM images WHERE path = ? LIMIT 1",
            (relative_path,),
        ).fetchone()
        if existing is not None or candidate.exists():
            continue
        return candidate

    raise RuntimeError(
        f"Не удалось подобрать свободный путь CROP для image_id={image_id}"
    )


# Несколько файлов одного профиля могут одновременно обновлять одну строку profiles.
# Полосатые блокировки ограничивают память фиксированным размером и сериализуют
# только импорты, чей url_source попадает в одну и ту же блокировку.
_IMPORT_PROFILE_LOCKS = tuple(threading.RLock() for _ in range(256))


def _import_profile_lock_for_json(
    json_path: Path, metadata: Any | None = None
):
    """Возвращает блокировку для url_source; использует уже прочитанный JSON."""
    if metadata is None:
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    if not isinstance(metadata, dict):
        return None
    source_url = str(metadata.get("url_source") or "").strip()
    if not source_url:
        return None
    return _IMPORT_PROFILE_LOCKS[hash(source_url) % len(_IMPORT_PROFILE_LOCKS)]


def _import_profile_metadata_key(metadata: dict[str, Any]) -> str:
    """Сравнивает профильные метаданные без URL отдельной фотографии."""
    profile_data = dict(metadata)
    # url_image относится к конкретной записи images, а не к профилю.
    # Если учитывать его, каждый файл альбома переписывает одну строку profiles.
    profile_data.pop("url_image", None)
    return json.dumps(
        profile_data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _saved_import_profile_metadata_key(raw_json: Any) -> str | None:
    try:
        value = json.loads(str(raw_json or ""))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    return _import_profile_metadata_key(value)


def _upsert_import_profile(conn: DatabaseConnection, metadata: dict[str, Any]) -> int:
    source_url = str(metadata.get("url_source") or "").strip() or None
    name = str(metadata.get("name") or "").strip()
    info = metadata.get("info")
    if not isinstance(info, dict):
        info = {}

    bdate = str(info.get("БД")) if info.get("БД") is not None else None
    city_title = str(info.get("Город")) if info.get("Город") is not None else None
    status_text = str(info.get("Статус")) if info.get("Статус") is not None else None
    raw_json = json.dumps(metadata, ensure_ascii=False)
    profile_metadata_key = _import_profile_metadata_key(metadata)

    if source_url:
        row = conn.execute(
            """SELECT id, first_name, bdate, city_title, status, raw_json
               FROM profiles
               WHERE source = 'import' AND profile_url = ? LIMIT 1""",
            (source_url,),
        ).fetchone()
        if row:
            profile_id = int(row[0])
            # На каждый url_image в альбоме приходится своя строка images.
            # Не обновляем одну и ту же profiles при обработке каждого кадра,
            # если данные профиля не изменились: это устраняет лишние round-trip
            # к удалённому MySQL и снижает конфликты параллельной записи (1020).
            profile_unchanged = (
                str(row[1] or "") == name
                and row[2] == bdate
                and row[3] == city_title
                and row[4] == status_text
                and _saved_import_profile_metadata_key(row[5]) == profile_metadata_key
            )
            if profile_unchanged:
                return profile_id

            conn.execute(
                """
                UPDATE profiles
                SET first_name = ?, last_name = '',
                    bdate = ?, city_title = ?, status = ?,
                    raw_json = ?, scanned_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    bdate,
                    city_title,
                    status_text,
                    raw_json,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    profile_id,
                ),
            )
            return profile_id

    cur = conn.execute(
        """
        INSERT INTO profiles (
            source, vk_user_id, screen_name, domain,
            first_name, last_name, maiden_name,
            bdate, site, status,
            followers_count, online, last_seen_time, last_seen_platform,
            verified, is_verified,
            occupation_type, occupation_id, occupation_name,
            city_id, city_title, home_town, sex,
            has_photo, has_mobile, can_access_closed, is_closed,
            deactivated, profile_url, raw_json, scanned_at
        )
        VALUES (?, NULL, NULL, NULL, ?, '', NULL, ?, ?, ?,
                NULL, NULL, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, ?, NULL, NULL,
                1, NULL, NULL, NULL, NULL, ?, ?, ?)
        """,
        (
            "import",
            name,
            bdate,
            str(info.get("Страна")) if info.get("Страна") is not None else None,
            status_text,
            city_title,
            source_url,
            raw_json,
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return int(cur.lastrowid)


# Каждый объект FaceAnalysis/ONNX Runtime защищается собственной блокировкой.
# Один session не вызывается одновременно из нескольких worker-ов, но разные
# GPU-сессии могут обрабатывать изображения параллельно. Ранее общий semaphore
# сериализовал ВСЕ inference и сводил на нет пул из нескольких GPU-моделей.
_FACE_INFERENCE_LOCKS: dict[int, threading.RLock] = {}
_FACE_INFERENCE_LOCKS_GUARD = threading.Lock()
_FACE_FALLBACK_LOCK = threading.RLock()
_FACE_CPU_FALLBACK_APP = None
_FACE_GPU_RUNTIME_DISABLED = False


def _face_inference_lock(app):
    key = id(app)
    with _FACE_INFERENCE_LOCKS_GUARD:
        lock = _FACE_INFERENCE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FACE_INFERENCE_LOCKS[key] = lock
        return lock


def _get_cpu_fallback_app():
    global _FACE_CPU_FALLBACK_APP
    with _FACE_FALLBACK_LOCK:
        if _FACE_CPU_FALLBACK_APP is None:
            print("[FACE INFERENCE] Создание резервной модели InsightFace на CPU...")
            _FACE_CPU_FALLBACK_APP = create_face_app(BACKEND_CPU)
        return _FACE_CPU_FALLBACK_APP


def _safe_face_app_get(app: FaceAnalysis, image: np.ndarray, *, stage: str):
    """Безопасный inference; при сбое GPU повторяет кадр на CPU.

    Вызовы одной ONNX-сессии сериализованы, но независимые сессии из пула
    работают параллельно. Это повышает throughput, не создавая гонок внутри
    одного FaceAnalysis.
    """
    global _FACE_GPU_RUNTIME_DISABLED
    active_app = app
    if _FACE_GPU_RUNTIME_DISABLED and face_app_backend(app) == BACKEND_GPU:
        try:
            active_app = _get_cpu_fallback_app()
        except Exception as fallback_exc:
            print(f"[FACE INFERENCE] Не удалось создать CPU fallback: {fallback_exc}")
            return None

    try:
        with _face_inference_lock(active_app):
            return active_app.get(image)
    except Exception as exc:
        text = str(exc).replace("\n", " ")
        if len(text) > 500:
            text = text[:497] + "..."
        is_gpu = face_app_backend(active_app) == BACKEND_GPU
        if is_gpu:
            print(f"[FACE INFERENCE] Ошибка GPU на этапе {stage}; повтор на CPU: {text}")
            try:
                with _FACE_FALLBACK_LOCK:
                    _FACE_GPU_RUNTIME_DISABLED = True
                cpu_app = _get_cpu_fallback_app()
                with _face_inference_lock(cpu_app):
                    return cpu_app.get(image)
            except Exception as fallback_exc:
                fallback_text = str(fallback_exc).replace("\n", " ")[:500]
                print(f"[FACE INFERENCE] Ошибка CPU fallback ({stage}): {fallback_text}")
        else:
            print(f"[FACE INFERENCE] Пропуск изображения: ошибка {stage}: {text}")
        return None


def _detect_faces_with_padding(
    app: FaceAnalysis,
    image: np.ndarray,
    padding_ratio: float = 0.40,
):
    """Распознаёт лица, а при неудаче повторяет детекцию с рамкой.

    Большое лицо иногда плохо детектируется, когда почти целиком занимает
    исходный кадр. В таком случае добавляем отражённую рамку вокруг изображения
    и повторяем inference. Координаты найденных лиц возвращаются уже в
    координатах исходного изображения.
    """
    detected = _safe_face_app_get(app, image, stage="основной кадр")
    # Не запускаем повторный inference на padding после ошибки CUDA/cuDNN:
    # повреждённый CUDA-вызов может повториться и заспамить журнал.
    if detected is None:
        return []
    valid = [
        face for face in detected
        if face.det_score >= DET_THRESHOLD
        and np.linalg.norm(
            np.asarray(face.normed_embedding, dtype=np.float32)
        ) > 0
    ]

    if valid:
        return valid

    height, width = image.shape[:2]
    pad_x = max(1, int(round(width * padding_ratio)))
    pad_y = max(1, int(round(height * padding_ratio)))

    padded = cv2.copyMakeBorder(
        image,
        pad_y, pad_y, pad_x, pad_x,
        cv2.BORDER_REFLECT_101,
    )

    detected = _safe_face_app_get(app, padded, stage="кадр с рамкой")
    if detected is None:
        return []
    result = []

    for face in detected:
        if face.det_score < DET_THRESHOLD:
            continue

        embedding = np.asarray(face.normed_embedding, dtype=np.float32)
        if np.linalg.norm(embedding) <= 0:
            continue

        # InsightFace возвращает bbox относительно padded-изображения.
        # Переводим его обратно в координаты исходника.
        raw_bbox = face.bbox.astype(np.float32).copy()
        raw_bbox[[0, 2]] -= pad_x
        raw_bbox[[1, 3]] -= pad_y

        raw_w = raw_bbox[2] - raw_bbox[0]
        raw_h = raw_bbox[3] - raw_bbox[1]
        if raw_w < 2.0 or raw_h < 2.0:
            continue

        # Если детектор фактически нашёл лицо в добавленной рамке, после
        # обратного пересчёта bbox будет сильно выходить за границы исходника.
        # Не превращаем такой результат в crop размером 1-2 пикселя.
        clipped = raw_bbox.copy()
        clipped[0] = max(0.0, min(float(width), clipped[0]))
        clipped[1] = max(0.0, min(float(height), clipped[1]))
        clipped[2] = max(0.0, min(float(width), clipped[2]))
        clipped[3] = max(0.0, min(float(height), clipped[3]))

        clipped_w = clipped[2] - clipped[0]
        clipped_h = clipped[3] - clipped[1]
        if clipped_w < 2.0 or clipped_h < 2.0:
            continue

        # Не принимаем bbox, у которого большая часть одной из сторон
        # находится только в padding. Это и предотвращает появление
        # вертикальных/горизонтальных полос в базе.
        if clipped_w < raw_w * 0.50 or clipped_h < raw_h * 0.50:
            continue

        # Меняем bbox объекта только для дальнейшего сохранения crop;
        # embedding и det_score остаются от второго прохода.
        face.bbox = clipped
        result.append(face)

    return result


CROP_MAX_SIZE = 240


def _resize_crop_for_storage(
    crop: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Уменьшает crop так, чтобы обе стороны были не больше 240 px.

    Пропорции сохраняются, увеличение маленьких crop не выполняется.
    Возвращается также коэффициент масштаба для пересчёта bbox лица.
    """
    height, width = crop.shape[:2]
    if width <= CROP_MAX_SIZE and height <= CROP_MAX_SIZE:
        return crop, 1.0

    scale = min(
        CROP_MAX_SIZE / float(width),
        CROP_MAX_SIZE / float(height),
    )
    new_width = max(1, int(round(width * scale)))
    new_height = max(1, int(round(height * scale)))

    resized = cv2.resize(
        crop,
        (new_width, new_height),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _face_crop_encoded(crop: np.ndarray) -> tuple[bytes, str]:
    ok, encoded = cv2.imencode(
        ".jpg",
        crop,
        [cv2.IMWRITE_JPEG_QUALITY, CROP_JPEG_QUALITY],
    )
    if not ok:
        raise RuntimeError("Не удалось кодировать crop в JPEG")
    data = encoded.tobytes()
    return data, hashlib.sha256(data).hexdigest()


def _find_existing_source_crop_rows(
    conn: DatabaseConnection,
    *,
    source_type: str,
    source_url: str | None = None,
    profile_id: int | None = None,
    url_image: str | None = None,
    fallback_image_url: str | None = None,
) -> list[tuple[int, str]]:
    """Находит CROP-записи, относящиеся к тому же исходному изображению."""
    if source_type == "local":
        if not source_url:
            return []
        rows = conn.execute(
            """
            SELECT id, path
            FROM images
            WHERE source_type = 'local' AND source_url = ?
            ORDER BY id
            """,
            (source_url,),
        ).fetchall()
    else:
        if profile_id is None:
            return []
        if url_image:
            rows = conn.execute(
                """
                SELECT id, path
                FROM images
                WHERE source_type = 'import'
                  AND profile_id = ? AND url_image = ?
                ORDER BY id
                """,
                (int(profile_id), url_image),
            ).fetchall()
        else:
            # Без url_image профильная ссылка source_url не позволяет определить,
            # какая исходная фотография породила CROP: у одного профиля может быть
            # сотни фотографий. Нельзя связывать все строки с NULL url_image с
            # текущим файлом — иначе последующие фотографии профиля будут пропущены.
            # Сопоставляем только по точному локальному URI исходного файла.
            if not fallback_image_url:
                return []
            rows = conn.execute(
                """
                SELECT id, path
                FROM images
                WHERE source_type = 'import'
                  AND profile_id = ? AND url_image = ?
                ORDER BY id
                """,
                (int(profile_id), fallback_image_url),
            ).fetchall()

    return [(int(row[0]), str(row[1] or "")) for row in rows]


def _delete_source_crop_rows(
    conn: DatabaseConnection, rows: list[tuple[int, str]]
) -> None:
    """Пакетно удаляет CROP-записи и embeddings в текущей транзакции."""
    if not rows:
        return
    ids = [(int(image_id),) for image_id, _stored_path in rows]
    # executemany переиспользует один cursor вместо создания cursor на каждую
    # строку. Это особенно заметно при повторной обработке больших альбомов.
    conn.executemany("DELETE FROM faces WHERE image_id = ?", ids)
    conn.executemany("DELETE FROM images WHERE id = ?", ids)


def _prepare_source_crop_overwrite(
    conn: DatabaseConnection, rows: list[tuple[int, str]]
) -> None:
    """Освобождает SHA-256 старых строк, сохраняя их ID и записи images.

    Частичный UNIQUE-индекс SQLite запрещает временные дубликаты SHA-256.
    NULL выводит заменяемые строки из индекса до вычисления новых хэшей;
    транзакция восстановит старые значения автоматически при rollback.
    """
    if rows:
        conn.executemany(
            "UPDATE images SET sha256 = NULL WHERE id = ?",
            [(int(image_id),) for image_id, _stored_path in rows],
        )


def _delete_processed_source_files(
    image_path: Path, metadata_path: Path | None = None
) -> bool:
    """Удаляет исходное изображение и, если передан, JSON после успешной обработки.

    Отсутствие найденных лиц не считается ошибкой обработки. Ошибки удаления
    файлов фиксируются отдельно и не маскируют результат распознавания.
    """
    deleted = True
    for path in (image_path, metadata_path):
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            deleted = False
            print(f"[WARN] Не удалось удалить обработанный исходник {path}: {exc}")
    return deleted


def _cleanup_replaced_crop_files(
    rows: list[tuple[int, str]], preserve_paths: list[Path] | None = None
) -> None:
    """Удаляет старые файлы CROP после успешной замены, не трогая исходники."""
    crop_root = Path(CROP_DIR).resolve()
    preserve = set()
    for value in preserve_paths or []:
        try:
            preserve.add(Path(value).resolve())
        except Exception:
            pass

    for image_id, stored_path in rows:
        if not stored_path:
            continue
        try:
            path = _resolve_db_crop_path(stored_path, image_id=int(image_id)).resolve()
            # Удаляем только файлы внутри активного каталога CROP. Это не даст
            # затронуть исходные фото при встрече с legacy-путём в БД.
            path.relative_to(crop_root)
            if path in preserve or not path.is_file():
                continue
            path.unlink()
        except (OSError, ValueError, RuntimeError):
            continue


def _rollback_crop_file_writes(
    created_files: list[Path], backups: dict[Path, bytes]
) -> None:
    """Откатывает изменения JPEG, в том числе если новый ID занял старый путь."""
    backup_paths = {Path(path).resolve() for path in backups}
    for path in created_files:
        try:
            if Path(path).resolve() not in backup_paths:
                Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    for path, old_bytes in backups.items():
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(old_bytes)
        except OSError:
            pass


def _restore_missing_source_crops(
    conn: DatabaseConnection,
    image: np.ndarray,
    detected_faces: list[Any],
    crop_rows: list[tuple[int, str]],
    *,
    min_match_similarity: float = 0.20,
) -> int:
    """Создаёт только отсутствующие CROP для уже известных записей images.

    Не вставляет и не обновляет строки БД/embedding: путь каждой строки
    берётся из images.path и разрешается относительно CROP_DIR профиля.
    Детектированные лица сопоставляются с сохранёнными embedding, чтобы
    изменившийся исходник не подменил старый CROP лицом другого человека.
    """
    if image is None or not detected_faces or not crop_rows:
        return 0

    used_faces: set[int] = set()
    restored = 0

    for image_id, stored_path in crop_rows:
        image_id = int(image_id)
        target = _resolve_db_crop_path(stored_path, image_id=image_id)
        if target.is_file():
            # И для существующего CROP резервируем его соответствующее лицо,
            # чтобы строки с отсутствующим файлом не сопоставились с ним повторно.
            pass

        embedding_row = conn.execute(
            "SELECT embedding, embedding_dim FROM faces WHERE image_id = ? ORDER BY id LIMIT 1",
            (image_id,),
        ).fetchone()
        if not embedding_row:
            continue

        try:
            stored_embedding = blob_to_embedding(
                embedding_row[0], int(embedding_row[1])
            )
        except Exception as exc:
            print(f"[CROP] image_id={image_id}: embedding недоступен: {exc}")
            continue

        candidates = [i for i in range(len(detected_faces)) if i not in used_faces]
        if not candidates:
            candidates = list(range(len(detected_faces)))

        best_index = -1
        best_similarity = -1.0
        for index in candidates:
            try:
                similarity = _cosine_similarity(
                    stored_embedding,
                    np.asarray(
                        detected_faces[index].normed_embedding, dtype=np.float32
                    ),
                )
            except Exception:
                continue
            if similarity > best_similarity:
                best_similarity = similarity
                best_index = index

        if best_index < 0 or best_similarity < min_match_similarity:
            continue

        used_faces.add(best_index)
        if target.is_file():
            continue

        crop_result = _safe_crop(
            image, detected_faces[best_index].bbox.astype(np.float32)
        )
        if crop_result is None:
            continue

        crop, _crop_left, _crop_top = crop_result
        crop, _scale = _resize_crop_for_storage(crop)
        try:
            encoded, _sha256 = _face_crop_encoded(crop)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(
                f".{target.stem}.restore_{threading.get_ident()}_{time.time_ns()}.tmp"
            )
            try:
                tmp.write_bytes(encoded)
                tmp.replace(target)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            restored += 1
            print(f"[CROP] Восстановлен существующий image_id={image_id}: {target}")
        except Exception as exc:
            print(f"[CROP] Не удалось восстановить image_id={image_id} в {target}: {exc}")

    return restored


def import_image_bundle(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    image_path: Path,
    json_path: Path,
    *,
    delete_source: bool = True,
    create_crop_files: bool = True,
    overwrite_existing_crops: bool = False,
    metadata: Any | None = None,
) -> dict[str, int | bool | str]:

    delete_source = bool(delete_source)
    create_crop_files = bool(create_crop_files)
    overwrite_existing_crops = bool(overwrite_existing_crops)

    if metadata is None:
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"Ошибка JSON {json_path.name}: {e}") from e

    if not isinstance(metadata, dict):
        raise RuntimeError(f"JSON {json_path.name} должен содержать объект")

    source_url = str(metadata.get("url_source") or "").strip() or None
    metadata_image_url = str(metadata.get("url_image") or "").strip() or None
    fallback_image_url = ""
    try:
        fallback_image_url = image_path.resolve().as_uri()
    except (OSError, ValueError):
        fallback_image_url = str(image_path.resolve())

    # Повторное сканирование уже импортированных изображений не должно заново
    # загружать картинку и запускать InsightFace, если исходник уже представлен
    # в БД и все его CROP на месте. Это особенно важно для удалённого MySQL.
    pre_profile_id = None
    preexisting_source_rows: list[tuple[int, str]] = []
    preflight_read_started = bool(source_url and not overwrite_existing_crops)
    if preflight_read_started:
        profile_row = conn.execute(
            "SELECT id FROM profiles WHERE source = 'import' AND profile_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        if profile_row:
            pre_profile_id = int(profile_row[0])
            preexisting_source_rows = _find_existing_source_crop_rows(
                conn,
                source_type="import",
                source_url=source_url,
                profile_id=pre_profile_id,
                url_image=metadata_image_url,
                fallback_image_url=fallback_image_url,
            )

            if preexisting_source_rows:
                missing_rows = []
                if create_crop_files:
                    missing_rows = [
                        (image_id, stored_path)
                        for image_id, stored_path in preexisting_source_rows
                        if not _resolve_db_storage_path(stored_path).is_file()
                    ]

                if not create_crop_files or not missing_rows:
                    # Обновляем метаданные профиля, как и раньше, но пропускаем
                    # повторное чтение изображения, inference и проверку SHA crop.
                    _upsert_import_profile(conn, metadata)
                    conn.commit()
                    deleted = (
                        _delete_processed_source_files(image_path, json_path)
                        if delete_source else False
                    )
                    return {
                        "faces": 0,
                        "deleted": deleted,
                        "message": "исходное изображение уже есть в БД; распознавание пропущено",
                        "updated": 0,
                        "added": 0,
                        "removed": 0,
                        "restored": 0,
                        "detected_faces": None,
                    }

    # Завершаем транзакцию чтения до длительной загрузки/распознавания кадра.
    # Для удалённого MySQL это не позволяет держать старый read snapshot всё
    # время inference и снижает вероятность конфликтов с параллельными UPDATE.
    if preflight_read_started:
        try:
            conn.rollback()
        except Exception:
            pass

    image = load_image(image_path)
    if image is None:
        raise RuntimeError(f"Не удалось открыть изображение: {image_path}")

    valid_faces = _detect_faces_with_padding(app, image, padding_ratio=0.40)

    if not valid_faces:
        # Распознавание завершилось штатно: отсутствие лиц — это результат,
        # а не ошибка. При включённом чекбоксе удаляем и изображение, и JSON.
        deleted = _delete_processed_source_files(image_path, json_path) if delete_source else False
        return {
            "faces": 0,
            "deleted": deleted,
            "message": "лицо не найдено",
            "detected_faces": 0,
        }

    profile_id = _upsert_import_profile(conn, metadata)
    # Если JSON не содержит URL оригинала, используем локальный URI исходника
    # независимо от режима создания CROP. Это позволяет отличать несколько
    # фотографий одного профиля и не пропускать новые файлы при повторном сканировании.
    image_url = metadata_image_url or fallback_image_url

    created = 0
    created_files: list[Path] = []
    crop_file_backups: dict[Path, bytes] = {}
    if pre_profile_id == profile_id:
        existing_source_rows = preexisting_source_rows
    else:
        existing_source_rows = _find_existing_source_crop_rows(
            conn,
            source_type="import",
            source_url=source_url,
            profile_id=profile_id,
            url_image=metadata_image_url,
            fallback_image_url=fallback_image_url,
        )
    restored_missing_crops = 0
    if create_crop_files and not overwrite_existing_crops and existing_source_rows:
        restored_missing_crops = _restore_missing_source_crops(
            conn, image, valid_faces, existing_source_rows
        )
    # При обычном сканировании существующие записи не переписываем. После
    # восстановления отсутствующих файлов дальше не добавляем повторные строки
    # images/faces для того же самого источника.
    old_crop_rows = existing_source_rows if overwrite_existing_crops else []
    if existing_source_rows and not overwrite_existing_crops:
        # _upsert_import_profile мог обновить профиль. Фиксируем транзакцию до
        # выхода из функции, пока блокировка данного url_source ещё удерживается.
        conn.commit()
        deleted = False
        if delete_source:
            try:
                image_path.unlink()
                json_path.unlink()
                deleted = True
            except OSError:
                deleted = False
        message = "исходное изображение уже есть в БД; распознавание пропущено"
        if restored_missing_crops:
            message += f"; восстановлено CROP: {restored_missing_crops}"
        return {
            "faces": 0,
            "deleted": deleted,
            "message": message,
            "updated": 0,
            "added": 0,
            "removed": 0,
            "restored": restored_missing_crops,
            "detected_faces": len(valid_faces),
        }

    old_crop_row_index = 0
    updated_count = 0
    added_count = 0
    removed_count = 0

    try:
        if old_crop_rows:
            # Не удаляем старые images/faces. Сохраняем существующие ID и
            # обновим строки на месте после успешного распознавания.
            _prepare_source_crop_overwrite(conn, old_crop_rows)

        for face in valid_faces:
            bbox = face.bbox.astype(np.float32)
            crop_result = _safe_crop(image, bbox)
            if crop_result is None:
                continue
            crop, crop_left, crop_top = crop_result

            # Для хранения ограничиваем crop максимумом 240x240,
            # сохраняя пропорции. Координаты bbox ниже пересчитываются
            # тем же коэффициентом. Маленькие crop не увеличиваем.
            crop, crop_scale = _resize_crop_for_storage(crop)

            encoded, sha256 = _face_crop_encoded(crop)

            duplicate = conn.execute(
                "SELECT id FROM images WHERE sha256 = ? LIMIT 1",
                (sha256,),
            ).fetchone()
            if duplicate:
                # Дубликат принадлежит другой записи. Старую строку источника
                # оставляем незанятой; она будет удалена лишь если после полного
                # сканирования для неё действительно не осталось лица.
                continue

            embedding = np.asarray(face.normed_embedding, dtype=np.float32)
            embedding_norm = float(np.linalg.norm(embedding))
            if embedding_norm <= 0.0:
                continue
            embedding /= embedding_norm

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if old_crop_row_index < len(old_crop_rows):
                # Переиспользуем ID существующего CROP вместо DELETE + INSERT.
                image_id, old_db_path = old_crop_rows[old_crop_row_index]
                image_id = int(image_id)
                old_crop_row_index += 1
                updated_count += 1
                # Существующий путь в БД — источник истины: он может содержать
                # подпапку профиля/пользовательскую структуру, отличную от bucket/id.
                crop_path = _resolve_db_crop_path(old_db_path, image_id=image_id)
                crop_dir = crop_path.parent
                conn.execute(
                    """
                    UPDATE images
                    SET path = ?, filename = ?, sha256 = ?, width = ?, height = ?,
                        processed = 1, source_type = 'import', source_url = ?,
                        url_image = ?, profile_id = ?, source_updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _relative_crop_db_path(crop_path), crop_path.name, sha256,
                        int(crop.shape[1]), int(crop.shape[0]), source_url,
                        image_url, profile_id, timestamp, image_id,
                    ),
                )
            else:
                # Новое лицо: резервируем ID; фактический CROP получает имя ID.
                added_count += 1
                temp_name = f"pending_{threading.get_ident()}_{time.time_ns()}.jpg"
                cur = conn.execute(
                    """
                    INSERT INTO images (
                        path, filename, sha256, width, height, processed,
                        source_type, source_url, url_image, profile_id, source_updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 'import', ?, ?, ?, ?)
                    """,
                    (
                        temp_name, temp_name, sha256, int(crop.shape[1]),
                        int(crop.shape[0]), source_url, image_url, profile_id, timestamp,
                    ),
                )
                image_id = int(cur.lastrowid)
                crop_path = _allocate_new_crop_path(conn, image_id)
                crop_dir = crop_path.parent
                conn.execute(
                    "UPDATE images SET path = ?, filename = ?, processed = 1 WHERE id = ?",
                    (_relative_crop_db_path(crop_path), crop_path.name, image_id),
                )

            if create_crop_files:
                crop_dir.mkdir(parents=True, exist_ok=True)
                if overwrite_existing_crops and crop_path.is_file():
                    crop_file_backups.setdefault(crop_path, crop_path.read_bytes())
                created_files.append(crop_path)
                crop_path.write_bytes(encoded)

            # При повторном сканировании embedding обновляется для того же ID.
            conn.execute("DELETE FROM faces WHERE image_id = ?", (image_id,))

            # После crop координаты лица относятся уже к crop.
            crop_x1 = max(0.0, float(bbox[0]) - crop_left) * crop_scale
            crop_y1 = max(0.0, float(bbox[1]) - crop_top) * crop_scale
            crop_x2 = min(
                float(crop.shape[1]) / crop_scale,
                float(bbox[2]) - crop_left,
            ) * crop_scale
            crop_y2 = min(
                float(crop.shape[0]) / crop_scale,
                float(bbox[3]) - crop_top,
            ) * crop_scale

            conn.execute(
                """
                INSERT INTO faces (
                    image_id, x1, y1, x2, y2,
                    det_score, embedding, embedding_dim
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    crop_x1, crop_y1, crop_x2, crop_y2,
                    float(face.det_score),
                    embedding_to_blob(embedding),
                    int(embedding.shape[0]),
                ),
            )
            created += 1

        if created > 0 and old_crop_row_index < len(old_crop_rows):
            removed_count = len(old_crop_rows) - old_crop_row_index
            _delete_source_crop_rows(conn, old_crop_rows[old_crop_row_index:])

        if created == 0:
            conn.rollback()
            _rollback_crop_file_writes(created_files, crop_file_backups)
            if delete_source:
                deleted = _delete_processed_source_files(image_path, json_path)
            else:
                deleted = False
            message = (
                f"распознано лиц: {len(valid_faces)}, "
                "новых CROP нет — соответствующие записи уже есть в БД"
            )
            if restored_missing_crops:
                message += f"; восстановлено CROP: {restored_missing_crops}"
            return {
                "faces": 0, "deleted": deleted, "message": message,
                "updated": 0, "added": 0, "removed": 0,
                "restored": restored_missing_crops,
                "detected_faces": len(valid_faces),
            }

        conn.commit()

    except Exception:
        conn.rollback()
        _rollback_crop_file_writes(created_files, crop_file_backups)
        raise

    if old_crop_rows:
        _cleanup_replaced_crop_files(old_crop_rows, created_files)

    if delete_source:
        try:
            image_path.unlink()
            json_path.unlink()
            deleted = True
        except OSError:
            deleted = False
    else:
        deleted = False

    return {
        "faces": created,
        "deleted": deleted,
        "message": "ok",
        "updated": updated_count,
        "added": added_count,
        "removed": removed_count,
        "detected_faces": len(valid_faces),
    }



def process_local_image(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    image_path: Path,
    *,
    delete_source: bool = False,
    create_crop_files: bool = True,
    overwrite_existing_crops: bool = False,
) -> dict[str, int | bool | str]:
    """Сканирует локальную фотографию без JSON-метаданных.

    Для каждого найденного лица создаётся отдельная запись images/faces.
    При create_crop_files=False embedding и ожидаемый путь CROP сохраняются
    в БД, но JPEG не записывается. Исходная фотография остаётся на месте,
    чтобы CROP можно было позднее восстановить кнопкой «Проверить CROP».
    В images.source_url хранится file://-ссылка на исходник.
    Исходный локальный файл никогда не удаляется; удаление разрешено только
    для пары изображение+JSON в import_image_bundle(). Параметр delete_source
    сохранён для совместимости со старыми вызывающими участками.
    """
    delete_source = False  # локальные исходники сохраняем при любых настройках
    overwrite_existing_crops = bool(overwrite_existing_crops)
    image_path = image_path.resolve()

    if not image_path.is_file():
        return {
            "faces": 0,
            "deleted": False,
            "message": "файл отсутствует",
        }

    try:
        source_url = image_path.as_uri()
    except (OSError, ValueError):
        source_url = str(image_path)

    # Если этот исходник уже был обработан в режиме "без JSON",
    # не создаём новые дубли при повторном сканировании.
    existing_rows = _find_existing_source_crop_rows(
        conn,
        source_type="local",
        source_url=source_url,
    )

    if existing_rows and not overwrite_existing_crops:
        missing_existing_rows = [
            (image_id, stored_path)
            for image_id, stored_path in existing_rows
            if not _resolve_db_crop_path(stored_path, image_id=int(image_id)).is_file()
        ]
        restored_missing_crops = 0
        detected_faces: int | None = None
        if create_crop_files and missing_existing_rows:
            existing_image = load_image(image_path)
            if existing_image is not None:
                existing_faces = _detect_faces_with_padding(
                    app, existing_image, padding_ratio=0.40
                )
                detected_faces = len(existing_faces)
                restored_missing_crops = _restore_missing_source_crops(
                    conn, existing_image, existing_faces, existing_rows
                )

        # Локальные исходные изображения сохраняются даже при повторном сканировании.
        deleted = False
        message = "исходник уже есть в БД"
        if restored_missing_crops:
            message += f"; восстановлено CROP: {restored_missing_crops}"
        return {
            "faces": 0,
            "deleted": deleted,
            "message": message,
            "restored": restored_missing_crops,
            "detected_faces": detected_faces,
        }

    image = load_image(image_path)
    if image is None:
        return {
            "faces": 0,
            "deleted": False,
            "message": "не удалось открыть изображение",
            "detected_faces": None,
        }

    valid_faces = _detect_faces_with_padding(
        app,
        image,
        padding_ratio=0.40,
    )

    if not valid_faces:
        # Отсутствие лиц — штатный результат, но локальный исходник сохраняем.
        return {
            "faces": 0,
            "deleted": False,
            "message": "лицо не найдено",
            "detected_faces": 0,
        }

    created = 0
    created_files: list[Path] = []
    crop_file_backups: dict[Path, bytes] = {}

    existing_row_index = 0
    updated_count = 0
    added_count = 0
    removed_count = 0

    try:
        if overwrite_existing_crops and existing_rows:
            _prepare_source_crop_overwrite(conn, existing_rows)

        for face in valid_faces:
            bbox = face.bbox.astype(np.float32)
            crop_result = _safe_crop(image, bbox)
            if crop_result is None:
                continue

            crop, crop_left, crop_top = crop_result
            crop, crop_scale = _resize_crop_for_storage(crop)
            encoded, sha256 = _face_crop_encoded(crop)

            # Не создаём одинаковый crop повторно; старые строки источника
            # уже имеют sha256=NULL и исключены из частичного UNIQUE-индекса.
            duplicate = conn.execute(
                "SELECT id FROM images WHERE sha256 = ? LIMIT 1",
                (sha256,),
            ).fetchone()
            if duplicate:
                continue

            embedding = np.asarray(face.normed_embedding, dtype=np.float32)
            norm = float(np.linalg.norm(embedding))
            if norm <= 0.0:
                continue
            embedding /= norm

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if existing_row_index < len(existing_rows):
                # Сохраняем ID CROP и обновляем строку images на месте.
                image_id, old_db_path = existing_rows[existing_row_index]
                image_id = int(image_id)
                existing_row_index += 1
                updated_count += 1
                # При обновлении не меняем сохранённое относительное имя/каталог CROP.
                crop_path = _resolve_db_crop_path(old_db_path, image_id=image_id)
                crop_dir = crop_path.parent
                conn.execute(
                    """
                    UPDATE images
                    SET path = ?, filename = ?, sha256 = ?, width = ?, height = ?,
                        processed = 1, source_type = 'local', source_url = ?,
                        url_image = NULL, profile_id = NULL, source_updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _relative_crop_db_path(crop_path), crop_path.name, sha256,
                        int(crop.shape[1]), int(crop.shape[0]), source_url,
                        timestamp, image_id,
                    ),
                )
            else:
                added_count += 1
                temp_name = f"pending_{threading.get_ident()}_{time.time_ns()}.jpg"
                cur = conn.execute(
                    """
                    INSERT INTO images (
                        path, filename, sha256, width, height, processed,
                        source_type, source_url, profile_id, source_updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 'local', ?, NULL, ?)
                    """,
                    (
                        temp_name, temp_name, sha256, int(crop.shape[1]),
                        int(crop.shape[0]), source_url, timestamp,
                    ),
                )
                image_id = int(cur.lastrowid)
                crop_path = _allocate_new_crop_path(conn, image_id)
                crop_dir = crop_path.parent
                conn.execute(
                    "UPDATE images SET path = ?, filename = ?, processed = 1 WHERE id = ?",
                    (_relative_crop_db_path(crop_path), crop_path.name, image_id),
                )

            if create_crop_files:
                crop_dir.mkdir(parents=True, exist_ok=True)
                if overwrite_existing_crops and crop_path.is_file():
                    crop_file_backups.setdefault(crop_path, crop_path.read_bytes())
                created_files.append(crop_path)
                crop_path.write_bytes(encoded)

            # Полностью заменяем embedding/координаты, оставляя image_id прежним.
            conn.execute("DELETE FROM faces WHERE image_id = ?", (image_id,))

            crop_x1 = max(0.0, float(bbox[0]) - crop_left) * crop_scale
            crop_y1 = max(0.0, float(bbox[1]) - crop_top) * crop_scale
            crop_x2 = min(
                float(crop.shape[1]) / crop_scale,
                float(bbox[2]) - crop_left,
            ) * crop_scale
            crop_y2 = min(
                float(crop.shape[0]) / crop_scale,
                float(bbox[3]) - crop_top,
            ) * crop_scale

            conn.execute(
                """
                INSERT INTO faces (
                    image_id, x1, y1, x2, y2,
                    det_score, embedding, embedding_dim
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    image_id,
                    crop_x1,
                    crop_y1,
                    crop_x2,
                    crop_y2,
                    float(face.det_score),
                    embedding_to_blob(embedding),
                    int(embedding.shape[0]),
                ),
            )
            created += 1

        if overwrite_existing_crops and created > 0 and existing_row_index < len(existing_rows):
            removed_count = len(existing_rows) - existing_row_index
            _delete_source_crop_rows(conn, existing_rows[existing_row_index:])

        if created == 0:
            conn.rollback()
            _rollback_crop_file_writes(created_files, crop_file_backups)

            # Все crop уже есть в БД. Даже после успешной обработки локальный
            # исходник оставляем на диске: удаляются только пары image+JSON.
            deleted = False

            return {
                "faces": 0,
                "deleted": deleted,
                "message": (
                    f"распознано лиц: {len(valid_faces)}, "
                    "новых CROP нет — соответствующие записи уже есть в БД"
                ),
                "updated": 0,
                "added": 0,
                "removed": 0,
                "detected_faces": len(valid_faces),
            }

        conn.commit()

    except Exception:
        conn.rollback()
        _rollback_crop_file_writes(created_files, crop_file_backups)
        raise

    if overwrite_existing_crops and existing_rows:
        _cleanup_replaced_crop_files(existing_rows, created_files)

    # Даже если распознавание прошло успешно, отдельный локальный исходник
    # без обработки пары image+JSON не удаляем.
    deleted = False

    return {
        "faces": created,
        "deleted": deleted,
        "message": "ok",
        "updated": updated_count,
        "added": added_count,
        "removed": removed_count,
        "detected_faces": len(valid_faces),
    }


# ============================================================
# SCAN ONE IMAGE
# ============================================================

def process_image(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    path: Path,
    *,
    source_type: str = "local",
    source_url: str | None = None,
    profile_id: int | None = None,
    force: bool = False,
) -> int:

    if not path.is_file():
        print(f"[SKIP] Файл отсутствует: {path}")
        return 0

    image = load_image(path)
    if image is None:
        print(f"[ERROR] {path}")
        return 0

    path_str = normalize_image_path(path)
    legacy_path = legacy_image_path(path)

    row = conn.execute(
        """
        SELECT id, processed, sha256
        FROM images
        WHERE path IN (?, ?)
        LIMIT 1
        """,
        (path_str, legacy_path),
    ).fetchone()

    current_sha256 = calculate_sha256(path)

    # Повторный проход по уже обработанному файлу.
    if row and row[1] == 1 and row[2] == current_sha256 and not force:
        return 0

    # То же содержимое может находиться под другим именем/путём.
    hash_row = conn.execute(
        """
        SELECT i.id, i.path, i.processed
        FROM images i
        WHERE i.sha256 = ?
        LIMIT 1
        """,
        (current_sha256,),
    ).fetchone()

    if hash_row and not force:
        stored_path = Path(hash_row[1])
        if (APP_DIR / stored_path).resolve().is_file():
            return 0

    image_id = get_or_create_image(
        conn,
        path,
        image,
        source_type=source_type,
        source_url=source_url,
        profile_id=profile_id,
    )

    # Если get_or_create_image вернул уже существующую обработанную запись
    # по SHA-256, повторно вычислять лица не нужно.
    existing_processed = conn.execute(
        "SELECT processed FROM images WHERE id = ?",
        (image_id,),
    ).fetchone()

    if existing_processed and existing_processed[0] == 1 and not force:
        return 0

    conn.execute(
        "DELETE FROM faces WHERE image_id = ?",
        (image_id,),
    )

    faces = _detect_faces_with_padding(app, image, padding_ratio=0.40)
    face_count = 0

    for face in faces:
        if face.det_score < DET_THRESHOLD:
            continue

        bbox = face.bbox.astype(np.float32)

        embedding = np.asarray(
            face.normed_embedding,
            dtype=np.float32,
        )

        norm = np.linalg.norm(embedding)
        if norm == 0:
            continue

        embedding /= norm

        conn.execute(
            """
            INSERT INTO faces (
                image_id,
                x1, y1, x2, y2,
                det_score,
                embedding,
                embedding_dim
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                image_id,
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
                float(face.det_score),
                embedding_to_blob(embedding),
                int(embedding.shape[0]),
            ),
        )

        face_count += 1

    conn.execute(
        """
        UPDATE images
        SET processed = 1
        WHERE id = ?
        """,
        (image_id,),
    )

    conn.commit()
    return face_count


# ============================================================
# FAISS / GPU SEARCH INDEX
# ============================================================

_GPU_INDEX_LOCK = threading.RLock()
_GPU_EMBEDDINGS = None
_GPU_FACE_IDS = None
_GPU_INDEX_DIM = 0
_GPU_INDEX_COUNT = 0
_GPU_INDEX_DTYPE = None
_GPU_INDEX_ERROR = None
_GPU_INDEX_WARNING = None
_GPU_INDEX_MODE = "none"  # none | full | streaming
_GPU_STREAM_CPU_INDEX = None  # keeps FAISS CPU index alive for chunked GPU search
_GPU_STREAM_FACE_IDS = None
_GPU_STREAM_DIM = 0
_GPU_STREAM_COUNT = 0

# Кэш метаданных CPU FAISS: количество лиц/размерность и mtime файла.
# Нужен для показа размера индекса даже в CPU-only режиме.
_FAISS_META_LOCK = threading.RLock()
_FAISS_META_PATH = ""
_FAISS_META_MTIME_NS = -1
_FAISS_META_COUNT = 0
_FAISS_META_DIM = 0
_FAISS_META_ERROR = None

# Один кэш CPU-индекса: CPU fallback не должен читать файл на несколько GB
# заново при каждом поиске. Ключ включает путь, mtime и размер.
_CPU_FAISS_CACHE_LOCK = threading.RLock()
_CPU_FAISS_CACHE_INDEX = None
_CPU_FAISS_CACHE_PATH = ""
_CPU_FAISS_CACHE_MTIME_NS = -1
_CPU_FAISS_CACHE_SIZE = -1


def _clear_cpu_faiss_cache() -> None:
    global _CPU_FAISS_CACHE_INDEX, _CPU_FAISS_CACHE_PATH
    global _CPU_FAISS_CACHE_MTIME_NS, _CPU_FAISS_CACHE_SIZE
    with _CPU_FAISS_CACHE_LOCK:
        _CPU_FAISS_CACHE_INDEX = None
        _CPU_FAISS_CACHE_PATH = ""
        _CPU_FAISS_CACHE_MTIME_NS = -1
        _CPU_FAISS_CACHE_SIZE = -1
    gc.collect()


def _set_cpu_faiss_cache(index, index_path_value: str | Path) -> None:
    global _CPU_FAISS_CACHE_INDEX, _CPU_FAISS_CACHE_PATH
    global _CPU_FAISS_CACHE_MTIME_NS, _CPU_FAISS_CACHE_SIZE
    path = Path(index_path_value).expanduser().resolve()
    stat = path.stat()
    with _CPU_FAISS_CACHE_LOCK:
        _CPU_FAISS_CACHE_INDEX = index
        _CPU_FAISS_CACHE_PATH = str(path)
        _CPU_FAISS_CACHE_MTIME_NS = int(stat.st_mtime_ns)
        _CPU_FAISS_CACHE_SIZE = int(stat.st_size)


def _get_cpu_faiss_index(index_path_value: str | Path):
    """Возвращает загруженный индекс из кэша или читает файл ровно один раз."""
    _ensure_runtime_dependencies(need_numpy=True, need_faiss=True)
    path = Path(index_path_value).expanduser().resolve()
    stat = path.stat()
    path_text = str(path)
    mtime_ns = int(stat.st_mtime_ns)
    size_bytes = int(stat.st_size)
    global _CPU_FAISS_CACHE_INDEX, _CPU_FAISS_CACHE_PATH
    global _CPU_FAISS_CACHE_MTIME_NS, _CPU_FAISS_CACHE_SIZE

    with _CPU_FAISS_CACHE_LOCK:
        if (
            _CPU_FAISS_CACHE_INDEX is not None
            and _CPU_FAISS_CACHE_PATH == path_text
            and _CPU_FAISS_CACHE_MTIME_NS == mtime_ns
            and _CPU_FAISS_CACHE_SIZE == size_bytes
        ):
            return _CPU_FAISS_CACHE_INDEX

        # Освобождаем индекс предыдущего профиля ДО чтения нового, иначе при
        # переключении БД в RAM могут одновременно находиться два больших индекса.
        _CPU_FAISS_CACHE_INDEX = None
        _CPU_FAISS_CACHE_PATH = ""
        _CPU_FAISS_CACHE_MTIME_NS = -1
        _CPU_FAISS_CACHE_SIZE = -1
        gc.collect()

        # Держим lock во время read_index: два одновременных поиска не должны
        # загрузить в RAM две копии многогигабайтного файла.
        index = faiss.read_index(path_text)
        try:
            after = path.stat()
            after_mtime = int(after.st_mtime_ns)
            after_size = int(after.st_size)
        except OSError:
            after_mtime, after_size = mtime_ns, size_bytes
        _CPU_FAISS_CACHE_INDEX = index
        _CPU_FAISS_CACHE_PATH = path_text
        _CPU_FAISS_CACHE_MTIME_NS = after_mtime
        _CPU_FAISS_CACHE_SIZE = after_size
        return index


def _clear_gpu_search_index(*, release_cuda_memory: bool = False) -> None:
    global _GPU_EMBEDDINGS
    global _GPU_FACE_IDS
    global _GPU_INDEX_DIM
    global _GPU_INDEX_COUNT
    global _GPU_INDEX_DTYPE
    global _GPU_INDEX_ERROR
    global _GPU_INDEX_WARNING, _GPU_INDEX_MODE
    global _GPU_STREAM_CPU_INDEX, _GPU_STREAM_FACE_IDS, _GPU_STREAM_DIM, _GPU_STREAM_COUNT

    with _GPU_INDEX_LOCK:
        _GPU_EMBEDDINGS = None
        _GPU_FACE_IDS = None
        _GPU_INDEX_DIM = 0
        _GPU_INDEX_COUNT = 0
        _GPU_INDEX_DTYPE = None
        _GPU_INDEX_ERROR = None
        _GPU_INDEX_WARNING = None
        _GPU_INDEX_MODE = "none"
        _GPU_STREAM_CPU_INDEX = None
        _GPU_STREAM_FACE_IDS = None
        _GPU_STREAM_DIM = 0
        _GPU_STREAM_COUNT = 0

    if release_cuda_memory and torch is not None:
        try:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def get_gpu_search_status() -> dict[str, Any]:
    with _GPU_INDEX_LOCK:
        count = int(_GPU_INDEX_COUNT if _GPU_INDEX_MODE == "full" else _GPU_STREAM_COUNT)
        dim = int(_GPU_INDEX_DIM if _GPU_INDEX_MODE == "full" else _GPU_STREAM_DIM)
        dtype = str(_GPU_INDEX_DTYPE) if _GPU_INDEX_DTYPE is not None else ("FP16 chunks" if _GPU_INDEX_MODE == "streaming" else "")
        error = _GPU_INDEX_ERROR
        warning = _GPU_INDEX_WARNING
        mode = _GPU_INDEX_MODE
    available = bool(torch is not None and torch.cuda.is_available())
    return {
        "available": available,
        "loaded": count > 0 and mode in ("full", "streaming"),
        "count": count,
        "dimension": dim,
        "dtype": dtype,
        "error": error,
        "warning": warning,
        "mode": mode,
    }


def _is_gpu_memory_error(error: Any) -> bool:
    """Распознаёт недостаток VRAM даже если backend вернул не наш текст ошибки."""
    value = str(error or "").lower()
    markers = (
        "недостаточно свободной vram",
        "out of memory",
        "cuda out of memory",
        "cuda error: out of memory",
        "cublas_status_alloc_failed",
        "cuda_error_out_of_memory",
        "failed to allocate",
        "memory allocation",
        "resource exhausted",
    )
    return any(marker in value for marker in markers)


def _gpu_index_capacity(count: int, dimension: int) -> tuple[bool, int, int, int]:
    """Возвращает fit, требуемые байты, свободные байты и резерв."""
    if torch is None or not torch.cuda.is_available():
        return False, 0, 0, int(GPU_INDEX_RESERVE_GB * 1024**3)
    free_bytes, _total_bytes = torch.cuda.mem_get_info(torch.device(f"cuda:{GPU_INDEX_DEVICE}"))
    bytes_per_element = 2 if GPU_INDEX_USE_FP16 else 4
    required = int(count * dimension * bytes_per_element * 1.05)
    reserve = int(GPU_INDEX_RESERVE_GB * 1024**3)
    return required <= max(0, int(free_bytes) - reserve), required, int(free_bytes), reserve


def _activate_gpu_streaming_index(cpu_index, face_ids: np.ndarray, dimension: int, count: int, warning: str) -> bool:
    """Оставляет FAISS на CPU RAM, но выполняет поиск cosine similarity на GPU небольшими чанками."""
    global _GPU_EMBEDDINGS, _GPU_FACE_IDS, _GPU_INDEX_DIM, _GPU_INDEX_COUNT
    global _GPU_INDEX_DTYPE, _GPU_INDEX_ERROR, _GPU_INDEX_WARNING, _GPU_INDEX_MODE
    global _GPU_STREAM_CPU_INDEX, _GPU_STREAM_FACE_IDS, _GPU_STREAM_DIM, _GPU_STREAM_COUNT
    if torch is None or not cuda_runtime_available():
        _GPU_INDEX_ERROR = _CUDA_RUNTIME_ERROR or "CUDA недоступна для потокового поиска"
        return False
    try:
        ids = np.ascontiguousarray(face_ids, dtype=np.int64)
        if ids.size != count:
            raise RuntimeError(f"Число face.id ({ids.size}) не совпадает с числом embeddings ({count})")
        with _GPU_INDEX_LOCK:
            _GPU_EMBEDDINGS = None
            _GPU_FACE_IDS = None
            _GPU_INDEX_DIM = 0
            _GPU_INDEX_COUNT = 0
            _GPU_INDEX_DTYPE = torch.float16 if GPU_INDEX_USE_FP16 else torch.float32
            _GPU_INDEX_ERROR = None
            _GPU_INDEX_WARNING = warning
            _GPU_INDEX_MODE = "streaming"
            _GPU_STREAM_CPU_INDEX = cpu_index
            _GPU_STREAM_FACE_IDS = ids
            _GPU_STREAM_DIM = int(dimension)
            _GPU_STREAM_COUNT = int(count)
        print(f"GPU streaming search enabled: {count:,} vectors; index kept in RAM; {warning}")
        return True
    except Exception as exc:
        _GPU_INDEX_ERROR = str(exc)
        return False


def _downgrade_full_gpu_index_to_streaming(
    index_path_value: str | Path | None = None,
    reason: str = "",
) -> bool:
    """Освобождает VRAM от полного FAISS и переводит поиск в GPU-чанки.

    Используется только как восстановление, если после загрузки индекса InsightFace
    не смог создать CUDA-сессии из-за нехватки VRAM. Это сохраняет GPU inference.
    """
    if get_gpu_search_status().get("mode") != "full":
        return get_gpu_search_status().get("mode") == "streaming"

    index_path = Path(index_path_value or INDEX_FILE).expanduser().resolve()
    cpu_index = None
    try:
        if not index_path.is_file():
            return False
        print(
            "VRAM недостаточно для совместной работы InsightFace и полного FAISS. "
            "Переводим FAISS в RAM и GPU-поиск чанками. Причина: " + str(reason)
        )
        cpu_index = _get_cpu_faiss_index(index_path)
        base_index = cpu_index.index if hasattr(cpu_index, "index") else cpu_index
        count = int(base_index.ntotal)
        dimension = int(getattr(base_index, "d", 0) or 0)
        if count <= 0 or not hasattr(cpu_index, "id_map"):
            del cpu_index
            return False
        face_ids = np.asarray(faiss.vector_to_array(cpu_index.id_map), dtype=np.int64)
        if face_ids.shape[0] != count:
            del cpu_index
            return False

        _clear_gpu_search_index(release_cuda_memory=True)
        warning = (
            "Полный FAISS освобождён из VRAM, чтобы сохранить CUDA inference; "
            "поиск выполняется GPU-чанками, индекс хранится в RAM."
        )
        return _activate_gpu_streaming_index(cpu_index, face_ids, dimension, count, warning)
    except Exception as exc:
        print(f"Не удалось переключить FAISS в GPU streaming после OOM: {exc}")
        return False


def _build_gpu_search_index_from_cpu_index(
    cpu_index,
    face_ids: np.ndarray,
    dimension: int,
    count: int,
) -> bool:
    """Переносит FAISS в GPU чанками, не создавая копию всех vectors в RAM."""
    global _GPU_EMBEDDINGS, _GPU_FACE_IDS, _GPU_INDEX_DIM, _GPU_INDEX_COUNT
    global _GPU_INDEX_DTYPE, _GPU_INDEX_ERROR, _GPU_INDEX_WARNING, _GPU_INDEX_MODE
    global _GPU_STREAM_CPU_INDEX, _GPU_STREAM_FACE_IDS, _GPU_STREAM_DIM, _GPU_STREAM_COUNT

    _ensure_runtime_dependencies(need_numpy=True, need_faiss=True, need_ml=True)
    if not cuda_runtime_available():
        _GPU_INDEX_ERROR = _CUDA_RUNTIME_ERROR or "CUDA недоступна для GPU search index"
        return False
    if torch is None or not torch.cuda.is_available():
        _GPU_INDEX_ERROR = "PyTorch CUDA недоступен"
        return False
    if count <= 0 or dimension <= 0:
        _GPU_INDEX_ERROR = "FAISS индекс пуст или имеет неверную размерность"
        return False

    ids = np.ascontiguousarray(face_ids, dtype=np.int64)
    if ids.size != int(count):
        _GPU_INDEX_ERROR = f"Число face.id ({ids.size}) не совпадает с числом embeddings ({count})"
        return False

    base_index = cpu_index.index if hasattr(cpu_index, "index") else cpu_index
    try:
        device = torch.device(f"cuda:{GPU_INDEX_DEVICE}")
        free_bytes, _total_bytes = torch.cuda.mem_get_info(device)
        gpu_dtype = torch.float16 if GPU_INDEX_USE_FP16 else torch.float32
        bytes_per_element = 2 if gpu_dtype == torch.float16 else 4
        required_bytes = int(count * dimension * bytes_per_element * 1.05 + count * 8)
        reserve_bytes = int(GPU_INDEX_RESERVE_GB * 1024**3)
        if required_bytes > max(0, int(free_bytes) - reserve_bytes):
            _GPU_INDEX_ERROR = (
                "Недостаточно свободной VRAM для полного GPU-индекса: "
                f"нужно примерно {required_bytes / 1024**3:.2f} GB, "
                f"свободно {free_bytes / 1024**3:.2f} GB, "
                f"резерв {GPU_INDEX_RESERVE_GB:.2f} GB"
            )
            return False

        # Выделяем итоговое хранилище один раз. Каждый временный CPU/GPU-чанк
        # имеет ограниченный размер независимо от полного размера индекса.
        gpu_vectors = torch.empty((int(count), int(dimension)), device=device, dtype=gpu_dtype)
        chunk_rows = max(1, int(FAISS_GPU_COPY_CHUNK_ROWS))
        for start in range(0, int(count), chunk_rows):
            size = min(chunk_rows, int(count) - start)
            chunk = np.asarray(base_index.reconstruct_n(start, size), dtype=np.float32)
            if chunk.shape != (size, int(dimension)):
                raise RuntimeError(
                    f"FAISS вернул некорректный блок {chunk.shape}; ожидался {(size, int(dimension))}"
                )
            chunk_tensor = torch.from_numpy(np.ascontiguousarray(chunk)).to(
                device=device, dtype=gpu_dtype, non_blocking=False
            )
            gpu_vectors[start:start + size].copy_(chunk_tensor)
            del chunk_tensor, chunk

        gpu_face_ids = torch.from_numpy(ids).to(device=device, dtype=torch.int64)
        torch.cuda.synchronize(device)

        with _GPU_INDEX_LOCK:
            _GPU_EMBEDDINGS = gpu_vectors
            _GPU_FACE_IDS = gpu_face_ids
            _GPU_INDEX_DIM = int(dimension)
            _GPU_INDEX_COUNT = int(count)
            _GPU_INDEX_DTYPE = gpu_dtype
            _GPU_INDEX_ERROR = None
            _GPU_INDEX_WARNING = None
            _GPU_INDEX_MODE = "full"
            _GPU_STREAM_CPU_INDEX = None
            _GPU_STREAM_FACE_IDS = None
            _GPU_STREAM_DIM = 0
            _GPU_STREAM_COUNT = 0

        print(
            f"GPU search index loaded in chunks: {count:,} faces, "
            f"dtype={gpu_dtype}, VRAM~{required_bytes / 1024**3:.2f} GB"
        )
        return True
    except Exception as exc:
        error_text = str(exc)
        _GPU_INDEX_ERROR = error_text
        print(f"GPU search index chunk-load error: {error_text}")
        _clear_gpu_search_index(release_cuda_memory=True)
        _GPU_INDEX_ERROR = error_text
        return False


def _load_gpu_search_index_from_faiss(
    index_path_value: str | Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """Загружает указанный FAISS в память и переносит embeddings в VRAM.

    Читателя вызываем только в worker-потоке. FAISS не предоставляет progress
    callback для read_index/reconstruct_n, поэтому на этих этапах показываем
    индикатор неопределённого прогресса с текстом текущей операции.
    """
    _ensure_runtime_dependencies(need_numpy=True, need_faiss=True)
    index_path = Path(index_path_value or INDEX_FILE).resolve()

    def report(message: str) -> None:
        if progress_callback:
            try:
                progress_callback(message)
            except Exception:
                pass

    if index_path.name == "faces.faiss":
        report("Пропуск устаревшего общего индекса faces.faiss")
        print(
            "Пропуск legacy FAISS faces.faiss: "
            "для активной БД требуется отдельный индекс в indexes/."
        )
        return False

    try:
        file_stat = index_path.stat()
    except OSError:
        report("FAISS-файл отсутствует")
        return False

    size_gb = float(file_stat.st_size) / (1024 ** 3)
    report(f"Чтение FAISS с диска: {index_path.name} • {size_gb:.2f} GB")

    try:
        # Используем переданный путь, а не глобальный INDEX_FILE: профиль БД
        # мог измениться, пока worker уже запускался.
        cpu_index = _get_cpu_faiss_index(index_path)
        base_index = cpu_index.index if hasattr(cpu_index, "index") else cpu_index
        ntotal = int(base_index.ntotal)
        dimension = int(getattr(base_index, "d", 0) or 0)

        # Сохраняем метаданные из уже загруженного объекта, чтобы статистика GUI
        # не читала несколько гигабайтный FAISS-файл повторно.
        try:
            current_stat = index_path.stat()
            with _FAISS_META_LOCK:
                global _FAISS_META_PATH, _FAISS_META_MTIME_NS
                global _FAISS_META_COUNT, _FAISS_META_DIM, _FAISS_META_ERROR
                _FAISS_META_PATH = str(index_path)
                _FAISS_META_MTIME_NS = int(current_stat.st_mtime_ns)
                _FAISS_META_COUNT = ntotal
                _FAISS_META_DIM = dimension
                _FAISS_META_ERROR = None
        except OSError:
            pass

        if ntotal <= 0:
            report("FAISS-файл прочитан, embeddings отсутствуют")
            del cpu_index
            return False

        if hasattr(cpu_index, "id_map"):
            face_ids = np.asarray(faiss.vector_to_array(cpu_index.id_map), dtype=np.int64)
            if face_ids.shape[0] != ntotal:
                raise RuntimeError(
                    "Повреждён FAISS индекс: число ID не совпадает "
                    f"с числом embedding ({face_ids.shape[0]} != {ntotal})."
                )
        else:
            raise RuntimeError(
                "FAISS индекс не содержит явной карты face.id "
                "(ожидался IndexIDMap). Создайте индекс заново."
            )

        fits, required_bytes, free_bytes, reserve_bytes = _gpu_index_capacity(ntotal, dimension)
        if not fits:
            warning = (
                "Полный индекс не помещается в VRAM: "
                f"нужно ~{required_bytes / 1024**3:.2f} GB, свободно {free_bytes / 1024**3:.2f} GB, "
                f"резерв {reserve_bytes / 1024**3:.2f} GB. Используется потоковый GPU-поиск."
            )
            report(warning)
            ok = _activate_gpu_streaming_index(cpu_index, face_ids, dimension, ntotal, warning)
            if ok:
                _set_cpu_faiss_cache(cpu_index, index_path)
                with _FAISS_META_LOCK:
                    _FAISS_META_PATH = str(index_path)
                    _FAISS_META_MTIME_NS = int(file_stat.st_mtime_ns)
                    _FAISS_META_COUNT = ntotal
                    _FAISS_META_DIM = dimension
                    _FAISS_META_ERROR = None
                report("GPU inference сохранён; поиск выполняется на GPU блоками, полный индекс остаётся в RAM")
                return True
            del cpu_index
            del face_ids
            gc.collect()
            report("Потоковый GPU-поиск недоступен; поиск будет использовать CPU FAISS")
            return False

        report(
            f"FAISS прочитан: {ntotal:,} embeddings • dim={dimension}; "
            "проверка VRAM и подготовка копирования блоками..."
        )
        ok = _build_gpu_search_index_from_cpu_index(cpu_index, face_ids, dimension, ntotal)
        if not ok and cuda_runtime_available() and _is_gpu_memory_error(_GPU_INDEX_ERROR):
            warning = str(_GPU_INDEX_ERROR or "VRAM закончилась при размещении полного индекса")
            ok = _activate_gpu_streaming_index(cpu_index, face_ids, dimension, ntotal, warning)
            if ok:
                _set_cpu_faiss_cache(cpu_index, index_path)
                report("Полный индекс не поместился; включён потоковый GPU-поиск")
        del face_ids
        if ok and get_gpu_search_status().get("mode") == "full":
            _clear_cpu_faiss_cache()
            del cpu_index
        elif not ok:
            del cpu_index
        gc.collect()
        report(
            "Полный GPU-индекс загружен в VRAM" if ok and get_gpu_search_status().get("mode") == "full"
            else "Включён потоковый GPU-поиск" if ok
            else "GPU-поиск не активирован; будет CPU fallback"
        )
        return ok

    except Exception as exc:
        error_text = str(exc)
        print(f"Не удалось загрузить GPU search index: {error_text}")
        report(f"Ошибка загрузки FAISS: {error_text}")
        return False


def get_faiss_index_file_status(
    index_path_value: str | Path | None = None,
    *,
    read_metadata: bool = True,
) -> dict[str, Any]:
    """Возвращает размер и (по запросу) метаданные FAISS.

    В GUI-статистике read_metadata=False: большой файл только stat()-ится,
    а не загружается целиком второй раз. Метаданные будут получены во время
    основной загрузки индекса либо при явно запрошенном read_metadata=True.
    """
    global _FAISS_META_PATH
    global _FAISS_META_MTIME_NS
    global _FAISS_META_COUNT
    global _FAISS_META_DIM
    global _FAISS_META_ERROR

    index_path = Path(index_path_value or INDEX_FILE).resolve()

    try:
        stat = index_path.stat()
    except (OSError, ValueError) as exc:
        if read_metadata:
            with _FAISS_META_LOCK:
                _FAISS_META_PATH = str(index_path)
                _FAISS_META_MTIME_NS = -1
                _FAISS_META_COUNT = 0
                _FAISS_META_DIM = 0
                _FAISS_META_ERROR = str(exc)
        return {
            "exists": False,
            "count": 0,
            "dimension": 0,
            "error": str(exc),
            "size_bytes": 0,
            "metadata_pending": False,
        }

    path_str = str(index_path)
    mtime_ns = int(stat.st_mtime_ns)
    size_bytes = int(stat.st_size)

    with _FAISS_META_LOCK:
        cache_matches = (
            _FAISS_META_PATH == path_str
            and _FAISS_META_MTIME_NS == mtime_ns
        )
        if cache_matches:
            return {
                "exists": True,
                "count": int(_FAISS_META_COUNT),
                "dimension": int(_FAISS_META_DIM),
                "error": _FAISS_META_ERROR,
                "size_bytes": size_bytes,
                "metadata_pending": False,
            }

    if not read_metadata:
        return {
            "exists": True,
            "count": 0,
            "dimension": 0,
            "error": None,
            "size_bytes": size_bytes,
            "metadata_pending": True,
        }

    try:
        _ensure_runtime_dependencies(need_numpy=True, need_faiss=True)
        cached_index = None
        with _CPU_FAISS_CACHE_LOCK:
            if (
                _CPU_FAISS_CACHE_INDEX is not None
                and _CPU_FAISS_CACHE_PATH == path_str
                and _CPU_FAISS_CACHE_MTIME_NS == mtime_ns
                and _CPU_FAISS_CACHE_SIZE == size_bytes
            ):
                cached_index = _CPU_FAISS_CACHE_INDEX
        index = cached_index if cached_index is not None else faiss.read_index(str(index_path))
        base_index = index.index if hasattr(index, "index") else index
        count = int(base_index.ntotal)
        dimension = int(base_index.d)

        with _FAISS_META_LOCK:
            _FAISS_META_PATH = path_str
            _FAISS_META_MTIME_NS = mtime_ns
            _FAISS_META_COUNT = count
            _FAISS_META_DIM = dimension
            _FAISS_META_ERROR = None

        if cached_index is None:
            del index
        return {
            "exists": True,
            "count": count,
            "dimension": dimension,
            "error": None,
            "size_bytes": size_bytes,
            "metadata_pending": False,
        }

    except Exception as exc:
        error_text = str(exc)
        with _FAISS_META_LOCK:
            _FAISS_META_PATH = path_str
            _FAISS_META_MTIME_NS = mtime_ns
            _FAISS_META_COUNT = 0
            _FAISS_META_DIM = 0
            _FAISS_META_ERROR = error_text

        return {
            "exists": True,
            "count": 0,
            "dimension": 0,
            "error": error_text,
            "size_bytes": size_bytes,
            "metadata_pending": False,
        }


def build_faiss_index(
    conn: DatabaseConnection,
    *,
    use_gpu_index: bool = True,
):
    """Создаёт индекс пакетно: память не расходуется на fetchall() всех embeddings."""
    _ensure_runtime_dependencies(need_numpy=True, need_faiss=True)
    removed = cleanup_missing_images(conn)
    if removed:
        print(f"Удалено записей отсутствующих файлов: {removed}")

    # Убираем прежний resident-index ДО сборки нового: иначе при обновлении
    # многогигабайтной БД старый CPU/GPU индекс и новый индекс занимали бы память одновременно.
    _clear_gpu_search_index(release_cuda_memory=True)
    _clear_cpu_faiss_cache()

    total_expected = int(conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] or 0)
    if total_expected <= 0:
        print("В базе нет лиц.")
        return

    index_path = Path(INDEX_FILE).expanduser().resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = index_path.with_name(index_path.name + f".{os.getpid()}.building")

    print(
        f"Создание CPU FAISS индекса пакетами по {FAISS_BUILD_BATCH_ROWS:,} embeddings "
        f"(в базе ожидается {total_expected:,} лиц)..."
    )
    stream_execute = getattr(conn, "execute_streaming", conn.execute)
    cursor = stream_execute("""
        SELECT id, embedding, embedding_dim
        FROM faces
        ORDER BY id
    """)

    try:
        first_batch = cursor.fetchmany(FAISS_BUILD_BATCH_ROWS)
    except Exception:
        cursor.close()
        raise
    if not first_batch:
        cursor.close()
        print("В базе больше нет лиц для индекса.")
        return

    dimension = int(first_batch[0][2])
    if dimension <= 0:
        cursor.close()
        raise RuntimeError(f"Некорректная размерность embedding: {dimension}")

    cpu_index = faiss.IndexIDMap(faiss.IndexFlatIP(dimension))
    face_ids = np.empty(total_expected, dtype=np.int64)
    processed = 0
    batch = first_batch

    try:
        while batch:
            batch_count = len(batch)
            vectors = np.empty((batch_count, dimension), dtype=np.float32)
            ids = np.empty(batch_count, dtype=np.int64)
            for i, (face_id, blob, row_dimension) in enumerate(batch):
                row_dimension = int(row_dimension)
                if row_dimension != dimension:
                    raise RuntimeError(
                        "В базе обнаружены embedding разной размерности: "
                        f"ожидалась {dimension}, получена {row_dimension} у face_id={face_id}"
                    )
                vectors[i] = blob_to_embedding(blob, dimension)
                ids[i] = int(face_id)

            required = processed + batch_count
            if required > face_ids.size:
                expanded = np.empty(max(required, max(1, face_ids.size * 2)), dtype=np.int64)
                expanded[:processed] = face_ids[:processed]
                face_ids = expanded
            cpu_index.add_with_ids(vectors, ids)
            face_ids[processed:required] = ids
            processed = required
            del vectors, ids, batch

            if processed % (FAISS_BUILD_BATCH_ROWS * 10) < FAISS_BUILD_BATCH_ROWS:
                print(f"FAISS: добавлено {processed:,} / ~{total_expected:,} лиц")
            batch = cursor.fetchmany(FAISS_BUILD_BATCH_ROWS)

        cursor.close()
        if processed <= 0:
            raise RuntimeError("Не удалось прочитать embeddings для FAISS")
        face_ids = np.ascontiguousarray(face_ids[:processed], dtype=np.int64)

        print(f"Запись временного FAISS-файла: {temp_path.name}")
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        faiss.write_index(cpu_index, str(temp_path))
        # Атомарная замена не повреждает предыдущий рабочий индекс при ошибке записи.
        os.replace(temp_path, index_path)

        stat = index_path.stat()
        with _FAISS_META_LOCK:
            global _FAISS_META_PATH, _FAISS_META_MTIME_NS
            global _FAISS_META_COUNT, _FAISS_META_DIM, _FAISS_META_ERROR
            _FAISS_META_PATH = str(index_path)
            _FAISS_META_MTIME_NS = int(stat.st_mtime_ns)
            _FAISS_META_COUNT = int(processed)
            _FAISS_META_DIM = int(dimension)
            _FAISS_META_ERROR = None

        # Индекс-файл обновился; не используем кэш от предыдущей версии.
        _clear_cpu_faiss_cache()
        print(f"CPU FAISS индекс создан: {processed:,} лиц")
        print(f"Размер embedding: {dimension}")
        print(f"Файл: {index_path} ({stat.st_size / (1024 ** 3):.2f} GB)")

        if use_gpu_index:
            gpu_ok = _build_gpu_search_index_from_cpu_index(
                cpu_index, face_ids, dimension, processed
            )
            if not gpu_ok and cuda_runtime_available() and _is_gpu_memory_error(_GPU_INDEX_ERROR):
                warning = str(_GPU_INDEX_ERROR or "VRAM закончилась при размещении полного индекса")
                gpu_ok = _activate_gpu_streaming_index(
                    cpu_index, face_ids, dimension, processed, warning
                )
                if gpu_ok:
                    _set_cpu_faiss_cache(cpu_index, index_path)
            if not gpu_ok:
                print(
                    "GPU search не активирован; поиск использует CPU FAISS, "
                    "но InsightFace может продолжать работать на GPU."
                )
                _set_cpu_faiss_cache(cpu_index, index_path)
            elif get_gpu_search_status().get("mode") == "full":
                _clear_cpu_faiss_cache()
        else:
            _clear_gpu_search_index(release_cuda_memory=True)
            _set_cpu_faiss_cache(cpu_index, index_path)
            print("GPU search отключён: выбран режим CPU-only.")

        gc.collect()
    except Exception:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ============================================================
# SEARCH
# ============================================================

# Результаты FAISS -> строки БД вынесены в отдельный модуль.
from facesearch.indexing.result_mapping import (
    _RESULT_SELECT_SQL,
    _fetch_result_rows_by_face_ids,
    _resolve_ranked_results,
    _result_row,
    _row_to_result,
)




def _search_embedding_gpu_streaming(
    conn: DatabaseConnection,
    embedding: np.ndarray,
    top_k: int,
    min_similarity: float,
):
    """Точный полный поиск по всем embeddings на GPU чанками, когда индекс не влезает в VRAM."""
    _ensure_runtime_dependencies(need_numpy=True, need_ml=True)
    if torch is None or not torch.cuda.is_available():
        return None
    with _GPU_INDEX_LOCK:
        cpu_index = _GPU_STREAM_CPU_INDEX
        face_ids = _GPU_STREAM_FACE_IDS
        count = int(_GPU_STREAM_COUNT)
        dimension = int(_GPU_STREAM_DIM)
        gpu_dtype = _GPU_INDEX_DTYPE or (torch.float16 if GPU_INDEX_USE_FP16 else torch.float32)
    if cpu_index is None or face_ids is None or count <= 0:
        return None

    query = np.asarray(embedding, dtype=np.float32).copy()
    norm = float(np.linalg.norm(query))
    if norm <= 0:
        raise ValueError("Embedding выбранного лица имеет нулевую норму.")
    query /= norm

    device = torch.device(f"cuda:{GPU_INDEX_DEVICE}")
    free_bytes, _total = torch.cuda.mem_get_info(device)
    bytes_per = 2 if gpu_dtype == torch.float16 else 4
    safe_bytes = max(32 * 1024**2, int(free_bytes) - int(GPU_INDEX_RESERVE_GB * 1024**3))
    chunk_rows = max(1024, min(65536, safe_bytes // max(1, dimension * bytes_per * 4)))
    base_index = cpu_index.index if hasattr(cpu_index, "index") else cpu_index
    query_gpu = torch.from_numpy(query).to(device=device, dtype=gpu_dtype).reshape(1, -1)
    best_scores = np.empty((0,), dtype=np.float32)
    best_ids = np.empty((0,), dtype=np.int64)
    k = min(max(1, int(top_k)), count)

    with torch.inference_mode():
        for start in range(0, count, int(chunk_rows)):
            size = min(int(chunk_rows), count - start)
            chunk = np.asarray(base_index.reconstruct_n(start, size), dtype=np.float32)
            chunk_tensor = torch.from_numpy(np.ascontiguousarray(chunk)).to(device=device, dtype=gpu_dtype)
            scores = torch.mm(query_gpu, chunk_tensor.transpose(0, 1))[0]
            local_k = min(k, size)
            values, indices = torch.topk(scores, k=local_k, largest=True, sorted=False)
            values_cpu = values.float().cpu().numpy()
            positions = indices.cpu().numpy().astype(np.int64, copy=False) + start
            ids_cpu = face_ids[positions]
            combined_scores = np.concatenate((best_scores, values_cpu))
            combined_ids = np.concatenate((best_ids, ids_cpu))
            keep = min(k, combined_scores.size)
            if combined_scores.size > keep:
                selected = np.argpartition(combined_scores, -keep)[-keep:]
                combined_scores = combined_scores[selected]
                combined_ids = combined_ids[selected]
            best_scores, best_ids = combined_scores, combined_ids
            del chunk, chunk_tensor, scores, values, indices

    order = np.argsort(best_scores)[::-1]
    ranked_matches = [(float(best_scores[idx]), int(best_ids[idx])) for idx in order]
    results, _stale_ids = _resolve_ranked_results(conn, ranked_matches, min_similarity)
    return results


def _search_embedding_gpu(
    conn: DatabaseConnection,
    embedding: np.ndarray,
    top_k: int,
    min_similarity: float,
):
    _ensure_runtime_dependencies(need_numpy=True, need_ml=True)
    if torch is None or not torch.cuda.is_available():
        return None

    with _GPU_INDEX_LOCK:
        gpu_vectors = _GPU_EMBEDDINGS
        gpu_face_ids = _GPU_FACE_IDS
        count = _GPU_INDEX_COUNT
        gpu_dtype = _GPU_INDEX_DTYPE
        streaming = _GPU_INDEX_MODE == "streaming" and _GPU_STREAM_CPU_INDEX is not None

    if streaming:
        try:
            return _search_embedding_gpu_streaming(conn, embedding, top_k, min_similarity)
        except Exception as exc:
            print(f"GPU streaming search failed, CPU FAISS fallback: {exc}")
            return None

    if gpu_vectors is None or gpu_face_ids is None or count <= 0:
        return None

    try:
        query = np.asarray(embedding, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm == 0:
            raise ValueError("Embedding выбранного лица имеет нулевую норму.")
        query /= norm

        # FP16-index для RTX 3060: меньше VRAM и меньше traffic при GEMM.
        query_gpu = torch.from_numpy(query).to(
            device=gpu_vectors.device,
            dtype=gpu_dtype,
        ).reshape(1, -1)

        with torch.inference_mode():
            # 1 x D @ D x N. Никакого CPU-копирования N embeddings.
            scores = torch.mm(query_gpu, gpu_vectors.transpose(0, 1))
            k = min(max(1, int(top_k)), count)
            values, indices = torch.topk(
                scores[0],
                k=k,
                largest=True,
                sorted=True,
            )

            values_cpu = values.float().cpu().numpy()
            ids_cpu = gpu_face_ids[indices].cpu().numpy().astype(np.int64, copy=False)

        ranked_matches = [(float(score), int(face_id)) for score, face_id in zip(values_cpu, ids_cpu)]
        results, _stale_ids = _resolve_ranked_results(conn, ranked_matches, min_similarity)
        return results

    except Exception as exc:
        # GPU search не должен ломать приложение: при любой CUDA/Torch
        # проблеме возвращаем управление CPU fallback.
        print(f"GPU поиск недоступен, CPU fallback: {exc}")
        return None


def _search_embedding_cpu(
    conn: DatabaseConnection,
    embedding: np.ndarray,
    top_k: int,
    min_similarity: float,
):
    _ensure_runtime_dependencies(need_numpy=True, need_faiss=True)
    current_index_path = Path(INDEX_FILE).resolve()

    if not current_index_path.exists():
        raise FileNotFoundError(
            f"FAISS индекс отсутствует: {current_index_path}\n"
            "Сначала выполните сканирование и создание индекса для активной БД."
        )

    query = np.asarray(embedding, dtype=np.float32)
    norm = np.linalg.norm(query)

    if norm == 0:
        raise ValueError("Embedding выбранного лица имеет нулевую норму.")

    query = (query / norm).reshape(1, -1)

    index = _get_cpu_faiss_index(current_index_path)
    scores, ids = index.search(query, top_k)

    ranked_matches = [(float(score), int(face_id)) for score, face_id in zip(scores[0], ids[0])]
    results, stale_ids = _resolve_ranked_results(conn, ranked_matches, min_similarity)

    if not results and stale_ids:
        raise RuntimeError(
            "FAISS индекс не соответствует текущей БД: "
            f"получены устаревшие face.id ({stale_ids}). "
            "Пересоздайте FAISS индекс для активного профиля БД."
        )

    return results


def search_embedding(
    conn: DatabaseConnection,
    embedding: np.ndarray,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = 0.0,
    backend: str = BACKEND_GPU,
):
    """Поиск похожих лиц с явным выбором GPU/CPU backend.

    GPU: пытаемся использовать постоянный GPU-тензор; при любой CUDA/Torch
    ошибке автоматически откатываемся на CPU FAISS.
    CPU: GPU вообще не используется.
    """
    if backend == BACKEND_CPU:
        return _search_embedding_cpu(
            conn,
            embedding,
            top_k,
            min_similarity,
        )

    gpu_results = _search_embedding_gpu(
        conn,
        embedding,
        top_k,
        min_similarity,
    )
    if gpu_results is not None:
        return gpu_results

    return _search_embedding_cpu(
        conn,
        embedding,
        top_k,
        min_similarity,
    )


def search_face(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    image_path: Path,
    top_k: int = DEFAULT_TOP_K,
):
    image = load_image(image_path)
    if image is None:
        raise RuntimeError(f"Не удалось открыть: {image_path}")

    faces = _detect_faces_with_padding(app, image, padding_ratio=0.40)

    if not faces:
        print("На изображении лица не найдены.")
        return

    for query_number, face in enumerate(faces, 1):
        if face.det_score < DET_THRESHOLD:
            continue

        embedding = np.asarray(
            face.normed_embedding,
            dtype=np.float32,
        )

        results = search_embedding(
            conn,
            embedding,
            top_k,
        )

        output_file = (
            f"face_search_results_{query_number}.html"
        )

        html_file = generate_html_gallery(
            results=results,
            query_image=image_path,
            output_file=output_file,
        )

        print(f"HTML: {html_file}")

        try:
            webbrowser.open(html_file.as_uri())
        except Exception:
            pass


# ============================================================
# HTML
# ============================================================













def generate_html_gallery(
    results,
    query_image: Path,
    output_file: str = "face_search_results.html",
    use_local_crops: bool = True,
    theme: str = "dark",
    retry_missing_crops: bool = False,
):
    """Создаёт HTML; при необходимости браузер повторно загружает создаваемые CROP."""
    return _generate_html_gallery_impl(
        results,
        query_image,
        output_file=output_file,
        use_local_crops=use_local_crops,
        crop_dir=CROP_DIR,
        crop_url=CROP_URL,
        app_dir=APP_DIR,
        load_image_fn=load_image,
        cv2_module=cv2,
        theme=theme,
        retry_missing_crops=retry_missing_crops,
    )


# ============================================================
# GUI
# ============================================================


class FaceSearchGUI:
    def __init__(self, root):
        self.root = root
        self._set_window_icon()

        self.gui_settings = _load_gui_settings()
        saved_theme = str(self.gui_settings.get("theme") or "light").strip().lower()
        self.theme_mode = "dark" if saved_theme == "dark" else "light"
        self._settings_ready = False
        self._settings_save_after = None

        self.root.title("Face Search — InsightFace CUDA + GPU Search")
        self.root.geometry(
            str(self.gui_settings.get("window_geometry") or "1250x900")
        )
        self.root.minsize(1000, 700)

        if bool(self.gui_settings.get("window_maximized", True)):
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass

        self.events = queue.Queue()
        self.app = None

        saved_folder = str(self.gui_settings.get("selected_folder") or "").strip()
        self.selected_dir = Path(saved_folder) if saved_folder else None
        saved_query = str(self.gui_settings.get("query_image") or "").strip()
        self.query_source_url = saved_query if urlparse(saved_query).scheme.lower() in ("http", "https") else ""
        self.query_path = Path(saved_query) if saved_query and not self.query_source_url else None
        self.query_image = None
        self.query_faces = []
        self.selected_face_index = None
        self.preview_photo = None
        self.preview_scale = 1.0
        self.preview_offset = (0, 0)
        self.busy = False
        self.operation = None

        self.scan_paused = threading.Event()
        self.scan_paused.set()
        self.scan_stop = threading.Event()
        self.scan_running = False
        self.auto_workers_controller = None
        self.auto_workers_status_var = tk.StringVar(value="")
        self._gpu_telemetry_stop = threading.Event()

        # CUDA проверяется параллельно с запуском окна: никакого cuda_runtime_available()
        # в конструкторе или в периодическом UI-таймере.
        self.cuda_available: bool | None = None
        self.cuda_check_done = False
        self.startup_db_ready = False
        self.startup_db_failed = False
        self.startup_db_pending = True
        self.startup_tasks_started = False
        self.startup_index_started = False
        self.startup_stats_pending = False
        # Первичная статистика завершена: последующие фоновые обновления
        # не должны повторно запускать стартовые индикаторы.
        self.startup_stats_initial_done = False
        self._stats_refresh_pending = False
        self._stats_refresh_profile = ""

        saved_backend = str(self.gui_settings.get("backend") or "").strip().lower()
        if saved_backend not in (BACKEND_GPU, BACKEND_CPU):
            saved_backend = BACKEND_GPU
        self.backend_mode_var = tk.StringVar(value=saved_backend)
        # Простая строка для worker-потоков: им не следует вызывать Tk StringVar.get().
        self._backend_runtime = saved_backend
        self.backend_status_var = tk.StringVar(value="Проверка CUDA/ONNX Runtime в фоне...")
        self.cpu_status_var = tk.StringVar(value="CPU: проверка...")
        self.gpu_status_var = tk.StringVar(value="GPU: проверка CUDA...")
        # Скорость сканирования выводится только в auto_workers_status_label.
        self.scan_rate_var = tk.StringVar(value="")

        self.db_profiles, self.active_db_name = _load_db_profiles()
        saved_db_profile = str(self.gui_settings.get("selected_db_profile") or "").strip()
        if saved_db_profile and any(
            str(p.get("name")) == saved_db_profile for p in self.db_profiles
        ):
            self.active_db_name = saved_db_profile

        startup_profile = next(
            (p for p in self.db_profiles if str(p.get("name")) == self.active_db_name),
            None,
        )
        if startup_profile is None:
            startup_profile = _default_profile()
            self.db_profiles = [startup_profile]
            self.active_db_name = str(startup_profile.get("name"))

        startup_profile = _normalize_profile_storage_paths(dict(startup_profile))
        for i, item in enumerate(self.db_profiles):
            if str(item.get("name")) == str(startup_profile.get("name")):
                self.db_profiles[i] = startup_profile
                break
        _save_db_profiles(self.db_profiles, self.active_db_name)
        self.startup_profile = dict(startup_profile)
        self.db_profile_var = tk.StringVar(value=self.active_db_name)

        # Долгая подготовка схемы, maintenance и первоначальное соединение выполняются
        # в startup worker; готовое соединение передаётся в UI после завершения worker-а.
        self.conn = None
        self.connected_db_name = ""
        self.gpu_index_loading = False

        self._build_ui()
        self._apply_theme()
        # Применяем сохранённое состояние элементов сразу после создания формы.
        # В частности, Spinbox потоков должен быть заблокирован уже при запуске,
        # если в settings.xml включена автоподстройка.
        self._update_worker_control_state()
        self._install_settings_traces()
        self._settings_ready = True
        self._update_backend_ui()  # не проверяет CUDA, использует пока неизвестный cached state
        self.root.bind("<Configure>", self._on_window_configure, add="+")
        # Нижняя строка статуса должна переноситься по реальной ширине окна,
        # а не по фиксированным 1050 px: иначе длинный счётчик/имя файла обрезается.
        self._update_status_wraplength()
        # Sash можно восстановить только после расчёта реальной ширины окна.
        self.paned.bind("<ButtonRelease-1>", self._on_paned_sash_release, add="+")
        self.root.after(100, self._restore_paned_sash_position)

        self.status_var.set("Запуск: подготовка базы данных и проверка CUDA...")
        self.stats_var.set("    Подключение к БД и подсчёт статистики.\n    Дождитесь завершения...")
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        self._show_startup_progress("Подготовка базы данных и проверка CUDA...")

        self.root.after(50, self._poll_events)
        self.root.after(100, self._start_startup_tasks)
        self.root.after(500, self._refresh_hardware_indicators)
        threading.Thread(
            target=_gpu_telemetry_worker,
            args=(self._gpu_telemetry_stop,),
            daemon=True,
            name="gpu-telemetry",
        ).start()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _show_startup_progress(self, message: str):
        """Показывает анимацию только пока действительно выполняется этап запуска."""
        try:
            if not hasattr(self, "startup_progress_frame"):
                return
            if not self.startup_progress_frame.winfo_manager():
                self.startup_progress_frame.pack(fill="x", pady=(3, 0))
            self.startup_progress_label_var.set(str(message or "Выполняется операция..."))
            # start() не вызываем повторно, если индикатор уже активен:
            # очередные сообщения worker не должны перезапускать анимацию.
            if not getattr(self, "_startup_progress_running", False):
                self.startup_progress.start(12)
                self._startup_progress_running = True
        except (tk.TclError, AttributeError):
            pass

    def _hide_startup_progress(self):
        """Останавливает индикатор и полностью убирает его из верхней панели."""
        try:
            if hasattr(self, "startup_progress"):
                self.startup_progress.stop()
            self._startup_progress_running = False
            if hasattr(self, "startup_progress_frame"):
                self.startup_progress_frame.pack_forget()
        except (tk.TclError, AttributeError):
            pass

    def _start_startup_tasks(self):
        if self.startup_tasks_started:
            return
        self.startup_tasks_started = True
        profile = dict(self.startup_profile)
        threading.Thread(
            target=self._startup_database_worker,
            args=(profile,),
            daemon=True,
            name="startup-database-init",
        ).start()
        threading.Thread(
            target=self._startup_cuda_worker,
            daemon=True,
            name="startup-cuda-check",
        ).start()

    def _startup_database_worker(self, profile: dict[str, Any]):
        def report(message: str):
            self.events.put(("startup_db_progress", str(message)))

        conn = None
        try:
            report("Подключение к БД и проверка таблиц...")
            conn = create_database_for_profile(
                profile,
                maintenance=True,
                create_if_missing=True,
                activate_storage=True,
                initialize=True,
                progress_callback=report,
                allow_thread_handoff=True,
            )
            report("Инициализация БД завершена")
            # Передаём уже подготовленное соединение в GUI. Worker больше не
            # использует его, поэтому повторное подключение к удалённой СУБД
            # не блокирует UI ещё на один сетевой handshake.
            ready_conn = conn
            conn = None
            self.events.put(("startup_db_ready", str(profile.get("name")), ready_conn))
        except Exception as exc:
            self.events.put(("startup_db_error", str(profile.get("name")), str(exc)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _startup_cuda_worker(self):
        try:
            available = bool(cuda_runtime_available())
            detail = "" if available else (_CUDA_RUNTIME_ERROR or _cuda_version_diagnostic())
        except Exception as exc:
            available = False
            detail = str(exc)
        self.events.put(("startup_cuda_done", available, detail))

    def _finish_startup_progress_if_ready(self):
        """Завершает стартовую анимацию только после отображения статистики БД."""
        done = self.startup_db_ready and self.cuda_check_done
        failed = self.startup_db_failed

        if failed:
            # При ошибке подключения статистику уже не дождёмся.
            try:
                self.progress.stop()
                self.progress.config(mode="determinate", value=0)
            except tk.TclError:
                pass

            self._hide_startup_progress()
            return

        if not done:
            return

        # Ждём только первичный запрос статистики. Последующие фоновые
        # обновления (например, после завершения загрузки FAISS) не должны
        # повторно включать стартовые progress bar-ы.
        if self.startup_stats_pending and not self.startup_stats_initial_done:
            current_message = ""
            try:
                current_message = str(self.startup_progress_label_var.get() or "")
            except (tk.TclError, AttributeError):
                pass

            # Если параллельно уже загружается FAISS, не затираем его реальный
            # статус сообщением о статистике.
            if self.gpu_index_loading:
                if (
                    not self.startup_progress_frame.winfo_manager()
                    or current_message.startswith("Получение и отображение статистики БД")
                ):
                    self._show_startup_progress("Загрузка FAISS продолжается...")
            else:
                self._show_startup_progress(
                    "Получение и отображение статистики БД..."
                )
            return

        # Первичная статистика получена и применена к интерфейсу. Повторный
        # запрос может оставаться в фоне, но стартовый индикатор уже завершён.
        try:
            self.progress.stop()
            self.progress.config(mode="determinate", value=0)
        except tk.TclError:
            pass

        # Единственная длительная стартовая операция после БД/CUDA — загрузка FAISS.
        # Оставляем движение только если worker действительно выполняется.
        if self.gpu_index_loading:
            if not self.startup_progress_frame.winfo_manager():
                self._show_startup_progress("Загрузка FAISS продолжается...")
            return

        gpu_status = get_gpu_search_status()
        index_ready = bool(gpu_status.get("loaded") or gpu_status.get("mode") == "streaming")
        index_exists = Path(INDEX_FILE).is_file()
        index_expected = (
            self.backend_mode_var.get() == BACKEND_GPU
            and self.cuda_available is True
            and index_exists
        )

        if index_expected and not index_ready and self.startup_index_started:
            # Стартовая загрузка уже запускалась, но активного worker нет.
            # Считаем её завершённой/прерванной, а не показываем бесконечный spinner.
            self.status_var.set(
                "БД готова. Загрузка FAISS не активна; GPU-поиск можно запустить отдельно."
            )
        elif index_ready:
            self.status_var.set("Запуск завершён. Можно работать с программой.")
        elif index_expected:
            self.status_var.set(
                "БД готова. FAISS пока не загружен; сканирование GPU доступно независимо."
            )
        else:
            self.status_var.set("Запуск завершён. Можно работать с программой.")

        self._hide_startup_progress()

    def _maybe_start_gpu_index_load(self):
        if not self.startup_db_ready or not self.cuda_check_done or self.conn is None:
            return
        if self.backend_mode_var.get() != BACKEND_GPU or not self.cuda_available:
            self._finish_startup_progress_if_ready()
            return
        gpu_status = get_gpu_search_status()
        if (
            self.startup_index_started
            or gpu_status.get("loaded")
            or gpu_status.get("mode") == "streaming"
        ):
            self._hide_startup_progress()
            self._finish_startup_progress_if_ready()
            return
        index_path = Path(INDEX_FILE)
        if not index_path.is_file():
            self._hide_startup_progress()
            self._finish_startup_progress_if_ready()
            return
        self.startup_index_started = True
        self._start_gpu_index_load()

    def _startup_ready_or_warn(self, operation_name: str = "операцию") -> bool:
        if self.startup_db_ready and self.conn is not None:
            return True
        if self.startup_db_failed:
            message = "База данных не подготовлена. Исправьте ошибку подключения и нажмите «Подключиться»."
        else:
            message = "База данных ещё инициализируется. Дождитесь завершения запуска."
        self.status_var.set(f"Нельзя начать {operation_name}: {message}")
        return False

    def _install_settings_traces(self):
        variables = (
            self.db_profile_var,
            self.backend_mode_var,
            self.folder_var,
            self.local_workers_var,
            self.scan_batch_size_var,
            self.auto_workers_var,
            self.delete_processed_var,
            self.use_json_metadata_var,
            self.create_crop_files_var,
            self.overwrite_existing_crops_var,
            self.use_local_crops_var,
            self.create_crops_on_html_var,
            self.query_var,
            self.topk_var,
            self.min_similarity_var,
        )

        for variable in variables:
            try:
                variable.trace_add(
                    "write",
                    self._on_settings_variable_changed,
                )
            except AttributeError:
                variable.trace(
                    "w",
                    self._on_settings_variable_changed,
                )

    def _on_settings_variable_changed(self, *_args):
        self._schedule_settings_save()

    def _on_stats_message_changed(self, *_args):
        """Показывает строку состояния только при наличии текста."""
        label = getattr(self, "stats_message_label", None)
        table = getattr(self, "stats_table", None)
        if label is None or table is None:
            return
        try:
            if str(self.stats_var.get() or "").strip():
                if not label.winfo_manager():
                    label.pack(fill="x", anchor="w", before=table, pady=(0, 2))
            elif label.winfo_manager():
                label.pack_forget()
        except tk.TclError:
            pass

    def _update_stats_table_wraplength(self):
        """Подгоняет перенос длинных значений под ширину правой панели статистики."""
        try:
            width = int(self.stats_table.winfo_width())
            if width <= 1:
                return
            # Узкая левая панель: учитываем ширину столбца показателей.
            value_width = max(70, width - 112)
            for label in self.stats_value_labels.values():
                label.configure(wraplength=value_width)
            self.stats_message_label.configure(wraplength=max(120, width - 12))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass

    def _update_status_wraplength(self):
        """Подгоняет перенос нижнего статуса под доступную ширину окна."""
        label = getattr(self, "status_label", None)
        if label is None:
            return
        try:
            window_width = int(self.root.winfo_width())
            if window_width <= 1:
                return
            # Учитываем внешние отступы страницы и внутренние отступы футера.
            wraplength = max(320, window_width - 52)
            if int(label.cget("wraplength")) != wraplength:
                label.configure(wraplength=wraplength)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _on_window_configure(self, _event=None):
        # Обновляем ширину строки при разворачивании, восстановлении и
        # ручном изменении размеров окна; сам статус продолжает занимать всю ширину.
        self._update_status_wraplength()
        self._update_stats_table_wraplength()
        if self._settings_ready:
            self._schedule_settings_save()

    def _on_paned_sash_release(self, _event=None):
        """Сохраняет горизонтальный размер панелей после перемещения разделителя."""
        if self._settings_ready:
            self._schedule_settings_save()

    def _restore_paned_sash_position(self, attempt: int = 0):
        """Восстанавливает оба разделителя после расчёта геометрии Tk."""
        try:
            if not self.paned.winfo_exists():
                return

            stats_position = max(
                0, int(self.gui_settings.get("paned_sash_position") or 0)
            )
            preview_position = max(
                0,
                int(self.gui_settings.get("paned_preview_sash_position") or 0),
            )
            if stats_position <= 0 and preview_position <= 0:
                return

            width = int(self.paned.winfo_width())
            if width <= 1:
                if attempt < 10:
                    self.root.after(
                        100,
                        lambda: self._restore_paned_sash_position(attempt + 1),
                    )
                return

            # Sash 0 разделяет статистику БД и панель поиска. Сохраняем
            # существующее поведение восстановления этой панели.
            if stats_position > 0:
                max_stats_position = max(180, width - 700)
                target_stats = max(180, min(stats_position, max_stats_position))
                self.paned.sashpos(0, target_stats)

            # Sash 1 разделяет поиск и просмотр изображения. Восстанавливаем
            # его отдельно, оставляя минимальное место для поиска и просмотра.
            if preview_position > 0:
                try:
                    first_sash = int(self.paned.sashpos(0))
                except (tk.TclError, TypeError, ValueError):
                    first_sash = stats_position or 220
                first_sash = max(180, first_sash)
                min_preview_sash = first_sash + 220
                max_preview_sash = max(min_preview_sash, width - 320)
                target_preview = max(
                    min_preview_sash,
                    min(preview_position, max_preview_sash),
                )
                self.paned.sashpos(1, target_preview)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _current_paned_sash_position(self) -> int:
        """Возвращает координату разделителя статистики или сохранённое значение."""
        fallback = max(0, int(self.gui_settings.get("paned_sash_position") or 0))
        try:
            if not hasattr(self, "paned") or not self.paned.winfo_exists():
                return fallback
            if int(self.paned.winfo_width()) <= 1:
                return fallback
            position = int(self.paned.sashpos(0))
            # Во время первичной раскладки ttk иногда временно возвращает 0.
            # Не затираем им корректное значение из settings.xml.
            return position if position > 0 else fallback
        except (tk.TclError, TypeError, ValueError):
            return fallback

    def _current_paned_preview_sash_position(self) -> int:
        """Возвращает координату разделителя перед правой панелью просмотра."""
        fallback = max(
            0, int(self.gui_settings.get("paned_preview_sash_position") or 0)
        )
        try:
            if not hasattr(self, "paned") or not self.paned.winfo_exists():
                return fallback
            if int(self.paned.winfo_width()) <= 1:
                return fallback
            position = int(self.paned.sashpos(1))
            # Во время layout sash может временно возвращать 0. Не затираем
            # ранее сохранённую координату правой панели этим временным значением.
            return position if position > 0 else fallback
        except (tk.TclError, TypeError, ValueError):
            return fallback

    def _schedule_settings_save(self):
        if not self._settings_ready:
            return

        try:
            if self._settings_save_after is not None:
                self.root.after_cancel(self._settings_save_after)
        except Exception:
            pass

        try:
            self._settings_save_after = self.root.after(
                500,
                self._save_gui_settings,
            )
        except tk.TclError:
            self._settings_save_after = None

    def _save_gui_settings(self):
        self._settings_save_after = None

        if not self._settings_ready:
            return

        try:
            try:
                maximized = str(self.root.state()) == "zoomed"
            except tk.TclError:
                maximized = False

            try:
                scan_batch_size = int(str(self.scan_batch_size_var.get()).strip() or 0)
            except (TypeError, ValueError):
                scan_batch_size = DEFAULT_SCAN_BATCH_SIZE
            scan_batch_size = max(0, min(MAX_SCAN_BATCH_SIZE, scan_batch_size))

            settings = {
                "window_geometry": self.root.geometry(),
                "window_maximized": maximized,
                "theme": self.theme_mode,
                "paned_sash_position": self._current_paned_sash_position(),
                "paned_preview_sash_position": self._current_paned_preview_sash_position(),
                "selected_db_profile": str(
                    self.db_profile_var.get() or ""
                ).strip(),
                "selected_folder": (
                    str(self.selected_dir)
                    if self.selected_dir is not None
                    else ""
                ),
                "query_image": (
                    self.query_source_url
                    or (str(self.query_path) if self.query_path is not None else "")
                ),
                "backend": str(self.backend_mode_var.get()),
                "workers": int(self.local_workers_var.get()),
                "scan_batch_size": scan_batch_size,
                "auto_workers": bool(self.auto_workers_var.get()),
                "delete_processed": bool(self.delete_processed_var.get()),
                "use_json_metadata": bool(self.use_json_metadata_var.get()),
                "create_crop_files": bool(self.create_crop_files_var.get()),
                "overwrite_existing_crops": bool(self.overwrite_existing_crops_var.get()),
                "use_local_crops": bool(self.use_local_crops_var.get()),
                "create_crops_on_html": bool(self.create_crops_on_html_var.get()),
                "top_k": int(self.topk_var.get()),
                "min_similarity": float(self.min_similarity_var.get()),
                "collapsible_sections": {
                    key: bool(group.get("expanded", True))
                    for key, group in getattr(self, "_collapsible_groups", {}).items()
                },
            }

            _save_gui_settings_data(settings)
        except Exception as exc:
            print(
                f"Предупреждение: не удалось сохранить "
                f"{SETTINGS_FILE}: {exc}"
            )

    def _refresh_database_combo(self):
        names = [str(p.get("name")) for p in self.db_profiles if p.get("name")]
        self.db_combo["values"] = names
        if not str(self.db_profile_var.get()).strip() and self.active_db_name in names:
            self.db_profile_var.set(self.active_db_name)
        self._update_database_info()

    def _get_selected_db_profile(self):
        name = str(self.db_profile_var.get()).strip()
        return next(
            (p for p in self.db_profiles if str(p.get("name")) == name),
            None,
        )

    def _on_database_selected(self, _event=None):
        # Выбор в ComboBox только меняет выделение. Реальное переключение
        # выполняется кнопкой «Подключиться», чтобы случайный клик не закрыл
        # активное соединение во время работы пользователя.
        self._update_database_info()

    def _update_database_info(self):
        profile = self._get_selected_db_profile()
        if not profile:
            self.db_info_var.set("Профиль не выбран")
            if hasattr(self, "db_connection_var"):
                self.db_connection_var.set("Соединение: нет выбранного профиля")
            return

        profile = _normalize_profile_storage_paths(dict(profile))
        backend = str(profile.get("type") or DB_TYPE_SQLITE)
        label = DB_TYPE_LABELS.get(backend, backend)
        if backend == DB_TYPE_SQLITE:
            location = str(profile.get("path") or "")
        else:
            location = (
                f"{profile.get('host') or 'localhost'}:"
                f"{profile.get('port') or DB_DEFAULT_PORTS.get(backend, '')}/"
                f"{profile.get('database') or ''}"
            )

        index_path = str(profile.get("index_path") or "авто")
        crop_path = str(profile.get("crop_path") or "авто")
        is_connected = (
            self.conn is not None
            and str(getattr(self, "connected_db_name", "")) == str(profile.get("name"))
            and str(self.active_db_name) == str(profile.get("name"))
        )

        self.db_info_var.set(
            f"{label} | {location} | FAISS: {index_path} | CROP: {crop_path}"
        )

        if hasattr(self, "db_connection_var"):
            if getattr(self, "startup_db_pending", False) and str(profile.get("name")) == str(getattr(self, "active_db_name", "")):
                self.db_connection_var.set("Соединение: выполняется начальная подготовка БД...")
            elif is_connected:
                gpu_status = get_gpu_search_status()
                if self.backend_mode_var.get() == BACKEND_GPU and gpu_status.get("loaded"):
                    faiss_state = (
                        f"FAISS: в VRAM ({gpu_status.get('count', 0):,}, "
                        f"{gpu_status.get('dtype', '')})"
                    )
                elif getattr(self, "gpu_index_loading", False) and self.backend_mode_var.get() == BACKEND_GPU:
                    faiss_state = "FAISS: загрузка в VRAM..."
                else:
                    faiss_state = (
                        "FAISS: файл есть, VRAM не загружен"
                        if Path(index_path).exists()
                        else "FAISS: файл отсутствует"
                    )
                self.db_connection_var.set(
                    f"Соединение: ПОДКЛЮЧЕНО • профиль «{self.connected_db_name}» • "
                    f"СУБД: {getattr(self.conn, 'backend', label)} • {faiss_state}"
                )
            else:
                self.db_connection_var.set(
                    f"Соединение: НЕ ПОДКЛЮЧЕНО к выбранному профилю «{profile.get('name')}"
                    "» — нажмите «Подключиться»"
                )

    def switch_database(self, *, force_reconnect: bool = False):
        if getattr(self, "startup_db_pending", False) and not getattr(self, "startup_db_failed", False):
            self.status_var.set("Дождитесь первичной инициализации базы данных.")
            return
        if self.busy:
            messagebox.showwarning(
                "Операция выполняется",
                "Нельзя переключать базу данных во время сканирования, "
                "индексации или поиска.",
            )
            return

        profile = self._get_selected_db_profile()
        if not profile:
            return

        profile = _normalize_profile_storage_paths(dict(profile))
        for i, item in enumerate(self.db_profiles):
            if str(item.get("name")) == str(profile.get("name")):
                self.db_profiles[i] = profile
                break

        same_profile = (
            str(profile.get("name")) == str(self.active_db_name)
            and self.conn is not None
            and str(getattr(self, "connected_db_name", "")) == str(profile.get("name"))
        )
        if not force_reconnect and same_profile:
            self._update_database_info()
            self._refresh_stats()
            self.status_var.set(f"БД уже подключена: {self.active_db_name}")
            return

        old_conn = self.conn
        old_active_name = self.active_db_name
        old_connected_name = getattr(self, "connected_db_name", "")
        old_db_file = globals().get("DB_FILE")
        old_index_file = globals().get("INDEX_FILE")
        old_crop_dir = globals().get("CROP_DIR")
        old_crop_url = globals().get("CROP_URL", "")

        new_conn = None
        try:
            self.status_var.set(f"Подключение к БД: {profile.get('name')}...")
            self.root.update_idletasks()

            # Сначала проверяем новое соединение. Старое не трогаем, пока
            # подключение нового профиля полностью не прошло.
            new_conn = create_database_for_profile(
                profile,
                maintenance=True,
                create_if_missing=True,
            )

            _clear_gpu_search_index(release_cuda_memory=True)

            self.conn = new_conn
            new_conn = None
            self.active_db_name = str(profile["name"])
            self.connected_db_name = self.active_db_name
            self.db_profile_var.set(self.active_db_name)
            _save_db_profiles(self.db_profiles, self.active_db_name)

            try:
                if old_conn is not None and old_conn is not self.conn:
                    old_conn.close()
            except Exception:
                pass

            self.query_faces = []
            self.selected_face_index = None
            self._redraw_preview()
            self._update_search_state()
            self._refresh_database_combo()
            self._refresh_stats()
            self._update_database_info()
            self.status_var.set(
                f"Подключена БД: {self.active_db_name}"
            )

            if self.backend_mode_var.get() == BACKEND_GPU:
                self._start_gpu_index_load()

        except Exception as exc:
            if new_conn is not None:
                try:
                    new_conn.close()
                except Exception:
                    pass

            self.conn = old_conn
            self.active_db_name = old_active_name
            self.connected_db_name = old_connected_name
            if old_db_file is not None:
                globals()["DB_FILE"] = old_db_file
            if old_index_file is not None:
                globals()["INDEX_FILE"] = old_index_file
            if old_crop_dir is not None:
                globals()["CROP_DIR"] = old_crop_dir
            globals()["CROP_URL"] = old_crop_url or ""

            self.db_profile_var.set(self.active_db_name)
            self._update_database_info()
            messagebox.showerror(
                "Ошибка подключения к БД",
                str(exc),
            )

    def _database_profile_dialog(self, profile=None):
        is_new = profile is None
        profile = dict(profile or _default_profile())
        if is_new:
            profile["name"] = ""

        win = tk.Toplevel(self.root)
        win.configure(background=getattr(self, "theme_colors", {}).get("window", "#f0f0f0"))
        win.title("Новая база данных" if is_new else "Настройка базы данных")
        # Размер диалога будет рассчитан по фактической высоте полей ниже,
        # чтобы не оставлять большое пустое пространство под кнопками.
        win.transient(self.root)
        win.grab_set()

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Имя профиля:").grid(row=0, column=0, sticky="w", pady=4)
        name_var = tk.StringVar(value=str(profile.get("name") or ""))
        ttk.Entry(frame, textvariable=name_var, width=55).grid(row=0, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Тип БД:").grid(row=1, column=0, sticky="w", pady=4)
        type_var = tk.StringVar(value=str(profile.get("type") or DB_TYPE_SQLITE))
        type_values = list(DB_TYPE_LABELS.values())
        backend_to_label = {k: v for k, v in DB_TYPE_LABELS.items()}
        label_to_backend = {v: k for k, v in DB_TYPE_LABELS.items()}
        type_var.set(backend_to_label.get(type_var.get(), type_var.get()))
        type_combo = ttk.Combobox(
            frame,
            textvariable=type_var,
            state="readonly",
            values=type_values,
            width=28,
        )
        type_combo.grid(row=1, column=1, sticky="w")

        ttk.Label(frame, text="Имя базы:").grid(row=2, column=0, sticky="w", pady=4)
        dbname_var = tk.StringVar(value=str(profile.get("database") or "faces"))
        ttk.Entry(frame, textvariable=dbname_var, width=55).grid(row=2, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="SQLite файл:").grid(row=3, column=0, sticky="w", pady=4)
        path_var = tk.StringVar(value=str(profile.get("path") or ""))
        path_entry = ttk.Entry(frame, textvariable=path_var, width=48)
        path_entry.grid(row=3, column=1, sticky="ew")
        ttk.Button(
            frame,
            text="Обзор...",
            command=lambda: path_var.set(
                filedialog.asksaveasfilename(
                    parent=win,
                    title="Файл SQLite",
                    defaultextension=".db",
                    filetypes=[("SQLite", "*.db *.sqlite *.sqlite3"), ("Все файлы", "*.*")]
                ) or path_var.get()
            ),
        ).grid(row=3, column=2, padx=(6, 0))

        ttk.Label(frame, text="Сервер:").grid(row=4, column=0, sticky="w", pady=4)
        host_var = tk.StringVar(value=str(profile.get("host") or "localhost"))
        ttk.Entry(frame, textvariable=host_var, width=55).grid(row=4, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Порт:").grid(row=5, column=0, sticky="w", pady=4)
        port_var = tk.StringVar(value=str(profile.get("port") or ""))
        ttk.Entry(frame, textvariable=port_var, width=20).grid(row=5, column=1, sticky="w")

        ttk.Label(frame, text="Пользователь:").grid(row=6, column=0, sticky="w", pady=4)
        user_var = tk.StringVar(value=str(profile.get("user") or ""))
        ttk.Entry(frame, textvariable=user_var, width=55).grid(row=6, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Пароль:").grid(row=7, column=0, sticky="w", pady=4)
        password_var = tk.StringVar(value=str(profile.get("password") or ""))
        ttk.Entry(frame, textvariable=password_var, show="*", width=55).grid(row=7, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="ODBC driver:").grid(row=8, column=0, sticky="w", pady=4)
        driver_var = tk.StringVar(value=str(profile.get("driver") or ""))
        ttk.Entry(frame, textvariable=driver_var, width=55).grid(row=8, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="FAISS индекс:").grid(row=9, column=0, sticky="w", pady=4)
        index_var = tk.StringVar(value=str(profile.get("index_path") or ""))
        ttk.Entry(frame, textvariable=index_var, width=48).grid(row=9, column=1, sticky="ew")
        ttk.Button(
            frame,
            text="Обзор...",
            command=lambda: index_var.set(
                filedialog.asksaveasfilename(
                    parent=win,
                    title="Файл FAISS",
                    defaultextension=".faiss",
                    filetypes=[("FAISS", "*.faiss"), ("Все файлы", "*.*")]
                ) or index_var.get()
            ),
        ).grid(row=9, column=2, padx=(6, 0))

        ttk.Label(frame, text="Каталог CROP:").grid(row=10, column=0, sticky="w", pady=4)
        crop_var = tk.StringVar(value=str(profile.get("crop_path") or ""))
        ttk.Entry(frame, textvariable=crop_var, width=48).grid(row=10, column=1, sticky="ew")
        ttk.Button(
            frame,
            text="Обзор...",
            command=lambda: crop_var.set(
                filedialog.askdirectory(
                    parent=win,
                    title="Локальный каталог CROP"
                ) or crop_var.get()
            ),
        ).grid(row=10, column=2, padx=(6, 0))

        ttk.Label(frame, text="URL CROP (необязательно):").grid(row=11, column=0, sticky="w", pady=4)
        crop_url_var = tk.StringVar(value=str(profile.get("crop_url") or ""))
        ttk.Entry(frame, textvariable=crop_url_var, width=60).grid(
            row=11, column=1, columnspan=2, sticky="ew"
        )
        ttk.Label(
            frame,
            text="Только для отображения в HTML. Новые CROP-файлы всегда сохраняются локально.",
            foreground="#666666",
        ).grid(row=12, column=0, columnspan=3, sticky="w", pady=(0, 4))

        status_var = tk.StringVar(value="")
        ttk.Label(
            frame,
            textvariable=status_var,
            wraplength=680,
            justify="left",
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(6, 4))

        def update_type(_event=None):
            backend = label_to_backend.get(type_var.get(), type_var.get())
            defaults = DB_DEFAULT_PORTS.get(backend)
            if backend != DB_TYPE_SQLITE and not port_var.get():
                port_var.set(str(defaults or ""))
            if backend == DB_TYPE_SQLITE:
                path_entry.configure(state="normal")
            else:
                path_entry.configure(state="normal")

        type_combo.bind("<<ComboboxSelected>>", update_type)
        update_type()

        def collect_profile():
            name = name_var.get().strip()
            if not name:
                raise ValueError("Укажите имя профиля.")
            backend = label_to_backend.get(type_var.get().strip(), type_var.get().strip())
            if backend not in DB_TYPE_LABELS:
                raise ValueError("Неизвестный тип базы данных.")

            result = {
                "name": name,
                "type": backend,
                "path": path_var.get().strip(),
                "database": dbname_var.get().strip(),
                "host": host_var.get().strip(),
                "port": port_var.get().strip(),
                "user": user_var.get().strip(),
                "password": password_var.get(),
                "driver": driver_var.get().strip(),
                "index_path": index_var.get().strip(),
                "crop_path": crop_var.get().strip(),
                "crop_url": crop_url_var.get().strip(),
            }

            if result["crop_url"]:
                parsed_crop_url = urlparse(result["crop_url"])
                if parsed_crop_url.scheme.lower() not in {"http", "https"} or not parsed_crop_url.netloc:
                    raise ValueError("URL CROP должен начинаться с http:// или https://")
                result["crop_url"] = result["crop_url"].rstrip("/")

            if backend == DB_TYPE_SQLITE and not result["path"]:
                raise ValueError("Для SQLite укажите путь к .db файлу.")
            if backend != DB_TYPE_SQLITE:
                if not result["database"]:
                    raise ValueError("Укажите имя серверной базы данных.")
                if not result["host"]:
                    raise ValueError("Укажите сервер.")
                if not result["user"]:
                    raise ValueError("Укажите пользователя.")

            return _normalize_profile_storage_paths(result)

        def save_profile():
            try:
                new_profile = collect_profile()
                old_name = str(profile.get("name") or "")
                names = [str(p.get("name")) for p in self.db_profiles]

                if is_new and new_profile["name"] in names:
                    raise ValueError("Профиль с таким именем уже существует.")
                if not is_new and new_profile["name"] != old_name and new_profile["name"] in names:
                    raise ValueError("Профиль с таким именем уже существует.")

                # Подключение выполняет также создание серверной БД и таблиц.
                status_var.set("Подключение/создание БД...")
                win.update_idletasks()
                test_conn = create_database_for_profile(
                    new_profile,
                    maintenance=True,
                    create_if_missing=True,
                    activate_storage=False,
                )
                test_conn.close()

                was_active = (not is_new and old_name == self.active_db_name)

                if is_new:
                    self.db_profiles.append(new_profile)
                else:
                    for i, item in enumerate(self.db_profiles):
                        if str(item.get("name")) == old_name:
                            self.db_profiles[i] = new_profile
                            break

                # При переименовании активного профиля активным должен стать
                # уже новое имя, иначе database_profiles.json будет хранить
                # ссылку на несуществующий профиль.
                target_active = new_profile["name"] if was_active else self.active_db_name
                _save_db_profiles(self.db_profiles, target_active)

                self._refresh_database_combo()
                self.db_profile_var.set(new_profile["name"])

                # При изменении параметров активного профиля, в том числе
                # пути к SQLite-файлу, необходимо реально переподключиться.
                # Ранее switch_database() выходил сразу, потому что имя
                # профиля оставалось тем же, и старое соединение продолжало
                # работать с прежней БД.
                if was_active:
                    self.switch_database(force_reconnect=True)
                elif self.active_db_name != new_profile["name"]:
                    self.switch_database()

                status_var.set("Профиль сохранён и БД доступна.")
                win.destroy()

            except Exception as exc:
                status_var.set(f"Ошибка: {exc}")

        buttons = ttk.Frame(frame)
        buttons.grid(row=14, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Сохранить / создать БД", command=save_profile).pack(side="left", padx=4)
        ttk.Button(buttons, text="Отмена", command=win.destroy).pack(side="left", padx=4)

        frame.columnconfigure(1, weight=1)

        # После создания всех строк подгоняем высоту окна под содержимое.
        # Небольшой запас оставляет аккуратный отступ снизу и учитывает DPI.
        win.update_idletasks()
        requested_height = frame.winfo_reqheight() + 8
        screen_height = win.winfo_screenheight()
        available_height = max(420, screen_height - 100)
        dialog_height = min(max(requested_height, 420), min(520, available_height))
        win.geometry(f"800x{dialog_height}")

        # Центрируем диалог относительно главного окна.
        try:
            self.root.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - 800) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog_height) // 2)
            win.geometry(f"800x{dialog_height}+{x}+{y}")
        except tk.TclError:
            pass

    def new_database_profile(self):
        if getattr(self, "startup_db_pending", False) and not getattr(self, "startup_db_failed", False):
            self.status_var.set("Дождитесь первичной инициализации базы данных.")
            return
        if self.busy:
            messagebox.showwarning("Операция выполняется", "Дождитесь завершения текущей операции.")
            return
        self._database_profile_dialog(None)

    def open_database_migration(self):
        """Запускает отдельную утилиту миграции в изолированном процессе."""
        script = APP_DIR / "database_migration.py"
        if not script.is_file():
            messagebox.showerror(
                "Миграция БД",
                f"Не найден файл утилиты миграции:\n{script}\n\n"
                "Поместите database_migration.py рядом с основным скриптом.",
                parent=self.root,
            )
            return

        python_exe = Path(sys.executable)
        if os.name == "nt" and python_exe.name.lower() == "python.exe":
            pythonw = python_exe.with_name("pythonw.exe")
            if pythonw.is_file():
                python_exe = pythonw

        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = subprocess.Popen(
                [str(python_exe), str(script)],
                cwd=str(APP_DIR),
                creationflags=flags,
                close_fds=True,
            )
            processes = getattr(self, "_migration_processes", [])
            processes.append(proc)
            self._migration_processes = processes
            self.status_var.set("Открыто отдельное окно миграции БД.")
            self.root.after(1200, self._poll_migration_processes)
        except Exception as exc:
            messagebox.showerror("Миграция БД", f"Не удалось открыть окно миграции:\n{exc}", parent=self.root)

    def _poll_migration_processes(self):
        """Подхватывает новые профили, созданные внешней утилитой миграции."""
        alive = []
        for proc in getattr(self, "_migration_processes", []):
            try:
                if proc.poll() is None:
                    alive.append(proc)
            except Exception:
                pass
        self._migration_processes = alive

        try:
            profiles, active = _load_db_profiles()
            current_names = [str(p.get("name")) for p in self.db_profiles]
            new_names = [str(p.get("name")) for p in profiles]
            if new_names != current_names:
                selected = str(self.db_profile_var.get() or self.active_db_name)
                self.db_profiles = profiles
                if active in new_names:
                    self.active_db_name = active
                self._refresh_database_combo()
                self.db_profile_var.set(selected if selected in new_names else self.active_db_name)
        except Exception as exc:
            print(f"Не удалось обновить список профилей после миграции: {exc}")

        if alive:
            try:
                self.root.after(1200, self._poll_migration_processes)
            except tk.TclError:
                pass

    def edit_database_profile(self):
        if getattr(self, "startup_db_pending", False) and not getattr(self, "startup_db_failed", False):
            self.status_var.set("Дождитесь первичной инициализации базы данных.")
            return
        if self.busy:
            messagebox.showwarning("Операция выполняется", "Дождитесь завершения текущей операции.")
            return
        profile = self._get_selected_db_profile()
        if profile:
            self._database_profile_dialog(profile)

    def delete_database_profile(self):
        if getattr(self, "startup_db_pending", False) and not getattr(self, "startup_db_failed", False):
            self.status_var.set("Дождитесь первичной инициализации базы данных.")
            return
        if self.busy:
            messagebox.showwarning("Операция выполняется", "Дождитесь завершения текущей операции.")
            return

        profile = self._get_selected_db_profile()
        if not profile:
            return
        if len(self.db_profiles) <= 1:
            messagebox.showwarning("База данных", "Нельзя удалить единственный профиль.")
            return

        name = str(profile.get("name"))
        if not messagebox.askyesno(
            "Удалить профиль",
            f"Удалить профиль «{name}»?\n\n"
            "Файлы SQLite/FAISS и данные на сервере НЕ удаляются. "
            "Удаляется только профиль подключения.",
        ):
            return

        self.db_profiles = [
            p for p in self.db_profiles
            if str(p.get("name")) != name
        ]
        need_switch = self.active_db_name == name
        if need_switch:
            self.active_db_name = str(self.db_profiles[0]["name"])
        _save_db_profiles(self.db_profiles, self.active_db_name)
        self._refresh_database_combo()
        if need_switch:
            self.db_profile_var.set(self.active_db_name)
            try:
                self.switch_database(force_reconnect=True)
            except Exception as exc:
                messagebox.showerror("Ошибка подключения", str(exc))

    def _set_window_icon(self) -> None:
        ico =  APP_DIR.joinpath("icons" ,"SearchFace.ico")
        png =  APP_DIR.joinpath("icons" , "SearchFace.png")
        # print("APP_DIR:", APP_DIR)
        # print("ico exists:", (APP_DIR / "SearchFace.ico").is_file())
        # print("png exists:", (APP_DIR / "SearchFace.png").is_file())
        if ico.is_file():
            try:
                #self.root.iconbitmap(default=str(ico))
                self.root.iconbitmap(str(ico))
            except tk.TclError:
                pass
            # except tk.TclError as e:
            #     print("iconbitmap failed:", e)

        if png.is_file():
            try:
                icon_image = Image.open(png)
                self._icon_photo = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self._icon_photo)
            except Exception:
                pass

    def toggle_theme(self):
        """Переключает светлую/тёмную тему и сразу сохраняет выбор."""
        self.theme_mode = "dark" if self.theme_mode == "light" else "light"
        self._apply_theme()
        self._settings_ready = True
        self._save_gui_settings()

    def _apply_theme(self):
        """Применяет согласованную светлую/тёмную палитру ко всему главному окну."""
        dark = self.theme_mode == "dark"
        # Тёмная тема использует нейтральную графитовую палитру без синих акцентов.
        # Светлая тема и семантический цвет опасных действий оставлены без изменений.
        colors = {
            "window": "#1c1c1c" if dark else "#f3f3f3",
            "surface": "#242424" if dark else "#ffffff",
            "panel": "#2c2c2c" if dark else "#e7e7e7",
            "field": "#191919" if dark else "#ffffff",
            "foreground": "#eeeeee" if dark else "#202020",
            "muted": "#b0b0b0" if dark else "#555555",
            "border": "#444444" if dark else "#bdbdbd",
            "button": "#303030" if dark else "#ffffff",
            "button_active": "#3d3d3d" if dark else "#e2e2e2",
            "selection": "#555555" if dark else "#cccccc",
            "accent": "#686868" if dark else "#444444",
            "accent_active": "#7a7a7a" if dark else "#303030",
            "canvas": "#202020" if dark else "#e0e0e0",
            "danger": "#a9333f" if dark else "#c43d4b",
        }
        self.theme_colors = colors

        # Clam позволяет одинаково контролировать палитру на Windows, Linux и macOS;
        # нативная Vista-тема игнорирует ряд цветов и делает интерфейс неоднородным.
        try:
            if "clam" in self.style.theme_names():
                self.style.theme_use("clam")
        except tk.TclError:
            pass

        try:
            self.root.configure(background=colors["window"])
            style = self.style
            style.configure(".", font=("Segoe UI", 9))
            style.configure("TFrame", background=colors["window"])
            style.configure("App.TFrame", background=colors["window"])
            style.configure("Header.TFrame", background=colors["surface"])
            style.configure("Surface.TFrame", background=colors["surface"])
            style.configure("Inset.TFrame", background=colors["panel"])
            style.configure(
                "TLabel", background=colors["window"], foreground=colors["foreground"],
                font=("Segoe UI", 9),
            )
            style.configure(
                "Title.TLabel", background=colors["surface"], foreground=colors["foreground"],
                font=("Segoe UI Semibold", 19),
            )
            style.configure(
                "Subtitle.TLabel", background=colors["surface"], foreground=colors["muted"],
                font=("Segoe UI", 9),
            )
            style.configure(
                "Section.TLabel", background=colors["surface"], foreground=colors["foreground"],
                font=("Segoe UI Semibold", 10),
            )
            style.configure(
                "Surface.TLabel", background=colors["surface"], foreground=colors["foreground"],
            )
            style.configure(
                "SurfaceMuted.TLabel", background=colors["surface"], foreground=colors["muted"],
            )
            style.configure(
                "Inset.TLabel", background=colors["panel"], foreground=colors["foreground"],
            )
            style.configure(
                "InsetMuted.TLabel", background=colors["panel"], foreground=colors["muted"],
            )
            style.configure(
                "Muted.TLabel", background=colors["window"], foreground=colors["muted"],
            )
            style.configure(
                "Status.TLabel", background=colors["surface"], foreground=colors["muted"],
                padding=(8, 5),
            )
            style.configure(
                "TLabelframe", background=colors["window"], foreground=colors["foreground"],
                bordercolor=colors["border"], relief="solid", padding=8,
            )
            style.configure(
                "TLabelframe.Label", background=colors["window"], foreground=colors["muted"],
                font=("Segoe UI Semibold", 9),
            )
            style.configure(
                "SectionHeader.TButton",
                background=colors["window"], foreground=colors["foreground"],
                bordercolor=colors["border"], borderwidth=0, relief="flat",
                anchor="w", padding=(8, 5), font=("Segoe UI Semibold", 10),
            )
            style.map(
                "SectionHeader.TButton",
                background=[
                    ("pressed", colors["selection"]),
                    ("active", colors["button_active"]),
                ],
                foreground=[("disabled", colors["muted"]), ("!disabled", colors["foreground"])],
            )
            style.configure(
                "TButton", background=colors["button"], foreground=colors["foreground"],
                bordercolor=colors["border"], padding=(10, 7), font=("Segoe UI", 9),
                relief="flat",
            )
            style.map(
                "TButton",
                background=[("disabled", colors["panel"]), ("pressed", colors["selection"]),
                            ("active", colors["button_active"])],
                foreground=[("disabled", colors["muted"]), ("!disabled", colors["foreground"])],
                bordercolor=[("focus", colors["accent"])],
            )
            style.configure(
                "Primary.TButton", background=colors["accent"], foreground="#ffffff",
                bordercolor=colors["accent"], padding=(12, 8), font=("Segoe UI Semibold", 9),
            )
            style.map(
                "Primary.TButton",
                background=[("disabled", colors["panel"]), ("pressed", colors["accent_active"]),
                            ("active", colors["accent_active"])],
                foreground=[("disabled", colors["muted"]), ("!disabled", "#ffffff")],
            )
            style.configure(
                "Danger.TButton", background=colors["danger"], foreground="#ffffff",
                bordercolor=colors["danger"], padding=(10, 7), font=("Segoe UI Semibold", 9),
            )
            style.map(
                "Danger.TButton",
                background=[("disabled", colors["panel"]), ("pressed", colors["danger"]),
                            ("active", colors["danger"])],
                foreground=[("disabled", colors["muted"]), ("!disabled", "#ffffff")],
            )
            style.configure(
                "Theme.TButton", background=colors["panel"], foreground=colors["foreground"],
                bordercolor=colors["border"], padding=(0, 0), font=("Segoe UI Symbol", 13),
            )
            style.map(
                "Theme.TButton", background=[("pressed", colors["selection"]),
                                               ("active", colors["button_active"])],
            )
            style.configure(
                "TEntry", fieldbackground=colors["field"], foreground=colors["foreground"],
                insertcolor=colors["foreground"], bordercolor=colors["border"], padding=(7, 6),
            )
            style.map("TEntry", fieldbackground=[("disabled", colors["panel"])])
            style.configure(
                "TCombobox", fieldbackground=colors["field"], background=colors["button"],
                foreground=colors["foreground"], arrowcolor=colors["muted"],
                bordercolor=colors["border"], padding=(7, 5),
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", colors["field"]), ("disabled", colors["panel"])],
                foreground=[("disabled", colors["muted"]), ("readonly", colors["foreground"])],
                selectbackground=[("readonly", colors["selection"])],
                selectforeground=[("readonly", colors["foreground"])],
            )
            style.configure(
                "TSpinbox", fieldbackground=colors["field"], foreground=colors["foreground"],
                arrowcolor=colors["muted"], bordercolor=colors["border"], padding=(4, 4),
            )
            style.configure("TCheckbutton", background=colors["window"], foreground=colors["foreground"], padding=(2, 3))
            style.map(
                "TCheckbutton", background=[("active", colors["window"])],
                foreground=[("disabled", colors["muted"]), ("!disabled", colors["foreground"])],
            )
            style.configure("TRadiobutton", background=colors["window"], foreground=colors["foreground"], padding=(2, 3))
            style.map(
                "TRadiobutton", background=[("active", colors["window"])],
                foreground=[("disabled", colors["muted"]), ("!disabled", colors["foreground"])],
            )
            style.configure("TSeparator", background=colors["border"])
            style.configure("TPanedwindow", background=colors["border"])
            style.configure(
                "TProgressbar", background=colors["accent"], troughcolor=colors["panel"],
                bordercolor=colors["panel"], lightcolor=colors["accent"], darkcolor=colors["accent"],
            )
            style.configure(
                "TScrollbar", background=colors["button"], troughcolor=colors["surface"],
                bordercolor=colors["border"], arrowcolor=colors["muted"],
            )
            style.configure(
                "Treeview", background=colors["field"], fieldbackground=colors["field"],
                foreground=colors["foreground"], rowheight=27, bordercolor=colors["border"],
                font=("Segoe UI", 9),
            )
            style.configure(
                "Treeview.Heading", background=colors["panel"], foreground=colors["foreground"],
                font=("Segoe UI Semibold", 9), padding=(8, 7),
            )
            style.map(
                "Treeview", background=[("selected", colors["accent"])],
                foreground=[("selected", "#ffffff")],
            )
        except tk.TclError:
            pass

        for widget, options in (
            (getattr(self, "face_list", None), {
                "background": colors["field"], "foreground": colors["foreground"],
                "selectbackground": colors["accent"], "selectforeground": "#ffffff",
                "highlightbackground": colors["border"], "highlightcolor": colors["accent"],
                "relief": "flat", "borderwidth": 0, "font": ("Segoe UI", 10),
            }),
            (getattr(self, "canvas", None), {
                "background": colors["canvas"], "highlightbackground": colors["border"],
            }),
        ):
            if widget is not None:
                try:
                    widget.configure(**options)
                except tk.TclError:
                    pass

        if hasattr(self, "theme_toggle_btn"):
            self.theme_toggle_btn.configure(text="☼" if dark else "☾")

        try:
            for child in self.root.winfo_children():
                if isinstance(child, tk.Toplevel):
                    child.configure(background=colors["window"])
        except tk.TclError:
            pass

    def _create_collapsible_group(
        self,
        parent,
        title: str,
        key: str,
        *,
        padding=(10, 8),
        pady=(0, 8),
    ):
        """Создаёт секцию с кликабельным заголовком и сохраняемым состоянием."""
        if not hasattr(self, "_collapsible_groups"):
            self._collapsible_groups = {}

        saved_states = self.gui_settings.get("collapsible_sections", {})
        if not isinstance(saved_states, dict):
            saved_states = {}
        expanded = bool(saved_states.get(key, COLLAPSIBLE_SECTION_DEFAULTS.get(key, True)))

        container = ttk.Frame(parent, style="App.TFrame")
        container.pack(fill="x", pady=pady)

        header = ttk.Button(
            container,
            text="",
            style="SectionHeader.TButton",
            command=lambda group_key=key: self._toggle_collapsible_group(group_key),
        )
        # ttk.Button не принимает параметр anchor напрямую; выравнивание
        # задаётся стилем SectionHeader.TButton ниже.
        header.pack(fill="x")

        content = ttk.LabelFrame(container, padding=padding)
        self._collapsible_groups[key] = {
            "title": title,
            "container": container,
            "header": header,
            "content": content,
            "expanded": expanded,
        }
        self._render_collapsible_group(key)
        return content

    def _render_collapsible_group(self, key: str):
        """Отображает стрелку и состояние тела одной сворачиваемой секции."""
        group = getattr(self, "_collapsible_groups", {}).get(key)
        if not group:
            return
        expanded = bool(group.get("expanded", True))
        arrow = "▾" if expanded else "▸"
        group["header"].configure(text=f"{arrow}  {group['title']}")
        content = group["content"]
        if expanded:
            if not content.winfo_manager():
                content.pack(fill="x")
        else:
            content.pack_forget()

    def _toggle_collapsible_group(self, key: str):
        """Переключает секцию и сохраняет её состояние в settings.xml."""
        group = getattr(self, "_collapsible_groups", {}).get(key)
        if not group:
            return
        group["expanded"] = not bool(group.get("expanded", True))
        self._render_collapsible_group(key)
        self._schedule_settings_save()

    def _build_ui(self):
        """Создаёт современную компоновку интерфейса, не меняя обработчики приложения."""
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.root.option_add("*Font", ("Segoe UI", 9))
        self.status_var = tk.StringVar(value="Готово")
        self.stats_var = tk.StringVar(value="")

        # Контейнер всей страницы — единые внешние отступы вместо разрозненных полей.
        self.page = ttk.Frame(self.root, style="App.TFrame", padding=(14, 10, 14, 8))
        self.page.pack(fill="both", expand=True)

        # --------------------------------------------------------
        # Заголовок приложения
        # --------------------------------------------------------
        header = ttk.Frame(self.page, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x", pady=(0, 10))
        header_text = ttk.Frame(header, style="Header.TFrame")
        header_text.pack(side="left", fill="x", expand=True)
        ttk.Label(header_text, text="FaceSearch", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header_text,
            text="Поиск лиц • локальные коллекции • FAISS / CUDA",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(1, 0))

        self.theme_toggle_btn = ttk.Button(
            header,
            text="☾",
            width=3,
            style="Theme.TButton",
            command=self.toggle_theme,
            takefocus=True,
        )
        self.theme_toggle_btn.pack(side="right", padx=(12, 0), ipadx=0, ipady=3)

        action_row = ttk.Frame(self.page, padding=(0, 0, 0, 8))
        action_row.pack(fill="x")
        self.scan_btn = ttk.Button(
            action_row, text="▶  Сканировать GPU", style="Primary.TButton",
            command=self.start_scan,
        )
        self.scan_btn.pack(side="left", padx=(0, 8))
        self.repair_crops_btn = ttk.Button(
            action_row, text="◇  Проверить CROP", command=self.start_crop_repair,
        )
        self.repair_crops_btn.pack(side="left", padx=(0, 8))
        self.pause_scan_btn = ttk.Button(
            action_row, text="Ⅱ  Пауза", command=self.toggle_scan_pause, state="disabled",
        )
        self.pause_scan_btn.pack(side="left", padx=(0, 8))
        self.stop_scan_btn = ttk.Button(
            action_row, text="■  Остановить", style="Danger.TButton",
            command=self.stop_scan, state="disabled",
        )
        self.stop_scan_btn.pack(side="left")
        # Кнопка HTML остаётся в постоянно видимой верхней панели действий,
        # а не внутри ограниченной по высоте панели поиска.
        self.open_results_btn = ttk.Button(
            action_row, text="↗  Открыть последний HTML", command=self.open_last_html,
            state="normal" if os.path.exists(os.path.join(os.getcwd(), "face_search_results_gui.html")) else "disabled",
        )
        self.open_results_btn.pack(side="right", padx=(10, 0))

        self.scan_control_status_var = tk.StringVar(value="")
        self.scan_control_status_label = ttk.Label(
            action_row, textvariable=self.scan_control_status_var, style="Muted.TLabel",
        )
        # Показывается только во время сканирования или восстановления CROP.

        # Динамический статус располагается сразу под кнопками и появляется только по необходимости.
        self.dynamic_status_area = ttk.Frame(self.page, style="App.TFrame")
        self.dynamic_status_area.pack(fill="x")
        self.auto_workers_status_label = ttk.Label(
            self.dynamic_status_area, textvariable=self.auto_workers_status_var, anchor="w",
            style="Muted.TLabel", padding=(4, 2),
        )
        # Элемент появляется только во время активной адаптивной операции.

        # --------------------------------------------------------
        # Источник изображений
        # --------------------------------------------------------
        source_frame = self._create_collapsible_group(
            self.page, "Источник изображений", "source_images", padding=(10, 8),
            pady=(0, 8),
        )
        source_row = ttk.Frame(source_frame)
        source_row.pack(fill="x")

        self.choose_folder_btn = ttk.Button(
            source_row, text="▣  Выбрать папку", command=self.choose_folder,
        )
        self.choose_folder_btn.pack(side="left", padx=(0, 10))
        saved_folder = str(self.gui_settings.get("selected_folder") or "").strip()
        self.folder_var = tk.StringVar(value=saved_folder or "Папка не выбрана")
        ttk.Label(
            source_row, textvariable=self.folder_var, style="Muted.TLabel", anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.local_workers_var = tk.IntVar(
            value=max(1, min(LOCAL_MAX_WORKERS, int(self.gui_settings.get("workers", LOCAL_DEFAULT_WORKERS))))
        )
        self.auto_workers_var = tk.BooleanVar(
            value=bool(self.gui_settings.get("auto_workers", AUTO_WORKERS_DEFAULT))
        )
        self.auto_workers_check = ttk.Checkbutton(
            source_row, text="Автоподстройка", variable=self.auto_workers_var,
            command=self._on_auto_workers_changed,
        )
        self.auto_workers_check.pack(side="right", padx=(10, 6))
        ttk.Label(source_row, text="Потоки:", style="Muted.TLabel").pack(side="right", padx=(8, 4))
        self.local_workers_spinbox = ttk.Spinbox(
            source_row, from_=1, to=LOCAL_MAX_WORKERS,
            textvariable=self.local_workers_var, width=4,
        )
        self.local_workers_spinbox.pack(side="right")

        # --------------------------------------------------------
        # Параметры сканирования. Grid не даёт длинным подписям вытеснять кнопки.
        # --------------------------------------------------------
        scan_options = self._create_collapsible_group(
            self.page, "Параметры обработки", "processing", padding=(10, 5),
            pady=(0, 6),
        )
        for column in range(3):
            scan_options.columnconfigure(column, weight=1)

        self.delete_processed_var = tk.BooleanVar(value=bool(self.gui_settings.get("delete_processed", False)))
        self.delete_processed_check = ttk.Checkbutton(
            scan_options, text="Удалять обработанные исходники", variable=self.delete_processed_var,
        )
        self.delete_processed_check.grid(row=0, column=0, sticky="w", padx=(0, 10), pady=2)
        self.use_json_metadata_var = tk.BooleanVar(value=bool(self.gui_settings.get("use_json_metadata", True)))
        self.use_json_metadata_check = ttk.Checkbutton(
            scan_options, text="Использовать JSON-описание", variable=self.use_json_metadata_var,
        )
        self.use_json_metadata_check.grid(row=0, column=1, sticky="w", padx=(0, 10), pady=2)
        self.create_crop_files_var = tk.BooleanVar(value=bool(self.gui_settings.get("create_crop_files", True)))
        self.create_crop_files_check = ttk.Checkbutton(
            scan_options, text="Создавать CROP при сканировании", variable=self.create_crop_files_var,
        )
        self.create_crop_files_check.grid(row=0, column=2, sticky="w", pady=2)
        self.use_local_crops_var = tk.BooleanVar(value=bool(self.gui_settings.get("use_local_crops", True)))
        self.use_local_crops_check = ttk.Checkbutton(
            scan_options, text="Использовать CROP в HTML", variable=self.use_local_crops_var,
        )
        self.use_local_crops_check.grid(row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        self.overwrite_existing_crops_var = tk.BooleanVar(value=bool(self.gui_settings.get("overwrite_existing_crops", False)))
        self.overwrite_existing_crops_check = ttk.Checkbutton(
            scan_options, text="Обновлять CROP и метаданные", variable=self.overwrite_existing_crops_var,
        )
        self.overwrite_existing_crops_check.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=2)
        self.create_crops_on_html_var = tk.BooleanVar(
            value=bool(self.gui_settings.get("create_crops_on_html", False))
        )
        self.create_crops_on_html_check = ttk.Checkbutton(
            scan_options,
            text="Создавать CROP при создании HTML",
            variable=self.create_crops_on_html_var,
        )
        self.create_crops_on_html_check.grid(
            row=1, column=2, sticky="w", padx=(0, 10), pady=2
        )
        self.scan_batch_size_var = tk.StringVar(
            value=str(self.gui_settings.get("scan_batch_size", DEFAULT_SCAN_BATCH_SIZE))
        )
        ttk.Label(
            scan_options, text="Размер пакета файлов",
        ).grid(row=2, column=0, sticky="w", padx=(0, 10), pady=2)
        self.scan_batch_size_spinbox = ttk.Spinbox(
            scan_options,
            from_=0,
            to=MAX_SCAN_BATCH_SIZE,
            increment=100,
            textvariable=self.scan_batch_size_var,
            width=10,
        )
        self.scan_batch_size_spinbox.grid(
            row=2, column=1, sticky="w", padx=(0, 10), pady=2
        )
        ttk.Label(
            scan_options,
            text=f"0 = весь каталог (потоково, пакетами по {DEFAULT_SCAN_BATCH_SIZE})",
            style="Muted.TLabel",
        ).grid(row=2, column=2, sticky="w", padx=(0, 10), pady=2)

        # --------------------------------------------------------
        # Вычисления
        # --------------------------------------------------------
        backend_frame = self._create_collapsible_group(
            self.page, "Вычисления", "compute", padding=(10, 6),
            pady=(0, 8),
        )
        self.backend_gpu_radio = ttk.Radiobutton(
            backend_frame, text="GPU (CUDA с переходом на CPU при ошибке)",
            value=BACKEND_GPU, variable=self.backend_mode_var, command=self._on_backend_change,
        )
        self.backend_gpu_radio.pack(side="left", padx=(0, 16))
        self.backend_cpu_radio = ttk.Radiobutton(
            backend_frame, text="Только CPU", value=BACKEND_CPU,
            variable=self.backend_mode_var, command=self._on_backend_change,
        )
        self.backend_cpu_radio.pack(side="left", padx=(0, 16))
        ttk.Label(
            backend_frame, textvariable=self.backend_status_var, style="Muted.TLabel",
        ).pack(side="left", fill="x", expand=True)

        # --------------------------------------------------------
        # Профиль базы данных: выбор/подключение отдельно от управления профилями
        # --------------------------------------------------------
        db_frame = self._create_collapsible_group(
            self.page, "База данных", "database", padding=(10, 8),
            pady=(0, 8),
        )

        # Одна строка: выбор и подключение слева, управление профилями справа.
        # Вертикальный разделитель и дополнительный отступ визуально отделяют
        # опасные/административные действия от обычного подключения.
        db_select_row = ttk.Frame(db_frame)
        db_select_row.pack(fill="x")
        ttk.Label(db_select_row, text="Активный профиль:", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.db_combo = ttk.Combobox(
            db_select_row, textvariable=self.db_profile_var, state="readonly", width=34,
        )
        self.db_combo.pack(side="left", padx=(0, 8))
        self.db_combo.bind("<<ComboboxSelected>>", self._on_database_selected)
        self.connect_db_btn = ttk.Button(
            db_select_row, text="Подключиться", style="Primary.TButton", command=self.switch_database,
        )
        self.connect_db_btn.pack(side="left", padx=(0, 8))
        self.refresh_db_btn = ttk.Button(
            db_select_row, text="↻  Обновить", command=self.refresh_database_info,
        )
        self.refresh_db_btn.pack(side="left", padx=(0, 0))

        ttk.Separator(db_select_row, orient="vertical").pack(
            side="left", fill="y", padx=(14, 12), pady=3,
        )
        self.new_db_btn = ttk.Button(
            db_select_row, text="＋ Новая БД", command=self.new_database_profile,
        )
        self.new_db_btn.pack(side="left", padx=(0, 6))
        self.edit_db_btn = ttk.Button(
            db_select_row, text="Настроить", command=self.edit_database_profile,
        )
        self.edit_db_btn.pack(side="left", padx=(0, 6))
        self.delete_db_btn = ttk.Button(
            db_select_row, text="Удалить профиль", command=self.delete_database_profile,
        )
        self.delete_db_btn.pack(side="left", padx=(0, 6))
        self.migrate_db_btn = ttk.Button(
            db_select_row, text="⇄  Миграция БД", command=self.open_database_migration,
        )
        self.migrate_db_btn.pack(side="left", padx=(0, 6))
        ttk.Separator(db_select_row, orient="vertical").pack(
            side="left", fill="y", padx=(8, 10), pady=3,
        )
        self.index_btn = ttk.Button(
            db_select_row, text="⟳  Обновить индекс", command=self.start_index,
        )
        self.index_btn.pack(side="left")

        info_frame = ttk.Frame(db_frame, style="Inset.TFrame", padding=(8, 6))
        info_frame.pack(fill="x", pady=(5, 0))
        self.db_info_var = tk.StringVar(value="")
        ttk.Label(
            info_frame, textvariable=self.db_info_var, anchor="w", style="InsetMuted.TLabel",
        ).pack(fill="x")
        self.db_connection_var = tk.StringVar(value="Соединение: инициализация...")
        ttk.Label(
            info_frame, textvariable=self.db_connection_var, anchor="w", style="Inset.TLabel",
        ).pack(fill="x", pady=(2, 0))
        self.startup_progress_frame = ttk.Frame(info_frame, style="Inset.TFrame")
        self.startup_progress_label_var = tk.StringVar(value="")
        ttk.Label(
            self.startup_progress_frame, textvariable=self.startup_progress_label_var,
            anchor="w", style="Muted.TLabel",
        ).pack(side="left", fill="x", expand=True)
        self.startup_progress = ttk.Progressbar(
            self.startup_progress_frame, mode="indeterminate", length=170,
        )
        self.startup_progress.pack(side="right", padx=(8, 0))
        self.startup_progress_frame.pack_forget()
        self._refresh_database_combo()

        # --------------------------------------------------------
        # Нижняя полоса состояния закреплена у нижнего края окна.
        # Её нужно упаковать ДО растягиваемой области поиска/просмотра,
        # иначе Panedwindow может занять весь остаток и вытеснить футер.
        # --------------------------------------------------------
        status_frame = ttk.Frame(self.page, style="Header.TFrame", padding=(8, 3))
        status_frame.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(
            status_frame, textvariable=self.status_var, style="Status.TLabel",
            anchor="w", justify="left", wraplength=1050,
        )
        self.status_label.pack(fill="x", expand=True)

        hardware_status_frame = ttk.Frame(status_frame, style="Header.TFrame")
        hardware_status_frame.pack(fill="x")
        self.cpu_status_label = ttk.Label(
            hardware_status_frame, textvariable=self.cpu_status_var,
            style="Status.TLabel", anchor="w",
        )
        self.cpu_status_label.pack(side="left", padx=(0, 6))
        self.gpu_status_label = ttk.Label(
            hardware_status_frame, textvariable=self.gpu_status_var,
            style="Status.TLabel", anchor="w",
        )
        self.gpu_status_label.pack(side="left", padx=(0, 6))
        # Скорости сканирования отображаются только в верхней строке мониторинга,
        # чтобы не дублировать их рядом с CPU/GPU.

        self.progress = ttk.Progressbar(self.page, mode="determinate", style="TProgressbar")
        self.progress.pack(side="bottom", fill="x", pady=(0, 5))

        # --------------------------------------------------------
        # Область поиска и просмотра занимает только пространство НАД футером.
        # --------------------------------------------------------
        self.paned = ttk.Panedwindow(self.page, orient="horizontal")
        self.paned.pack(fill="both", expand=True, pady=(4, 8))
        # Компактная статистика вынесена в отдельную левую панель рядом с поиском.
        db_stats_frame = ttk.LabelFrame(
            self.paned, text="Статистика БД", padding=(6, 4), width=245,
        )
        # Фиксируем компактную ширину панели, но оставляем ей всю высоту рабочей области.
        db_stats_frame.pack_propagate(False)
        db_stats_frame.columnconfigure(1, weight=1)

        self.stats_message_label = ttk.Label(
            db_stats_frame, textvariable=self.stats_var, justify="left",
            anchor="w", wraplength=220,
        )
        self.stats_table = ttk.Frame(db_stats_frame)
        self.stats_table.pack(fill="x", anchor="nw")
        self.stats_table.columnconfigure(1, weight=1)

        self.stats_value_vars = {}
        self.stats_value_labels = {}
        stats_rows = [
            ("profiles", "Профилей", "e"),
            ("images", "Изображений", "e"),
            ("faces", "Лиц в БД", "e"),
            ("backend", "СУБД", "w"),
            ("profile", "Активный профиль", "w"),
            ("faiss_size", "Размер FAISS", "w"),
            ("faiss_state", "Состояние FAISS", "w"),
            ("gpu", "GPU search", "w"),
        ]
        for row_index, (key, title, anchor) in enumerate(stats_rows):
            ttk.Label(
                self.stats_table, text=title, anchor="w",
            ).grid(row=row_index, column=0, sticky="nw", padx=(1, 6), pady=1)
            value_var = tk.StringVar(value="—")
            self.stats_value_vars[key] = value_var
            value_label = ttk.Label(
                self.stats_table, textvariable=value_var,
                anchor=anchor, justify="left", wraplength=120,
            )
            value_label.grid(
                row=row_index, column=1, sticky="new", padx=(1, 1), pady=1,
            )
            self.stats_value_labels[key] = value_label

        # Строка состояния/ошибки отображается только при наличии сообщения.
        self.stats_var.trace_add("write", self._on_stats_message_changed)
        self._on_stats_message_changed()

        left = ttk.Frame(self.paned, style="Surface.TFrame", padding=(12, 10))
        right = ttk.Frame(self.paned, style="Surface.TFrame", padding=(12, 10))
        self.paned.add(db_stats_frame, weight=0)
        self.paned.add(left, weight=1)
        self.paned.add(right, weight=2)

        # Элементы управления поиском закреплены внизу левой панели, чтобы
        # не обрезаться при небольшой высоте окна. Сначала упаковываем нижние
        # элементы: Tkinter резервирует для них место до растягиваемого списка.
        self.search_btn = ttk.Button(
            left, text="⌕  Искать выбранное лицо", style="Primary.TButton", command=self.start_search,
        )
        self.search_btn.pack(side="bottom", fill="x", pady=(6, 0))

        controls = ttk.Frame(left, style="Surface.TFrame")
        controls.pack(side="bottom", fill="x", pady=(0, 8))
        controls.columnconfigure(0, weight=1, uniform="search_controls")
        controls.columnconfigure(1, weight=1, uniform="search_controls")

        topk_control = ttk.Frame(controls, style="Surface.TFrame")
        topk_control.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(
            topk_control, text="Количество результатов", style="SurfaceMuted.TLabel",
        ).pack(anchor="w", pady=(0, 3))
        self.topk_var = tk.IntVar(
            value=max(1, min(500, int(self.gui_settings.get("top_k", DEFAULT_TOP_K))))
        )
        self.topk_spinbox = ttk.Spinbox(
            topk_control, from_=1, to=500, textvariable=self.topk_var, width=12,
        )
        self.topk_spinbox.pack(fill="x")

        similarity_control = ttk.Frame(controls, style="Surface.TFrame")
        similarity_control.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(
            similarity_control, text="Минимальное сходство, %", style="SurfaceMuted.TLabel",
        ).pack(anchor="w", pady=(0, 3))
        self.min_similarity_var = tk.DoubleVar(
            value=max(0.0, min(100.0, float(self.gui_settings.get("min_similarity", 43.0))))
        )
        self.min_similarity_spinbox = ttk.Spinbox(
            similarity_control, from_=0, to=100, increment=1,
            textvariable=self.min_similarity_var, width=12,
        )
        self.min_similarity_spinbox.pack(fill="x")

        ttk.Label(left, text="Поиск по лицу", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            left, text="Выберите фотографию и лицо для поиска похожих изображений.",
            style="SurfaceMuted.TLabel", wraplength=520, justify="left",
        ).pack(anchor="w", pady=(2, 6))

        query_row = ttk.Frame(left, style="Surface.TFrame")
        query_row.pack(fill="x", pady=(0, 6))
        self.choose_query_btn = ttk.Button(
            query_row, text="▧  Выбрать фотографию", style="Primary.TButton", command=self.choose_query,
        )
        self.choose_query_btn.pack(side="left", padx=(0, 8))

        # Открывает повторно последний выбранный файл без диалога выбора.
        # Кнопка активируется только если путь задан и файл доступен.
        self.open_query_btn = ttk.Button(
            query_row, text="Открыть", command=self.open_query, state="disabled",
        )
        self.open_query_btn.pack(side="right", padx=(8, 0))

        saved_query = str(self.gui_settings.get("query_image") or "").strip()
        self.query_var = tk.StringVar(value=saved_query or "Файл не выбран")
        self.query_path_entry = ttk.Entry(
            query_row, textvariable=self.query_var, width=42,
        )
        self.query_path_entry.pack(side="left", fill="x", expand=True)

        # Явно обрабатываем горячие клавиши: копирование, вставку, вырезание
        # и выделение всего текста. У ttk.Entry штатного контекстного меню нет.
        for sequence, handler in (
            ("<Control-c>", self._query_entry_copy),
            ("<Control-C>", self._query_entry_copy),
            ("<Control-v>", self._query_entry_paste),
            ("<Control-V>", self._query_entry_paste),
            ("<Control-x>", self._query_entry_cut),
            ("<Control-X>", self._query_entry_cut),
            ("<Control-a>", self._query_entry_select_all),
            ("<Control-A>", self._query_entry_select_all),
            ("<Shift-F10>", self._show_query_entry_keyboard_menu),
        ):
            self.query_path_entry.bind(sequence, handler)

        # В русской раскладке keysym для Ctrl+C/V/X/A становится кириллическим.
        # Общий обработчик проверяет и Windows keycode, и фактический символ.
        self.query_path_entry.bind(
            "<Control-KeyPress>", self._query_entry_control_key, add="+",
        )

        self.query_entry_context_menu = tk.Menu(self.root, tearoff=False)
        self.query_entry_context_menu.add_command(
            label="Вырезать", command=self._query_entry_cut,
        )
        self.query_entry_context_menu.add_command(
            label="Копировать", command=self._query_entry_copy,
        )
        self.query_entry_context_menu.add_command(
            label="Вставить", command=self._query_entry_paste,
        )
        self.query_entry_context_menu.add_separator()
        self.query_entry_context_menu.add_command(
            label="Выделить всё", command=self._query_entry_select_all,
        )
        self.query_path_entry.bind("<Button-3>", self._show_query_entry_context_menu)

        self.root.after_idle(self._show_query_path_tail)
        # Проверяем сохранённый путь уже при построении формы: отсутствующий
        # или недоступный файл не должен оставлять кнопку активной.
        self._update_open_query_button_state()

        ttk.Label(left, text="Найденные лица", style="Section.TLabel").pack(anchor="w")
        self.face_list = tk.Listbox(
            left, height=3, exportselection=False, activestyle="none",
            selectmode="browse", relief="flat", borderwidth=0, highlightthickness=1,
        )
        self.face_list.pack(fill="both", expand=True, pady=(4, 4))
        self.face_list.bind("<<ListboxSelect>>", self.on_face_list_select)

        ttk.Label(right, text="Предпросмотр изображения", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            right, text="Нажмите на лицо на изображении, чтобы выбрать его.",
            style="SurfaceMuted.TLabel",
        ).pack(anchor="w", pady=(2, 8))
        canvas_frame = ttk.Frame(right, style="Surface.TFrame")
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            canvas_frame, background="#202020", highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Configure>", self._redraw_preview)

        self._update_backend_ui()

    # --------------------------------------------------------
    # GUI state
    # --------------------------------------------------------

    def _refresh_hardware_indicators(self):
        """Обновляет CPU/GPU-телеметрию из лёгкого кэша без блокирования Tk."""
        try:
            live_system = _read_cpu_ram_metrics()
            cpu_percent = live_system.get("cpu")
            ram_percent = live_system.get("ram")
            if cpu_percent is not None:
                self.cpu_status_var.set(
                    f"CPU: {float(cpu_percent):.0f}%"
                    + (f" • RAM {float(ram_percent):.0f}%" if ram_percent is not None else "")
                )
            else:
                self.cpu_status_var.set("CPU: загрузка недоступна")

            requested_backend = self.backend_mode_var.get()
            if self.cuda_available is None:
                self.gpu_status_var.set("GPU: проверка CUDA/ONNX Runtime...")
            elif not self.cuda_available:
                self.gpu_status_var.set("GPU: недоступен")
            elif requested_backend == BACKEND_CPU:
                self.gpu_status_var.set("GPU: отключён (CPU-only)")
            else:
                status = get_gpu_search_status()
                gpu_metrics = _read_gpu_system_metrics(refresh=False)
                try:
                    name = torch.cuda.get_device_name(GPU_INDEX_DEVICE) if torch is not None else "CUDA"
                except Exception:
                    name = "CUDA"

                util = gpu_metrics.get("gpu_util")
                if util is not None:
                    util_text = f"GPU {float(util):.0f}%"
                else:
                    util_text = "GPU util н/д (nvidia-smi недоступен)"
                used = gpu_metrics.get("gpu_mem_used_mb")
                total = gpu_metrics.get("gpu_mem_total_mb")
                if used is not None and total:
                    vram_text = f"VRAM {float(used) / 1024:.1f}/{float(total) / 1024:.1f} GB"
                else:
                    vram_text = "VRAM н/д"

                actual = face_app_backend(self.app)
                if actual == BACKEND_GPU:
                    state_text = "CUDA inference активен"
                elif self.gpu_index_loading:
                    state_text = "CUDA доступна; загрузка search index"
                else:
                    state_text = "CUDA доступна"
                if status.get("mode") == "streaming":
                    index_text = f" • поиск GPU чанками, FAISS в RAM ({status.get('count', 0):,})"
                    if actual == BACKEND_GPU:
                        state_text = "CUDA inference активен"
                elif status.get("loaded"):
                    index_text = f" • индекс VRAM ({status.get('count', 0):,})"
                elif status.get("error"):
                    index_text = " • FAISS GPU недоступен; CUDA inference отдельно"
                else:
                    index_text = " • FAISS GPU не загружен; CUDA inference доступен"
                self.gpu_status_var.set(f"{state_text} • {name} • {util_text} • {vram_text}{index_text}")
        except Exception as exc:
            self.cpu_status_var.set("CPU: состояние неизвестно")
            self.gpu_status_var.set(f"GPU: ошибка проверки ({exc})")
        try:
            self.root.after(1000, self._refresh_hardware_indicators)
        except tk.TclError:
            pass

    def _update_backend_ui(self):
        available = self.cuda_available
        if available is None:
            self.backend_gpu_radio.config(state="disabled")
            self.backend_status_var.set("Проверка CUDA/ONNX Runtime выполняется в фоне...")
            if self.backend_mode_var.get() not in (BACKEND_GPU, BACKEND_CPU):
                self.backend_mode_var.set(BACKEND_GPU)
            self.scan_btn.config(
                text="▶ Сканировать GPU" if self.backend_mode_var.get() == BACKEND_GPU else "▶ Сканировать CPU"
            )
            self._backend_runtime = self.backend_mode_var.get()
            return

        if available:
            self.backend_gpu_radio.config(state="normal")
            if self.backend_mode_var.get() not in (BACKEND_GPU, BACKEND_CPU):
                self.backend_mode_var.set(BACKEND_GPU)
            if self.backend_mode_var.get() == BACKEND_GPU:
                self.scan_btn.config(text="▶ Сканировать GPU")
                self.backend_status_var.set("CUDA доступна")
            else:
                self.scan_btn.config(text="▶ Сканировать CPU")
                self.backend_status_var.set("Выбран CPU: GPU не используется")
        else:
            self.backend_gpu_radio.config(state="disabled")
            self.backend_mode_var.set(BACKEND_CPU)
            self.scan_btn.config(text="▶ Сканировать CPU")
            detail = _CUDA_RUNTIME_ERROR or _cuda_version_diagnostic() or "не удалось загрузить CUDA Execution Provider"
            self.backend_status_var.set("CUDA недоступна — используется только CPU: " + detail)

        self._backend_runtime = self.backend_mode_var.get()

    def _on_backend_change(self):
        if self.busy:
            return

        mode = self.backend_mode_var.get()
        self._backend_runtime = mode
        if mode != BACKEND_GPU:
            self.gpu_index_loading = False
            self.startup_index_started = False
            self._hide_startup_progress()
            _clear_gpu_search_index(release_cuda_memory=True)
        if mode == BACKEND_GPU and self.cuda_available is not True:
            if self.cuda_available is False:
                self.backend_mode_var.set(BACKEND_CPU)
                self.backend_status_var.set("CUDA недоступна — выбор GPU отключён")
            else:
                self.backend_status_var.set("Проверка CUDA ещё выполняется; GPU станет доступен после завершения проверки")
            return

        # При переключении backend старая FaceAnalysis не должна оставаться
        # активной: следующая операция создаст ровно одну модель выбранного типа.
        old_app = self.app
        self.app = None
        if old_app is not None:
            try:
                del old_app
                gc.collect()
                if mode == BACKEND_CPU and torch is not None and torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

        self.query_faces = []
        self.selected_face_index = None
        self._redraw_preview()
        self._update_search_state()

        if mode == BACKEND_GPU:
            self.scan_btn.config(text="▶ Сканировать GPU")
            self.backend_status_var.set("Выбран GPU: CUDA → CPU fallback при ошибке")
            if Path(INDEX_FILE).exists() and not get_gpu_search_status().get("loaded"):
                self._start_gpu_index_load()
        else:
            self.scan_btn.config(text="▶ Сканировать CPU")
            _clear_gpu_search_index(release_cuda_memory=True)
            try:
                if torch is not None and torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            self.backend_status_var.set("Выбран CPU: GPU не используется")

        self._refresh_hardware_indicators()
        self.status_var.set(
            "Режим изменён. Для нового режима выберите фотографию заново."
        )

    def _set_busy(self, value):
        self.busy = value
        state = "disabled" if value else "normal"

        self.scan_btn.config(state=state)
        self.index_btn.config(state=state)
        self.repair_crops_btn.config(state=state)
        self.delete_processed_check.config(state=state)
        self.use_json_metadata_check.config(state=state)
        self.create_crop_files_check.config(state=state)
        self.use_local_crops_check.config(state=state)
        self.create_crops_on_html_check.config(state=state)
        # Новая настройка перезаписи должна блокироваться вместе с остальными
        # параметрами сканирования и индексации, чтобы флажок не менялся
        # посреди операции после того, как worker уже прочитал его значение.
        self.overwrite_existing_crops_check.config(state=state)
        self.scan_batch_size_spinbox.config(state=state)
        # Не меняем параметры автоподстройки посреди активной операции:
        # выбранный режим потоков фиксируется при запуске задачи.
        self.auto_workers_check.config(state=state)
        self.backend_cpu_radio.config(state=state)
        self.backend_gpu_radio.config(
            state=(
                "disabled"
                if value or self.cuda_available is not True
                else "normal"
            )
        )

        self.search_btn.config(
            state=(
                state
                if self.selected_face_index is not None
                else "disabled"
            )
        )

        scan_controls_enabled = bool(
            value and self.operation in ("scan", "crop_repair") and self.scan_running
        )
        self.pause_scan_btn.config(
            state="normal" if scan_controls_enabled else "disabled"
        )
        self.stop_scan_btn.config(
            state="normal" if scan_controls_enabled else "disabled"
        )
        # Остальные элементы главной формы тоже переводим в согласованное состояние.
        # Это не только визуальная защита: действия с БД не должны запускаться
        # одновременно с обработкой, когда worker использует текущий профиль.
        for name in (
            "choose_folder_btn", "choose_query_btn", "topk_spinbox",
            "min_similarity_spinbox", "db_combo", "connect_db_btn",
            "refresh_db_btn", "new_db_btn", "edit_db_btn",
            "delete_db_btn", "migrate_db_btn",
        ):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            try:
                # Открытие готового HTML безопасно во время вычислений; остальные
                # действия меняют источник/параметры или используют ресурсы БД.
                widget.configure(state="disabled" if value else "normal")
                if name == "db_combo" and not value:
                    widget.configure(state="readonly")
            except (tk.TclError, AttributeError):
                pass

        self._update_worker_control_state()


    def _show_scan_control_status(self, message: str):
        """Показывает краткий статус только активной операции сканирования/CROP."""
        label = getattr(self, "scan_control_status_label", None)
        if label is None:
            return
        self.scan_control_status_var.set(str(message or ""))
        if not label.winfo_manager():
            label.pack(side="left", padx=(12, 0))

    def _hide_scan_control_status(self):
        """Убирает статус сканирования/CROP в состоянии ожидания."""
        self.scan_control_status_var.set("")
        label = getattr(self, "scan_control_status_label", None)
        if label is not None and label.winfo_manager():
            label.pack_forget()

    def _set_auto_workers_status(self, message: str):
        """Отображает мониторинг и скорость сканирования в верхней строке."""
        label = getattr(self, "auto_workers_status_label", None)
        if label is None:
            return
        # При ручном сканировании тот же блок показывает telemetry, но не
        # меняет заданное пользователем количество потоков.
        is_active_scan = self.operation == "scan" and self.scan_running
        is_auto_crop_operation = (
            bool(self.auto_workers_var.get())
            and self.operation == "crop_repair"
            and self.scan_running
        )
        if not (is_active_scan or is_auto_crop_operation):
            self._hide_auto_workers_status()
            return
        self._scan_monitor_base_status = str(message or "")
        if is_active_scan:
            # AdaptiveWorkerController добавляет свою скорость изображений/с.
            # Для сканирования оставляем скорость из общего счётчика изображений
            # и лиц, иначе «изобр/с» показывается дважды в одной строке.
            self._scan_monitor_base_status = re.sub(
                r"\\s*•\\s*[-+]?\\d+(?:[.,]\\d+)?\\s*изобр/с\\s*•\\s*",
                " • ",
                self._scan_monitor_base_status,
                count=1,
            )
        # Пересчитываем окно и на очередном обновлении telemetry: если новые
        # результаты временно не поступают, скорость постепенно падает к нулю.
        if is_active_scan and hasattr(self, "_scan_rate_samples"):
            now = time.monotonic()
            window_seconds = max(0.001, float(getattr(self, "_scan_rate_window_seconds", 5.0)))
            cutoff = now - window_seconds
            while self._scan_rate_samples and self._scan_rate_samples[0][0] < cutoff:
                self._scan_rate_samples.popleft()
            images_in_window = sum(sample[1] for sample in self._scan_rate_samples)
            faces_in_window = sum(sample[2] for sample in self._scan_rate_samples)
            started_at = float(getattr(self, "_scan_rate_started_at", now))
            elapsed = min(window_seconds, max(0.001, now - started_at))
            self._scan_rate_text = (
                f"Скорость: {images_in_window / elapsed:.1f} изображений/с • "
                f"{faces_in_window / elapsed:.1f} лиц/с"
            )
        rate_text = str(getattr(self, "_scan_rate_text", "") or "").strip()
        display_text = self._scan_monitor_base_status
        if is_active_scan and rate_text:
            display_text = f"{display_text} | {rate_text}" if display_text else rate_text
        self.auto_workers_status_var.set(display_text)
        if not label.winfo_manager():
            label.pack(fill="x", padx=0, pady=(0, 3))

    def _set_crop_progress_status(self, message: str):
        """Показывает ход восстановления CROP в строке над сведениями об устройствах.

        Эта строка используется и при ручном количестве потоков: она отражает
        процесс CROP, а не остаётся с устаревшим сообщением о запуске AUTO.
        """
        label = getattr(self, "auto_workers_status_label", None)
        if label is None:
            return
        self.auto_workers_status_var.set(str(message or ""))
        if not label.winfo_manager():
            label.pack(fill="x", padx=0, pady=(0, 3))

    def _hide_auto_workers_status(self):
        """Очищает и скрывает адаптивный статус вне активной AUTO-задачи."""
        self.auto_workers_status_var.set("")
        label = getattr(self, "auto_workers_status_label", None)
        if label is not None and label.winfo_manager():
            label.pack_forget()

    def _prepare_scan_controls(self):
        self.scan_paused.set()
        self.scan_stop.clear()
        self.scan_running = True
        self.pause_scan_btn.config(
            state="normal",
            text="⏸ Пауза",
        )
        self.stop_scan_btn.config(state="normal")
        operation_label = "Проверка CROP" if self.operation == "crop_repair" else "Сканирование"
        self._show_scan_control_status(f"{operation_label} выполняется")

    def toggle_scan_pause(self):
        if not self.scan_running:
            return

        operation_label = "Проверка CROP" if self.operation == "crop_repair" else "Сканирование"
        if self.scan_paused.is_set():
            self.scan_paused.clear()
            self.pause_scan_btn.config(text="▶ Продолжить")
            self.scan_control_status_var.set(
                f"Пауза: {operation_label.lower()}, текущие задачи завершаются"
            )
            self.status_var.set(f"{operation_label} приостановлена...")
        else:
            self.scan_paused.set()
            self.pause_scan_btn.config(text="⏸ Пауза")
            self.scan_control_status_var.set(f"{operation_label} продолжается")
            self.status_var.set(f"Продолжаем: {operation_label.lower()}...")

    def stop_scan(self):
        if not self.scan_running:
            return

        operation_label = "проверку CROP" if self.operation == "crop_repair" else "сканирование"
        self.scan_stop.set()
        self.scan_paused.set()  # разблокировать ожидающие worker-ы, чтобы они вышли
        self.pause_scan_btn.config(state="disabled")
        self.stop_scan_btn.config(state="disabled")
        self.scan_control_status_var.set(
            f"Остановка: завершаются уже начатые задачи ({operation_label})..."
        )
        self.status_var.set(f"Останавливаем {operation_label}...")

    # --------------------------------------------------------
    # Local scan
    # --------------------------------------------------------

    def _update_worker_control_state(self):
        """Блокирует ручное поле потоков при включённой автоподстройке."""
        auto = bool(self.auto_workers_var.get())
        try:
            state = "disabled" if auto or self.busy else "normal"
            self.local_workers_spinbox.configure(state=state)
            # При активной операции нельзя переключать авто-режим: он уже
            # прочитан стартующим worker-ом и изменение не повлияет на задачу.
            self.auto_workers_check.configure(
                state="disabled" if self.busy else "normal"
            )
        except (AttributeError, tk.TclError):
            pass
    def _on_auto_workers_changed(self):
        # Сам отмеченный чекбокс уже показывает выбранный режим. До запуска
        # задачи не выводим ложный статус «выключена» или «подготовка».
        self._update_worker_control_state()
        self._hide_auto_workers_status()
        self._schedule_settings_save()

    def choose_folder(self):
        directory = filedialog.askdirectory(
            title="Папка с фотографиями"
        )

        if directory:
            self.selected_dir = Path(directory)
            self.folder_var.set(
                str(self.selected_dir)
            )
            self.status_var.set(
                "Папка выбрана."
            )
            self._schedule_settings_save()

    def start_scan(self):
        if not self._startup_ready_or_warn("сканирование"):
            return

        if self.busy:
            return

        if not self.selected_dir:
            messagebox.showwarning(
                "Сканирование",
                "Сначала выберите папку.",
            )
            return

        if not self.selected_dir.exists():
            messagebox.showerror(
                "Ошибка",
                "Папка не существует.",
            )
            return

        raw_batch_size = str(self.scan_batch_size_var.get() or "").strip()
        try:
            scan_batch_size = int(raw_batch_size) if raw_batch_size else 0
        except (TypeError, ValueError):
            messagebox.showerror(
                "Сканирование",
                "Размер пакета должен быть целым числом от 0 "
                f"до {MAX_SCAN_BATCH_SIZE:,}.",
            )
            return
        if scan_batch_size < 0 or scan_batch_size > MAX_SCAN_BATCH_SIZE:
            messagebox.showerror(
                "Сканирование",
                "Размер пакета должен быть от 0 "
                f"до {MAX_SCAN_BATCH_SIZE:,}.",
            )
            return

        auto_workers = bool(self.auto_workers_var.get())

        # Профиль среды определяем до запуска: это влияет только на стартовое
        # число потоков. После запуска AUTO продолжает подстраиваться по telemetry.
        selected_profile_name = str(self.db_profile_var.get() or self.connected_db_name or "")
        scan_profile = next(
            (
                profile for profile in getattr(self, "db_profiles", [])
                if str(profile.get("name") or "") == selected_profile_name
            ),
            None,
        )
        scan_profile = scan_profile or {}
        scan_db_type = str(scan_profile.get("type") or DB_TYPE_SQLITE).lower()
        scan_db_host = str(scan_profile.get("host") or "").strip().lower()
        remote_db = (
            scan_db_type != DB_TYPE_SQLITE
            and scan_db_host not in {"", "localhost", "127.0.0.1", "::1"}
        )
        if scan_db_type == DB_TYPE_SQLITE:
            sqlite_path = str(scan_profile.get("path") or "")
            remote_db = sqlite_path.startswith(("\\\\", "//"))

        if auto_workers:
            # Быстрый предварительный выбор по CPU не блокирует Tkinter.
            # Полный профиль GPU/диска/БД определяется в scan worker в фоне.
            _auto_min, workers, _auto_max = _auto_worker_limits()
        else:
            try:
                workers = int(self.local_workers_var.get())
            except (TypeError, ValueError):
                messagebox.showerror(
                    "Сканирование",
                    "Количество потоков должно быть числом.",
                )
                return

            if workers < 1:
                messagebox.showerror(
                    "Сканирование",
                    "Количество потоков должно быть больше нуля.",
                )
                return

            workers = min(workers, LOCAL_MAX_WORKERS)

        if bool(self.delete_processed_var.get()):
            confirmed = messagebox.askyesno(
                "Подтверждение удаления исходников",
                "Включено удаление исходников.\n\n"
                "Будут удаляться только пары «изображение + одноимённый JSON», "
                "обработанные с включённым параметром «Использовать JSON-описание». "
                "Пары, у которых лицо не найдено, тоже удаляются после штатной обработки.\n\n"
                "Локальные изображения без JSON не удаляются ни при каких условиях. "
                "Если при обработке возникнет ошибка, исходные файлы сохраняются.\n\n"
                "Продолжить сканирование?",
                parent=self.root,
            )
            if not confirmed:
                self.status_var.set("Сканирование отменено пользователем; исходники не удалялись.")
                return

        self.operation = "scan"
        self._prepare_scan_controls()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        requested_delete_processed = bool(self.delete_processed_var.get())
        use_json_metadata = bool(self.use_json_metadata_var.get())
        create_crop_files = bool(self.create_crop_files_var.get())
        overwrite_existing_crops = bool(self.overwrite_existing_crops_var.get())
        delete_processed = requested_delete_processed

        mode_text = (
            "JSON-описание"
            if use_json_metadata
            else "только лица + путь к файлу"
        )
        delete_text = (
            ", удаление исходников ВКЛ"
            if delete_processed
            else ", удаление исходников ВЫКЛ"
        )
        crop_text = (
            ", создание CROP ВКЛ"
            if create_crop_files
            else ", создание CROP ВЫКЛ (исходники сохраняются)"
        )
        overwrite_text = ", перезапись CROP ВКЛ" if overwrite_existing_crops else ""
        batch_text = (
            f", пакет файлов: {scan_batch_size}"
            if scan_batch_size > 0
            else f", все файлы (внутренний пакет {DEFAULT_SCAN_BATCH_SIZE})"
        )
        backend = self.backend_mode_var.get()
        backend_text = "GPU (CUDA, fallback CPU)" if backend == BACKEND_GPU else "CPU"
        worker_text = "AUTO" if auto_workers else str(workers)
        self.status_var.set(
            f"Сканирование HDD... {backend_text}; потоков: {worker_text}; "
            f"{mode_text}{delete_text}{crop_text}{overwrite_text}{batch_text}"
        )
        if auto_workers:
            self._set_auto_workers_status("Авто: запуск адаптивного контроллера...")
        else:
            self._set_auto_workers_status(
                f"Мониторинг: ручной режим, фиксировано {workers} поток(ов); сбор нагрузки включён"
            )

        threading.Thread(
            target=self._scan_worker,
            args=(
                self.selected_dir,
                workers,
                delete_processed,
                use_json_metadata,
                backend,
                auto_workers,
                create_crop_files,
                overwrite_existing_crops,
                remote_db,
                scan_batch_size,
            ),
            daemon=True,
        ).start()

    def _scan_worker(
        self,
        directory,
        workers,
        delete_processed=False,
        use_json_metadata=True,
        backend=BACKEND_GPU,
        auto_workers=False,
        create_crop_files=True,
        overwrite_existing_crops=False,
        remote_db=False,
        scan_batch_size=DEFAULT_SCAN_BATCH_SIZE,
    ):
        """Высокопроизводительный HDD -> CPU/GPU inference -> DB pipeline.

        Принципы:
        1. На GPU используется ограниченный пул FaceAnalysis/ONNX Runtime моделей:
           ориентир — одна модель на два worker-потока, с лимитом по VRAM.
        2. Если свободной видеопамяти недостаточно, потоки совместно используют
           уже созданные модели вместо отказа сканирования.
        3. На CPU остаётся один экземпляр модели, поскольку ONNX Runtime сам
           распараллеливает CPU-инференс.
        4. DB connection переиспользуется каждым worker-потоком.
        5. Тяжёлая очистка duplicate SHA выполняется только один раз до worker-ов.
        """
        try:
            # Потоково перечисляем каталог: не создаём список из сотен тысяч или
            # миллионов Path-объектов перед началом распознавания.
            effective_batch_size = (
                min(MAX_SCAN_BATCH_SIZE, int(scan_batch_size))
                if int(scan_batch_size or 0) > 0
                else DEFAULT_SCAN_BATCH_SIZE
            )
            self.events.put((
                "scan_startup_status",
                f"Поиск изображений; подготовка пакета до {effective_batch_size} файлов...",
            ))
            def report_discovery(entries_seen: int, images_found: int):
                self.events.put((
                    "scan_discovery_status",
                    int(entries_seen),
                    int(images_found),
                ))

            batch_iter = iter(_iter_image_batches(
                directory,
                effective_batch_size,
                status_callback=report_discovery,
                stop_check=self.scan_stop.is_set,
            ))
            first_batch = next(batch_iter, [])
            total = len(first_batch)
            self.events.put(("scan_total", total))
            self.events.put((
                "scan_startup_status",
                f"Подготовлено в первом пакете изображений: {total}. Проверка базы данных..."
                if total else "Изображения в выбранной папке не найдены.",
            ))
            if not first_batch:
                self.events.put(("scan_done", 0, 0, 0, False, 0, 0, 0))
                return

            def iter_scan_paths():
                nonlocal total
                yield from first_batch
                for batch in batch_iter:
                    total += len(batch)
                    self.events.put(("scan_discovered", total))
                    yield from batch

            # Схема и legacy CROP-пути проверяются ровно один раз за сканирование.
            # Рабочие потоки ниже открывают свои соединения без повторной миграции.
            init_conn = create_database(maintenance=False, initialize=True)
            try:
                pass
            finally:
                init_conn.close()

            self.events.put((
                "scan_startup_status",
                f"Первый пакет: {total} изображений. Подготовка модели распознавания...",
            ))

            # Сначала создаём базовую модель выбранного backend; дополнительные
            # GPU-модели при необходимости будут добавлены в ограниченный пул.
            app = create_face_app(backend)
            self.app = app
            actual_backend = face_app_backend(app)
            # Модель №1 уже загружена. Дополнительные сессии создаются только
            # для GPU, лениво, когда подключаются новые worker-потоки.
            model_pool = [app]
            model_pool_lock = threading.RLock()
            model_local = threading.local()
            model_pool_state = {
                "max_count": 1,
                "assigned_threads": 0,
                "capacity_exhausted": False,
            }

            def gpu_memory_info_mb():
                """Текущая свободная/общая VRAM; None означает неизвестное значение."""
                if actual_backend != BACKEND_GPU or torch is None:
                    return None, None
                try:
                    if not torch.cuda.is_available():
                        return None, None
                    free_bytes, total_bytes = torch.cuda.mem_get_info(GPU_INDEX_DEVICE)
                    return (
                        float(free_bytes) / (1024 ** 2),
                        float(total_bytes) / (1024 ** 2),
                    )
                except Exception:
                    return None, None

            def get_worker_app():
                """Назначает потоку свою модель; если VRAM мало — делит имеющиеся."""
                local_app = getattr(model_local, "app", None)
                if local_app is not None:
                    return local_app

                with model_pool_lock:
                    local_app = getattr(model_local, "app", None)
                    if local_app is not None:
                        return local_app

                    slot = model_pool_state["assigned_threads"]
                    model_pool_state["assigned_threads"] = slot + 1
                    preferred_index = slot // 2

                    while (
                        preferred_index >= len(model_pool)
                        and len(model_pool) < model_pool_state["max_count"]
                        and not model_pool_state["capacity_exhausted"]
                    ):
                        free_mb, _total_mb = gpu_memory_info_mb()
                        # Оставляем динамический запас 1.5 ГБ, но не ограничиваем
                        # 12-ГБ GPU четырьмя моделями: фактическая VRAM проверяется
                        # перед каждой загрузкой, поэтому пул растёт по реальной памяти.
                        if free_mb is None or free_mb < 1536:
                            model_pool_state["capacity_exhausted"] = True
                            if free_mb is None:
                                print(
                                    "[SCAN MODEL POOL] Свободная VRAM неизвестна; "
                                    "оставляем одну/уже созданные модели."
                                )
                            else:
                                print(
                                    "[SCAN MODEL POOL] Дополнительная модель не "
                                    f"создана: свободно VRAM {free_mb:.0f} MB."
                                )
                            break

                        model_number = len(model_pool) + 1
                        self.events.put((
                            "scan_startup_status",
                            f"Подготовка GPU-модели {model_number}/"
                            f"{model_pool_state['max_count']}...",
                        ))
                        try:
                            candidate = create_face_app(BACKEND_GPU)
                        except Exception as exc:
                            model_pool_state["capacity_exhausted"] = True
                            print(
                                "[SCAN MODEL POOL] Не удалось создать модель "
                                f"{model_number}: {type(exc).__name__}: {exc}"
                            )
                            break

                        if face_app_backend(candidate) != BACKEND_GPU:
                            model_pool_state["capacity_exhausted"] = True
                            print(
                                "[SCAN MODEL POOL] Дополнительная модель не "
                                "использовала CUDA; сохраняем текущий пул."
                            )
                            break

                        model_pool.append(candidate)
                        print(
                            f"[SCAN MODEL POOL] GPU-модель {len(model_pool)}/"
                            f"{model_pool_state['max_count']} готова."
                        )

                    if preferred_index < len(model_pool):
                        selected_app = model_pool[preferred_index]
                    else:
                        # VRAM-лимит достигнут: распределяем потоки по готовым
                        # моделям вместо прерывания сканирования.
                        selected_app = model_pool[slot % len(model_pool)]

                    model_local.app = selected_app
                    return selected_app

            self.events.put(("backend_actual", actual_backend, backend))

            total_faces = 0
            total_updated_crops = 0
            total_added_crops = 0
            total_removed_crops = 0
            overwrite_updated_label = "CROP заменено" if create_crop_files else "записей обновлено"
            overwrite_added_label = "новых" if create_crop_files else "новых записей"
            completed = 0
            db_local = threading.local()
            self._scan_worker_connections = []
            scan_connections_lock = threading.Lock()

            def get_worker_conn():
                conn = getattr(db_local, "conn", None)
                if conn is not None:
                    return conn

                # Ошибка открытия соединения не должна вылетать из Future без
                # привязки к файлу. Кратковременные сбои подключения повторяем.
                last_error = None
                for connection_attempt in range(3):
                    try:
                        # maintenance=False: обслуживание БД не запускается
                        # повторно на каждом изображении и в каждом worker-е.
                        conn = create_database(
                            maintenance=False,
                            initialize=False,
                        )
                        db_local.conn = conn
                        with scan_connections_lock:
                            self._scan_worker_connections.append(conn)
                        return conn
                    except Exception as exc:
                        last_error = exc
                        print(
                            "[SCAN DB] Не удалось открыть соединение "
                            f"(попытка {connection_attempt + 1}/3): "
                            f"{type(exc).__name__}: {exc}"
                        )
                        if connection_attempt < 2:
                            time.sleep(0.2 * (connection_attempt + 1))

                raise RuntimeError(
                    "Не удалось открыть соединение с БД после 3 попыток: "
                    f"{type(last_error).__name__}: {last_error}"
                ) from last_error

            def worker(path):
                # Пауза действует между изображениями; текущий inference не
                # прерываем, чтобы не оставлять незавершённую транзакцию/файл.
                if self.scan_stop.is_set():
                    return path, 0, None, {
                        "faces": 0,
                        "deleted": False,
                        "stopped": True,
                        "message": "остановлено до начала обработки",
                    }

                self.scan_paused.wait()

                if self.scan_stop.is_set():
                    return path, 0, None, {
                        "faces": 0,
                        "deleted": False,
                        "stopped": True,
                        "message": "остановлено до начала обработки",
                    }

                try:
                    # Сначала назначаем модель, поскольку создание ONNX-сессии
                    # может занять время; в этот момент DB-соединение ещё не открыто.
                    worker_app = get_worker_app()
                    conn = get_worker_conn()

                    # MySQL может вернуть ER_CHECKREAD (1020), когда несколько
                    # worker-ов одновременно обновляют одну строку profiles. MSSQL
                    # также может возвращать deadlock/lock timeout. Повторяем только
                    # распознанные временные конфликты и ограниченное число раз.
                    max_attempts = 4 if conn.backend in (DB_TYPE_MYSQL, DB_TYPE_MSSQL) else 1
                except Exception as exc:
                    # Это исключение раньше выходило из worker до его основного
                    # try/except, после чего интерфейс показывал «<unknown>».
                    try:
                        stale_conn = getattr(db_local, "conn", None)
                        if stale_conn is not None:
                            stale_conn.close()
                    except Exception:
                        pass
                    try:
                        del db_local.conn
                    except AttributeError:
                        pass
                    print(
                        f"[SCAN ERROR] {path}: {type(exc).__name__}: {exc}"
                    )
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
                    return path, 0, exc, {
                        "faces": 0,
                        "deleted": False,
                        "message": "ошибка подключения к БД",
                    }

                for attempt in range(max_attempts):
                    try:
                        json_path = path.with_suffix(".json")

                        if use_json_metadata and json_path.is_file():
                            try:
                                metadata = json.loads(json_path.read_text(encoding="utf-8"))
                            except Exception:
                                # import_image_bundle сформирует штатную ошибку JSON;
                                # здесь не скрываем детали парсинга.
                                metadata = None
                            profile_lock = _import_profile_lock_for_json(
                                json_path, metadata=metadata
                            )
                            if profile_lock is None:
                                result = import_image_bundle(
                                    conn,
                                    worker_app,
                                    path,
                                    json_path,
                                    delete_source=delete_processed,
                                    create_crop_files=create_crop_files,
                                    overwrite_existing_crops=overwrite_existing_crops,
                                    metadata=metadata,
                                )
                            else:
                                # Удерживаем блокировку до завершения транзакции импорта,
                                # чтобы одинаковый url_source не создавался одновременно.
                                with profile_lock:
                                    try:
                                        result = import_image_bundle(
                                            conn,
                                            worker_app,
                                            path,
                                            json_path,
                                            delete_source=delete_processed,
                                            create_crop_files=create_crop_files,
                                            overwrite_existing_crops=overwrite_existing_crops,
                                            metadata=metadata,
                                        )
                                    except Exception:
                                        # Откатываем транзакцию до освобождения lock:
                                        # следующий файл того же профиля не должен
                                        # читать незавершённое состояние предыдущего.
                                        try:
                                            conn.rollback()
                                        except Exception:
                                            pass
                                        raise
                            count = int(result["faces"])
                            return path, count, None, result

                        result = process_local_image(
                            conn,
                            worker_app,
                            path,
                            delete_source=delete_processed,
                            create_crop_files=create_crop_files,
                            overwrite_existing_crops=overwrite_existing_crops,
                        )
                        return path, int(result["faces"]), None, result

                    except Exception as exc:
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                        if (
                            conn.backend not in (DB_TYPE_MYSQL, DB_TYPE_MSSQL)
                            or attempt + 1 >= max_attempts
                        ):
                            return path, 0, exc, {
                                "faces": 0,
                                "deleted": False,
                            }

                        error_text = str(exc).lower()
                        error_code = None
                        try:
                            if getattr(exc, "args", None):
                                error_code = int(exc.args[0])
                        except (TypeError, ValueError):
                            pass
                        transient = (
                            error_code in {1020, 1205, 1206, 1213}
                            or any(
                                marker in error_text
                                for marker in (
                                    "record has changed since last read",
                                    "deadlock",
                                    "1020",
                                    "1205",
                                    "1206",
                                    "1213",
                                    "lock wait timeout",
                                    "lock request time out",
                                    "1222",
                                    "timeout expired",
                                    "hyt00",
                                    "40001",
                                )
                            )
                        )
                        if not transient:
                            return path, 0, exc, {
                                "faces": 0,
                                "deleted": False,
                            }

                        print(
                            f"[SCAN DB RETRY] {path.name}: временный конфликт БД "
                            f"({error_code or type(exc).__name__}), "
                            f"повтор {attempt + 2}/{max_attempts}"
                        )
                        time.sleep(0.15 * (attempt + 1))

                return path, 0, RuntimeError(
                    "Не удалось завершить запись в БД после повторных попыток."
                ), {"faces": 0, "deleted": False}

            # Для AUTO pool создаём только один раз с верхним пределом, а фактическое
            # количество одновременно работающих задач регулируем limiter-ом. Это
            # позволяет менять число потоков без пересоздания ThreadPoolExecutor.
            controller = AdaptiveWorkerController(
                # 0 означает использовать стартовую оценку после определения
                # фактического backend, накопителя и типа БД в фоновом потоке.
                initial_workers=0 if auto_workers else workers,
                backend=actual_backend,
                operation="scan",
                input_paths=[directory],
                output_path=CROP_DIR,
                remote_db=remote_db,
                auto_adjust=auto_workers,
            )
            if auto_workers:
                workers = controller.current_workers
            limiter = AdaptiveConcurrencyLimiter(controller.current_workers)
            if auto_workers:
                initial_status = (
                    f"Авто: старт {controller.current_workers} из {controller.max_workers} потоков"
                )
            else:
                initial_status = (
                    f"Мониторинг (ручной режим): фиксировано {workers} поток(ов)"
                )
            self.events.put(("auto_workers_status", initial_status))

            def adaptive_worker(path):
                acquired = False
                try:
                    if limiter is not None:
                        acquired = limiter.acquire(self.scan_stop)
                        if not acquired:
                            return path, 0, None, {
                                "faces": 0,
                                "deleted": False,
                                "stopped": True,
                                "message": "остановлено до начала обработки",
                            }

                    try:
                        return worker(path)
                    except Exception as exc:
                        # Последний защитный уровень: даже неожиданная ошибка
                        # из limiter/worker-а должна сохранить конкретный путь.
                        print(
                            f"[SCAN ERROR] {path}: {type(exc).__name__}: {exc}"
                        )
                        traceback.print_exception(type(exc), exc, exc.__traceback__)
                        return path, 0, exc, {
                            "faces": 0,
                            "deleted": False,
                            "message": "внутренняя ошибка worker",
                        }
                except Exception as exc:
                    print(
                        f"[SCAN ERROR] {path}: {type(exc).__name__}: {exc}"
                    )
                    traceback.print_exception(type(exc), exc, exc.__traceback__)
                    return path, 0, exc, {
                        "faces": 0,
                        "deleted": False,
                        "message": "ошибка запуска worker-а",
                    }
                finally:
                    if limiter is not None and acquired:
                        try:
                            limiter.release()
                        except Exception as exc:
                            # Сбой release не должен заменять результат изображения.
                            print(
                                "[SCAN WARN] Ошибка освобождения лимитера: "
                                f"{type(exc).__name__}: {exc}"
                            )

            executor_max_workers = controller.max_workers if controller is not None else workers

            # Верхний предел пула определяется количеством возможных worker-ов
            # и объёмом VRAM. Модели создаются лениво, только когда нужны потоки.
            if actual_backend == BACKEND_GPU:
                _free_mb, total_vram_mb = gpu_memory_info_mb()
                if total_vram_mb is None:
                    metrics = _read_gpu_system_metrics(refresh=True)
                    try:
                        total_vram_mb = float(metrics.get("gpu_mem_total_mb") or 0.0)
                    except (TypeError, ValueError):
                        total_vram_mb = 0.0

                # Это верхний защитный предел, а не целевое число моделей.
                # Фактическое число дополнительно ограничивается свободной VRAM
                # перед каждой инициализацией CUDA-сессии.
                if total_vram_mb >= 24576:
                    vram_model_cap = 12
                elif total_vram_mb >= 16384:
                    vram_model_cap = 10
                elif total_vram_mb >= 12288:
                    # В том числе RTX 3060 12 GB: больше прежнего лимита в 4 модели.
                    vram_model_cap = 8
                elif total_vram_mb >= 8192:
                    vram_model_cap = 6
                elif total_vram_mb >= 6144:
                    vram_model_cap = 4
                elif total_vram_mb >= 4096:
                    vram_model_cap = 2
                else:
                    # Для GPU с малым объёмом VRAM оставляем одну модель.
                    vram_model_cap = 1

                model_pool_state["max_count"] = max(
                    1,
                    min((executor_max_workers + 1) // 2, vram_model_cap),
                )
            else:
                model_pool_state["max_count"] = 1

            self.events.put((
                "scan_startup_status",
                f"Пул моделей: 1/{model_pool_state['max_count']}; "
                "дополнительные модели будут загружаться по мере необходимости.",
            ))

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=executor_max_workers,
                thread_name_prefix="gpu-face",
            ) as executor:
                path_iter = iter_scan_paths()
                pending = set()
                # Future -> исходный файл: даже при непредвиденном исключении
                # не теряем имя изображения и не показываем «<unknown>».
                future_paths = {}

                # Очередь небольшая: достаточно держать executor занятым, но не
                # резервировать память под огромное количество Future.
                preload = max(
                    executor_max_workers * 2,
                    executor_max_workers + 2,
                )
                for _ in range(preload):
                    try:
                        path = next(path_iter)
                    except StopIteration:
                        break
                    future = executor.submit(adaptive_worker, path)
                    pending.add(future)
                    future_paths[future] = path

                while pending:
                    done, pending = concurrent.futures.wait(
                        pending,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )

                    for future in done:
                        completed += 1

                        expected_path = future_paths.pop(future, None)
                        try:
                            result_path, count, error, import_result = future.result()
                        except Exception as exc:
                            result_path = expected_path or Path("<unknown>")
                            count = 0
                            error = exc
                            import_result = {}
                            print(
                                f"[SCAN FUTURE ERROR] {result_path}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            traceback.print_exception(
                                type(exc), exc, exc.__traceback__
                            )

                        if error is not None:
                            error_message = str(error).strip()
                            error_detail = f"{type(error).__name__}: {error_message}"
                            if not error_message:
                                error_detail += " (исключение без текстового сообщения)"
                            # Ошибки не должны теряться, когда следующее обновление
                            # прогресса меняет строку состояния GUI.
                            print(f"[SCAN ERROR] {result_path}: {error_detail}")
                            if isinstance(error, BaseException) and error.__traceback__ is not None:
                                traceback.print_exception(
                                    type(error), error, error.__traceback__
                                )
                            self.events.put((
                                "scan_error",
                                str(result_path),
                                error_detail,
                            ))
                        else:
                            total_faces += count
                            updated_crops = int(import_result.get("updated", 0) or 0)
                            added_crops = int(import_result.get("added", count if not overwrite_existing_crops else 0) or 0)
                            removed_crops = int(import_result.get("removed", 0) or 0)
                            if overwrite_existing_crops:
                                total_updated_crops += updated_crops
                                total_added_crops += added_crops
                                total_removed_crops += removed_crops
                                details = (
                                    f"{overwrite_updated_label}: {updated_crops}; "
                                    f"{overwrite_added_label}: {added_crops}; "
                                    f"удалено лишних: {removed_crops}; лиц={count}"
                                )
                                result_note = str(import_result.get("message") or "").strip()
                                if count == 0 and result_note and result_note != "ok":
                                    details += f" ({result_note})"
                                message = f"{result_path.name}: {details}"
                            elif import_result.get("stopped"):
                                message = f"{result_path.name}: пропущено из-за остановки"
                            elif import_result.get("deleted"):
                                message = (
                                    f"{result_path.name}: лиц={count}, "
                                    "исходник удалён"
                                )
                            elif count == 0 and import_result.get("message"):
                                message = (
                                    f"{result_path.name}: "
                                    f"{import_result['message']}"
                                )
                            else:
                                message = f"{result_path.name}: лиц={count}"

                            # Для скорости используем лица, найденные детектором,
                            # а не только новые CROP/строки, добавленные в БД.
                            # None означает, что модель не запускалась (например,
                            # исходник и его CROP уже были обработаны ранее).
                            detected_count = import_result.get("detected_faces")
                            if detected_count is not None:
                                try:
                                    detected_count = max(0, int(detected_count))
                                except (TypeError, ValueError):
                                    detected_count = 0

                            self.events.put((
                                "scan_progress",
                                completed,
                                total,
                                count,
                                message,
                                detected_count,
                            ))

                        # Контроллер получает реальную скорость завершения изображений
                        # и снимки нагрузки CPU/RAM/HDD/NET/GPU.
                        if controller is not None and not self.scan_stop.is_set():
                            report = controller.observe(completed)
                            if report is not None:
                                limiter.set_target(int(report["workers"]))
                                self.events.put((
                                    "auto_workers_status",
                                    controller.status_text(report),
                                ))

                        # После Stop новые файлы больше не подаём. После Pause
                        # также не пополняем очередь; уже поданные worker-ы корректно
                        # завершатся/перейдут в ожидание паузы.
                        if not self.scan_stop.is_set() and self.scan_paused.is_set():
                            try:
                                next_path = next(path_iter)
                            except StopIteration:
                                next_path = None

                            if next_path is not None:
                                future = executor.submit(adaptive_worker, next_path)
                                pending.add(future)
                                future_paths[future] = next_path

            stopped = self.scan_stop.is_set()
            if controller is not None:
                final_report = controller.observe(completed)
                if final_report is not None:
                    self.events.put(("auto_workers_status", controller.status_text(final_report)))
                final_prefix = "Авто: завершено" if auto_workers else "Мониторинг: завершено"
                final_workers_text = (
                    f"итоговая настройка {controller.current_workers} потоков"
                    if auto_workers else f"ручная настройка сохранена: {workers} поток(ов)"
                )
                self.events.put((
                    "auto_workers_status",
                    f"{final_prefix}, {final_workers_text}"
                ))
            self.events.put((
                "scan_done", total_faces, completed, total, stopped,
                total_updated_crops, total_added_crops, total_removed_crops,
            ))

            # Закрываем переиспользованные connections worker-ов.
            connections = getattr(self, "_scan_worker_connections", [])
            for conn in connections:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                delattr(self, "_scan_worker_connections")
            except Exception:
                pass

        except Exception as exc:
            # Даже при аварийном завершении не оставляем открытые ODBC
            # connections SQL Server.
            connections = getattr(self, "_scan_worker_connections", [])
            for conn in connections:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                delattr(self, "_scan_worker_connections")
            except Exception:
                pass

            self.scan_running = False
            self.events.put(("fatal", str(exc)))

    # --------------------------------------------------------
    # Query image
    # --------------------------------------------------------

    def _query_entry_control_key(self, event=None):
        """Обрабатывает Ctrl+C/V/X/A независимо от раскладки клавиатуры."""
        if event is None:
            return None

        # На Windows keycode обычно остаётся VK-кодом физической клавиши
        # (A=65, C=67, V=86, X=88), даже когда символ раскладки кириллический.
        keycode = getattr(event, "keycode", None)
        handlers_by_keycode = {
            67: self._query_entry_copy,
            86: self._query_entry_paste,
            88: self._query_entry_cut,
            65: self._query_entry_select_all,
        }
        handler = handlers_by_keycode.get(keycode)

        # Резервная проверка для систем/Tk-сборок, где keycode зависит от раскладки.
        if handler is None:
            keys = {
                "c": self._query_entry_copy,
                "с": self._query_entry_copy,
                "cyrillic_es": self._query_entry_copy,
                "v": self._query_entry_paste,
                "м": self._query_entry_paste,
                "cyrillic_em": self._query_entry_paste,
                "x": self._query_entry_cut,
                "ч": self._query_entry_cut,
                "cyrillic_che": self._query_entry_cut,
                "a": self._query_entry_select_all,
                "ф": self._query_entry_select_all,
                "cyrillic_ef": self._query_entry_select_all,
            }
            for key in (
                str(getattr(event, "keysym", "") or "").lower(),
                str(getattr(event, "char", "") or "").lower(),
            ):
                handler = keys.get(key)
                if handler is not None:
                    break

        if handler is None:
            # Не мешаем другим Ctrl-сочетаниям, например Ctrl+стрелка.
            return None
        return handler(event)

    def _query_entry_copy(self, event=None):
        """Копирует выделенный текст из поля пути/URL в системный буфер."""
        entry = getattr(self, "query_path_entry", None)
        if entry is None:
            return "break"
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
            selected = entry.get()[start:end]
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            # Нет выделения или системный буфер недоступен.
            pass
        return "break"

    def _query_entry_cut(self, event=None):
        """Вырезает выделенный текст, сохраняя его в системном буфере."""
        entry = getattr(self, "query_path_entry", None)
        if entry is None:
            return "break"
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
        except tk.TclError:
            return "break"
        self._query_entry_copy()
        entry.delete(start, end)
        return "break"

    def _query_entry_paste(self, event=None):
        """Вставляет текст из системного буфера вместо выделенного фрагмента."""
        entry = getattr(self, "query_path_entry", None)
        if entry is None:
            return "break"
        try:
            value = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        try:
            start = entry.index("sel.first")
            end = entry.index("sel.last")
            entry.delete(start, end)
        except tk.TclError:
            pass
        entry.insert("insert", value)
        entry.xview("insert")
        return "break"

    def _query_entry_select_all(self, event=None):
        """Выделяет весь текст в поле пути/URL."""
        entry = getattr(self, "query_path_entry", None)
        if entry is not None:
            entry.selection_range(0, "end")
            entry.icursor("end")
            entry.focus_set()
        return "break"

    def _show_query_entry_context_menu(self, event):
        """Показывает стандартные команды редактирования по правому клику."""
        entry = getattr(self, "query_path_entry", None)
        menu = getattr(self, "query_entry_context_menu", None)
        if entry is None or menu is None:
            return "break"
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _show_query_entry_keyboard_menu(self, event=None):
        """Открывает контекстное меню с клавиатуры (Shift+F10)."""
        entry = getattr(self, "query_path_entry", None)
        if entry is None:
            return "break"
        try:
            x = entry.winfo_rootx() + min(entry.winfo_width() - 1, 40)
            y = entry.winfo_rooty() + entry.winfo_height()
            self.query_entry_context_menu.tk_popup(x, y)
        finally:
            try:
                self.query_entry_context_menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _show_query_path_tail(self):
        """Показывает конец длинного пути в однострочном поле выбора файла."""
        entry = getattr(self, "query_path_entry", None)
        if entry is None:
            return
        try:
            entry.xview_moveto(1.0)
        except tk.TclError:
            pass

    def _query_file_is_available(self) -> bool:
        """Проверяет введённый локальный путь или прямой HTTP(S)-адрес."""
        value = str(self.query_var.get() or "").strip() if hasattr(self, "query_var") else ""
        parsed = urlparse(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            return True
        if not value or value == "Файл не выбран":
            return False
        try:
            path = Path(value).expanduser()
            return path.is_file() and os.access(str(path), os.R_OK)
        except (OSError, TypeError, ValueError):
            return False

    def _update_open_query_button_state(self):
        """Кнопка остаётся активной, чтобы можно было сначала вставить URL."""
        button = getattr(self, "open_query_btn", None)
        if button is None:
            return
        try:
            button.configure(state="normal")
        except tk.TclError:
            pass

    def open_query(self):
        """Открывает локальное изображение или загружает изображение по HTTP(S)-URL."""
        if not self._startup_ready_or_warn("открытие выбранного изображения"):
            return

        value = str(self.query_var.get() or "").strip()
        if not value or value == "Файл не выбран":
            self.status_var.set("Укажите путь к файлу или HTTP(S)-адрес изображения.")
            return

        parsed = urlparse(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            try:
                request = Request(value, headers={"User-Agent": "FaceSearch/1.0"})
                with urlopen(request, timeout=25) as response:
                    content_type = str(response.headers.get("Content-Type", "")).lower()
                    data = response.read(25 * 1024 * 1024 + 1)
                if len(data) > 25 * 1024 * 1024:
                    raise RuntimeError("Размер изображения превышает лимит 25 МБ.")
                if content_type and not content_type.startswith("image/"):
                    raise RuntimeError(f"Ссылка вернула не изображение (Content-Type: {content_type}).")
                from io import BytesIO
                with Image.open(BytesIO(data)) as source_image:
                    source_image.load()
                    image = source_image.convert("RGB")
                cache_dir = APP_DIR / "query_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_name = hashlib.sha256(value.encode("utf-8")).hexdigest() + ".jpg"
                cached_path = cache_dir / cache_name
                image.save(cached_path, format="JPEG", quality=95)
                self.query_source_url = value
                self.query_path = cached_path
            except Exception as exc:
                messagebox.showerror("Ошибка загрузки изображения", f"Не удалось загрузить изображение по ссылке:\n{exc}")
                self.status_var.set("Не удалось загрузить изображение по URL.")
                return
        else:
            path = Path(value).expanduser()
            if not path.is_file() or not os.access(str(path), os.R_OK):
                self.status_var.set("Указанный файл отсутствует или недоступен.")
                self._update_open_query_button_state()
                return
            self.query_path = path
            self.query_source_url = ""

        self._schedule_settings_save()
        self._detect_query_faces()

    def choose_query(self):
        if not self._startup_ready_or_warn("выбор изображения"):
            return

        path = filedialog.askopenfilename(
            title="Выберите фотографию для поиска",
            filetypes=[
                (
                    "Изображения",
                    "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff",
                ),
                (
                    "Все файлы",
                    "*.*",
                ),
            ],
        )

        if not path:
            return

        self.query_path = Path(path)
        self.query_source_url = ""
        self.query_var.set(str(self.query_path))
        self.root.after_idle(self._show_query_path_tail)
        self._update_open_query_button_state()
        self._schedule_settings_save()
        self._detect_query_faces()

    def _ensure_app(self):
        if self.app is None:
            backend = self.backend_mode_var.get()
            self.status_var.set(
                "Загрузка InsightFace CUDA..."
                if backend == BACKEND_GPU
                else "Загрузка InsightFace CPU..."
            )
            self.root.update_idletasks()
            self.app = create_face_app(backend)
            actual = face_app_backend(self.app)
            self._handle_actual_backend(actual, backend)
        return self.app

    def _handle_actual_backend(self, actual_backend: str, requested_backend: str):
        if actual_backend == BACKEND_GPU:
            self.backend_status_var.set("Активен GPU: CUDAExecutionProvider (проверен)")
        else:
            if requested_backend == BACKEND_GPU:
                detail = _CUDA_RUNTIME_ERROR or "ONNX Runtime CUDA недоступен"
                self.backend_status_var.set(
                    "GPU недоступен — активен CPU fallback: " + detail
                )
            else:
                self.backend_status_var.set(
                    "Активен CPU: GPU не используется"
                )
        try:
            self._refresh_hardware_indicators()
        except Exception:
            pass

    def _get_worker_app(self):
        # ВАЖНО: НЕ создаём модель на каждый worker.
        # Все worker-потоки используют один и тот же FaceAnalysis/ORT session.
        return self._ensure_app()

    def _start_gpu_index_load(self):
        if getattr(self, "gpu_index_loading", False):
            return
        if self.backend_mode_var.get() != BACKEND_GPU:
            self.gpu_index_loading = False
            self._update_database_info()
            return

        profile_name = str(getattr(self, "connected_db_name", self.active_db_name))
        index_path = str(Path(INDEX_FILE).resolve())
        self.gpu_index_loading = True
        try:
            size = Path(index_path).stat().st_size
            size_text = f"{size / (1024 ** 3):.2f} GB"
        except OSError:
            size_text = "размер неизвестен"
        self._show_startup_progress(
            f"FAISS для поиска: {Path(index_path).name} • {size_text}"
        )
        self.status_var.set(
            "База данных готова. Загружается FAISS для поиска: "
            f"{Path(index_path).name} • {size_text}. "
            "Это отдельный этап подготовки поиска, анимация исчезнет после его завершения."
        )
        self._update_database_info()

        threading.Thread(
            target=self._gpu_index_load_worker,
            args=(profile_name, index_path),
            daemon=True,
            name=f"gpu-index-loader-{profile_name}",
        ).start()

    def _gpu_index_load_worker(self, profile_name: str, index_path: str):
        try:
            if self._backend_runtime != BACKEND_GPU:
                return

            if not Path(index_path).exists():
                self.events.put((
                    "gpu_index_done",
                    False,
                    get_gpu_search_status(),
                    profile_name,
                    index_path,
                ))
                return

            ok = _load_gpu_search_index_from_faiss(
                index_path,
                progress_callback=lambda message: self.events.put((
                    "gpu_index_progress", profile_name, index_path, str(message)
                )),
            )

            # Не позволяем завершившемуся старому потоку заменить индекс
            # уже подключённого другого профиля.
            current_index_path = str(Path(INDEX_FILE).resolve())
            if (
                self._backend_runtime != BACKEND_GPU
                or str(getattr(self, "connected_db_name", "")) != profile_name
                or current_index_path != index_path
            ):
                _clear_gpu_search_index(release_cuda_memory=True)
                return

            self.events.put((
                "gpu_index_done",
                ok,
                get_gpu_search_status(),
                profile_name,
                index_path,
            ))
        except Exception as exc:
            self.events.put((
                "gpu_index_done",
                False,
                {"error": str(exc)},
                profile_name,
                index_path,
            ))

    def _detect_query_faces(self):
        try:
            self._ensure_app()

            image = load_image(
                self.query_path
            )

            if image is None:
                raise RuntimeError(
                    "Не удалось открыть изображение."
                )

            self.query_image = image
            self.query_faces = _detect_faces_with_padding(
                self.app,
                image,
                padding_ratio=0.40,
            )

            self.face_list.delete(
                0,
                tk.END,
            )

            valid = []

            for i, face in enumerate(
                self.query_faces
            ):
                if face.det_score < DET_THRESHOLD:
                    continue

                valid.append(i)

                b = face.bbox.astype(int)

                self.face_list.insert(
                    tk.END,
                    f"Лицо #{i + 1} "
                    f"confidence={face.det_score:.3f} "
                    f"[{b[0]}, {b[1]}, {b[2]}, {b[3]}]",
                )

            self.selected_face_index = None

            if valid:
                self.face_list.selection_set(0)
                self.on_face_list_select()

                self.status_var.set(
                    f"Найдено лиц: {len(valid)}. "
                    "Выберите лицо мышью или в списке."
                )
            else:
                self._redraw_preview()
                self.status_var.set(
                    "Лица не найдены."
                )

        except Exception as e:
            messagebox.showerror(
                "Ошибка",
                str(e),
            )
            self.status_var.set(
                "Ошибка определения лиц."
            )

    # --------------------------------------------------------
    # Preview
    # --------------------------------------------------------

    def _redraw_preview(self, event=None):
        self.canvas.delete("all")

        if self.query_image is None:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            self.canvas.create_text(
                w / 2,
                h / 2,
                text="Выберите фотографию",
                fill="#888888",
                font=("Segoe UI", 16),
                anchor="center",   # центр текста в указанной точке
            )
            return

        h, w = self.query_image.shape[:2]

        cw = max(
            100,
            self.canvas.winfo_width(),
        )
        ch = max(
            100,
            self.canvas.winfo_height(),
        )

        scale = min(
            cw / w,
            ch / h,
        )

        self.preview_scale = scale

        nw = max(
            1,
            int(w * scale),
        )
        nh = max(
            1,
            int(h * scale),
        )

        rgb = cv2.cvtColor(
            self.query_image,
            cv2.COLOR_BGR2RGB,
        )

        pil = Image.fromarray(
            rgb
        ).resize(
            (nw, nh),
            Image.Resampling.LANCZOS,
        )

        self.preview_photo = ImageTk.PhotoImage(
            pil
        )

        ox = (cw - nw) // 2
        oy = (ch - nh) // 2

        self.preview_offset = (
            ox,
            oy,
        )

        self.canvas.create_image(
            ox,
            oy,
            anchor="nw",
            image=self.preview_photo,
        )

        for i, face in enumerate(
            self.query_faces
        ):
            if face.det_score < DET_THRESHOLD:
                continue

            x1, y1, x2, y2 = (
                face.bbox.astype(float)
            )

            sx1 = ox + x1 * scale
            sy1 = oy + y1 * scale
            sx2 = ox + x2 * scale
            sy2 = oy + y2 * scale

            selected = (
                i == self.selected_face_index
            )

            width = (
                4 if selected else 2
            )

            outline = (
                "#00ff66"
                if selected
                else "#ffcc00"
            )

            self.canvas.create_rectangle(
                sx1,
                sy1,
                sx2,
                sy2,
                outline=outline,
                width=width,
            )

            self.canvas.create_text(
                sx1 + 5,
                sy1 + 5,
                anchor="nw",
                text=(
                    f"#{i + 1} "
                    f"{face.det_score:.2f}"
                ),
                fill=outline,
                font=(
                    "Segoe UI",
                    10,
                    "bold",
                ),
            )

    def _face_at_canvas(self, x, y):
        if self.query_image is None:
            return None

        ox, oy = self.preview_offset
        scale = self.preview_scale

        if scale <= 0:
            return None

        ix = (x - ox) / scale
        iy = (y - oy) / scale

        for i, face in enumerate(
            self.query_faces
        ):
            if face.det_score < DET_THRESHOLD:
                continue

            x1, y1, x2, y2 = face.bbox

            if (
                x1 <= ix <= x2
                and y1 <= iy <= y2
            ):
                return i

        return None

    def on_canvas_click(self, event):
        index = self._face_at_canvas(
            event.x,
            event.y,
        )

        if index is None:
            return

        self.selected_face_index = index

        self.face_list.selection_clear(
            0,
            tk.END,
        )

        self.face_list.selection_set(
            index
        )

        self.face_list.see(index)

        self._redraw_preview()
        self._update_search_state()

    def on_face_list_select(self, event=None):
        sel = self.face_list.curselection()

        if not sel:
            return

        visible = [
            i
            for i, face in enumerate(
                self.query_faces
            )
            if face.det_score >= DET_THRESHOLD
        ]

        if sel[0] < len(visible):
            self.selected_face_index = (
                visible[sel[0]]
            )

            self._redraw_preview()
            self._update_search_state()

    def _update_search_state(self):
        self.search_btn.config(
            state=(
                "disabled"
                if (
                    self.busy
                    or self.selected_face_index
                    is None
                )
                else "normal"
            )
        )

    # --------------------------------------------------------
    # FAISS / Search
    # --------------------------------------------------------

    def start_index(self):
        if not self._startup_ready_or_warn("создание FAISS-индекса"):
            return

        if self.busy:
            return

        self.operation = "index"
        self._set_busy(True)

        self.progress.config(
            mode="indeterminate"
        )
        self.progress.start(10)

        if self.backend_mode_var.get() == BACKEND_GPU:
            self.status_var.set(
                "Создание CPU FAISS + загрузка GPU-индекса в VRAM..."
            )
        else:
            self.status_var.set(
                "Создание CPU FAISS (режим CPU-only)..."
            )

        backend = self.backend_mode_var.get()

        threading.Thread(
            target=self._index_worker,
            args=(backend,),
            daemon=True,
        ).start()

    def start_crop_repair(self):
        """Проверяет наличие CROP и восстанавливает отсутствующие из исходников."""
        if not self._startup_ready_or_warn("восстановление CROP"):
            return

        if self.busy:
            return

        auto_workers = bool(self.auto_workers_var.get())

        if auto_workers:
            _auto_min, workers, _auto_max = _auto_worker_limits()
        else:
            try:
                workers = int(self.local_workers_var.get())
            except (TypeError, ValueError):
                messagebox.showerror(
                    "Восстановление CROP",
                    "Количество потоков должно быть числом.",
                )
                return

            if workers < 1:
                messagebox.showerror(
                    "Восстановление CROP",
                    "Количество потоков должно быть больше нуля.",
                )
                return

            workers = min(workers, LOCAL_MAX_WORKERS)

        self._crop_repair_after_html = False
        self.operation = "crop_repair"
        self._prepare_scan_controls()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        worker_text = "AUTO" if auto_workers else str(workers)
        # Статус CROP выводим в выделенную строку над индикаторами CPU/GPU.
        # Не помещаем подробный ход операции в status_var рядом с телеметрией.
        self._set_crop_progress_status(
            f"Проверка CROP: поиск отсутствующих файлов... потоков: {worker_text}"
        )

        backend = self.backend_mode_var.get()
        threading.Thread(
            target=self._crop_repair_worker,
            args=(backend, workers, auto_workers),
            daemon=True,
            name="crop-repair",
        ).start()

    def _start_html_crop_repair_for_results(self, face_ids):
        """Восстанавливает CROP только для лиц, попавших в открытую HTML-галерею."""
        if self.busy:
            return

        normalized_ids = sorted({
            int(value) for value in (face_ids or [])
            if str(value).strip().isdigit() and int(value) > 0
        })
        if not normalized_ids:
            return

        auto_workers = bool(self.auto_workers_var.get())
        if auto_workers:
            _auto_min, workers, _auto_max = _auto_worker_limits()
        else:
            try:
                workers = max(1, min(int(self.local_workers_var.get()), LOCAL_MAX_WORKERS))
            except (TypeError, ValueError):
                workers = LOCAL_DEFAULT_WORKERS

        self._crop_repair_after_html = True
        self.operation = "crop_repair"
        self._prepare_scan_controls()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        worker_text = "AUTO" if auto_workers else str(workers)
        self._set_crop_progress_status(
            "HTML открыт. Создание отсутствующих CROP для результатов поиска... "
            f"потоков: {worker_text}"
        )

        backend = self.backend_mode_var.get()
        threading.Thread(
            target=self._crop_repair_worker,
            args=(backend, workers, auto_workers, normalized_ids),
            daemon=True,
            name="html-crop-repair",
        ).start()

    def _start_html_regeneration_after_crop(self):
        """Повторно формирует HTML после восстановления CROP и открывает обновлённый файл."""
        context = getattr(self, "_html_crop_rebuild_context", None)
        if not context:
            return False

        # Контекст одноразовый: повторное событие не должно запускать вторую генерацию.
        self._html_crop_rebuild_context = None
        self.operation = "html_regenerate"
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(10)
        self.status_var.set("CROP восстановлены. Повторное создание HTML...")

        threading.Thread(
            target=self._html_regeneration_worker,
            args=(context,),
            daemon=True,
            name="html-regenerate-after-crop",
        ).start()
        return True

    def _html_regeneration_worker(self, context):
        """Повторно создаёт HTML из результатов завершившегося поиска, не выполняя поиск заново."""
        try:
            results = context["results"]
            html_file = generate_html_gallery(
                results=results,
                query_image=Path(context["query_image"]),
                output_file=str(context["html_file"]),
                use_local_crops=bool(context["use_local_crops"]),
                theme=str(context["theme"]),
                retry_missing_crops=True,
            )
            self.events.put(("html_regenerated", html_file, len(results)))
        except Exception as exc:
            self.events.put(("html_regeneration_error", str(exc)))

    def _crop_repair_worker(
        self,
        backend=BACKEND_GPU,
        workers=LOCAL_DEFAULT_WORKERS,
        auto_workers=False,
        face_ids=None,
    ):
        try:
            conn = create_database(maintenance=False)
            try:
                # Быстрая предварительная выборка позволяет не создавать
                # InsightFace вообще, если все CROP уже существуют.
                normalized_face_ids = None
                if face_ids is not None:
                    normalized_face_ids = sorted({
                        int(value) for value in face_ids
                        if str(value).strip().isdigit() and int(value) > 0
                    })
                    if not normalized_face_ids:
                        self.events.put((
                            "crop_repair_done",
                            {
                                "checked": 0, "already_ok": 0, "missing": 0,
                                "repaired": 0, "failed": 0,
                                "deleted_unavailable_records": 0, "stopped": False,
                            },
                        ))
                        return
                    placeholders = ",".join("?" for _ in normalized_face_ids)
                    rows = conn.execute(
                        f"""
                        SELECT i.id, i.path
                        FROM images i
                        WHERE EXISTS (
                            SELECT 1 FROM faces f
                            WHERE f.image_id = i.id AND f.id IN ({placeholders})
                        )
                        """,
                        tuple(normalized_face_ids),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT i.id, i.path
                        FROM images i
                        WHERE EXISTS (
                            SELECT 1 FROM faces f WHERE f.image_id = i.id
                        )
                        """
                    ).fetchall()
                missing = 0
                checked = 0
                for image_id, db_path in rows:
                    if self.scan_stop.is_set():
                        break
                    self.scan_paused.wait()
                    if self.scan_stop.is_set():
                        break
                    crop_path = _resolve_db_crop_path(db_path, image_id=int(image_id))
                    checked += 1
                    if not crop_path.is_file():
                        missing += 1

                if self.scan_stop.is_set():
                    self.events.put((
                        "crop_repair_done",
                        {
                            "checked": checked,
                            "already_ok": checked - missing,
                            "missing": missing,
                            "repaired": 0,
                            "failed": 0,
                            "stopped": True,
                        },
                    ))
                    return

                self.events.put((
                    "crop_check_started", len(rows), missing
                ))

                if missing == 0:
                    self.events.put((
                        "crop_repair_done",
                        {
                            "checked": len(rows),
                            "already_ok": len(rows),
                            "missing": 0,
                            "repaired": 0,
                            "failed": 0,
                            "stopped": False,
                        },
                    ))
                    return

                app = self.app
                if app is None or face_app_backend(app) != backend:
                    app = create_face_app(backend)
                    self.app = app
                    actual = face_app_backend(app)
                    self.events.put(("backend_actual", actual, backend))

                def progress(done, total_missing, repaired, failed, message):
                    self.events.put((
                        "crop_repair_progress",
                        done,
                        total_missing,
                        repaired,
                        failed,
                        message,
                    ))

                result = repair_missing_crops(
                    conn,
                    app,
                    workers=workers,
                    auto_workers=auto_workers,
                    progress_callback=progress,
                    pause_event=self.scan_paused,
                    stop_event=self.scan_stop,
                    face_ids=normalized_face_ids,
                    delete_unavailable_records=(normalized_face_ids is None),
                )
                self.events.put(("crop_repair_done", result))
            finally:
                conn.close()

        except Exception as exc:
            self.events.put(("fatal", str(exc)))

    def _index_worker(self, backend=BACKEND_GPU):
        try:
            conn = create_database()

            try:
                build_faiss_index(
                    conn,
                    use_gpu_index=(backend == BACKEND_GPU),
                )
            finally:
                conn.close()

            self.events.put(
                ("index_done", get_gpu_search_status())
            )

        except Exception as e:
            self.events.put(
                ("fatal", str(e))
            )

    def start_search(self):
        if not self._startup_ready_or_warn("поиск"):
            return

        if (
            self.busy
            or self.selected_face_index is None
        ):
            return

        if not Path(INDEX_FILE).exists():
            messagebox.showwarning(
                "Индекс отсутствует",
                "Сначала просканируйте данные "
                "и создайте FAISS индекс.",
            )
            return

        face = self.query_faces[
            self.selected_face_index
        ]

        embedding = np.asarray(
            face.normed_embedding,
            dtype=np.float32,
        ).copy()

        query_path = self.query_path

        top_k = max(
            1,
            int(self.topk_var.get()),
        )

        min_similarity_percent = min(
            100.0,
            max(0.0, float(self.min_similarity_var.get())),
        )
        min_similarity = min_similarity_percent / 100.0

        self.operation = "search"
        self._set_busy(True)

        self.progress.config(
            mode="indeterminate"
        )
        self.progress.start(10)

        selected_backend = self.backend_mode_var.get()
        if selected_backend == BACKEND_CPU:
            search_status = "Поиск похожих лиц на CPU..."
        elif get_gpu_search_status().get("loaded"):
            search_status = "Поиск похожих лиц на GPU VRAM..."
        else:
            search_status = "Поиск похожих лиц на CPU (GPU index недоступен)..."

        self.status_var.set(search_status)

        threading.Thread(
            target=self._search_worker,
            args=(
                embedding,
                query_path,
                top_k,
                min_similarity,
                self.backend_mode_var.get(),
                bool(self.create_crops_on_html_var.get()),
                bool(self.use_local_crops_var.get()),
                self.theme_mode,
            ),
            daemon=True,
        ).start()

    def _search_worker(
        self,
        embedding,
        query_path,
        top_k,
        min_similarity,
        backend=BACKEND_GPU,
        create_crops_on_html=False,
        use_local_crops=True,
        theme="dark",
    ):
        try:
            conn = create_database()

            try:
                results = search_embedding(
                    conn,
                    embedding,
                    top_k,
                    min_similarity,
                    backend,
                )
            finally:
                conn.close()

            output_file = (
                "face_search_results_gui.html"
            )

            html_file = generate_html_gallery(
                results=results,
                query_image=query_path,
                output_file=output_file,
                use_local_crops=bool(use_local_crops),
                theme=theme,
                retry_missing_crops=bool(create_crops_on_html),
            )

            result_face_ids = []
            if create_crops_on_html:
                for result in results:
                    try:
                        face_id = int(result.get("face_id"))
                    except (TypeError, ValueError):
                        continue
                    if face_id > 0:
                        result_face_ids.append(face_id)

            self.events.put(
                (
                    "search_done",
                    html_file,
                    len(results),
                    result_face_ids,
                    results,
                    Path(query_path),
                    bool(use_local_crops),
                    theme,
                    bool(create_crops_on_html),
                )
            )

        except Exception as e:
            self.events.put(
                ("fatal", str(e))
            )

    def open_last_html(self):
        path = Path(
            "face_search_results_gui.html"
        ).resolve()

        if path.exists():
            webbrowser.open(
                path.as_uri()
            )

    # --------------------------------------------------------
    # Stats / events
    # --------------------------------------------------------

    def refresh_database_info(self):
        """
        Обновляет информацию из уже открытого соединения без переподключения.

        Метод одинаково работает для SQLite, PostgreSQL, MySQL и MSSQL:
        используется self.conn, схема и индексы не изменяются, новое соединение
        не создаётся. Перед чтением выполняется rollback(), чтобы безопасно
        завершить возможную read-only транзакцию или сбросить состояние
        PostgreSQL после ранее возникшего deadlock/ошибки транзакции.
        """
        conn = getattr(self, "conn", None)
        if conn is None:
            self.db_connection_var.set(
                "Соединение: НЕТ • сначала подключитесь к выбранной БД"
            )
            self.stats_var.set("Нет активного соединения с БД")
            self.status_var.set("Обновление невозможно: активная БД не подключена.")
            return

        # Не запускаем refresh одновременно с переключением профилей/закрытием.
        # Сканирование, поиск и восстановление CROP используют собственные
        # соединения, поэтому обновление статистики через self.conn допустимо.
        try:
            self.status_var.set(
                f"Обновление информации БД «{getattr(self, 'connected_db_name', self.active_db_name)}»..."
            )
            self.root.update_idletasks()

            try:
                # Для PostgreSQL после deadlock/SQL-ошибки соединение может
                # остаться в aborted transaction. Rollback не переподключает
                # БД, а только возвращает текущее соединение в рабочее состояние.
                conn.rollback()
            except Exception:
                # Для некоторых драйверов rollback может быть не нужен/не
                # поддерживаться в этом состоянии; сам SELECT ниже даст точный
                # результат.
                pass

            # Быстрая проверка живости текущего соединения. Это обычный SELECT,
            # а не reconnect или инициализация схемы.
            conn.execute("SELECT 1").fetchone()

            # _refresh_stats() использует именно self.conn и перечитывает
            # актуальные COUNT(*) из текущей БД.
            self._refresh_stats()

            # Завершаем read-only транзакцию текущего UI-соединения, чтобы оно
            # не удерживало старый snapshot PostgreSQL/MySQL после обновления.
            try:
                conn.rollback()
            except Exception:
                pass

            self._update_database_info()
            self.status_var.set(
                f"Информация БД обновлена: «{getattr(self, 'connected_db_name', self.active_db_name)}"
                f"» • СУБД: {getattr(conn, 'backend', 'unknown')}"
            )

        except Exception as exc:
            self.status_var.set(f"Ошибка обновления БД: {exc}")
            if hasattr(self, "db_connection_var"):
                self.db_connection_var.set(
                    f"Соединение: ОШИБКА чтения — {exc}"
                )

    def _refresh_stats(self):
        """Планирует чтение статистики в фоне; GUI не ждёт COUNT(*) и FAISS."""
        if self.conn is None:
            if hasattr(self, "stats_var"):
                self.stats_var.set("Нет активного соединения с БД; выполняется подключение")
            self._update_database_info()
            return

        profile_name = str(getattr(self, "connected_db_name", self.active_db_name) or self.active_db_name)
        profile = next(
            (dict(p) for p in self.db_profiles if str(p.get("name")) == profile_name),
            None,
        )
        if profile is None:
            self.stats_var.set("Не найден профиль активной БД")
            return

        if self.startup_stats_pending:
            self._stats_refresh_pending = True
            self._stats_refresh_profile = profile_name
            return

        self.startup_stats_pending = True
        self._stats_refresh_profile = profile_name
        self._update_database_info()
        threading.Thread(
            target=self._stats_worker,
            args=(profile,),
            daemon=True,
            name=f"db-stats-{profile_name}",
        ).start()

    def _stats_worker(self, profile: dict[str, Any]):
        conn = None
        profile_name = str(profile.get("name") or "")
        try:
            conn = create_database_for_profile(
                profile,
                maintenance=False,
                create_if_missing=False,
                activate_storage=False,
                initialize=False,
            )
            db_stats = _get_database_statistics(conn)
            images = db_stats["images"]
            faces = db_stats["faces"]
            profiles = db_stats["profiles"]
            index_path = Path(str(profile.get("index_path") or INDEX_FILE)).expanduser().resolve()
            faiss_status = get_faiss_index_file_status(index_path, read_metadata=False)
            self.events.put((
                "stats_done", profile_name, getattr(conn, "backend", "unknown"),
                images, faces, profiles, faiss_status, get_gpu_search_status(),
            ))
        except Exception as exc:
            self.events.put(("stats_error", profile_name, str(exc)))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _apply_stats(self, event):
        (
            _kind, profile_name, backend, images, faces, profiles,
            faiss_file_status, gpu_status,
        ) = event
        selected_backend = self.backend_mode_var.get()
        faiss_count = int(faiss_file_status.get("count") or 0)
        faiss_dim = int(faiss_file_status.get("dimension") or 0)
        faiss_error = faiss_file_status.get("error")
        size_bytes = int(faiss_file_status.get("size_bytes") or 0)

        if not faiss_file_status.get("exists"):
            faiss_text = "файл отсутствует"
        elif faiss_error:
            faiss_text = f"ошибка чтения: {faiss_error}"
        elif faiss_file_status.get("metadata_pending"):
            faiss_text = f"{size_bytes / (1024 ** 3):.2f} GB; метаданные при загрузке"
        else:
            faiss_text = f"{faiss_count:,} лиц" + (f", dim={faiss_dim}" if faiss_dim else "")

        if selected_backend == BACKEND_CPU:
            gpu_text = "не используется (CPU-only)"
        elif gpu_status.get("mode") == "streaming":
            gpu_text = f"GPU streaming: {gpu_status.get('count', 0):,} embeddings в RAM, поиск чанками на GPU"
        elif gpu_status.get("loaded"):
            gpu_text = f"VRAM: {gpu_status.get('count', 0):,} embeddings ({gpu_status.get('dtype', '')})"
        elif gpu_status.get("error"):
            gpu_text = f"ошибка: {gpu_status.get('error')}"
        elif self.cuda_available is True:
            gpu_text = "доступен, индекс не загружен"
        elif self.cuda_available is None:
            gpu_text = "проверка CUDA..."
        else:
            gpu_text = "недоступен"

        if faiss_count and faiss_count != int(faces):
            faiss_sync = f"НЕАКТУАЛЕН: БД={int(faces):,}, FAISS={faiss_count:,}"
        elif faiss_count:
            faiss_sync = "актуален"
        elif faiss_file_status.get("exists") and faiss_file_status.get("metadata_pending"):
            faiss_sync = "проверка при загрузке индекса"
        else:
            faiss_sync = "нет индекса"

        # Обновляем значения в отдельных ячейках: строки выравниваются
        # независимо от длины подписей, а счётчики имеют одинаковый правый край.
        values = {
            "profiles": f"{int(profiles):,}",
            "images": f"{int(images):,}",
            "faces": f"{int(faces):,}",
            "backend": str(backend),
            "profile": str(profile_name),
            "faiss_size": faiss_text,
            "faiss_state": faiss_sync,
            "gpu": gpu_text,
        }
        for key, value in values.items():
            variable = self.stats_value_vars.get(key)
            if variable is not None:
                variable.set(str(value))
        self.stats_var.set("")
        self._update_stats_table_wraplength()
        self._update_database_info()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "auto_workers_status":
                    # Событие могло прийти в очередь одновременно с завершением
                    # worker-а; helper отсекает устаревший статус после завершения.
                    self._set_auto_workers_status(str(event[1]))

                elif kind == "startup_db_progress":
                    message = event[1]
                    self._show_startup_progress(message)
                    self.status_var.set(message)

                elif kind == "startup_db_ready":
                    profile_name = str(event[1])
                    ready_conn = event[2] if len(event) > 2 else None
                    if profile_name != str(self.startup_profile.get("name") or ""):
                        if ready_conn is not None:
                            try:
                                ready_conn.close()
                            except Exception:
                                pass
                        continue
                    try:
                        # Соединение передано worker-ом после завершения инициализации;
                        # в UI оно используется последовательно, без конкурентного доступа.
                        self.conn = ready_conn or create_database_for_profile(
                            dict(self.startup_profile),
                            maintenance=False,
                            create_if_missing=False,
                            activate_storage=True,
                            initialize=False,
                            allow_thread_handoff=True,
                        )
                        self.active_db_name = profile_name
                        self.connected_db_name = profile_name
                        self.startup_db_ready = True
                        self.startup_db_pending = False
                        self.db_profile_var.set(profile_name)
                        self.db_connection_var.set(f"Соединение: подключено • профиль «{profile_name}»")
                        self._refresh_database_combo()
                        self._refresh_stats()
                        self.status_var.set("База данных готова; завершается проверка оборудования...")
                        self._maybe_start_gpu_index_load()
                        self._finish_startup_progress_if_ready()
                    except Exception as exc:
                        self.startup_db_failed = True
                        self.startup_db_pending = False
                        self.status_var.set(f"Ошибка подключения после инициализации БД: {exc}")
                        self.db_connection_var.set(f"Соединение: ОШИБКА — {exc}")
                        self.stats_var.set("Не удалось открыть активную БД")
                        self._finish_startup_progress_if_ready()

                elif kind == "startup_db_error":
                    profile_name, error = event[1:]
                    if profile_name != str(self.startup_profile.get("name") or ""):
                        continue
                    self.startup_db_failed = True
                    self.startup_db_pending = False
                    self.db_connection_var.set(f"Соединение: ОШИБКА — {error}")
                    self.startup_progress_label_var.set(f"Ошибка запуска БД: {error}")
                    self.status_var.set(f"Ошибка запуска базы данных: {error}")
                    try:
                        self.progress.stop()
                        self.progress.config(mode="determinate", value=0)
                    except tk.TclError:
                        pass
                    self._hide_startup_progress()
                    self.stats_var.set("Ошибка подключения к базе данных")

                elif kind == "startup_cuda_done":
                    available, detail = event[1:]
                    self.cuda_available = bool(available)
                    self.cuda_check_done = True
                    self._update_backend_ui()
                    if not self.cuda_available:
                        self.gpu_status_var.set("GPU: недоступен")
                    self._maybe_start_gpu_index_load()
                    self._finish_startup_progress_if_ready()

                elif kind == "stats_done":
                    profile_name = str(event[1])
                    is_current_profile = (
                        profile_name == str(getattr(self, "connected_db_name", ""))
                    )
                    if is_current_profile:
                        self._apply_stats(event)
                        self.startup_stats_initial_done = True
                    self.startup_stats_pending = False
                    if self._stats_refresh_pending:
                        self._stats_refresh_pending = False
                        self._refresh_stats()
                    self._finish_startup_progress_if_ready()

                elif kind == "stats_error":
                    profile_name, error = event[1:]
                    if profile_name == str(getattr(self, "connected_db_name", "")):
                        self.stats_var.set(f"Ошибка статистики: {error}")
                        # Ошибка первичного запроса тоже завершает стартовый
                        # этап: не оставляем progress bar крутиться бесконечно.
                        self.startup_stats_initial_done = True
                    self.startup_stats_pending = False
                    if self._stats_refresh_pending:
                        self._stats_refresh_pending = False
                        self._refresh_stats()
                    self._finish_startup_progress_if_ready()

                elif kind == "gpu_index_progress":
                    profile_name, index_path, message = event[1:]
                    if (
                        self.backend_mode_var.get() == BACKEND_GPU
                        and self.gpu_index_loading
                        and str(getattr(self, "connected_db_name", "")) == str(profile_name)
                        and str(Path(INDEX_FILE).resolve()) == str(Path(index_path).resolve())
                    ):
                        self._show_startup_progress(message)
                        self.status_var.set(message)

                elif kind == "backend_actual":
                    actual_backend, requested_backend = event[1:]
                    self._handle_actual_backend(actual_backend, requested_backend)
                    if actual_backend == BACKEND_GPU:
                        self.scan_btn.config(text="▶ Сканировать GPU")
                    else:
                        self.scan_btn.config(text="▶ Сканировать CPU")

                elif kind == "scan_startup_status":
                    self.status_var.set(str(event[1]))

                elif kind == "scan_discovery_status":
                    entries_seen = max(0, int(event[1] or 0))
                    images_found = max(0, int(event[2] or 0))
                    self.status_var.set(
                        "Обход каталога: проверено элементов "
                        f"{entries_seen:,}; найдено изображений {images_found:,}"
                    )

                elif kind == "scan_total":
                    total = event[1]
                    self._scan_progress_value = 0
                    self._scan_rate_started_at = time.monotonic()
                    self._scan_rate_last_completed = 0
                    # Скользящее окно: время, число завершённых изображений и лиц.
                    self._scan_rate_window_seconds = 5.0
                    self._scan_rate_samples = deque()
                    self._scan_rate_text = "Скорость: 0.0 изображений/с • 0.0 лиц/с"
                    self._set_auto_workers_status(
                        str(getattr(self, "_scan_monitor_base_status", ""))
                    )

                    self.progress.stop()
                    self.progress.config(
                        mode="determinate",
                        maximum=max(1, total),
                        value=0,
                    )

                    self.status_var.set(
                        f"Подготовлен первый пакет изображений: {total}"
                    )

                elif kind == "scan_discovered":
                    total = max(0, int(event[1] or 0))
                    current_value = int(getattr(self, "_scan_progress_value", 0) or 0)
                    self.progress.stop()
                    self.progress.config(
                        mode="determinate",
                        maximum=max(1, total),
                        value=min(current_value, max(1, total)),
                    )
                    self.status_var.set(f"Найдено изображений: {total}")

                elif kind == "scan_progress":
                    n, total, count, path = event[1:5]
                    # Старые события используют count; новые отдельно передают
                    # число лиц, найденных при inference. None означает, что
                    # изображение было пропущено без запуска детектора.
                    detected_count = event[5] if len(event) > 5 else count

                    self._scan_progress_value = int(n or 0)
                    self.progress.config(value=n)

                    # Скорость за скользящее окно последних 5 секунд, а не
                    # среднее за всё сканирование. Одно изображение может содержать
                    # несколько лиц, поэтому учитываем обе величины отдельно.
                    now = time.monotonic()
                    if n <= 1 or not hasattr(self, "_scan_rate_samples"):
                        self._scan_rate_started_at = now
                        self._scan_rate_last_completed = 0
                        self._scan_rate_window_seconds = 5.0
                        self._scan_rate_samples = deque()
                    previous_n = int(getattr(self, "_scan_rate_last_completed", 0) or 0)
                    completed_delta = max(0, int(n or 0) - previous_n)
                    faces_delta = max(0, int(detected_count or 0))
                    self._scan_rate_last_completed = int(n or 0)
                    if completed_delta or faces_delta:
                        self._scan_rate_samples.append((now, completed_delta, faces_delta))
                    window_seconds = max(0.001, float(getattr(self, "_scan_rate_window_seconds", 5.0)))
                    cutoff = now - window_seconds
                    while self._scan_rate_samples and self._scan_rate_samples[0][0] < cutoff:
                        self._scan_rate_samples.popleft()
                    images_in_window = sum(sample[1] for sample in self._scan_rate_samples)
                    faces_in_window = sum(sample[2] for sample in self._scan_rate_samples)
                    # В начале сканирования делим на реально прошедшее время;
                    # после заполнения окна используем ровно 5 секунд.
                    elapsed = min(window_seconds, max(0.001, now - self._scan_rate_started_at))
                    images_per_sec = images_in_window / elapsed
                    faces_per_sec = faces_in_window / elapsed

                    self._scan_rate_text = (
                        f"Скорость: {images_per_sec:.1f} изображений/с • "
                        f"{faces_per_sec:.1f} лиц/с"
                    )
                    # Главная строка мониторинга находится вверху интерфейса.
                    # Обновляем её при каждом результате и повторно добавляем
                    # скорость после любого очередного telemetry-события.
                    monitor_label = getattr(self, "auto_workers_status_label", None)
                    if monitor_label is not None and monitor_label.winfo_manager():
                        base = str(getattr(self, "_scan_monitor_base_status", "") or "")
                        self.auto_workers_status_var.set(
                            f"{base} | {self._scan_rate_text}" if base else self._scan_rate_text
                        )
                    self.status_var.set(
                        f"[{n}/{total}] добавлено лиц: {count} — {path}"
                    )

                elif kind == "scan_error":
                    error_path = str(event[1]) if len(event) > 1 else "файл не определён"
                    error_reason = str(event[2]) if len(event) > 2 else "причина не передана"
                    self.status_var.set(
                        f"Ошибка: {error_path} — {error_reason}"
                    )

                elif kind == "gpu_index_done":
                    ok, status = event[1:3]
                    profile_name = event[3] if len(event) > 3 else str(self.connected_db_name)
                    index_path = event[4] if len(event) > 4 else str(Path(INDEX_FILE).resolve())

                    # Игнорируем результат старого loader-потока.
                    if (
                        str(getattr(self, "connected_db_name", "")) != str(profile_name)
                        or str(Path(INDEX_FILE).resolve()) != str(Path(index_path).resolve())
                    ):
                        continue

                    self.gpu_index_loading = False
                    self.startup_index_started = False
                    self._hide_startup_progress()
                    if ok and status.get("mode") == "streaming":
                        self.status_var.set(
                            "Запуск завершён. Полный FAISS не помещается в VRAM: "
                            "включён потоковый GPU-поиск; "
                            "InsightFace продолжает работать на GPU. "
                            f"Векторов: {status.get('count', 0):,}."
                        )
                    elif ok:
                        self.status_var.set(
                            "Запуск завершён. GPU-индекс загружен в VRAM: "
                            f"{status.get('count', 0):,} embeddings, "
                            f"{status.get('dtype', '')}"
                        )
                    elif status.get("error"):
                        self.status_var.set(
                            "Запуск завершён. GPU-поиск индексом недоступен — используется CPU FAISS; "
                            "CUDA-распознавание для сканирования остаётся независимым. "
                            f"Причина: {status.get('error')}"
                        )
                    else:
                        self.status_var.set("Запуск завершён. FAISS не загружен в VRAM; доступен CPU fallback.")
                    self._refresh_stats()
                    self._update_database_info()

                elif kind == "scan_done":
                    total_faces = event[1]
                    completed = event[2] if len(event) > 2 else 0
                    total = event[3] if len(event) > 3 else completed
                    stopped = bool(event[4]) if len(event) > 4 else False
                    total_updated = int(event[5]) if len(event) > 5 else 0
                    total_added = int(event[6]) if len(event) > 6 else 0
                    total_removed = int(event[7]) if len(event) > 7 else 0

                    self.progress.config(
                        value=min(completed, int(self.progress["maximum"] or completed))
                    )

                    if stopped:
                        if bool(self.overwrite_existing_crops_var.get()):
                            if bool(self.create_crop_files_var.get()):
                                summary = (
                                    f"CROP перезаписано: {total_updated}; новых CROP: {total_added}; "
                                    f"удалено лишних: {total_removed}; лиц: {total_faces}"
                                )
                            else:
                                summary = (
                                    f"записей БД обновлено: {total_updated}; новых: {total_added}; "
                                    f"удалено лишних: {total_removed}; лиц: {total_faces}"
                                )
                            self.status_var.set(
                                f"Сканирование остановлено. Обработано: {completed}; "
                                f"обнаружено: {total}. {summary}"
                            )
                        else:
                            self.status_var.set(
                                "Сканирование остановлено. "
                                f"Обработано: {completed}; обнаружено: {total}; "
                                f"добавлено лиц: {total_faces}"
                            )
                        self.scan_control_status_var.set(
                            f"Остановлено: обработано {completed}; обнаружено {total}"
                        )
                    else:
                        if bool(self.overwrite_existing_crops_var.get()):
                            if bool(self.create_crop_files_var.get()):
                                summary = (
                                    f"CROP перезаписано: {total_updated}; новых CROP: {total_added}; "
                                    f"удалено лишних: {total_removed}; лиц: {total_faces}"
                                )
                            else:
                                summary = (
                                    f"записей БД обновлено: {total_updated}; новых: {total_added}; "
                                    f"удалено лишних: {total_removed}; лиц: {total_faces}"
                                )
                            self.status_var.set(f"Сканирование завершено. {summary}")
                        else:
                            self.status_var.set(
                                "Сканирование завершено. "
                                f"Добавлено лиц: {total_faces}"
                            )
                        self.scan_control_status_var.set("Сканирование завершено")

                    self.scan_running = False
                    self.operation = None
                    self._hide_scan_control_status()
                    self._hide_auto_workers_status()
                    self._set_busy(False)
                    self._refresh_stats()
                    self._update_search_state()

                elif kind == "crop_check_started":
                    checked, missing = event[1:]
                    self.progress.stop()
                    if missing:
                        self.progress.config(
                            mode="determinate",
                            maximum=max(1, missing),
                            value=0,
                        )
                    else:
                        self.progress.config(
                            mode="determinate",
                            maximum=max(1, checked),
                            value=checked,
                        )
                    crop_scope = (
                        "CROP из HTML" if getattr(self, "_crop_repair_after_html", False)
                        else "CROP"
                    )
                    self._set_crop_progress_status(
                        f"{crop_scope}: проверено записей {checked}; отсутствует файлов: {missing}; "
                        + ("начинается восстановление..." if missing else "все файлы на месте")
                    )

                elif kind == "crop_repair_progress":
                    done, total_missing, repaired, failed, message = event[1:]
                    self.progress.config(
                        mode="determinate",
                        maximum=max(1, int(total_missing)),
                        value=min(int(done), int(total_missing)),
                    )
                    # Прогресс восстановления обновляет ту же строку, где ранее
                    # отображалось «Авто: запуск адаптивного контроллера для CROP...».
                    self._set_crop_progress_status(
                        f"CROP [{done}/{total_missing}] — восстановлено: {repaired}, "
                        f"ошибок: {failed}. {message}"
                    )

                elif kind == "crop_repair_done":
                    result = event[1]
                    was_html_crop_repair = bool(
                        getattr(self, "_crop_repair_after_html", False)
                    )
                    self.progress.stop()
                    self.progress.config(
                        mode="determinate",
                        value=min(
                            int(result.get("repaired", 0)) + int(result.get("failed", 0)),
                            max(1, int(result.get("missing", 0))),
                        ),
                    )

                    stopped = bool(result.get("stopped"))
                    self.scan_running = False
                    self.operation = None
                    self._hide_auto_workers_status()
                    self._set_busy(False)
                    self._refresh_stats()
                    self._update_search_state()

                    summary = (
                        f"Проверено: {result.get('checked', 0)}; "
                        f"уже были: {result.get('already_ok', 0)}; "
                        f"отсутствовали: {result.get('missing', 0)}; "
                        f"восстановлено: {result.get('repaired', 0)}; "
                        f"ошибок восстановления: {result.get('failed', 0)}; "
                        f"удалено записей по недоступным URL: "
                        f"{result.get('deleted_unavailable_records', 0)}."
                    )
                    if getattr(self, "_crop_repair_after_html", False):
                        operation_title = "Создание CROP после HTML"
                    else:
                        operation_title = "Проверка CROP"
                    if stopped:
                        self.status_var.set(f"{operation_title} остановлено. " + summary)
                    else:
                        self.status_var.set(f"{operation_title} завершено. " + summary)
                    self._crop_repair_after_html = False
                    self._hide_scan_control_status()

                    if was_html_crop_repair:
                        if stopped:
                            self._html_crop_rebuild_context = None
                        else:
                            # После завершения CROP повторно создаём HTML из тех же
                            # результатов, чтобы ссылки и карточки соответствовали диску.
                            self._start_html_regeneration_after_crop()

                elif kind == "html_regenerated":
                    html_file, count = event[1:3]
                    self.progress.stop()
                    self.progress.config(value=0)
                    self.operation = None
                    self._set_busy(False)

                    try:
                        # Уникальный query-параметр заставляет браузер открыть именно
                        # обновлённый файл, а не использовать уже открытую страницу.
                        refreshed_url = (
                            Path(html_file).resolve().as_uri()
                            + f"?refresh={time.time_ns()}"
                        )
                        webbrowser.open(refreshed_url, new=2)
                    except Exception as exc:
                        print(f"Не удалось открыть обновлённый HTML в браузере: {exc}")

                    self.status_var.set(
                        "CROP восстановлены. HTML повторно создан и открыт в браузере. "
                        f"Результатов: {count}."
                    )
                    self._update_search_state()

                elif kind == "html_regeneration_error":
                    error_text = str(event[1]) if len(event) > 1 else "неизвестная ошибка"
                    self.progress.stop()
                    self.progress.config(value=0)
                    self.operation = None
                    self._set_busy(False)
                    self.status_var.set(
                        "CROP обработаны, но повторно создать HTML не удалось: "
                        + error_text
                    )
                    self._update_search_state()

                elif kind == "index_done":
                    self.progress.stop()
                    self.progress.config(
                        value=0
                    )

                    status = event[1] if len(event) > 1 else get_gpu_search_status()
                    if self.backend_mode_var.get() == BACKEND_CPU:
                        self.status_var.set(
                            "CPU FAISS индекс создан; GPU отключён (CPU-only)."
                        )
                    elif status.get("loaded"):
                        self.status_var.set(
                            "Индекс создан: CPU FAISS + "
                            f"GPU VRAM ({status.get('count', 0):,}, {status.get('dtype', '')})."
                        )
                    else:
                        self.status_var.set(
                            "CPU FAISS индекс создан; GPU-индекс недоступен — CPU fallback."
                        )

                    self.operation = None
                    self._set_busy(False)
                    self._refresh_stats()
                    self._update_search_state()

                elif kind == "search_done":
                    html_file, count = event[1:3]
                    result_face_ids = event[3] if len(event) > 3 else []
                    search_results = event[4] if len(event) > 4 else []
                    query_image = event[5] if len(event) > 5 else self.query_path
                    use_local_crops = event[6] if len(event) > 6 else bool(self.use_local_crops_var.get())
                    html_theme = event[7] if len(event) > 7 else self.theme_mode
                    create_crops_after_html = event[8] if len(event) > 8 else bool(self.create_crops_on_html_var.get())

                    self.progress.stop()
                    self.progress.config(
                        value=0
                    )

                    self.status_var.set(
                        "Поиск завершён. "
                        f"Найдено результатов: {count}"
                    )

                    self.operation = None
                    self._set_busy(False)

                    self.open_results_btn.config(
                        state="normal"
                    )

                    try:
                        webbrowser.open(
                            Path(html_file)
                            .resolve()
                            .as_uri()
                        )
                    except Exception as exc:
                        print(f"Не удалось открыть HTML в браузере: {exc}")

                    # Браузер уже открыт. Если настройка включена, восстанавливаем
                    # отсутствующие CROP только для показанных результатов, а затем
                    # повторно генерируем HTML из сохранённого результата поиска.
                    if create_crops_after_html and result_face_ids:
                        self._html_crop_rebuild_context = {
                            "results": search_results,
                            "query_image": query_image,
                            "html_file": html_file,
                            "use_local_crops": use_local_crops,
                            "theme": html_theme,
                        }
                        self._start_html_crop_repair_for_results(result_face_ids)
                    else:
                        self._html_crop_rebuild_context = None

                    self._update_search_state()

                elif kind == "fatal":
                    self.progress.stop()
                    self.progress.config(
                        value=0
                    )

                    self.scan_running = False
                    self.operation = None
                    self.scan_stop.set()
                    self.scan_paused.set()
                    self._hide_scan_control_status()
                    self._hide_auto_workers_status()
                    self._set_busy(False)
                    self._update_search_state()

                    messagebox.showerror(
                        "Ошибка",
                        event[1],
                    )

                    self.status_var.set(
                        "Операция завершилась с ошибкой."
                    )

        except queue.Empty:
            pass
        except Exception as exc:
            # Ошибка обработки одного GUI-события не должна навсегда отключать
            # опрос очереди. Иначе scan worker продолжает работать и ставить
            # события в очередь, а статус интерфейса остаётся на первом сообщении.
            print(f"[GUI EVENT POLL ERROR] {exc}")
            traceback.print_exc()
        finally:
            # Всегда планируем следующий опрос, даже если обработчик события
            # неожиданно выбросил исключение. После закрытия окна after уже не нужен.
            try:
                if self.root.winfo_exists():
                    self.root.after(100, self._poll_events)
            except (tk.TclError, RuntimeError):
                pass

    def _close(self):
        try:
            self._gpu_telemetry_stop.set()
        except Exception:
            pass
        try:
            self._settings_ready = True
            self._save_gui_settings()
        except Exception:
            pass

        try:
            self.scan_stop.set()
            self.scan_paused.set()
        except Exception:
            pass

        try:
            conn = self.conn
            self.conn = None
            if conn is not None:
                conn.close()
        except Exception:
            pass

        try:
            _clear_gpu_search_index(release_cuda_memory=True)
        except Exception:
            pass

        try:
            self.root.destroy()
        except tk.TclError:
            pass


def launch_gui():
    root = tk.Tk()
    FaceSearchGUI(root)
    root.mainloop()


# ============================================================
# STATISTICS
# ============================================================

def _get_database_statistics(conn: DatabaseConnection) -> dict[str, int]:
    """Возвращает согласованную статистику БД по смыслу полей images/faces.

    Профили — количество уникальных непустых images.source_url.
    Изображения — уникальные images.url_image; для строк без url_image
    вместо него учитывается уникальный images.source_url (локальный оригинал).
    Лица — количество записей в faces.

    Запросы используют общие для SQLite/PostgreSQL/MySQL/MSSQL конструкции.
    """
    profiles = int(conn.execute(
        """
        SELECT COUNT(DISTINCT TRIM(source_url))
        FROM images
        WHERE source_url IS NOT NULL AND TRIM(source_url) <> ''
        """
    ).fetchone()[0] or 0)

    remote_images = int(conn.execute(
        """
        SELECT COUNT(DISTINCT TRIM(url_image))
        FROM images
        WHERE url_image IS NOT NULL AND TRIM(url_image) <> ''
        """
    ).fetchone()[0] or 0)

    local_images = int(conn.execute(
        """
        SELECT COUNT(DISTINCT TRIM(source_url))
        FROM images
        WHERE (url_image IS NULL OR TRIM(url_image) = '')
          AND source_url IS NOT NULL
          AND TRIM(source_url) <> ''
        """
    ).fetchone()[0] or 0)

    faces = int(conn.execute(
        "SELECT COUNT(*) FROM faces"
    ).fetchone()[0] or 0)

    return {
        "profiles": profiles,
        "images": remote_images + local_images,
        "faces": faces,
    }


def print_stats(conn: DatabaseConnection):
    stats = _get_database_statistics(conn)
    images = stats["images"]
    faces = stats["faces"]

    print()
    print("=" * 60)
    print("DATABASE")
    print("=" * 60)
    print(f"СУБД:              {getattr(conn, 'backend', 'unknown')}")
    print(f"Профилей:          {stats['profiles']:,}")
    print(f"Изображений:       {images:,}")
    print(f"Лиц в БД:          {faces:,}")
    print(
        "Среднее лиц/изображение:  "
        f"{faces / images:.2f}"
        if images
        else "Среднее лиц/изображение:  0"
    )


# ============================================================
# CROP REPAIR / VALIDATION
# ============================================================

def _resolve_db_storage_path(value: Any) -> Path:
    """Разрешает images.path с поддержкой переносимого CROP-формата.

    Новые CROP-записи хранят только путь относительно CROP_DIR.
    Для совместимости поддерживаются старые абсолютные пути и обычные
    пути исходных изображений относительно APP_DIR.
    """
    raw = str(value or "").strip()
    if not raw:
        return Path("")

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()

    crop_candidate = (CROP_DIR / path).resolve()
    if crop_candidate.is_file():
        return crop_candidate

    return (APP_DIR / path).resolve()


def _crop_path_migration_needed(conn: DatabaseConnection) -> bool:
    """Быстро определяет, есть ли в БД абсолютные/legacy CROP-пути."""
    # Передаём шаблоны LIKE параметрами: буквальный символ '%' внутри SQL-строки
    # конфликтует с плейсхолдерами некоторых DB-API драйверов (PostgreSQL/MySQL).
    row = conn.execute(
        """
        SELECT 1
        FROM images i
        WHERE i.path LIKE ?
           OR i.path LIKE ?
           OR i.path LIKE ?
           OR i.path LIKE ?
        """,
        (r"%:\%", "/%", "file:%", r"\\%"),
    ).fetchone()
    return row is not None


def _migrate_crop_paths(conn: DatabaseConnection) -> int:
    """Переводит старые CROP-ссылки в путь относительно CROP_DIR."""
    rows = conn.execute(
        """
        SELECT DISTINCT i.id, i.path, i.filename
        FROM images i
        JOIN faces f ON f.image_id = i.id
        WHERE i.path LIKE ?
           OR i.path LIKE ?
           OR i.path LIKE ?
           OR i.path LIKE ?
        """,
        (r"%:\%", "/%", "file:%", r"\\%"),
    ).fetchall()

    changed = 0
    for image_id, db_path, filename in rows:
        image_id = int(image_id)
        expected = _expected_crop_path(image_id)
        raw = str(db_path or "").strip()

        if expected.is_file():
            desired = _relative_crop_db_path(expected)
            if raw.replace("\\", "/") != desired:
                conn.execute(
                    "UPDATE images SET path = ?, filename = ? WHERE id = ?",
                    (desired, expected.name, image_id),
                )
                changed += 1
            continue

        if raw:
            try:
                resolved = Path(raw).expanduser().resolve()
                crop_root = CROP_DIR.resolve()
                if crop_root in resolved.parents:
                    desired = _relative_crop_db_path(resolved)
                    if raw.replace("\\", "/") != desired:
                        conn.execute(
                            "UPDATE images SET path = ?, filename = ? WHERE id = ?",
                            (desired, resolved.name, image_id),
                        )
                        changed += 1
            except (OSError, ValueError):
                pass

    if changed:
        conn.commit()
    return changed


def _expected_crop_path(image_id: int) -> Path:
    """Возвращает стандартный путь CROP для существующего image.id."""
    bucket = ((int(image_id) - 1) // 1000 + 1) * 1000
    return CROP_DIR / str(bucket) / f"{int(image_id)}.jpg"


def _resolve_db_crop_path(value: Any, *, image_id: int | None = None) -> Path:
    """Разрешает путь именно CROP, сохраняя относительный путь из БД.

    Относительные значения images.path всегда относятся к CROP_DIR активного
    профиля. В отличие от универсального resolver, этот путь выбирается даже
    тогда, когда файла пока нет: именно сюда нужно восстановить отсутствующий
    CROP, а не в APP_DIR.
    """
    raw = str(value or "").strip()
    if not raw or raw == "." or Path(raw).name.startswith("pending_"):
        return _expected_crop_path(int(image_id)) if image_id is not None else Path(".")

    parsed = urlparse(raw)
    if parsed.scheme.lower() == "file":
        try:
            path_text = url2pathname(unquote(parsed.path or ""))
            if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
                path_text = f"//{parsed.netloc}{path_text}"
            legacy_path = Path(path_text).expanduser()
            if legacy_path.is_absolute():
                return legacy_path.resolve()
        except (OSError, ValueError):
            pass

    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()

    # Не проверяем is_file() до выбора корня: файла может не быть, но место
    # его восстановления определяется сохранённым путём и каталогом профиля.
    return (Path(CROP_DIR).expanduser() / path).resolve()


def _source_url_to_local_path(source_url: Any) -> Path | None:
    """Извлекает локальный файл из file:// URL или обычного пути."""
    raw = str(source_url or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme == "file":
        try:
            path_text = url2pathname(unquote(parsed.path or ""))
            if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
                path_text = f"//{parsed.netloc}{path_text}"
            path = Path(path_text)
            if path.is_file():
                return path.resolve()
        except (OSError, ValueError):
            return None
        return None

    # Локальный путь без URI-схемы.
    if not scheme:
        try:
            path = Path(raw).expanduser()
            if path.is_file():
                return path.resolve()
        except (OSError, ValueError):
            pass

    return None


def _repair_ssl_context():
    """Возвращает TLS-контекст с актуальным CA bundle, если certifi доступен."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # Не отключаем проверку сертификатов даже при отсутствии certifi.
        return ssl.create_default_context()


def _load_repair_source_image(
    source_url: Any,
    image_url: Any,
) -> tuple[np.ndarray | None, str | None]:
    """Загружает исходное изображение для восстановления CROP.

    Приоритет источника:
    1. Существующий локальный файл из ``source_url``.
    2. Существующий локальный файл из ``url_image``.
    3. Интернет-изображение из ``url_image``, если поле заполнено.
    4. Интернет-ресурс из ``source_url``, если ``url_image`` пустой или
       не содержит HTTP(S)-адреса (например, у локального файла без JSON).

    Для интернет-фото не загружаем ``source_url`` первым: в импортированных
    записях это обычно страница профиля, а изображение находится в ``url_image``.
    При скачивании ``url_image`` в Referer передаётся ``source_url``, если это
    HTTP(S)-адрес страницы.
    """
    _ensure_runtime_dependencies(need_numpy=True, need_cv2=True)

    source_text = str(source_url or "").strip()
    image_text = str(image_url or "").strip()

    # Локальные источники проверяем до сети. source_url может быть file:// URL
    # либо путём, сохранённым при сканировании без JSON.
    local_source = _source_url_to_local_path(source_text)
    if local_source is not None:
        image = load_image(local_source)
        if image is not None:
            return image, str(local_source)

    local_image_path = _source_url_to_local_path(image_text)
    if local_image_path is not None:
        image = load_image(local_image_path)
        if image is not None:
            return image, str(local_image_path)

    source_parsed = urlparse(source_text) if source_text else None
    image_parsed = urlparse(image_text) if image_text else None
    source_is_http = bool(
        source_parsed and source_parsed.scheme.lower() in ("http", "https")
    )
    image_is_http = bool(
        image_parsed and image_parsed.scheme.lower() in ("http", "https")
    )

    # В таблице images.url_image хранится прямая ссылка на фотографию.
    # Используем её первой и не запрашиваем source_url (часто страницу профиля),
    # если прямая ссылка на изображение уже имеется. Когда url_image пустой или
    # не является HTTP(S)-ссылкой, используем source_url — в частности file://
    # для локальных фотографий, просканированных без JSON.
    remote_candidates: list[tuple[str, str, int]] = []
    if image_is_http:
        referer = source_text if source_is_http else ""
        remote_candidates.append(
            (image_text, referer, max(1, int(REPAIR_HTTP_RETRIES) + 1))
        )
    elif source_is_http:
        remote_candidates.append(
            (source_text, "", max(1, int(REPAIR_HTTP_RETRIES) + 1))
        )

    headers_base = {
        "User-Agent": REPAIR_HTTP_USER_AGENT,
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }

    for remote_url, referer, total_attempts in remote_candidates:
        last_error: Exception | None = None
        attempt = 0
        for attempt in range(total_attempts):
            try:
                headers = dict(headers_base)
                if referer:
                    headers["Referer"] = referer

                request = Request(remote_url, headers=headers, method="GET")
                with urlopen(request, timeout=REPAIR_HTTP_TIMEOUT, context=_repair_ssl_context()) as response:
                    content_type = ""
                    try:
                        content_type = str(
                            response.headers.get_content_type() or ""
                        ).lower()
                    except Exception:
                        pass
                    if content_type in ("text/html", "application/json"):
                        last_error = RuntimeError(
                            f"сервер вернул {content_type}, а не изображение"
                        )
                        break
                    data = response.read()

                if not data:
                    raise RuntimeError("сервер вернул пустой ответ")

                image = cv2.imdecode(
                    np.frombuffer(data, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if image is not None:
                    return image, remote_url

                raise RuntimeError("ответ не является распознаваемым изображением")
            except Exception as exc:
                last_error = exc
                # Сертификатная ошибка не исправится повторным запросом.
                if isinstance(exc, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    break
                if attempt + 1 >= total_attempts:
                    break
                delay = REPAIR_HTTP_BACKOFF * (2 ** attempt)
                time.sleep(min(delay, 5.0))

        if last_error is not None:
            print(
                f"[CROP HTTP] Не удалось загрузить {remote_url}: "
                f"{last_error} (попыток: {min(total_attempts, attempt + 1)})"
            )

    return None, None


def _is_direct_http_image_url(value: Any) -> bool:
    """True only for a syntactically valid HTTP(S) URL stored as image URL."""
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _delete_records_for_unavailable_image_url(
    conn: DatabaseConnection,
    image_url: str,
) -> int:
    """Удаляет все CROP/face-записи, ссылающиеся на недоступный URL изображения.

    Удаление выполняется только для прямого HTTP(S)-URL изображения из
    ``images.url_image``. ``source_url`` намеренно не используется: для импорта
    это часто URL страницы профиля, а не исходного файла. Локальные источники
    эта функция не затрагивает.
    """
    normalized_url = str(image_url or "").strip()
    if not _is_direct_http_image_url(normalized_url):
        return 0

    rows = conn.execute(
        "SELECT id FROM images WHERE TRIM(url_image) = ? ORDER BY id",
        (normalized_url,),
    ).fetchall()
    if not rows:
        return 0

    image_ids = [(int(row[0]),) for row in rows]
    try:
        # Сначала удаляем лица/embeddings, затем все image rows для этого URL.
        conn.executemany("DELETE FROM faces WHERE image_id = ?", image_ids)
        conn.executemany("DELETE FROM images WHERE id = ?", image_ids)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    return len(image_ids)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    aa = np.asarray(a, dtype=np.float32)
    bb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(aa))
    nb = float(np.linalg.norm(bb))
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return float(np.dot(aa / na, bb / nb))


def _repair_single_missing_crop(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    row: tuple[Any, ...],
    source_cache: dict[str, tuple[np.ndarray | None, str | None]],
    used_matches: dict[str, set[int]],
    min_match_similarity: float = 0.20,
) -> tuple[bool, str]:
    """Восстанавливает один отсутствующий CROP по исходному изображению."""
    (
        image_id,
        crop_db_path,
        filename,
        source_type,
        source_url,
        url_image,
        face_id,
        embedding_blob,
        embedding_dim,
    ) = row

    image_id = int(image_id)
    face_id = int(face_id)

    # Сохраняем путь из images.path. Для относительного значения базой служит
    # CROP_DIR активного профиля, даже если файла ещё нет.
    crop_path = _resolve_db_crop_path(crop_db_path, image_id=image_id)

    local_source = _source_url_to_local_path(source_url)
    if local_source is not None:
        source_key = str(local_source)
    else:
        # Для импортированных записей source_url обычно является URL
        # профиля/страницы, а само изображение находится в url_image.
        source_key = str(url_image or "").strip()
        if not source_key:
            source_key = str(source_url or "").strip()

    if source_key not in source_cache:
        source_cache[source_key] = _load_repair_source_image(
            source_url,
            url_image,
        )

    image, source_description = source_cache[source_key]
    if image is None:
        return False, "исходное изображение недоступно"

    try:
        valid_faces = _detect_faces_with_padding(
            app,
            image,
            padding_ratio=0.40,
        )
    except Exception as exc:
        return False, f"ошибка детекции: {exc}"

    if not valid_faces:
        return False, "лицо в исходнике не найдено"

    try:
        stored_embedding = blob_to_embedding(
            embedding_blob,
            int(embedding_dim),
        )
    except Exception as exc:
        return False, f"ошибка чтения embedding: {exc}"

    used = used_matches.setdefault(source_key, set())
    candidate_indices = [
        i for i in range(len(valid_faces))
        if i not in used
    ]
    # Если в исходнике меньше лиц, чем восстанавливаемых записей,
    # допускаем повторное сопоставление с тем же лицом.
    if not candidate_indices:
        candidate_indices = list(range(len(valid_faces)))

    best_index = -1
    best_face = None
    best_similarity = -1.0
    for index in candidate_indices:
        detected = valid_faces[index]
        similarity = _cosine_similarity(
            stored_embedding,
            np.asarray(detected.normed_embedding, dtype=np.float32),
        )
        if similarity > best_similarity:
            best_similarity = similarity
            best_face = detected
            best_index = index

    if best_index >= 0 and len(candidate_indices) < len(valid_faces):
        # Зарезервировать детектированное лицо для следующей строки того же
        # исходника нельзя только тогда, когда кандидат уже был повторно
        # использован; обычный случай получает уникальное сопоставление.
        used.add(best_index)
    elif best_index >= 0 and best_index not in used and len(candidate_indices) == len(valid_faces):
        used.add(best_index)

    if best_face is None or best_similarity < min_match_similarity:
        return False, (
            f"лицо не сопоставлено с embedding "
            f"(similarity={max(best_similarity, -1.0):.3f})"
        )

    bbox = best_face.bbox.astype(np.float32)
    crop_result = _safe_crop(image, bbox)
    if crop_result is None:
        return False, "некорректный bbox лица"

    crop, crop_left, crop_top = crop_result
    crop, crop_scale = _resize_crop_for_storage(crop)

    try:
        encoded, _new_sha256 = _face_crop_encoded(crop)
    except Exception as exc:
        return False, f"ошибка кодирования CROP: {exc}"

    # Восстанавливаем файл в точный путь, полученный из images.path и CROP_DIR профиля.
    target = crop_path
    target.parent.mkdir(parents=True, exist_ok=True)

    # Пишем сначала во временный файл рядом с CROP, затем заменяем.
    tmp = target.with_name(
        f".{target.stem}.repair_{threading.get_ident()}_{time.time_ns()}.tmp"
    )
    try:
        tmp.write_bytes(encoded)
        tmp.replace(target)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"ошибка записи CROP: {exc}"

    relative = _relative_crop_db_path(target)

    # Bbox в таблице faces исторически хранится в координатах CROP.
    crop_x1 = max(0.0, float(bbox[0]) - crop_left) * crop_scale
    crop_y1 = max(0.0, float(bbox[1]) - crop_top) * crop_scale
    crop_x2 = min(
        float(crop.shape[1]) / crop_scale,
        float(bbox[2]) - crop_left,
    ) * crop_scale
    crop_y2 = min(
        float(crop.shape[0]) / crop_scale,
        float(bbox[3]) - crop_top,
    ) * crop_scale

    try:
        conn.execute(
            """
            UPDATE images
            SET path = ?, filename = ?, width = ?, height = ?, processed = 1
            WHERE id = ?
            """,
            (
                relative,
                target.name,
                int(crop.shape[1]),
                int(crop.shape[0]),
                image_id,
            ),
        )
        conn.execute(
            """
            UPDATE faces
            SET x1 = ?, y1 = ?, x2 = ?, y2 = ?
            WHERE id = ?
            """,
            (
                crop_x1,
                crop_y1,
                crop_x2,
                crop_y2,
                face_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        # Файл уже был создан. Не удаляем его: проверка повторного запуска
        # сможет использовать физически существующий CROP.
        return False, f"CROP создан, но БД не обновлена: {exc}"

    source_text = source_description or "источник"
    return True, (
        f"создан из {source_text}; similarity={best_similarity:.3f}; "
        f"face_id={face_id}"
    )


def repair_missing_crops(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    *,
    workers: int = LOCAL_DEFAULT_WORKERS,
    auto_workers: bool = False,
    progress_callback=None,
    pause_event: threading.Event | None = None,
    stop_event: threading.Event | None = None,
    face_ids: set[int] | list[int] | tuple[int, ...] | None = None,
    delete_unavailable_records: bool = True,
) -> dict[str, int | bool]:
    """Проверяет и многопоточно восстанавливает отсутствующие CROP.

    Сначала проверяются локальные источники из images.source_url и
    images.url_image. Если локального файла нет, при заполненном images.url_image
    загружается эта прямая ссылка на фотографию. Если url_image пустой или не
    содержит HTTP(S)-ссылки, используется images.source_url. Для загрузки
    url_image сохраняется Referer source_url.

    workers берётся из поля «Потоки обработки» GUI. При auto_workers=True фактическое
    число одновременно работающих worker-ов динамически изменяется контроллером
    по скорости обработки и загрузке CPU/RAM/HDD/SSD/NET/GPU. Потоки одновременно
    читают исходники с HDD и загружают изображения по HTTP(S). Для каждого
    уникального источника создаётся отдельная задача: источник читается один
    раз, после чего все связанные с ним CROP восстанавливаются последовательно
    внутри этой задачи. Это не допускает лишних повторных скачиваний одного
    и того же URL и сохраняет корректное сопоставление нескольких лиц из одной
    фотографии. Каждый worker использует собственное DB connection.
    """
    query = """
        SELECT
            i.id,
            i.path,
            i.filename,
            i.source_type,
            i.source_url,
            i.url_image,
            f.id,
            f.embedding,
            f.embedding_dim
        FROM images i
        JOIN faces f ON f.image_id = i.id
    """
    params = ()
    if face_ids is not None:
        normalized_face_ids = set()
        for value in face_ids:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                normalized_face_ids.add(value)
        normalized_face_ids = sorted(normalized_face_ids)
        if not normalized_face_ids:
            return {
                "checked": 0, "already_ok": 0, "missing": 0,
                "repaired": 0, "failed": 0,
                "deleted_unavailable_records": 0, "stopped": False,
            }
        placeholders = ",".join("?" for _ in normalized_face_ids)
        query += f" WHERE f.id IN ({placeholders})"
        params = tuple(normalized_face_ids)
    query += " ORDER BY i.id, f.id"
    rows = (
        conn.execute(query, params).fetchall()
        if params
        else conn.execute(query).fetchall()
    )

    total_rows = len(rows)
    missing_rows: list[tuple[Any, ...]] = []
    already_ok = 0
    checked_rows = 0

    def operation_allowed() -> bool:
        if stop_event is not None and stop_event.is_set():
            return False
        if pause_event is not None:
            # Таймаут нужен, чтобы остановка освобождала worker даже при паузе.
            while not pause_event.wait(timeout=0.25):
                if stop_event is not None and stop_event.is_set():
                    return False
        return not (stop_event is not None and stop_event.is_set())

    for row in rows:
        if not operation_allowed():
            break
        image_id = int(row[0])
        crop_path = _resolve_db_crop_path(row[1], image_id=image_id)
        checked_rows += 1
        if crop_path.is_file():
            already_ok += 1
            continue

        missing_rows.append(row)

    total_missing = len(missing_rows)
    if stop_event is not None and stop_event.is_set():
        return {
            "checked": checked_rows,
            "already_ok": already_ok,
            "missing": total_missing,
            "repaired": 0,
            "failed": 0,
            "deleted_unavailable_records": 0,
            "stopped": True,
        }
    workers = max(1, min(int(workers), LOCAL_MAX_WORKERS))

    if progress_callback is not None:
        progress_callback(
            0,
            total_missing,
            0,
            already_ok,
            (
                "Проверка завершена: отсутствующих CROP = "
                f"{total_missing}; потоков = {workers}"
            ),
        )

    if total_missing == 0:
        return {
            "checked": checked_rows,
            "already_ok": already_ok,
            "missing": 0,
            "repaired": 0,
            "failed": 0,
            "deleted_unavailable_records": 0,
            "stopped": False,
        }

    def source_key_for_row(row: tuple[Any, ...]) -> str:
        source_url = row[4]
        url_image = row[5]
        local_source = _source_url_to_local_path(source_url)
        if local_source is not None:
            return str(local_source)

        image_url = str(url_image or "").strip()
        if image_url:
            return image_url
        return str(source_url or "").strip()

    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for row in missing_rows:
        grouped.setdefault(source_key_for_row(row), []).append(row)

    repaired = 0
    failed = 0
    completed = 0
    deleted_unavailable_records = 0
    progress_lock = threading.Lock()
    db_local = threading.local()
    db_connections: list[DatabaseConnection] = []
    db_connections_lock = threading.Lock()

    def worker(group_item):
        nonlocal deleted_unavailable_records
        source_key, group_rows = group_item
        if not operation_allowed():
            return []
        # Одно соединение на поток, а не новое соединение на каждую фотографию.
        # Иначе на большом источнике повторялись и подключение, и инициализация
        # схемы, и проверка путей CROP.
        worker_conn = getattr(db_local, "connection", None)
        if worker_conn is None:
            worker_conn = create_database(
                maintenance=False,
                initialize=False,
                check_crop_migration=False,
            )
            db_local.connection = worker_conn
            with db_connections_lock:
                db_connections.append(worker_conn)

        first_row = group_rows[0]
        source_url = first_row[4]
        image_url = first_row[5]

        source_result = _load_repair_source_image(
            source_url,
            image_url,
        )
        if not operation_allowed():
            return []

        # Если именно прямая ссылка на изображение недоступна после всех
        # HTTP-попыток, удаляем все связанные с этим URL image rows и faces,
        # в том числе записи для других лиц с уже существующими CROP-файлами.
        # Недоступность одного только локального файла сюда не попадает.
        group_uses_same_image_url = all(
            str(row[5] or "").strip() == str(image_url or "").strip()
            for row in group_rows
        )
        if (
            delete_unavailable_records
            and source_result[0] is None
            and _is_direct_http_image_url(image_url)
            and group_uses_same_image_url
        ):
            try:
                deleted_count = _delete_records_for_unavailable_image_url(
                    worker_conn,
                    str(image_url).strip(),
                )
            except Exception as exc:
                deleted_count = 0
                deletion_note = f"не удалось удалить записи БД: {exc}"
            else:
                if deleted_count:
                    with progress_lock:
                        deleted_unavailable_records += deleted_count
                    deletion_note = f"удалено записей БД: {deleted_count}"
                else:
                    deletion_note = "связанных записей в БД не найдено"

            results = []
            for index, row in enumerate(group_rows):
                message = "URL изображения недоступен; " + deletion_note
                if index:
                    message = "тот же недоступный URL; " + deletion_note
                results.append((row, False, message))
            return results

        source_cache = {source_key: source_result}
        used_matches = {source_key: set()}

        results = []
        for row in group_rows:
            if not operation_allowed():
                break
            ok, message = _repair_single_missing_crop(
                worker_conn,
                app,
                row,
                source_cache,
                used_matches,
            )
            results.append((row, ok, message))
        return results

    controller = None
    limiter = AdaptiveConcurrencyLimiter(workers)
    if auto_workers:
        # Для CROP особенно важны сеть и скорость скачивания, но сам inference
        # также учитывается. Controller видит оба класса ресурсов.
        local_source_paths = [
            Path(key) for key in grouped
            if key and not str(key).lower().startswith(("http://", "https://"))
            and not str(key).lower().startswith("file://")
        ]
        controller = AdaptiveWorkerController(
            initial_workers=workers,
            backend=BACKEND_GPU if str(getattr(app, "_face_backend", BACKEND_CPU)) == BACKEND_GPU else BACKEND_CPU,
            operation="crop_repair",
            input_paths=local_source_paths,
            output_path=CROP_DIR,
            input_is_network=(
                not local_source_paths
                and any(str(key).lower().startswith(("http://", "https://")) for key in grouped)
            ),
        )
        limiter.set_target(controller.current_workers)
        if progress_callback is not None:
            progress_callback(
                0,
                total_missing,
                0,
                already_ok,
                f"Авто: старт {controller.current_workers} из {controller.max_workers} потоков",
            )

    def adaptive_group_worker(item):
        acquired = limiter.acquire(None)
        if not acquired:
            return []
        try:
            return worker(item)
        finally:
            limiter.release()

    executor_max_workers = controller.max_workers if controller is not None else workers
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=executor_max_workers,
            thread_name_prefix="crop-repair",
        ) as executor:
            future_groups = {
                executor.submit(adaptive_group_worker, item): len(item[1])
                for item in grouped.items()
            }

            for future in concurrent.futures.as_completed(future_groups):
                try:
                    results = future.result()
                except Exception as exc:
                    # При штатной остановке незавершённая группа не считается ошибкой.
                    if stop_event is not None and stop_event.is_set():
                        continue
                    # Ошибка одной группы не должна останавливать остальные.
                    failed_group_size = int(future_groups[future])
                    with progress_lock:
                        failed += failed_group_size
                        completed += failed_group_size
                        current_completed = completed
                        current_repaired = repaired
                        current_failed = failed
                    if progress_callback is not None:
                        progress_callback(
                            current_completed,
                            total_missing,
                            current_repaired,
                            current_failed,
                            (
                                f"Ошибка worker для группы из {failed_group_size} "
                                f"записей: {exc}"
                            ),
                        )
                    continue

                for row, ok, message in results:
                    with progress_lock:
                        completed += 1
                        if ok:
                            repaired += 1
                        else:
                            failed += 1
                        current_completed = completed
                        current_repaired = repaired
                        current_failed = failed

                    if progress_callback is not None:
                        progress_message = (
                            f"[{current_completed}/{total_missing}] "
                            f"image_id={int(row[0])}: {message}"
                        )
                        if controller is not None:
                            report = controller.observe(current_completed)
                            if report is not None:
                                limiter.set_target(int(report["workers"]))
                                progress_message = controller.status_text(report) + " | " + progress_message
                        progress_callback(
                            current_completed,
                            total_missing,
                            current_repaired,
                            current_failed,
                            progress_message,
                        )
    finally:
        # Завершаем все connection-ы после остановки пула потоков.
        for worker_conn in db_connections:
            try:
                worker_conn.close()
            except Exception:
                pass

    if controller is not None and progress_callback is not None:
        final_report = controller.observe(completed)
        if final_report is not None:
            limiter.set_target(int(final_report["workers"]))
            progress_callback(
                completed,
                total_missing,
                repaired,
                failed,
                controller.status_text(final_report),
            )

    return {
        "checked": total_rows,
        "already_ok": already_ok,
        "missing": total_missing,
        "repaired": repaired,
        "failed": failed,
        "deleted_unavailable_records": deleted_unavailable_records,
        "stopped": bool(stop_event is not None and stop_event.is_set()),
    }


# ============================================================
# CLI SCAN
# ============================================================

def scan_directory(
    conn: DatabaseConnection,
    app: FaceAnalysis,
    directory: Path,
    *,
    delete_processed: bool = False,
    use_json_metadata: bool = True,
):
    """Сканирует папку в консольном режиме."""
    files = (
        path for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )

    total_faces = 0
    processed_files = 0

    print("Обход каталога и обработка выполняются потоково.")
    print(
        "Режим: "
        + ("image + JSON" if use_json_metadata else "только лица + путь к файлу")
    )
    print(
        "Удаление исходников: "
        + ("ВКЛ" if delete_processed else "ВЫКЛ")
    )

    for index, path in enumerate(files, 1):
        processed_files = index
        try:
            json_path = path.with_suffix(".json")

            if use_json_metadata and json_path.is_file():
                result = import_image_bundle(
                    conn,
                    app,
                    path,
                    json_path,
                    delete_source=delete_processed,
                )
                count = int(result["faces"])
                deleted = bool(result.get("deleted"))
            else:
                result = process_local_image(
                    conn,
                    app,
                    path,
                    delete_source=delete_processed,
                )
                count = int(result["faces"])
                deleted = bool(result.get("deleted"))

            total_faces += count
            suffix = ", исходник удалён" if deleted else ""
            print(f"[{index}] {path.name}: лиц={count}{suffix}")

        except Exception as e:
            print(f"[ERROR] {path}: {e}")

    print(
        f"Сканирование завершено. Обработано изображений: {processed_files}; "
        f"добавлено лиц: {total_faces}"
    )
    return total_faces


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "InsightFace + multi-database storage + FAISS + JSON image importer"
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=False,
    )

    subparsers.add_parser(
        "gui",
        help="Запустить графический интерфейс",
    )

    scan_parser = subparsers.add_parser(
        "scan",
        help="Сканировать локальную папку",
    )

    scan_parser.add_argument(
        "directory",
        type=Path,
        help="Папка с фотографиями",
    )
    scan_parser.add_argument(
        "--delete-processed",
        action="store_true",
        help="Удалять исходные файлы после успешного добавления лиц",
    )
    scan_parser.add_argument(
        "--no-json",
        action="store_true",
        help="Игнорировать одноимённые JSON и сохранять только лица + путь к фото",
    )

    subparsers.add_parser(
        "build-index",
        help="Создать FAISS индекс",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="Поиск похожих лиц",
    )

    search_parser.add_argument(
        "image",
        type=Path,
        help="Фотография для поиска",
    )

    search_parser.add_argument(
        "-k",
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="Количество результатов",
    )

    subparsers.add_parser(
        "stats",
        help="Статистика базы",
    )

    args = parser.parse_args()

    if args.command is None:
        args.command = "gui"

    # Окно GUI запускается немедленно: подключение/инициализация БД уже
    # реализованы в FaceSearchGUI через startup worker. Раньше main() выполнял
    # всю миграцию БД синхронно, закрывал соединение и только затем открывал окно.
    if args.command == "gui":
        launch_gui()
        return

    conn = create_database()

    try:
        if args.command == "scan":
            if not args.directory.exists():
                print(
                    f"Папка не существует: {args.directory}"
                )
                sys.exit(1)

            app = create_face_app()

            scan_directory(
                conn,
                app,
                args.directory,
                delete_processed=bool(args.delete_processed),
                use_json_metadata=not bool(args.no_json),
            )

        elif args.command == "build-index":
            build_faiss_index(conn)

        elif args.command == "search":
            if not args.image.exists():
                print(
                    f"Файл не существует: {args.image}"
                )
                sys.exit(1)

            app = create_face_app()

            search_face(
                conn,
                app,
                args.image,
                args.top_k,
            )

        elif args.command == "stats":
            print_stats(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
