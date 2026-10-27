from pathlib import Path

import pytest

from tryon.settings import SettingsError, derive_endpoint, load_settings


def test_derive_endpoint_strips_compatible_mode():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")


def test_derive_endpoint_without_suffix_uses_host_only():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")


def test_missing_env_file_reports_readable_error(tmp_path):
    with pytest.raises(SettingsError, match="找不到"):
        load_settings(tmp_path / "nope" / ".env")


def test_quoted_values_are_unwrapped(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        'OPENAI_URL="https://x/compatible-mode/v1"\nAPI_KEY=\'sk-abc\'\nMODEL="qwen-image-3.0-pro"\n',
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.api_key == "sk-abc" and s.model == "qwen-image-3.0-pro"
    assert s.endpoint == "https://x/api/v1/services/aigc/multimodal-generation/generation"


def test_load_settings_reads_real_env_file():
    s = load_settings(Path(__file__).resolve().parents[1] / ".env")
    assert s.model == "qwen-image-3.0-pro" and s.api_key and s.endpoint.startswith("https://")


def test_default_env_path_is_resolved_outside_the_cwd(tmp_path, monkeypatch):
    """站在任何目录里启动，都应该找到项目根的那份 .env。"""
    if not (Path(__file__).resolve().parents[1] / ".env").exists():
        pytest.skip("本机没有 .env，默认路径检查跳过")
    monkeypatch.chdir(tmp_path)
    s = load_settings()
    assert s.endpoint.startswith("https://") and s.model


def test_load_settings_missing_key_raises(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OPENAI_URL=https://x/compatible-mode/v1\nMODEL=m\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="API_KEY"):
        load_settings(p)


def test_source_and_comfy_url_default_when_env_says_nothing(tmp_path):
    """两个新键都是可选项：.env 里没有时按 API + 内置地址走，不能报缺配置。"""
    p = tmp_path / ".env"
    p.write_text("OPENAI_URL=https://x/compatible-mode/v1\nAPI_KEY=k\nMODEL=m\n", encoding="utf-8")
    s = load_settings(p)
    assert s.source == "api" and s.comfy_url == "http://127.0.0.1:8188"


def test_comfy_url_and_source_read_from_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "OPENAI_URL=https://x/compatible-mode/v1\nAPI_KEY=k\nMODEL=m\n"
        "COMFY_URL=http://10.0.0.9:8188\nTRYON_SOURCE=comfyui\n",
        encoding="utf-8",
    )
    s = load_settings(p)
    assert (s.source, s.comfy_url) == ("comfyui", "http://10.0.0.9:8188")


def test_illegal_tryon_source_in_env_is_treated_as_api(tmp_path):
    """可选增强项写错了不该拦住启动，按默认来源走。"""
    p = tmp_path / ".env"
    p.write_text(
        "OPENAI_URL=https://x/compatible-mode/v1\nAPI_KEY=k\nMODEL=m\nTRYON_SOURCE=gps\n",
        encoding="utf-8",
    )
    assert load_settings(p).source == "api"
