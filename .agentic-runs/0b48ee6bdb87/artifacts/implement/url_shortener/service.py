"""SQLite-backed URL shortener with a small WSGI JSON API."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 16_384
MAX_URL_LENGTH = 2_048
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_CODE_ATTEMPTS = 5


class CodeGenerationError(RuntimeError):
    """Raised when the code generator repeatedly produces existing codes."""


class SQLiteURLStore:
    def __init__(
        self,
        database_path: str | Path,
        code_generator: Callable[[], str] | None = None,
    ) -> None:
        self.database_path = str(Path(database_path).expanduser())
        if self.database_path == ":memory:":
            raise ValueError("a filesystem database path is required for persistent connections")
        Path(self.database_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._code_generator = code_generator or (lambda: secrets.token_urlsafe(6))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS short_urls (
                    code TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    clicks INTEGER NOT NULL DEFAULT 0 CHECK (clicks >= 0),
                    last_accessed_at TEXT
                )
                """
            )

    def create_url(self, target_url: str) -> dict[str, Any]:
        created_at = _utc_now()
        for _ in range(MAX_CODE_ATTEMPTS):
            code = self._code_generator()
            if not CODE_PATTERN.fullmatch(code):
                raise ValueError("code generator produced an invalid short code")
            with closing(self._connect()) as connection, connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO short_urls (code, target_url, created_at) VALUES (?, ?, ?)",
                    (code, target_url, created_at),
                )
                if cursor.rowcount == 1:
                    return {"code": code, "created_at": created_at}
        raise CodeGenerationError("unable to allocate a unique short code")

    def record_click(self, code: str) -> str | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT target_url FROM short_urls WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE short_urls
                SET clicks = clicks + 1, last_accessed_at = ?
                WHERE code = ?
                """,
                (_utc_now(), code),
            )
            return str(row["target_url"])

    def get_stats(self, code: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT code, target_url, created_at, clicks, last_accessed_at
                FROM short_urls WHERE code = ?
                """,
                (code,),
            ).fetchone()
        if row is None:
            return None
        return {
            "code": str(row["code"]),
            "target_url": str(row["target_url"]),
            "created_at": str(row["created_at"]),
            "clicks": int(row["clicks"]),
            "last_accessed_at": row["last_accessed_at"],
        }


class UrlShortenerApp:
    def __init__(self, database_path: str | Path, base_url: str) -> None:
        parsed_base = urlsplit(base_url)
        if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.store = SQLiteURLStore(database_path)

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            status, headers, body = self._dispatch(environ)
        except (sqlite3.Error, CodeGenerationError):
            error_stream = environ.get("wsgi.errors")
            if error_stream is not None:
                error_stream.write("URL shortener database operation failed\n")
            status, headers, body = _json_response(503, {"error": "service temporarily unavailable"})
        start_response(status, headers)
        return [body]

    def _dispatch(self, environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]], bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))

        if path == "/health":
            if method != "GET":
                return _method_not_allowed("GET")
            return _json_response(200, {"status": "ok"})

        if path == "/api/urls":
            if method != "POST":
                return _method_not_allowed("POST")
            request = _read_json_request(environ)
            if isinstance(request, tuple):
                return request
            if set(request) != {"url"}:
                return _json_response(400, {"error": "request body must contain only the url field"})
            target_url = request.get("url")
            if not isinstance(target_url, str) or not _is_valid_target_url(target_url):
                return _json_response(400, {"error": "url must be an absolute HTTP(S) URL"})
            created = self.store.create_url(target_url)
            return _json_response(
                201,
                {
                    "code": created["code"],
                    "short_url": f"{self.base_url}/{created['code']}",
                    "target_url": target_url,
                    "created_at": created["created_at"],
                },
            )

        stats_match = re.fullmatch(r"/api/urls/([A-Za-z0-9_-]{1,64})/stats", path)
        if stats_match:
            if method != "GET":
                return _method_not_allowed("GET")
            stats = self.store.get_stats(stats_match.group(1))
            if stats is None:
                return _json_response(404, {"error": "short URL not found"})
            return _json_response(200, stats)

        if path.startswith("/api/"):
            return _json_response(404, {"error": "endpoint not found"})
        if method != "GET":
            return _method_not_allowed("GET")
        code = path.removeprefix("/")
        if "/" in code or not CODE_PATTERN.fullmatch(code):
            return _json_response(404, {"error": "short URL not found"})
        target_url = self.store.record_click(code)
        if target_url is None:
            return _json_response(404, {"error": "short URL not found"})
        return (
            "302 Found",
            [("Location", target_url), ("Content-Length", "0"), ("Cache-Control", "no-store")],
            b"",
        )


def _read_json_request(environ: dict[str, Any]) -> dict[str, Any] | tuple[str, list[tuple[str, str]], bytes]:
    content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _json_response(415, {"error": "Content-Type must be application/json"})
    try:
        content_length = int(environ.get("CONTENT_LENGTH") or "0")
    except (TypeError, ValueError):
        return _json_response(400, {"error": "invalid Content-Length"})
    if content_length <= 0:
        return _json_response(400, {"error": "request body is required"})
    if content_length > MAX_REQUEST_BYTES:
        return _json_response(413, {"error": "request body is too large"})
    stream = environ.get("wsgi.input")
    if stream is None:
        return _json_response(400, {"error": "request body is required"})
    try:
        body = stream.read(content_length)
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _json_response(400, {"error": "request body must be valid JSON"})
    if not isinstance(value, dict):
        return _json_response(400, {"error": "request body must be a JSON object"})
    return value


def _is_valid_target_url(value: str) -> bool:
    if not value or len(value) > MAX_URL_LENGTH or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def _json_response(status_code: int, value: dict[str, Any]) -> tuple[str, list[tuple[str, str]], bytes]:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    phrase = {
        200: "OK",
        201: "Created",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        415: "Unsupported Media Type",
        503: "Service Unavailable",
    }[status_code]
    return (
        f"{status_code} {phrase}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
        body,
    )


def _method_not_allowed(allowed: str) -> tuple[str, list[tuple[str, str]], bytes]:
    status, headers, body = _json_response(405, {"error": "method not allowed"})
    headers.append(("Allow", allowed))
    return status, headers, body


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
