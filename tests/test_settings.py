from pathlib import Path

import pytest

from tryon.settings import SettingsError, derive_endpoint, load_settings


def test_derive_endpoint_strips_compatible_mode():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")


def test_derive_endpoint_without_suffix_uses_host_only():
    assert derive_endpoint("https://token-plan.maas.qianwenaiapi.com/") == (
        "https://token-plan.maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation")


def test_load_settings_reads_real_env_file():
    s = load_settings(Path(__file__).resolve().parents[1] / ".env")
    assert s.model == "qwen-image-3.0-pro" and s.api_key and s.endpoint.startswith("https://")


def test_load_settings_missing_key_raises(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OPENAI_URL=https://x/compatible-mode/v1\nMODEL=m\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="API_KEY"):
        load_settings(p)
