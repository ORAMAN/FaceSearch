# -*- coding: utf-8 -*-
"""Единый DB-API интерфейс для четырёх поддерживаемых СУБД."""
from __future__ import annotations
import re
import threading
import time
from ..config import DB_TYPE_SQLITE, DB_TYPE_POSTGRESQL, DB_TYPE_MYSQL, DB_TYPE_MSSQL

class DBResult:
    """Небольшой совместимый слой над DB-API cursor."""

    def __init__(self, cursor):
        self.cursor = cursor

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchmany(self, size: int | None = None):
        """Читает ограниченную пачку строк, не материализуя весь результат запроса."""
        return self.cursor.fetchmany() if size is None else self.cursor.fetchmany(int(size))

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass

    @property
    def rowcount(self):
        return self.cursor.rowcount

    @property
    def lastrowid(self):
        value = getattr(self.cursor, "lastrowid", None)
        if value is not None:
            return value
        try:
            row = self.cursor.fetchone()
            if row:
                return row[0]
        except Exception:
            pass
        return None


class DatabaseConnection:
    """
    Единый интерфейс для SQLite / PostgreSQL / MySQL / MS SQL Server.

    Остальной код проекта продолжает использовать:
        conn.execute(...).fetchone()
        conn.execute(...).fetchall()
        conn.executemany(...)
        conn.commit()
        conn.rollback()
        conn.close()

    SQL с '?' автоматически преобразуется в параметризацию драйвера.
    """

    def __init__(self, raw_connection, backend: str):
        self.raw = raw_connection
        self.backend = backend

    @property
    def is_sqlite(self) -> bool:
        return self.backend == DB_TYPE_SQLITE

    def _adapt_sql(self, sql: str) -> str:
        sql = str(sql)

        if self.backend == DB_TYPE_MSSQL:
            # SQL Server не поддерживает LIMIT 1.
            import re
            sql = re.sub(
                r"SELECT(\s+)LIMIT\s+1",
                r"SELECT\1TOP 1",
                sql,
                flags=re.IGNORECASE,
            )
            # На случай SELECT ... FROM ... LIMIT 1.
            if re.search(r"\sLIMIT\s+1\s*$", sql, re.IGNORECASE):
                sql = re.sub(r"\sLIMIT\s+1\s*$", "", sql, flags=re.IGNORECASE)

        if self.backend in (DB_TYPE_POSTGRESQL, DB_TYPE_MYSQL):
            # В основном коде проекта параметры SQL записаны в DB-API
            # стиле SQLite: '?'. И PostgreSQL (psycopg), и MySQL (PyMySQL)
            # используют '%s'. Без этого преобразования PyMySQL получает
            # запрос с '?' и параметрами, что приводит к:
            # "not all arguments converted during string formatting".
            sql = sql.replace("?", "%s")

        return sql

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor()
        adapted = self._adapt_sql(sql)
        if params is None:
            params = ()

        # Получаем ID вставленной строки единообразно для серверных СУБД.
        if self.backend == DB_TYPE_POSTGRESQL and re.match(
            r"\s*INSERT\s+INTO\s+(profiles|images|faces)\b",
            adapted,
            re.IGNORECASE,
        ) and " RETURNING " not in adapted.upper():
            adapted = adapted.rstrip().rstrip(";") + " RETURNING id"
        elif self.backend == DB_TYPE_MSSQL and re.match(
            r"\s*INSERT\s+INTO\s+(profiles|images|faces)\b",
            adapted,
            re.IGNORECASE,
        ):
            # pyodbc + SQL Server: не используем SCOPE_IDENTITY() отдельным
            # SELECT после INSERT. При параллельном сканировании это даёт
            # менее надёжное поведение с несколькими result-set/rowcount.
            # OUTPUT INSERTED.id возвращает ID непосредственно из INSERT.
            stripped = adapted.rstrip().rstrip(";")
            if "OUTPUT INSERTED.ID" not in stripped.upper():
                match = re.search(r"\bVALUES\b", stripped, re.IGNORECASE)
                if match:
                    adapted = (
                        stripped[:match.start()]
                        + "OUTPUT INSERTED.id "
                        + stripped[match.start():]
                    )
                else:
                    # Теоретический fallback для INSERT ... SELECT.
                    match = re.search(r"\bSELECT\b", stripped, re.IGNORECASE)
                    if match:
                        adapted = (
                            stripped[:match.start()]
                            + "OUTPUT INSERTED.id "
                            + stripped[match.start():]
                        )
                    else:
                        adapted = stripped

        cur.execute(adapted, params)
        return DBResult(cur)

    def execute_streaming(self, sql: str, params=()):
        """Выполняет SELECT потоковым курсором там, где драйвер это поддерживает.

        Нужен для чтения миллионов embedding из удалённой БД: обычные курсоры
        psycopg/PyMySQL могут буферизовать весь ответ до первого fetchmany().
        """
        if self.raw is None:
            raise RuntimeError("Соединение с БД уже закрыто.")
        if self.backend == DB_TYPE_POSTGRESQL:
            cursor_name = f"face_faiss_{threading.get_ident()}_{time.time_ns()}"
            cur = self.raw.cursor(name=cursor_name)
        elif self.backend == DB_TYPE_MYSQL:
            import pymysql
            cur = self.raw.cursor(pymysql.cursors.SSCursor)
        else:
            # SQLite/pyodbc сами выдают строки по мере fetchmany; сохраняем
            # обычный cursor, совместимый с текущей реализацией этих драйверов.
            cur = self.raw.cursor()
        cur.execute(self._adapt_sql(sql), tuple(params or ()))
        return DBResult(cur)

    def executemany(self, sql: str, seq_of_params):
        cur = self.raw.cursor()
        cur.executemany(self._adapt_sql(sql), seq_of_params)
        return DBResult(cur)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        """Безопасно закрывает соединение и допускает повторный вызов.

        GUI может закрыть соединение при закрытии окна, а внешний main()/finally
        затем попытаться закрыть тот же объект ещё раз. Для PyMySQL это вызывает
        "Already closed".
        """
        raw = self.raw
        if raw is None:
            return

        # Сразу помечаем объект закрытым, чтобы повторный вызов был безопасным.
        self.raw = None
        try:
            raw.close()
        except Exception as exc:
            # PyMySQL/pyodbc могут сообщать, что соединение уже закрыто.
            # Это нормальная ситуация при повторном close().
            if "already closed" not in str(exc).lower():
                raise


def _safe_db_identifier(name: str) -> str:
    """Проверка имени базы перед DDL. Значение никогда не подставляется из SQL-параметра."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("Имя базы данных не задано.")
    if len(name) > 128:
        raise ValueError("Имя базы данных слишком длинное.")
    if any(ch in name for ch in "\x00\r\n\t"):
        raise ValueError("Имя базы данных содержит недопустимые символы.")
    return name


def _quote_identifier(name: str, backend: str) -> str:
    name = _safe_db_identifier(name)
    if backend == DB_TYPE_MSSQL:
        return "[" + name.replace("]", "]]") + "]"
    if backend == DB_TYPE_MYSQL:
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'
