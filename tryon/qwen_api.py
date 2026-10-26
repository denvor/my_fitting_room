"""千问图像编辑接口：造请求体、发请求、解析响应、把结果图下载到本地。"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from tryon.image_prep import PreparedImage, api_size
from tryon.prompt import TRYON_PROMPT
from tryon.settings import Settings

DOWNLOAD_TIMEOUT = 120
RETRY_DELAY = 2.0


@dataclass(frozen=True)
class Generation:
    image: bytes
    width: int
    height: int
    usage: dict
    request_id: str
    rewrite_status: str


class ApiError(Exception):
    """接口明确报错，三个字段原样透传，不改写。"""

    def __init__(self, code: str, message: str, request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


class ResultMissing(Exception):
    """接口返回 200，但结果里没有图。"""

    def __init__(self, request_id: str = ""):
        super().__init__("接口返回成功，但结果里没有图片")
        self.request_id = request_id


class ResultFetchFailed(Exception):
    """图已经生成（额度已花），但下载不下来。"""

    def __init__(self, url: str, reason: str):
        super().__init__(f"结果图下载失败：{reason}")
        self.url = url
        self.reason = reason


def _post(url: str, body: dict, api_key: str, timeout: int) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:  # 非 200 的响应体里带着 code/message，要留给上层
        return e.code, e.read()


def _get(url: str, timeout: int) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def build_request(person: PreparedImage, garment: PreparedImage, size: str, model: str) -> dict:
    """图像1=人像、图像2=衣服、最后一条文本，接口按位置引用。"""
    return {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": person.data_uri},
                        {"image": garment.data_uri},
                        {"text": TRYON_PROMPT},
                    ],
                }
            ]
        },
        "parameters": {"n": 1, "size": size, "watermark": False},
    }


def download_image(url: str, attempts: int = 3, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    last_reason = ""
    for i in range(attempts):
        try:
            return _get(url, timeout)
        except Exception as e:
            last_reason = str(e) or e.__class__.__name__
            if i < attempts - 1:
                _sleep(RETRY_DELAY)
    raise ResultFetchFailed(url=url, reason=last_reason)


def generate(settings: Settings, person: PreparedImage, garment: PreparedImage, timeout: int = 300) -> Generation:
    size = api_size(person.width, person.height)
    body = build_request(person, garment, size, settings.model)
    status, raw = _post(settings.endpoint, body, settings.api_key, timeout)

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("响应不是对象")
    except Exception:
        raise ApiError(
            code=f"HTTP_{status}",
            message=f"接口返回的内容读不懂（HTTP {status}）：{bytes(raw[:200])!r}",
        )

    request_id = str(data.get("request_id", ""))
    if status != 200 or data.get("code"):
        raise ApiError(
            code=str(data.get("code") or f"HTTP_{status}"),
            message=str(data.get("message") or f"接口返回 HTTP {status}"),
            request_id=request_id,
        )

    urls = [
        item["image"]
        for choice in data.get("output", {}).get("choices", [])
        for item in choice.get("message", {}).get("content", [])
        if isinstance(item, dict) and isinstance(item.get("image"), str)
    ]
    if not urls:
        raise ResultMissing(request_id=request_id)

    usage: dict = data.get("usage") or {}
    return Generation(
        image=download_image(urls[0]),
        width=int(usage.get("output_width", 0)),
        height=int(usage.get("output_height", 0)),
        usage=usage,
        request_id=request_id,
        rewrite_status=str(data.get("output", {}).get("rewrite_status", "")),
    )
