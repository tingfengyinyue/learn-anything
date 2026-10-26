"""CLI 用的 HTTP 客户端，凭据保存在 ~/.learn-anything/client.json。"""
import json
import os
from pathlib import Path

import httpx

CONFIG_PATH = Path(os.environ.get("LA_CLIENT_CONFIG", Path.home() / ".learn-anything" / "client.json"))
DEFAULT_SERVER = "http://127.0.0.1:8765"


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG_PATH.chmod(0o600)


class Client:
    def __init__(self, server: str | None = None, token: str | None = None):
        cfg = load_config()
        self.server = (server or os.environ.get("LA_SERVER") or cfg.get("server") or DEFAULT_SERVER).rstrip("/")
        self.token = token if token is not None else cfg.get("token")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, *, timeout: float = 60, **kw):
        try:
            r = httpx.request(method, self.server + path, headers=self._headers(), timeout=timeout, **kw)
        except httpx.ConnectError:
            raise ApiError(0, f"连不上服务端 {self.server}。先运行 `learn server`，或用 --server / LA_SERVER 指定地址")
        except httpx.TimeoutException:
            raise ApiError(0, "请求超时（模型生成可能较慢，可以稍后用 `learn status` 查看）")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            if isinstance(detail, list):  # FastAPI 校验错误
                detail = "；".join(f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail)
            raise ApiError(r.status_code, str(detail))
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.text

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json if json is not None else {}, **kw)

    def patch(self, path, json):
        return self.request("PATCH", path, json=json)

    def delete(self, path):
        return self.request("DELETE", path)

    def enrollment(self, topic: str) -> dict:
        return self.get("/enrollments/by-topic", params={"name": topic})
