import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from url_shortener.service import MAX_REQUEST_BYTES, SQLiteURLStore, UrlShortenerApp


class UrlShortenerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "urls.sqlite3"
        self.app = UrlShortenerApp(self.database, "http://localhost:8000")

    def tearDown(self):
        self.directory.cleanup()

    def request(
        self,
        path: str,
        method: str = "GET",
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> tuple[str, dict[str, str], bytes]:
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
        }
        response = b"".join(self.app(environ, start_response))
        return captured["status"], captured["headers"], response

    def create_url(self, target: str = "https://example.com/path") -> dict[str, Any]:
        status, _, body = self.request(
            "/api/urls",
            method="POST",
            body=json.dumps({"url": target}).encode(),
        )
        self.assertEqual(status, "201 Created")
        return json.loads(body)

    def test_create_redirect_and_analytics_are_persistent(self):
        created = self.create_url()

        status, headers, body = self.request(f"/{created['code']}")
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "https://example.com/path")
        self.assertEqual(body, b"")

        status, _, body = self.request(f"/api/urls/{created['code']}/stats")
        self.assertEqual(status, "200 OK")
        stats = json.loads(body)
        self.assertEqual(stats["clicks"], 1)
        self.assertEqual(stats["target_url"], "https://example.com/path")
        self.assertIsNotNone(stats["last_accessed_at"])

        restarted_app = UrlShortenerApp(self.database, "http://localhost:8000")
        self.app = restarted_app
        status, _, body = self.request(f"/api/urls/{created['code']}/stats")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["clicks"], 1)

    def test_invalid_urls_and_non_json_are_rejected(self):
        for invalid_url in ("", "ftp://example.com", "https:///missing-host", "https://user:pass@example.com"):
            with self.subTest(url=invalid_url):
                status, _, _ = self.request(
                    "/api/urls",
                    method="POST",
                    body=json.dumps({"url": invalid_url}).encode(),
                )
                self.assertEqual(status, "400 Bad Request")

        status, _, _ = self.request(
            "/api/urls",
            method="POST",
            body=b'{"url":"https://example.com"}',
            content_type="text/plain",
        )
        self.assertEqual(status, "415 Unsupported Media Type")

        status, _, _ = self.request(
            "/api/urls",
            method="POST",
            body=b'{"url":"https://example.com","unexpected":true}',
        )
        self.assertEqual(status, "400 Bad Request")

    def test_request_size_method_and_unknown_code_are_bounded(self):
        status, _, _ = self.request(
            "/api/urls",
            method="POST",
            body=b"x" * (MAX_REQUEST_BYTES + 1),
        )
        self.assertEqual(status, "413 Payload Too Large")

        status, headers, _ = self.request("/api/urls")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(headers["Allow"], "POST")

        status, _, _ = self.request("/not-found")
        self.assertEqual(status, "404 Not Found")

    def test_unique_code_collision_is_retried_and_failure_is_bounded(self):
        values = iter(("existing", "existing", "new-code"))
        store = SQLiteURLStore(self.database, lambda: next(values))
        store.create_url("https://example.com/first")
        created = store.create_url("https://example.com/second")
        self.assertEqual(created["code"], "new-code")

        colliding_store = SQLiteURLStore(self.database, lambda: "existing")
        with self.assertRaises(RuntimeError):
            colliding_store.create_url("https://example.com/third")

    def test_health_endpoint(self):
        status, _, body = self.request("/health")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_concurrent_redirects_are_counted_and_database_errors_are_explicit(self):
        created = self.create_url()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self.request, f"/{created['code']}") for _ in range(16)]
            for future in futures:
                self.assertEqual(future.result()[0], "302 Found")

        status, _, body = self.request(f"/api/urls/{created['code']}/stats")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["clicks"], 16)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TABLE short_urls")
            connection.commit()
        finally:
            connection.close()
        status, _, body = self.request(f"/api/urls/{created['code']}/stats")
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(json.loads(body)["error"], "service temporarily unavailable")


if __name__ == "__main__":
    unittest.main()
