from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                parts = [p for p in path.split("/") if p]
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif parts == ["api", "items"]:
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "records"):
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(int(parts[2]), role)})
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications"):
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"rectifications": service.list_rectifications(
                        role, item_id=int(parts[2]))})
                elif (len(parts) == 6 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications" and parts[5] == "retests"):
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"retests": service.list_retests(
                        int(parts[2]), int(parts[4]), role)})
                elif parts == ["api", "rectifications"]:
                    actor, role = self._identity()
                    del actor
                    query = parse_qs(parsed.query)
                    status = query.get("status", [None])[0]
                    self._json(200, {"rectifications": service.list_rectifications(
                        role, status=status)})
                elif len(parts) == 3 and parts[:2] == ["api", "items"]:
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(int(parts[2]), role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                actor, role = self._identity()
                body = self._body()
                if parts == ["api", "items"]:
                    self._json(201, service.create_item(body, actor, role))
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "records"):
                    self._json(201, service.add_record(int(parts[2]), body, actor, role))
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "transition"):
                    self._json(200, service.transition(
                        int(parts[2]), body.get("target"),
                        body.get("expected_version"), actor, role))
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "parameters"):
                    self._json(200, service.update_parameters(
                        int(parts[2]), body, actor, role))
                elif (len(parts) == 4 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications"):
                    self._json(201, service.register_rectification(
                        int(parts[2]), body, actor, role))
                elif (len(parts) == 6 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications" and parts[5] == "retests"):
                    self._json(201, service.submit_retest(
                        int(parts[2]), int(parts[4]), body, actor, role))
                elif (len(parts) == 6 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications" and parts[5] == "close"):
                    self._json(200, service.close_rectification(
                        int(parts[2]), int(parts[4]), actor, role))
                elif (len(parts) == 6 and parts[:2] == ["api", "items"]
                      and parts[3] == "rectifications" and parts[5] == "deadline"):
                    self._json(200, service.update_deadline(
                        int(parts[2]), int(parts[4]), body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
