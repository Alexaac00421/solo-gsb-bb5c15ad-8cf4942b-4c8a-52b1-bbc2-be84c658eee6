"""HTTP API 层：仅使用 Python 标准库（http.server），零第三方依赖。"""
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db, service


def _route_table():
    """(method, 路径正则, handler)；路径参数用命名分组，如 (?P<recipe_id>\\d+)。"""
    return [
        ("GET", r"^/health$", "health"),
        ("POST", r"^/ingredients$", "create_ingredient"),
        ("GET", r"^/ingredients$", "list_ingredients"),
        ("GET", r"^/ingredients/(?P<ingredient_id>\d+)$", "get_ingredient"),
        ("PUT", r"^/ingredients/(?P<ingredient_id>\d+)$", "revise_ingredient"),
        ("POST", r"^/recipes$", "create_recipe"),
        ("GET", r"^/recipes$", "list_recipes"),
        ("GET", r"^/recipes/(?P<recipe_id>\d+)$", "get_recipe"),
        ("PUT", r"^/recipes/(?P<recipe_id>\d+)$", "revise_recipe"),
        ("POST", r"^/recipes/(?P<recipe_id>\d+)/publish$", "publish_recipe"),
        ("GET", r"^/recipes/(?P<recipe_id>\d+)/labels$", "list_labels"),
        ("GET", r"^/labels/(?P<label_id>\d+)$", "get_label"),
    ]


class _Handler(BaseHTTPRequestHandler):
    server_version = "AllergenLabelService/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------ 基础设施

    def log_message(self, fmt, *args):  # 简洁访问日志
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, code: str, message: str, details=None):
        err = {"code": code, "message": message}
        if details is not None:
            err["details"] = details
        self._send_json(status, {"error": err})

    def _read_body(self) -> dict:
        """读取 JSON 请求体；无 body 时返回 None。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise service.ApiError(400, "BAD_JSON", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise service.ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return data

    # ------------------------------------------------ 分发

    def _dispatch(self, method: str):
        path = urlparse(self.path).path
        path_matched = False
        for m, pattern, name in _route_table():
            match = re.match(pattern, path)
            if not match:
                continue
            if m != method:
                path_matched = True
                continue
            handler = getattr(self, f"_h_{name}")
            conn = db.connect(self.server.db_path)
            try:
                params = {k: int(v) for k, v in match.groupdict().items()}
                status, obj = handler(conn, params)
                self._send_json(status, obj)
            except service.ApiError as e:
                self._send_error(e.status, e.code, e.message, e.details)
            except sqlite3.Error as e:
                err = service.map_sqlite_error(e)
                self._send_error(err.status, err.code, err.message)
            except BrokenPipeError:
                pass
            except Exception as e:  # 兜底，不外泄堆栈
                self._send_error(500, "INTERNAL", f"服务器内部错误: {type(e).__name__}")
            finally:
                conn.close()
            return
        if path_matched:
            self._send_error(405, "METHOD_NOT_ALLOWED", f"{method} {path} 方法不允许")
        else:
            self._send_error(404, "NOT_FOUND", f"{method} {path} 路由不存在")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    # ------------------------------------------------ 各端点

    def _h_health(self, conn, params):
        return 200, {"status": "ok"}

    def _h_create_ingredient(self, conn, params):
        body = self._read_body()
        if body is None:
            raise service.ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return 201, service.create_ingredient(conn, body)

    def _h_list_ingredients(self, conn, params):
        return 200, service.list_ingredients(conn)

    def _h_get_ingredient(self, conn, params):
        return 200, service.get_ingredient(conn, params["ingredient_id"])

    def _h_revise_ingredient(self, conn, params):
        body = self._read_body()
        if body is None:
            raise service.ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return 200, service.revise_ingredient(conn, params["ingredient_id"], body)

    def _h_create_recipe(self, conn, params):
        body = self._read_body()
        if body is None:
            raise service.ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return 201, service.create_recipe(conn, body)

    def _h_list_recipes(self, conn, params):
        return 200, service.list_recipes(conn)

    def _h_get_recipe(self, conn, params):
        return 200, service.get_recipe(conn, params["recipe_id"])

    def _h_revise_recipe(self, conn, params):
        body = self._read_body()
        if body is None:
            raise service.ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return 200, service.revise_recipe(conn, params["recipe_id"], body)

    def _h_publish_recipe(self, conn, params):
        body = self._read_body() or {}  # 发布允许空 body
        return 201, service.publish_recipe(conn, params["recipe_id"], body)

    def _h_list_labels(self, conn, params):
        return 200, service.list_labels(conn, params["recipe_id"])

    def _h_get_label(self, conn, params):
        return 200, service.get_label(conn, params["label_id"])


def create_server(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.db_path = db_path
    server.daemon_threads = True
    return server


def main():
    db_path = os.environ.get("DB_PATH", "/data/app.db")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    db.init_db(db_path)  # 启动时初始化数据库
    server = create_server(db_path, host, port)
    print(f"allergen-label service listening on http://{host}:{port}, db={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
