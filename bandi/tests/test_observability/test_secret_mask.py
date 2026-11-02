from __future__ import annotations

from bandi.observability.secret_mask import mask_dict, mask_sensitive


class TestSecretMask:
    def test_mask_sensitive_long(self) -> None:
        result = mask_sensitive("sk-abc12345")
        assert result == "sk-a****"

    def test_mask_sensitive_short(self) -> None:
        result = mask_sensitive("abc")
        assert result == "****"

    def test_mask_sensitive_empty(self) -> None:
        result = mask_sensitive("")
        assert result == "****"

    def test_mask_dict_sensitive_keys(self) -> None:
        data = {"api_key": "sk-1234567890", "name": "test", "token": "abc"}
        result = mask_dict(data)
        assert result["api_key"] != "sk-1234567890"
        assert result["name"] == "test"
        assert result["token"] != "abc"

    def test_mask_dict_nested(self) -> None:
        data = {"config": {"secret": "mysecret", "visible": "hello"}}
        result = mask_dict(data)
        assert result["config"]["secret"] != "mysecret"
        assert result["config"]["visible"] == "hello"

    def test_mask_dict_recursive_into_list(self) -> None:
        """#7：meta 中的 list[dict] 必须递归脱敏——敏感 key 不因嵌套进列表而漏网。"""
        data = {"items": [{"api_key": "sk-1234567890", "name": "ok"}, {"token": "abc"}]}
        result = mask_dict(data)
        assert result["items"][0]["api_key"] != "sk-1234567890"
        assert result["items"][0]["name"] == "ok"
        assert result["items"][1]["token"] != "abc"

    def test_mask_dict_no_false_positive_on_key_substring(self) -> None:
        """裸 "key" 子串不得误伤 monkey_patch/key_value/hockey/turkey 等正常字段。"""
        data = {
            "monkey_patch": "no",
            "key_value": "yes",
            "hockey": "game",
            "turkey": "bird",
            "api_key": "sk-1234567890",
        }
        result = mask_dict(data)
        assert result["monkey_patch"] == "no", "monkey_patch 不得被误伤"
        assert result["key_value"] == "yes", "key_value 不得被误伤"
        assert result["hockey"] == "game", "hockey 不得被误伤"
        assert result["turkey"] == "bird", "turkey 不得被误伤"
        assert result["api_key"] != "sk-1234567890", "真实敏感键仍须脱敏"

    def test_mask_dict_combined_sensitive_keys(self) -> None:
        """access_key/secret_key/private_key 组合键必须脱敏。"""
        data = {"access_key": "AKIA123", "secret_key": "s3cr3t", "user_name": "alice"}
        result = mask_dict(data)
        assert result["access_key"] != "AKIA123"
        assert result["secret_key"] != "s3cr3t"
        assert result["user_name"] == "alice"
