"""HTTP 层：urllib 封装，带 gzip、重试、指数退避。"""
from __future__ import annotations

import gzip
import http.client
import json
import re
import random
import socket
import time
import urllib.error
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


_TOKEN_RE = re.compile(r"/bot[^/\s]+/")


def redact(url: str) -> str:
    """把 URL 里的 bot token 替换掉，避免写进日志。"""
    return _TOKEN_RE.sub("/bot<TOKEN>/", url)


class PostError(RuntimeError):
    """POST 重试耗尽后抛出，保留原始异常类型以便调用方区分处理。"""

    def __init__(self, url: str, original: BaseException):
        super().__init__(f"POST failed: {redact(url)} :: {original}")
        self.original = original


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url} :: {body[:300]}")
        self.status = status
        self.body = body


def _request(req: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return resp.status, raw


def http_get(
    url: str,
    timeout: float = 25,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> str:
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
        "Referer": "https://t.me/",
    }
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            status, raw = _request(req, timeout)
            if status != 200:
                raise HttpError(status, raw.decode("utf-8", "replace"), url)
            return raw.decode("utf-8", "replace")
        except HttpError as e:
            last = e
            if e.status in (404, 403):
                raise
            if e.status == 429:
                time.sleep(min(60, 10 * (attempt + 1)))
            else:
                time.sleep(2 ** attempt + random.random())
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError) as e:
            last = e
            time.sleep(2 ** attempt + random.random())
    raise RuntimeError(f"GET failed after {retries}: {redact(url)} :: {last}")


def http_post_json(
    url: str,
    payload: dict,
    timeout: float = 60,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            status, raw = _request(req, timeout)
            text = raw.decode("utf-8", "replace")
            if status >= 400:
                raise HttpError(status, text, url)
            return json.loads(text)
        except HttpError as e:
            last = e
            if 400 <= e.status < 500 and e.status != 429:
                raise
            time.sleep(2 ** attempt + random.random())
        except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt + random.random())
    raise PostError(url, last if last else RuntimeError("unknown"))
