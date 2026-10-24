# -*- coding: utf-8 -*-
"""Системная телеметрия и адаптивное управление числом worker-потоков."""
from __future__ import annotations
import ctypes
import os
import re
import shutil
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
try:
    import psutil
except ImportError:
    psutil = None
# Backend identifiers are intentionally local to telemetry: this module must not
# import the GUI module (which would create a circular import).
BACKEND_GPU = "gpu"
BACKEND_CPU = "cpu"

from ..config import (
    APP_DIR, LOCAL_DEFAULT_WORKERS, LOCAL_MAX_WORKERS, AUTO_WORKERS_DEFAULT,
    AUTO_WORKERS_MIN, AUTO_WORKERS_MAX_HARD, AUTO_WORKERS_SAMPLE_SEC,
    AUTO_WORKERS_MIN_CHANGE_SEC, AUTO_WORKERS_SPEED_REGRESSION,
    AUTO_WORKERS_CPU_HIGH, AUTO_WORKERS_CPU_CRITICAL, AUTO_WORKERS_GPU_HIGH,
    AUTO_WORKERS_GPU_CRITICAL, AUTO_WORKERS_RAM_HIGH, AUTO_WORKERS_RAM_CRITICAL,
    AUTO_WORKERS_DISK_HIGH_MBPS, AUTO_WORKERS_NET_SATURATION,
    GPU_INDEX_DEVICE,
)

