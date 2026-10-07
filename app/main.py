"""HTTP 层：基于标准库 http.server 的 JSON API。

路由：
  POST   /ingredients                 新建原料草稿
  GET    /ingredients/{id}            查询原料
  PUT    /ingredients/{id}            修订原料（须带所见 version）
  POST   /recipes                     新建配方草稿
  GET    /recipes/{id}                查询配方
  PUT    /recipes/{id}                修订配方（须带所见 version）
  POST   /recipes/{id}/publish        发布标签（须带所见 version）
  GET    /recipes/{id}/labels         列出该配方全部标签
  GET    /labels/{id}                 查询标签（不可变）
  GET    /healthz                     健康检查
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, service

DB_PATH = os.environ.get("DB_PATH", "/data/kitchen.db")
PORT = int(os.environ.get("PORT", "8080"))

ROUTES = [
    ("POST",   re.compile(r"^/ingredients$"),                        "create_ingredient"),
    ("GET",    re.compile(r"^/ingredients/(\d+)$"),                  "get_ingredient"),
    ("PUT",    re.compile(r"^/ingredients/(\d+)$"),                  "update_ingredient"),
    ("POST",   re.compile(r"^/recipes$"),                            "create_recipe"),
    ("GET",    re.compile(r"^/recipes/(\d+)$"),                      "get_recipe"),
    ("PUT",    re.compile(r"^/recipes/(\d+)$"),                      "update_recipe"),
    ("POST",   re.compile(r"^/recipes/(\d+)/publish$"),              "publish_label"),
    ("GET",    re.compile(r"^/recipes/(\d+)/labels$"),               "list_labels"),
    ("GET",    re.compile(r"^/labels/(\d+)$"),                       "get_label"),
]

HANDLERS = {
    "create_ingredient": lambda conn, body, ids: service.create_ingredient(conn, body),
    "get_ingredient":    lambda conn, body, ids: service.get_ingredient(conn, ids[0]),
    "update_ingredient": lambda conn, body, ids: service.update_ingredient(conn, ids[0], body),
    "create_recipe":     lambda conn, body, ids: service.create_recipe(conn, body),
    "get_recipe":        lambda conn, body, ids: service.get_recipe(conn, ids[0]),
    "update_recipe":     lambda conn, body, ids: service.update_recipe(conn, ids[0], body),
    "publish_label":     lambda conn, body, ids: service.publish_label(conn, ids[0], body),
    "list_labels":       lambda conn, body, ids: service.list_labels(conn, ids[0]),
    "get_label":         lambda conn, body, ids: service.get_label(conn, ids[0]),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KitchenLabel/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志，需要时可去掉
        pass

    # -- 工具 ------------------------------------------------------
    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise service.ApiError(400, "validation", "请求体必须是合法 JSON")
        if not isinstance(body, dict):
            raise service.ApiError(400, "validation", "请求体必须是 JSON 对象")
        return body

    def _dispatch(self, method: str) -> None:
        if method == "GET" and self.path == "/healthz":
            return self._send(200, {"status": "ok"})
        for route_method, pattern, name in ROUTES:
            if route_method != method:
                continue
            m = pattern.match(self.path)
            if not m:
                continue
            ids = [int(g) for g in m.groups()]
            conn = db.connect(self.server.db_path)
            try:
                body = self._read_body() if method in ("POST", "PUT") else {}
                status, payload = HANDLERS[name](conn, body, ids)
                self._send(status, payload)
            except service.ApiError as e:
                self._send(e.status, {"error": {"code": e.code, "message": e.message}})
            except Exception as e:  # 兜底，不泄露内部细节
                self._send(500, {"error": {"code": "internal", "message": str(e)}})
            finally:
                conn.close()
            return
        self._send(404, {"error": {"code": "not_found", "message": f"无此路由: {method} {self.path}"}})

    # -- 方法分发 ---------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")


def main() -> None:
    db.init_db(DB_PATH)  # 启动时初始化数据库（幂等）
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.db_path = DB_PATH
    print(f"kitchen-label service listening on 0.0.0.0:{PORT}, db={DB_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
