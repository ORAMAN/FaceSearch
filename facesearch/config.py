# -*- coding: utf-8 -*-
"""Общие настройки и пути FaceSearch. Не импортирует ML-библиотеки."""
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

MODEL_NAME = "buffalo_l"
DB_FILE = "faces.db"
INDEX_FILE = "faces.faiss"
DET_THRESHOLD = 0.50
DET_SIZE = (640, 640)
DEFAULT_TOP_K = 24
# Количество путей в одном пакете при потоковом сканировании каталога.
# Значение 0 в настройках GUI означает один общий пакет из всех найденных изображений.
DEFAULT_SCAN_BATCH_SIZE = 1000
MAX_SCAN_BATCH_SIZE = 100000
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LOCAL_DEFAULT_WORKERS = 8
LOCAL_MAX_WORKERS = 24
REPAIR_HTTP_TIMEOUT = 30
REPAIR_HTTP_RETRIES = 4
REPAIR_HTTP_BACKOFF = 0.75
REPAIR_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)
GPU_WARMUP = True
GPU_INDEX_USE_FP16 = True
GPU_INDEX_RESERVE_GB = 2.00
GPU_INDEX_DEVICE = 0
FAISS_BUILD_BATCH_ROWS = 8192
FAISS_GPU_COPY_CHUNK_ROWS = 16384
DB_RESULT_BATCH_SIZE = 500
CROP_MARGIN = 0.20
CROP_JPEG_QUALITY = 95
AUTO_WORKERS_DEFAULT = True
AUTO_WORKERS_MIN = 2
AUTO_WORKERS_MAX_HARD = LOCAL_MAX_WORKERS
AUTO_WORKERS_SAMPLE_SEC = 3.0
AUTO_WORKERS_MIN_CHANGE_SEC = 5.0
AUTO_WORKERS_SPEED_REGRESSION = 0.93
AUTO_WORKERS_CPU_HIGH = 90.0
AUTO_WORKERS_CPU_CRITICAL = 96.0
AUTO_WORKERS_GPU_HIGH = 92.0
AUTO_WORKERS_GPU_CRITICAL = 97.0
AUTO_WORKERS_RAM_HIGH = 88.0
AUTO_WORKERS_RAM_CRITICAL = 94.0
AUTO_WORKERS_DISK_HIGH_MBPS = 300.0
AUTO_WORKERS_NET_SATURATION = 0.85
PROFILE_COLUMNS = {
    "domain": "TEXT", "site": "TEXT", "status": "TEXT",
    "followers_count": "INTEGER", "online": "INTEGER",
    "last_seen_time": "INTEGER", "last_seen_platform": "INTEGER",
    "verified": "INTEGER", "is_verified": "INTEGER",
    "occupation_type": "TEXT", "occupation_id": "INTEGER", "occupation_name": "TEXT",
}
IMAGE_COLUMNS = {
    "source_type": "TEXT NOT NULL DEFAULT 'local'", "source_url": "TEXT",
    "url_image": "TEXT", "profile_id": "INTEGER", "source_updated_at": "TEXT",\n    "metadata_json": "TEXT",
}
DB_CONFIG_FILE = APP_DIR / "database_profiles.json"
SETTINGS_FILE = APP_DIR / "settings.xml"
DB_TYPE_SQLITE = "sqlite"
DB_TYPE_POSTGRESQL = "postgresql"
DB_TYPE_MYSQL = "mysql"
DB_TYPE_MSSQL = "mssql"
DB_TYPE_LABELS = {
    DB_TYPE_SQLITE: "SQLite", DB_TYPE_POSTGRESQL: "PostgreSQL",
    DB_TYPE_MYSQL: "MySQL", DB_TYPE_MSSQL: "Microsoft SQL Server",
}
DB_TYPE_DRIVERS = {
    DB_TYPE_SQLITE: "встроен", DB_TYPE_POSTGRESQL: "psycopg",
    DB_TYPE_MYSQL: "PyMySQL", DB_TYPE_MSSQL: "pyodbc",
}
DEFAULT_DB_PROFILE_NAME = "SQLite — faces"
DB_DEFAULT_PORTS = {DB_TYPE_POSTGRESQL: 5432, DB_TYPE_MYSQL: 3306, DB_TYPE_MSSQL: 1433}
