"""无第三方依赖的制造交付编排 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import DeliveryError, ValidationFailed
from .service import ManufacturingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: ManufacturingService) -> None:
        self.service = service
        # 单 SQLite 连接：所有业务请求整体串行，保证只读多语句接口
        # （状态页、计划视图）不会与写入事务交错读到中间状态。
        self._gate = threading.RLock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    @staticmethod
    def _require(payload: Mapping[str, Any], *fields: str) -> None:
        missing = [field for field in fields if field not in payload]
        if missing:
            raise ValidationFailed(f"缺少字段：{', '.join(missing)}")

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        with self._gate:
            return self._route(method, target, headers, body)

    def _route(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            service = self.service

            if method == "POST" and path == "/users":
                self._require(payload, "user_id", "display_name", "role")
                return Response(201, service.create_user(payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/customers/constraints":
                return Response(201, service.create_customer_constraint(actor, payload))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["customers", "constraints"]:
                return Response(200, service.customer_constraint(parts[2]))

            if method == "POST" and path == "/parts":
                return Response(201, service.register_part(actor, payload))
            if method == "POST" and path == "/substitution-rules":
                return Response(201, service.register_substitution_rule(actor, payload))

            if method == "POST" and path == "/batches":
                return Response(201, service.register_batch(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "certify":
                return Response(200, service.certify_batch(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                return Response(200, service.batch(parts[1]))

            if method == "POST" and path == "/lines":
                return Response(201, service.create_line(actor, payload))
            if method == "POST" and path == "/gates":
                return Response(201, service.create_gate(actor, payload))
            if method == "POST" and path == "/shipping-windows":
                return Response(201, service.create_shipping_window(actor, payload))

            if method == "POST" and path == "/orders":
                return Response(201, service.submit_order(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "orders":
                return Response(200, service.order(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "cancel":
                return Response(200, service.cancel_order(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "plan":
                return Response(201, service.plan_order(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "confirm":
                self._require(payload, "expected_revision")
                return Response(200, service.confirm_order(actor, parts[1], int(payload["expected_revision"])))
            if method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "status":
                return Response(200, service.order_status(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "revisions":
                return Response(200, service.list_revisions(actor, parts[1]))
            if method == "GET" and len(parts) == 4 and parts[0] == "orders" and parts[2] == "revisions":
                return Response(200, service.plan_revision(actor, parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "latest-plan":
                return Response(200, service.latest_plan(actor, parts[1]))
            if method == "GET" and len(parts) == 4 and parts[0] == "orders" and parts[2] == "diff":
                digits = parts[3].split("..")
                if len(digits) != 2 or not all(item.isdigit() for item in digits):
                    raise ValidationFailed("差异区间必须是 N..M 形式的修订号")
                return Response(200, service.compare_revisions(actor, parts[1], int(digits[0]), int(digits[1])))
            if method == "POST" and len(parts) == 3 and parts[0] == "orders" and parts[2] == "rollback":
                self._require(payload, "target_revision")
                return Response(200, service.rollback_plan(actor, parts[1], int(payload["target_revision"])))

            if method == "POST" and len(parts) == 4 and parts[0] == "orders" and parts[2] == "units" and parts[3] == "start":
                return Response(200, service.start_unit(actor, parts[1], int(payload.get("unit_no", 0))))
            if method == "POST" and len(parts) == 4 and parts[0] == "orders" and parts[2] == "units" and parts[3] == "gate":
                self._require(payload, "unit_no", "gate_id", "passed")
                return Response(200, service.record_gate_result(
                    actor, parts[1], int(payload["unit_no"]), payload["gate_id"],
                    bool(payload["passed"]), str(payload.get("note", "")),
                ))
            if method == "POST" and len(parts) == 4 and parts[0] == "orders" and parts[2] == "units" and parts[3] == "ship":
                return Response(200, service.ship_unit(actor, parts[1], int(payload.get("unit_no", 0))))

            if method == "POST" and path == "/changes":
                return Response(201, service.propose_change(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "changes" and parts[2] == "evaluate":
                return Response(201, service.evaluate_change(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "changes" and parts[2] == "apply":
                self._require(payload, "expected_revision")
                return Response(200, service.apply_change(actor, parts[1], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "changes" and parts[2] == "reject":
                self._require(payload, "reason")
                return Response(200, service.reject_change(actor, parts[1], payload["reason"]))

            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except DeliveryError as exc:
            error = {"code": exc.code, "message": str(exc)}
            details = getattr(exc, "details", None)
            if details is not None:
                error["details"] = details
            return Response(exc.status, {"error": error})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PowerDelivery/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动输变电装备制造交付编排服务")
    parser.add_argument("--database", type=Path, default=Path("power_delivery.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(ManufacturingService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
