# 功能：契约——配置加载自动解密 dpapi: 保护的密钥（fail-safe + 用户目录隔离）
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.security.secret_protect import protect

_WIN = sys.platform == "win32"

_ENV_KEYS = (
    "BANDI_API_KEY",
    "BANDI_LLM_API_KEY",
    "BANDI_AUTH_SECRET",
    "BANDI_CONFIG",
    "BANDI_ENV",
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 功能：隔离 home/cwd/env——绝不读写真实 ~/.bandi 配置
    # （cwd 用独立子目录：home 与 cwd 相同会让同一文件同时命中 user/project 两层
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _write_user_config(home: Path, api_key: str) -> None:
    config_dir = home / ".bandi"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(f'llm:\n  api_key: "{api_key}"\n', encoding="utf-8")


class TestProtectedConfigLoad:
    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_dpapi_config_loads_plaintext(self, isolated_home: Path) -> None:
        secret = "sk-配置密钥-🔑"
        _write_user_config(isolated_home, protect(secret))
        config = BandiConfig.load()
        assert config.llm.api_key == secret

    def test_plain_value_unchanged(self, isolated_home: Path) -> None:
        _write_user_config(isolated_home, "sk-plain")
        config = BandiConfig.load()
        assert config.llm.api_key == "sk-plain"

    def test_bad_base64_kept_without_crash(self, isolated_home: Path) -> None:
        _write_user_config(isolated_home, "dpapi:!!!not-base64!!!")
        config = BandiConfig.load()
        assert config.llm.api_key == "dpapi:!!!not-base64!!!"

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_env_override_dpapi_value_decrypted(self, isolated_home: Path) -> None:
        from unittest.mock import patch

        secret = "sk-env-密钥"
        with patch.dict("os.environ", {"BANDI_API_KEY": protect(secret)}):
            config = BandiConfig.load()
        assert config.llm.api_key == secret

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_direct_llm_config_construction_decrypts(self, isolated_home: Path) -> None:
        from bandi.core.config import LLMConfig

        assert LLMConfig(api_key=protect("sk-direct")).api_key == "sk-direct"

    def test_missing_config_uses_default(self, isolated_home: Path) -> None:
        assert BandiConfig.load().llm.api_key == ""


class TestLoadIsolation:
    def test_load_does_not_write_user_dir(self, isolated_home: Path) -> None:
        _write_user_config(isolated_home, "sk-plain")
        before: Any = sorted(p.name for p in (isolated_home / ".bandi").iterdir())
        BandiConfig.load()
        after = sorted(p.name for p in (isolated_home / ".bandi").iterdir())
        assert before == after


def _read_saved_api_key(home: Path) -> str:
    # 功能：读回 save 落盘的 llm.api_key 原值
    import yaml

    raw = yaml.safe_load((home / ".bandi" / "config.yaml").read_text(encoding="utf-8"))
    return str((raw.get("llm") or {}).get("api_key", ""))


class TestProtectedConfigSave:
    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_protected_value_stays_protected_after_save(self, isolated_home: Path) -> None:
        # 破坏者：load 解密后一次 save 回写明文 = 保护静默失效
        secret = "sk-保存密钥-🔐"
        _write_user_config(isolated_home, protect(secret))
        config = BandiConfig.load()
        config.save()
        saved = _read_saved_api_key(isolated_home)
        assert saved.startswith("dpapi:"), "受保护配置 save 必须仍以 dpapi: 落盘"
        assert secret not in saved
        assert BandiConfig.load().llm.api_key == secret

    def test_plain_value_save_keeps_plaintext(self, isolated_home: Path) -> None:
        _write_user_config(isolated_home, "sk-plain")
        config = BandiConfig.load()
        config.save()
        assert _read_saved_api_key(isolated_home) == "sk-plain"
        assert BandiConfig.load().llm.api_key == "sk-plain"

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_env_protected_override_saved_not_plaintext(self, isolated_home: Path) -> None:
        from unittest.mock import patch

        secret = "sk-env-密钥"
        with patch.dict("os.environ", {"BANDI_API_KEY": protect(secret)}):
            config = BandiConfig.load()
        config.save()
        saved = _read_saved_api_key(isolated_home)
        assert secret not in saved
        assert saved.startswith("dpapi:")

    def test_bad_protected_value_not_rewritten_as_junk(self, isolated_home: Path) -> None:
        # 坏密文 load 保留原值（fail-safe）——save 后该值原样保留，不得二次包装
        _write_user_config(isolated_home, "dpapi:!!!not-base64!!!")
        config = BandiConfig.load()
        config.save()
        assert _read_saved_api_key(isolated_home) == "dpapi:!!!not-base64!!!"

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_put_settings_model_endpoint_keeps_key_protected(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 端到端：真实 PUT /api/settings/model（model switch 同源 save 路径）后盘上仍密文
        import importlib

        from fastapi.testclient import TestClient

        secret = "sk-端点密钥"
        _write_user_config(isolated_home, protect(secret))
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_API_KEY", "static-key")
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer static-key"
        resp = client.put("/api/settings/model", json={"model": "deepseek-v4-flash"})
        assert resp.status_code == 200, resp.text
        saved = _read_saved_api_key(isolated_home)
        assert saved.startswith("dpapi:"), "端点触发的 save 不得把 api_key 明文回写"
        assert secret not in saved
        assert BandiConfig.load().llm.api_key == secret
