import json

import pytest

from tryon import qwen_api
from tryon.image_prep import PreparedImage
from tryon.prompt import TRYON_PROMPT
from tryon.qwen_api import (
    ApiError,
    ResultFetchFailed,
    ResultMissing,
    build_request,
    download_image,
    generate,
)
from tryon.settings import Settings

SETTINGS = Settings(endpoint="https://example.test/generation", api_key="test-key", model="qwen-image-3.0-pro")
PERSON = PreparedImage(data=b"person-jpeg", width=896, height=1152, data_uri="data:image/jpeg;base64,cGVvcGxl")
GARMENT = PreparedImage(data=b"garment-jpeg", width=896, height=1152, data_uri="data:image/jpeg;base64,Z2FybWVudA==")


def ok_response():
    """照接口真实返回的结构完整造一份（两图 + usage + rewrite_status）。"""
    return {
        "request_id": "r-1",
        "output": {
            "rewrite_status": "success",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "image", "image": "https://oss.test/a.png"},
                            {"type": "image", "image": "https://oss.test/b.png"},
                        ],
                    },
                }
            ],
        },
        "usage": {
            "output_width": 896,
            "output_height": 1152,
            "input_image_count": 2,
            "input_image_type": "qima_input_1k",
            "output_image_count": 1,
            "output_image_type": "qima_output_1k",
        },
    }


def fake_post(responses, posted):
    """造一个替身 _post：按顺序吐出 (status, body_bytes)，并记下每次发出去的请求体。"""

    def _post(url, body, api_key, timeout):
        posted.append({"url": url, "body": body, "api_key": api_key, "timeout": timeout})
        status, payload = responses.pop(0)
        return status, json.dumps(payload).encode()

    return _post


def test_build_request_image_order_is_person_then_garment_then_text():
    body = build_request(PERSON, GARMENT, "896*1152", "qwen-image-3.0-pro")
    content = body["input"]["messages"][0]["content"]
    assert [k for item in content for k in item] == ["image", "image", "text"]
    assert content[0]["image"] == PERSON.data_uri
    assert content[1]["image"] == GARMENT.data_uri
    assert content[2]["text"] == TRYON_PROMPT
    assert body["parameters"] == {"n": 1, "size": "896*1152", "watermark": False}
    assert body["model"] == "qwen-image-3.0-pro"


def test_generate_returns_image_and_usage(monkeypatch):
    posted, fetched = [], []
    monkeypatch.setattr(qwen_api, "_post", fake_post([(200, ok_response())], posted))
    monkeypatch.setattr(qwen_api, "_get", lambda url, timeout: fetched.append(url) or b"fakepng")

    g = generate(SETTINGS, PERSON, GARMENT)

    assert g.image == b"fakepng"
    assert g.usage["output_image_count"] == 1 and g.usage["input_image_type"] == "qima_input_1k"
    assert (g.request_id, g.rewrite_status, g.width, g.height) == ("r-1", "success", 896, 1152)
    assert fetched == ["https://oss.test/a.png"]  # 只取第一张

    sent = posted[0]["body"]["input"]["messages"][0]["content"]
    assert sent[0]["image"].startswith("data:image/jpeg;base64,")  # 内嵌，不是外网 URL
    assert sent[1]["image"].startswith("data:image/jpeg;base64,")
    assert posted[0]["url"] == SETTINGS.endpoint
    assert posted[0]["api_key"] == "test-key"
    assert posted[0]["body"]["parameters"]["size"] == "896*1152"  # 尺寸按人像图


def test_generate_raises_apierror_with_server_message(monkeypatch):
    error = {"request_id": "r-2", "code": "InvalidParameter", "message": "Failed to download image URL after retries."}
    monkeypatch.setattr(qwen_api, "_post", fake_post([(400, error)], []))

    with pytest.raises(ApiError) as e:
        generate(SETTINGS, PERSON, GARMENT)

    assert e.value.code == "InvalidParameter"
    assert e.value.message == "Failed to download image URL after retries."
    assert e.value.request_id == "r-2"


def test_generate_raises_apierror_when_200_carries_code(monkeypatch):
    ok = ok_response()
    ok["code"] = "Throttling"
    monkeypatch.setattr(qwen_api, "_post", fake_post([(200, ok)], []))

    with pytest.raises(ApiError) as e:
        generate(SETTINGS, PERSON, GARMENT)

    assert e.value.code == "Throttling"


def test_generate_handles_200_without_image(monkeypatch):
    empty = ok_response()
    empty["output"]["choices"][0]["message"]["content"] = []
    monkeypatch.setattr(qwen_api, "_post", fake_post([(200, empty)], []))

    with pytest.raises(ResultMissing):
        generate(SETTINGS, PERSON, GARMENT)


def test_generate_handles_unparseable_body(monkeypatch):
    monkeypatch.setattr(qwen_api, "_post", lambda url, body, api_key, timeout: (500, b"<html>boom</html>"))

    with pytest.raises(ApiError) as e:
        generate(SETTINGS, PERSON, GARMENT)

    assert "500" in e.value.code


def test_download_image_retries_three_times_then_reports_url(monkeypatch):
    calls = []

    def always_fail(url, timeout):
        calls.append(url)
        raise OSError("connection reset")

    monkeypatch.setattr(qwen_api, "_get", always_fail)
    monkeypatch.setattr(qwen_api, "_sleep", lambda s: None)

    with pytest.raises(ResultFetchFailed) as e:
        download_image("https://oss.test/a.png", attempts=3)

    assert len(calls) == 3 and e.value.url == "https://oss.test/a.png"
    assert "connection reset" in str(e.value)