def _auto_worker_limits() -> tuple[int, int, int]:
    """Возвращает (min, start, max) для текущей машины.

    Для i7-3770 (4C/8T) автоматически получаем примерно 2/6/16.
    Верхний предел остаётся ограничен LOCAL_MAX_WORKERS.
    """
    logical = 0
    physical = 0
    if psutil is not None:
        try:
            logical = int(psutil.cpu_count(logical=True) or 0)
        except Exception:
            pass
        try:
            physical = int(psutil.cpu_count(logical=False) or 0)
        except Exception:
            pass

    if logical <= 0:
        logical = 8
    if physical <= 0:
        physical = max(2, logical // 2)

    minimum = max(AUTO_WORKERS_MIN, min(4, physical))
    start = max(minimum, min(8, physical + 2, logical))
    maximum = min(
        AUTO_WORKERS_MAX_HARD,
        max(start + 2, logical * 2),
    )
    if maximum < start:
        maximum = start
    return minimum, start, maximum


_GPU_METRICS_LOCK = threading.Lock()
_GPU_METRICS_CACHE: dict[str, Any] = {
    "time": 0.0,
    "gpu_util": None,
    "gpu_mem_util": None,
    "gpu_mem_used_mb": None,
    "gpu_mem_total_mb": None,
    "gpu_vram_pct": None,
}
_SYSTEM_METRICS_LOCK = threading.Lock()
_SYSTEM_METRICS_CACHE: dict[str, Any] = {
    "time": 0.0,
    "cpu": None,
    "ram": None,
    "ram_available_gb": None,
}
_WINDOWS_DRIVE_DISK_MAP: dict[str, int] | None = None
_WINDOWS_DRIVE_DISK_MAP_LOCK = threading.Lock()
_NVIDIA_SMI_PATH_CHECKED = False
_NVIDIA_SMI_PATH_CACHE = ""
_NVIDIA_SMI_PATH_LOCK = threading.Lock()


def _find_nvidia_smi() -> str | None:
    """Находит nvidia-smi в PATH и стандартных каталогах драйвера NVIDIA."""
    global _NVIDIA_SMI_PATH_CHECKED, _NVIDIA_SMI_PATH_CACHE
    if _NVIDIA_SMI_PATH_CHECKED:
        return _NVIDIA_SMI_PATH_CACHE or None
    with _NVIDIA_SMI_PATH_LOCK:
        if _NVIDIA_SMI_PATH_CHECKED:
            return _NVIDIA_SMI_PATH_CACHE or None
        candidates: list[Path] = []
        found = shutil.which("nvidia-smi")
        if found:
            candidates.append(Path(found))
        if os.name == "nt":
            program_roots = {
                os.environ.get("ProgramW6432", ""),
                os.environ.get("ProgramFiles", ""),
                r"C:\Program Files",
            }
            for root in program_roots:
                if root:
                    candidates.append(Path(root) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            candidates.append(Path(system_root) / "System32" / "nvidia-smi.exe")
        for candidate in candidates:
            try:
                if candidate.is_file():
                    _NVIDIA_SMI_PATH_CACHE = str(candidate.resolve())
                    break
            except OSError:
                continue
        _NVIDIA_SMI_PATH_CHECKED = True
        return _NVIDIA_SMI_PATH_CACHE or None


def _refresh_cpu_ram_metrics() -> dict[str, Any]:
    """Один владелец счётчика CPU: общий cache читают и GUI, и AUTO."""
    now = time.monotonic()
    result: dict[str, Any] = {"time": now, "cpu": None, "ram": None, "ram_available_gb": None}
    if psutil is not None:
        try:
            result["cpu"] = float(psutil.cpu_percent(interval=None))
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            result["ram"] = float(vm.percent)
            result["ram_available_gb"] = float(vm.available) / (1024 ** 3)
        except Exception:
            pass
    with _SYSTEM_METRICS_LOCK:
        _SYSTEM_METRICS_CACHE.update(result)
        return dict(_SYSTEM_METRICS_CACHE)


def _read_cpu_ram_metrics() -> dict[str, Any]:
    with _SYSTEM_METRICS_LOCK:
        cached = dict(_SYSTEM_METRICS_CACHE)
    if float(cached.get("time") or 0.0) <= 0.0:
        return _refresh_cpu_ram_metrics()
    return cached


def _read_gpu_system_metrics(*, refresh: bool = False) -> dict[str, Any]:
    """Возвращает кэш телеметрии GPU; nvidia-smi запускается только в фоне."""
    now = time.monotonic()
    with _GPU_METRICS_LOCK:
        cached = dict(_GPU_METRICS_CACHE)
    if not refresh or now - float(cached.get("time") or 0) < 1.5:
        return cached

    result = dict(cached)
    smi_path = _find_nvidia_smi()
    if smi_path:
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = subprocess.run(
                [
                    smi_path,
                    "-i", str(GPU_INDEX_DEVICE),
                    "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1.0,
                creationflags=creationflags,
                check=False,
            )
            line = next((x.strip() for x in proc.stdout.splitlines() if x.strip()), "")
            parts = [x.strip() for x in line.split(",")]

            def parse_number(index: int) -> float | None:
                if index >= len(parts) or parts[index].strip().upper() in {"N/A", "NA", "NOT SUPPORTED", "[N/A]"}:
                    return None
                try:
                    return float(parts[index])
                except (TypeError, ValueError):
                    return None

            if proc.returncode == 0 and parts:
                gpu_util = parse_number(0)
                memory_util = parse_number(1)
                memory_used = parse_number(2)
                memory_total = parse_number(3)
                if gpu_util is not None:
                    result["gpu_util"] = gpu_util
                if memory_util is not None:
                    result["gpu_mem_util"] = memory_util
                if memory_used is not None:
                    result["gpu_mem_used_mb"] = memory_used
                if memory_total is not None and memory_total > 0:
                    result["gpu_mem_total_mb"] = memory_total
                used = result.get("gpu_mem_used_mb")
                total = result.get("gpu_mem_total_mb")
                result["gpu_vram_pct"] = float(used) / float(total) * 100.0 if used is not None and total else None
        except Exception:
            pass

    # Torch fallback достоверно даёт VRAM, но не GPU utilization.
    # Берём PyTorch только из sys.modules: телеметрия не должна сама
    # импортировать его и нарушать порядок загрузки CUDA DLL.
    torch_module = sys.modules.get("torch")
    if result.get("gpu_mem_total_mb") is None and torch_module is not None:
        try:
            if torch_module.cuda.is_available():
                free_bytes, total_bytes = torch_module.cuda.mem_get_info(GPU_INDEX_DEVICE)
                total_mb = float(total_bytes) / (1024 ** 2)
                used_mb = max(0.0, float(total_bytes - free_bytes) / (1024 ** 2))
                result["gpu_mem_total_mb"] = total_mb
                result["gpu_mem_used_mb"] = used_mb
                result["gpu_vram_pct"] = used_mb / total_mb * 100.0 if total_mb > 0 else None
        except Exception:
            pass
    result["time"] = now
    with _GPU_METRICS_LOCK:
        _GPU_METRICS_CACHE.update(result)
        return dict(_GPU_METRICS_CACHE)


def _gpu_telemetry_worker(stop_event: threading.Event) -> None:
    """Снимает CPU/RAM/GPU telemetry в фоне, не затрагивая UI thread."""
    while not stop_event.is_set():
        _refresh_cpu_ram_metrics()
        _read_gpu_system_metrics(refresh=True)
        stop_event.wait(2.0)


def _windows_drive_to_disk_map() -> dict[str, int]:
    """Однократно сопоставляет буквы томов с физическими дисками на Windows."""
    global _WINDOWS_DRIVE_DISK_MAP
    if _WINDOWS_DRIVE_DISK_MAP is not None:
        return _WINDOWS_DRIVE_DISK_MAP
    with _WINDOWS_DRIVE_DISK_MAP_LOCK:
        if _WINDOWS_DRIVE_DISK_MAP is not None:
            return _WINDOWS_DRIVE_DISK_MAP
        mapping: dict[str, int] = {}
        if os.name == "nt":
            try:
                command = (
                    "Get-Partition | Where-Object { $_.DriveLetter } | "
                    "ForEach-Object { '{0}={1}' -f $_.DriveLetter, $_.DiskNumber }"
                )
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    creationflags=creationflags,
                    check=False,
                )
                for line in proc.stdout.splitlines():
                    match = re.match(r"\s*([A-Za-z])\s*=\s*(\d+)\s*$", line)
                    if match:
                        mapping[match.group(1).upper()] = int(match.group(2))
            except Exception:
                pass
        _WINDOWS_DRIVE_DISK_MAP = mapping
        return mapping


def _path_drive_letter(path: Any) -> str:
    if path is None:
        return ""
    value = str(path).strip()
    if not value or value.lower().startswith(("http://", "https://", "file://")):
        return ""
    match = re.match(r"^\s*([A-Za-z]):[\\/]", value)
    if match:
        return match.group(1).upper()
    try:
        return Path(value).resolve().drive.rstrip(":").upper()
    except Exception:
        return ""


def _disk_keys_for_paths(paths: list[Any] | tuple[Any, ...], disk_names: set[str]) -> tuple[list[str], str]:
    """Возвращает ключи физических дисков для списка входных путей."""
    labels: list[str] = []
    keys: list[str] = []
    if os.name == "nt":
        drive_map = _windows_drive_to_disk_map()
        for path in paths:
            letter = _path_drive_letter(path)
            if not letter:
                continue
            labels.append(f"{letter}:")
            disk_number = drive_map.get(letter)
            if disk_number is not None:
                candidates = [
                    name for name in disk_names
                    if re.search(rf"(?:PhysicalDrive|Disk)\s*{disk_number}\b", name, re.IGNORECASE)
                    or name.strip().upper() == str(disk_number)
                ]
                if candidates:
                    keys.append(candidates[0])
    else:
        try:
            partitions = sorted(
                psutil.disk_partitions(all=True) if psutil is not None else [],
                key=lambda p: len(str(p.mountpoint)),
                reverse=True,
            )
            for path in paths:
                value = str(path)
                partition = next(
                    (p for p in partitions if value.startswith(str(p.mountpoint))),
                    None,
                )
                if partition:
                    labels.append(str(partition.mountpoint))
                    device = str(partition.device).split("/")[-1]
                    candidates = [name for name in disk_names if name == device or name.startswith(device)]
                    if candidates:
                        keys.append(candidates[0])
        except Exception:
            pass
    return sorted(set(keys)), ",".join(sorted(set(labels))) or "системный"


def _disk_busy_percent(old, new, elapsed: float) -> float | None:
    if old is None or new is None:
        return None
    try:
        old_ms = float(getattr(old, "read_time", 0) or 0) + float(getattr(old, "write_time", 0) or 0)
        new_ms = float(getattr(new, "read_time", 0) or 0) + float(getattr(new, "write_time", 0) or 0)
        if new_ms < old_ms:
            return None
        return min(100.0, max(0.0, (new_ms - old_ms) / max(0.1, elapsed) / 10.0))
    except Exception:
        return None


def _read_system_metrics(
    previous: dict[str, Any] | None = None,
    *,
    input_paths: list[Any] | tuple[Any, ...] | None = None,
    output_path: Any = None,
    input_is_network: bool = False,
) -> dict[str, Any]:
    """Снимок CPU/RAM/GPU/NET и отдельно скорость дискового чтения/записи.

    Для локальных источников по возможности измеряется физический диск с
    изображениями отдельно от физического диска каталога CROP. Если драйвер
    ОС не раскрывает счётчики по устройствам, используется общий I/O как fallback.
    """
    now = time.monotonic()
    dt = max(0.1, now - float(previous.get("time", now - 1.0))) if previous else 1.0
    metrics: dict[str, Any] = {
        "time": now, "cpu": 0.0, "ram": 0.0, "ram_available_gb": 0.0,
        "disk_read_mbps": 0.0, "disk_write_mbps": 0.0, "disk_total_mbps": 0.0,
        "input_disk_read_mbps": 0.0, "crop_disk_write_mbps": 0.0,
        "input_disk_busy_pct": None, "crop_disk_busy_pct": None,
        "input_disk_label": "источник", "crop_disk_label": "CROP",
        "net_mbps": 0.0, "net_link_mbps": None, "net_util": None,
    }
    disk_by_name = {}
    net = None
    live_system = _read_cpu_ram_metrics()
    if live_system.get("cpu") is not None:
        metrics["cpu"] = float(live_system["cpu"])
    if live_system.get("ram") is not None:
        metrics["ram"] = float(live_system["ram"])
    if live_system.get("ram_available_gb") is not None:
        metrics["ram_available_gb"] = float(live_system["ram_available_gb"])

    if psutil is not None:
        try:
            disk_by_name = psutil.disk_io_counters(perdisk=True) or {}
            metrics["disk_by_name"] = disk_by_name
            if previous:
                old_disks = previous.get("disk_by_name") or {}
                read_bytes = write_bytes = 0.0
                for name, current in disk_by_name.items():
                    old = old_disks.get(name)
                    if old is None:
                        continue
                    read_bytes += max(0.0, float(current.read_bytes) - float(old.read_bytes))
                    write_bytes += max(0.0, float(current.write_bytes) - float(old.write_bytes))
                metrics["disk_read_mbps"] = read_bytes / dt / (1024 ** 2)
                metrics["disk_write_mbps"] = write_bytes / dt / (1024 ** 2)
        except Exception:
            metrics["disk_by_name"] = {}
        try:
            net = psutil.net_io_counters()
            if previous and net is not None and previous.get("net") is not None:
                old_net = previous["net"]
                delta_bytes = (
                    max(0.0, float(net.bytes_recv) - float(old_net.bytes_recv))
                    + max(0.0, float(net.bytes_sent) - float(old_net.bytes_sent))
                )
                metrics["net_mbps"] = delta_bytes * 8.0 / dt / 1_000_000.0
        except Exception:
            net = None
        metrics["net"] = net
        try:
            speeds = [
                float(stat.speed)
                for stat in psutil.net_if_stats().values()
                if getattr(stat, "isup", False) and float(getattr(stat, "speed", 0) or 0) > 0
            ]
            if speeds:
                metrics["net_link_mbps"] = max(speeds)
                metrics["net_util"] = metrics["net_mbps"] / metrics["net_link_mbps"]
        except Exception:
            pass

    disk_names = set((metrics.get("disk_by_name") or {}).keys())
    old_disks = (previous or {}).get("disk_by_name") or {}
    in_keys, input_label = _disk_keys_for_paths(list(input_paths or []), disk_names)
    out_keys, output_label = _disk_keys_for_paths([output_path] if output_path else [], disk_names)
    if input_is_network and not input_paths:
        input_label = "HTTP(S)"
    metrics["input_disk_label"] = input_label
    metrics["crop_disk_label"] = output_label
    generic_labels = {"системный", "все диски", "источник", "CROP"}
    metrics["input_output_same_disk"] = bool(set(in_keys) & set(out_keys)) or (
        input_label == output_label and input_label not in generic_labels
    )

    def rates_for(keys: list[str], direction: str) -> tuple[float, float | None]:
        counters = metrics.get("disk_by_name") or {}
        if not previous or not keys:
            return 0.0, None
        old = (previous.get("disk_by_name") or {})
        total_bytes = 0.0
        busy_values = []
        for key in keys:
            cur = counters.get(key)
            prev = old.get(key)
            if cur is None or prev is None:
                continue
            field = "read_bytes" if direction == "read" else "write_bytes"
            total_bytes += max(0.0, float(getattr(cur, field, 0)) - float(getattr(prev, field, 0)))
            busy_value = _disk_busy_percent(prev, cur, dt)
            if busy_value is not None:
                busy_values.append(busy_value)
        rate = total_bytes / dt / (1024 ** 2)
        return rate, max(busy_values) if busy_values else None

    input_rate, input_busy = rates_for(in_keys, "read")
    crop_rate, crop_busy = rates_for(out_keys, "write")
    if not in_keys:
        if input_is_network and not input_paths:
            # У удалённых источников скорость относится к NET; общий системный
            # disk-read не выдаём за чтение исходных изображений.
            input_rate = 0.0
            input_busy = None
            metrics["input_disk_label"] = "HTTP(S)"
        else:
            input_rate = float(metrics.get("disk_read_mbps") or 0.0)
            metrics["input_disk_label"] = "все диски"
    if not out_keys:
        crop_rate = float(metrics.get("disk_write_mbps") or 0.0)
        metrics["crop_disk_label"] = "все диски"
    metrics["input_disk_read_mbps"] = input_rate
    metrics["crop_disk_write_mbps"] = crop_rate
    metrics["input_disk_busy_pct"] = input_busy
    metrics["crop_disk_busy_pct"] = crop_busy
    metrics["disk_total_mbps"] = input_rate + crop_rate
    metrics.update(_read_gpu_system_metrics(refresh=False))
    return metrics


class AdaptiveWorkerController:
    """Постепенно подбирает число worker-потоков по измеренной пропускной способности.

    Контроллер не пытается держать конкретный ресурс на 100% любой ценой.
    Основной критерий — изображения/сек, а загрузки CPU/GPU/RAM/диска/сети
    используются как ограничители и признаки узкого места.
    """

    def __init__(
        self,
        *,
        initial_workers: int,
        backend: str,
        operation: str,
        input_paths: list[Any] | tuple[Any, ...] | None = None,
        output_path: Any = None,
        input_is_network: bool = False,
        remote_db: bool = False,
        auto_adjust: bool = True,
    ):
        self.enabled = True
        self.backend = backend
        self.operation = operation
        self.input_paths = list(input_paths or [])
        self.output_path = output_path
        self.input_is_network = bool(input_is_network)
        # GUI may monitor resources while the user has selected a fixed worker
        # count. In that mode the controller reports telemetry but never changes
        # the requested concurrency.
        self.auto_adjust = bool(auto_adjust)
        # The caller may use a server database over the network. This flag is
        # diagnostic only: traffic is still measured from system network counters.
        self.remote_db = bool(remote_db)
        min_w, start_w, max_w = _auto_worker_limits()
        if self.auto_adjust:
            self.min_workers = min_w
            self.max_workers = max_w
            requested_workers = int(initial_workers or start_w)
            self.current_workers = max(
                self.min_workers,
                min(self.max_workers, requested_workers),
            )
        else:
            # Preserve even a manual setting below the AUTO minimum (e.g. 1).
            requested_workers = max(1, int(initial_workers or 1))
            self.min_workers = 1
            self.max_workers = requested_workers
            self.current_workers = requested_workers
        self.last_report = time.monotonic()
        self.last_change = self.last_report
        self.last_completed = 0
        self.last_speed = 0.0
        self.speed_ema = 0.0
        self.previous_target = self.current_workers
        self.baseline_after_change: float | None = None
        self.last_reason = "старт автоподстройки"
        self._last_sample = _read_system_metrics(
            input_paths=self.input_paths,
            output_path=self.output_path,
            input_is_network=self.input_is_network,
        )
        self._last_metrics: dict[str, Any] = self._last_sample

    def observe(self, completed: int, *, adapt: bool = True) -> dict[str, Any] | None:
        now = time.monotonic()
        elapsed = now - self.last_report
        if elapsed < AUTO_WORKERS_SAMPLE_SEC and completed - self.last_completed < max(2, self.current_workers // 2):
            return None

        metrics = _read_system_metrics(
            self._last_sample,
            input_paths=self.input_paths,
            output_path=self.output_path,
            input_is_network=self.input_is_network,
        )
        done_delta = max(0, int(completed) - self.last_completed)
        speed = done_delta / max(0.1, elapsed)
        if self.speed_ema <= 0:
            self.speed_ema = speed
        else:
            self.speed_ema = self.speed_ema * 0.65 + speed * 0.35

        old_workers = self.current_workers
        new_workers = old_workers
        reason = "стабильно"

        cpu = float(metrics.get("cpu") or 0.0)
        ram = float(metrics.get("ram") or 0.0)
        ram_free = float(metrics.get("ram_available_gb") or 0.0)
        gpu = metrics.get("gpu_util")
        gpu = float(gpu) if gpu is not None else None
        gpu_vram = metrics.get("gpu_vram_pct")
        gpu_vram = float(gpu_vram) if gpu_vram is not None else None
        input_disk_busy = metrics.get("input_disk_busy_pct")
        crop_disk_busy = metrics.get("crop_disk_busy_pct")
        input_disk_busy = float(input_disk_busy) if input_disk_busy is not None else None
        crop_disk_busy = float(crop_disk_busy) if crop_disk_busy is not None else None
        input_disk_rate = float(metrics.get("input_disk_read_mbps") or 0.0)
        crop_disk_rate = float(metrics.get("crop_disk_write_mbps") or 0.0)
        net_util = metrics.get("net_util")
        net_util = float(net_util) if net_util is not None else None

        saturated = []
        if ram >= AUTO_WORKERS_RAM_HIGH or ram_free < 1.5:
            saturated.append("RAM")
        if cpu >= AUTO_WORKERS_CPU_HIGH:
            saturated.append("CPU")
        if self.backend == BACKEND_GPU and gpu is not None and gpu >= AUTO_WORKERS_GPU_HIGH:
            saturated.append("GPU")
        if self.backend == BACKEND_GPU and gpu_vram is not None and gpu_vram >= 92.0:
            saturated.append("VRAM")
        same_io_disk = bool(metrics.get("input_output_same_disk"))
        if same_io_disk:
            shared_busy_values = [v for v in (input_disk_busy, crop_disk_busy) if v is not None]
            shared_busy = max(shared_busy_values) if shared_busy_values else None
            if shared_busy is not None and shared_busy >= 90.0:
                saturated.append(
                    f"общий диск чтения/записи ({metrics.get('input_disk_label', 'источник')})"
                )
            elif shared_busy is None and input_disk_rate + crop_disk_rate >= AUTO_WORKERS_DISK_HIGH_MBPS:
                saturated.append("общая пропускная способность диска")
        else:
            if input_disk_busy is not None and input_disk_busy >= 90.0:
                saturated.append("диск чтения исходников")
            elif input_disk_busy is None and input_disk_rate >= AUTO_WORKERS_DISK_HIGH_MBPS:
                saturated.append("пропускная способность чтения")
            if crop_disk_busy is not None and crop_disk_busy >= 90.0:
                saturated.append("диск записи CROP")
            elif crop_disk_busy is None and crop_disk_rate >= AUTO_WORKERS_DISK_HIGH_MBPS:
                saturated.append("пропускная способность записи CROP")
        if net_util is not None and net_util >= AUTO_WORKERS_NET_SATURATION:
            saturated.append(
                "сеть / удалённая БД" if self.remote_db else "сеть"
            )

        regression = False
        if self.baseline_after_change is not None and speed > 0:
            regression = speed < self.baseline_after_change * AUTO_WORKERS_SPEED_REGRESSION

        enough_time = now - self.last_change >= AUTO_WORKERS_MIN_CHANGE_SEC
        severe = (
            ram >= AUTO_WORKERS_RAM_CRITICAL
            or cpu >= AUTO_WORKERS_CPU_CRITICAL
            or (self.backend == BACKEND_GPU and gpu is not None and gpu >= AUTO_WORKERS_GPU_CRITICAL)
            or (self.backend == BACKEND_GPU and gpu_vram is not None and gpu_vram >= 97.0)
            or (
                bool(metrics.get("input_output_same_disk"))
                and max([v for v in (input_disk_busy, crop_disk_busy) if v is not None], default=0.0) >= 98.0
            )
            or (
                not bool(metrics.get("input_output_same_disk"))
                and (
                    (input_disk_busy is not None and input_disk_busy >= 98.0)
                    or (crop_disk_busy is not None and crop_disk_busy >= 98.0)
                )
            )
        )

        if not adapt:
            # Monitor-only samples keep resource telemetry fresh while a network
            # request or DB import is slow, without changing worker concurrency.
            reason = "ожидание завершения задач; телеметрия обновлена"
        elif not self.auto_adjust:
            reason = "ручной режим, число потоков не меняется"
        elif enough_time:
            if severe or regression:
                if old_workers > self.min_workers:
                    new_workers = old_workers - 1
                    reason = (
                        "регресс скорости после увеличения"
                        if regression and not severe
                        else "ресурс близок к пределу: " + ", ".join(saturated)
                    )
            elif not saturated:
                # Увеличиваем только когда скорость хотя бы не падает и
                # основные ресурсы не выглядят насыщенными.
                if self.last_speed <= 0 or speed >= self.last_speed * 0.97:
                    if old_workers < self.max_workers:
                        new_workers = old_workers + 1
                        reason = "ресурсы свободны, увеличиваем нагрузку"
            else:
                reason = "ограничение: " + ", ".join(saturated)

        changed = new_workers != old_workers
        if changed:
            self.previous_target = old_workers
            self.current_workers = new_workers
            self.last_change = now
            self.baseline_after_change = speed if speed > 0 else self.speed_ema
        elif regression and old_workers <= self.min_workers:
            self.baseline_after_change = None

        self.last_completed = int(completed)
        self.last_speed = speed
        self.last_report = now
        self._last_sample = metrics
        self._last_metrics = metrics
        self.last_reason = reason

        gpu_text = f"{gpu:.0f}%" if gpu is not None else "н/д"
        net_mbps = float(metrics.get("net_mbps") or 0.0)
        # Не округляем небольшие сетевые скорости до «0.0 Mbps»:
        # при загрузке множества небольших файлов они часто ниже 0.1 Mbps.
        if net_mbps >= 0.01:
            net_text = f"{net_mbps:.2f} Mbps"
        elif net_mbps > 0:
            net_text = f"{net_mbps * 1000.0:.1f} Kbps"
        else:
            net_text = "0 Mbps"
        same_io_disk = bool(metrics.get("input_output_same_disk"))
        write_label = "WRITE" if self.operation == "vk_download" else "CROP-WRITE"
        disk_text = (
            f"READ[{metrics.get('input_disk_label', 'источник')}] {input_disk_rate:.1f} MB/s"
            f" / {write_label}[{metrics.get('crop_disk_label', 'CROP')}] {crop_disk_rate:.1f} MB/s"
            + (" (общий физический диск)" if same_io_disk else "")
        )
        report = {
            "workers": self.current_workers,
            "old_workers": old_workers,
            "changed": changed,
            "speed": speed,
            "speed_ema": self.speed_ema,
            "cpu": cpu,
            "ram": ram,
            "ram_available_gb": ram_free,
            "gpu": gpu,
            "gpu_mem_util": metrics.get("gpu_mem_util"),
            "gpu_vram_pct": gpu_vram,
            "disk_total_mbps": float(metrics.get("disk_total_mbps") or 0.0),
            "disk_text": disk_text,
            "input_disk_read_mbps": input_disk_rate,
            "crop_disk_write_mbps": crop_disk_rate,
            "input_disk_busy_pct": input_disk_busy,
            "crop_disk_busy_pct": crop_disk_busy,
            "net_mbps": metrics.get("net_mbps"),
            "net_text": net_text,
            "reason": reason,
            "bottlenecks": saturated,
        }
        return report

    def status_text(self, report: dict[str, Any]) -> str:
        bottlenecks = report.get("bottlenecks") or []
        bottleneck_text = ", ".join(bottlenecks) if bottlenecks else "узкого места нет"
        vram = report.get("gpu_vram_pct")
        vram_text = f"{float(vram):.0f}%" if vram is not None else "н/д"
        gpu = report.get("gpu")
        gpu_text = f"{float(gpu):.0f}%" if gpu is not None else "н/д"
        mode_text = (
            f"Авто: {int(report['workers'])} поток(ов)"
            if self.auto_adjust
            else f"Мониторинг (ручной режим): фиксировано {int(report['workers'])} поток(ов)"
        )
        return (
            f"{mode_text} • "
            f"{float(report['speed_ema']):.2f} ф/с • "
            f"CPU {float(report['cpu']):.0f}% • "
            f"GPU {gpu_text} • VRAM {vram_text} • "
            f"RAM {float(report['ram']):.0f}% • "
            f"{report['disk_text']} • "
            f"NET {report['net_text']} • "
            f"{report['reason']} • {bottleneck_text}"
        )


class AdaptiveConcurrencyLimiter:
    """Позволяет менять число одновременно выполняемых worker-ов без пересоздания pool."""

    def __init__(self, target: int):
        self._condition = threading.Condition()
        self._target = max(1, int(target))
        self._active = 0

    @property
    def target(self) -> int:
        with self._condition:
            return self._target

    def set_target(self, target: int) -> None:
        with self._condition:
            self._target = max(1, int(target))
            self._condition.notify_all()

    def acquire(self, stop_event: threading.Event | None = None) -> bool:
        with self._condition:
            while self._active >= self._target:
                if stop_event is not None and stop_event.is_set():
                    return False
                self._condition.wait(timeout=0.25)
            self._active += 1
            return True

    def release(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()
