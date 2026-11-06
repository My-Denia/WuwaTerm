#!/usr/bin/env python3
"""Exercise an installed SDK-free distribution using synthetic local inputs."""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("core", "api", "build"), default="core")
    mode = parser.parse_args().mode
    assert importlib.util.find_spec("telegram") is None

    from wuwaterm.application import (
        ProtectedStructure,
        ProtectedText,
        build_term_service,
        build_translator,
        lookup_exact_terms,
    )
    from wuwaterm.db import initialize

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            content = payload["messages"][1]["content"]
            result = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)

    with tempfile.TemporaryDirectory(prefix="wuwaterm-package-smoke-") as temp:
        db_path = Path(temp) / "fixture.db"
        with sqlite3.connect(db_path) as db:
            initialize(db)
            db.execute(
                "INSERT INTO terms(category, source_file, source_id, text_key, zh, en, "
                "zh_norm, en_norm, pinyin, pinyin_abbrev, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("term", "fixture", "1", "1", "声骸", "Echo", "声骸", "echo", "shenghai", "sh", 1),
            )
            db.commit()

        service = build_term_service(db_path)
        assert lookup_exact_terms(service, "Echo")[0].zh == "声骸"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["WUWATERM_OPENAI_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
        os.environ["WUWATERM_OPENAI_API_KEY"] = "fixture"
        os.environ["WUWATERM_OPENAI_MODEL"] = "fixture"
        try:
            translator = build_translator(db_path)
            source = ProtectedText(
                ("Use ", "Echo", " now."),
                (ProtectedStructure("[open]"), ProtectedStructure("[close]")),
            )

            async def translate() -> str:
                try:
                    result = await translator.translate_protected_async(source, to_chinese=True)
                    return result.render()
                finally:
                    await translator.aclose()

            assert asyncio.run(translate()) == "Use [open]声骸[close] now."
            assert len(requests) == 1
            assert "[open]" not in requests[0]["messages"][1]["content"]
            assert "wuwaterm.telegram_html" not in sys.modules
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        if mode == "api":
            from fastapi.testclient import TestClient
            from wuwaterm_api.app import create_app
            from wuwaterm_api.settings import ApiSettings

            app = create_app(ApiSettings(db_path=db_path, device_db_path=Path(temp) / "devices.db"))
            with TestClient(app) as client:
                assert client.get("/healthz").status_code == 200
        elif mode == "build":
            from wuwaterm.build_pinyin import pinyin_for

            assert pinyin_for("声骸") == "shenghai"

    assert importlib.util.find_spec("telegram") is None
    print(f"installed {mode} public-interface smoke passed")


if __name__ == "__main__":
    main()
