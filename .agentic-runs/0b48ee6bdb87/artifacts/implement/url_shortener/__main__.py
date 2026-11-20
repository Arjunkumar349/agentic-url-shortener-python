"""Run the URL shortener's local WSGI server."""

from __future__ import annotations

import argparse
import os
from wsgiref.simple_server import make_server

from .service import UrlShortenerApp


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local URL shortener service.")
    parser.add_argument("--host", default=os.environ.get("URL_SHORTENER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("URL_SHORTENER_PORT", "8000")))
    parser.add_argument("--database", default=os.environ.get("URL_SHORTENER_DB", "url-shortener.sqlite3"))
    args = parser.parse_args()

    app = UrlShortenerApp(args.database, f"http://{args.host}:{args.port}")
    with make_server(args.host, args.port, app) as server:
        print(f"URL shortener listening on http://{args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
