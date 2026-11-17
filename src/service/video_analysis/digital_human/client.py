from __future__ import annotations

import json
import ipaddress
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


_ALLOWED_NETWORKS = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
)))


class DuixError(RuntimeError):
    pass


class DuixConnectionError(DuixError):
    pass


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise DuixError("Duix 地址必须是无路径、无凭据的 HTTP(S) 服务根地址")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise DuixError("Duix 地址必须使用内网 IP") from exc
    if not any(address in network for network in _ALLOWED_NETWORKS):
        raise DuixError("Duix 地址只允许内网私有 IP")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise DuixError("Duix 地址端口无效") from exc
    return value.strip().rstrip("/")


def _request(base_url: str, endpoint: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(validate_base_url(base_url) + endpoint, data=data,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise DuixError(f"Duix HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DuixConnectionError(f"Duix 服务连接失败: {exc}") from exc
    if not isinstance(result, dict):
        raise DuixError("Duix 返回了无效 JSON")
    return result


def submit(base_url: str, payload: dict) -> dict:
    result = _request(base_url, "/easy/submit", payload, timeout=120)
    if str(result.get("code")) != "10000":
        raise DuixError("Duix 拒绝任务: " + json.dumps(result, ensure_ascii=False))
    return result


def query(base_url: str, code: str) -> dict:
    result = _request(base_url, "/easy/query?" + urlencode({"code": code}), timeout=60)
    if str(result.get("code")) != "10000" or not isinstance(result.get("data"), dict):
        raise DuixError("Duix 查询失败: " + json.dumps(result, ensure_ascii=False))
    return result["data"]

