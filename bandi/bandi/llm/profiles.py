# 功能：多 Provider 档案（Day137b-2b 「B 方案」）——档案并存，跨 provider 不丢 key
#
# - 存储位置 `~/.bandi/llm_profiles.json`（与 `~/.bandi/config.yaml` 同级，不进仓库）；
# - api_key 落盘前经 `core/security/secret_protect` 的 通道加密（复用安全 10 项实现，不重造）；
# - `resolve_llm_config(base)` 是**唯一解析入口**：有激活档案 → 覆盖 base 的模型/凭证/地址；
# 无档案或档案文件损坏 → 原样返回（跟随 config.yaml，零迁移、fail-open 不阻断对话）；
# - 会话级临时覆盖（-2 的 payload 通道）与档案级激活**共存**：档案是全局，payload 是当次。
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bandi.core.config import LLMConfig
from bandi.core.security.secret_protect import protect, unprotect

logger = logging.getLogger(__name__)

PROFILES_FILENAME = "llm_profiles.json"

EXTRA_WHITELIST: tuple[str, ...] = (
    "timeout",
    "max_output_tokens",
    "thinking_mode",
    "budget_limit",
    "extra_headers",
    "stream_timeout",
)

# profile_id 需路径安全（同时用作前端 key 与文件内键）
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ProfilesFileError(RuntimeError):
    # 功能：档案文件存在但无法解析/校验（与「文件缺失」区分——缺失是全新安装的正常态）
    pass


class LlmProfile(BaseModel):
    # 功能：单个 Provider 档案（构造即校验：extra=forbid + id 路径安全 + extra 白名单）
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    name: str = ""
    provider: str = ""
    model: str
    api_key: str = ""
    base_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile_id")
    @classmethod
    def _check_profile_id(cls, value: str) -> str:
        if not _PROFILE_ID_RE.match(value or ""):
            raise ValueError("profile_id 必须匹配 ^[A-Za-z0-9._-]{1,64}$（路径安全）")
        return value

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("model 不得为空")
        return value.strip()

    @field_validator("extra")
    @classmethod
    def _check_extra(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value) - set(EXTRA_WHITELIST))
        if unknown:
            raise ValueError(f"extra 仅接受 {list(EXTRA_WHITELIST)}；收到未知字段 {unknown}")
        return value


def mask_api_key(api_key: str) -> str:
    # 功能：API Key 出参掩码（保留服务商前缀，不透露尾部明文；空值返回空串）
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    return f"{key[:3]}***"


def _default_path() -> Path:
    return Path.home() / ".bandi" / PROFILES_FILENAME


class ProfileStore:
    # 功能：档案存储（原子写 + mtime 缓存 + 坏文件与缺失区分 + 密钥加密落盘）
    #
    # 并发前提：写操作持 self._lock（进程内串行）；跨进程（多实例）以原子 replace 保证不撕裂
    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._profiles: dict[str, LlmProfile] = {}
        self._active_id: str = ""
        self._feedback_id: str = ""

    @property
    def path(self) -> Path:
        return self._path

    # ---- 读 ----
    def _read_raw(self) -> dict[str, Any] | None:
        # 功能：读原始 JSON；文件缺失 → None（正常态）；解析失败 → ProfilesFileError（坏文件）
        if not self._path.exists():
            return None
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:  # 权限/占用等——与坏文件同类处理（上层 fail-open）
            raise ProfilesFileError(f"档案文件不可读: {exc}") from exc
        if not text.strip():
            raise ProfilesFileError("档案文件为空（既不缺失也不是合法 JSON）")
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProfilesFileError(f"档案文件 JSON 解析失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProfilesFileError("档案文件顶层必须是对象")
        return payload

    def _refresh(self) -> None:
        # 功能：按 mtime 变更重载（外部直接编辑文件也能生效）
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        payload = self._read_raw()
        if payload is None:
            self._profiles, self._active_id, self._feedback_id = {}, "", ""
            self._mtime = None
            return
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, dict):
            raise ProfilesFileError("profiles 字段必须是对象")
        profiles: dict[str, LlmProfile] = {}
        for key, item in raw_profiles.items():
            if not isinstance(item, dict):
                raise ProfilesFileError(f"档案 {key} 必须是对象")
            data = {**item, "profile_id": str(item.get("profile_id") or key)}
            data["api_key"] = unprotect(str(data.get("api_key") or ""))
            profiles[str(key)] = LlmProfile(**data)
        active = str(payload.get("active_profile_id") or "")
        if active and active not in profiles:
            # 激活指针指向已删除档案 → 视为未激活（不静默指向不存在的档案）
            active = ""
        feedback = str(payload.get("feedback_profile_id") or "")
        if feedback and feedback not in profiles:
            feedback = ""
        self._profiles, self._active_id, self._feedback_id, self._mtime = (
            profiles,
            active,
            feedback,
            mtime,
        )

    def list_profiles(self) -> list[LlmProfile]:
        with self._lock:
            self._refresh()
            return sorted(self._profiles.values(), key=lambda item: item.profile_id)

    def get(self, profile_id: str) -> LlmProfile | None:
        with self._lock:
            self._refresh()
            return self._profiles.get(profile_id)

    def active_profile(self) -> LlmProfile | None:
        with self._lock:
            self._refresh()
            return self._profiles.get(self._active_id) if self._active_id else None

    def active_profile_id(self) -> str:
        with self._lock:
            self._refresh()
            return self._active_id

    def feedback_profile(self) -> LlmProfile | None:
        # 功能：反馈模型档案（反思验收 LLM-as-Judge 专用）；未指定返回 None
        with self._lock:
            self._refresh()
            return self._profiles.get(self._feedback_id) if self._feedback_id else None

    def feedback_profile_id(self) -> str:
        with self._lock:
            self._refresh()
            return self._feedback_id

    # ---- 写 ----
    def _write(self, profiles: dict[str, LlmProfile], active_id: str, feedback_id: str) -> None:
        payload = {
            "active_profile_id": active_id,
            "feedback_profile_id": feedback_id,
            "profiles": {
                key: {**profile.model_dump(), "api_key": protect(profile.api_key)}
                for key, profile in profiles.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # 原子替换：读到半截文件的窗口为零
        self._profiles, self._active_id, self._feedback_id = profiles, active_id, feedback_id
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None

    def upsert(self, profile: LlmProfile) -> LlmProfile:
        # 功能：新增/覆盖档案（首次创建即自动激活——「配置完就能用」）
        with self._lock:
            self._refresh()
            profiles = dict(self._profiles)
            profiles[profile.profile_id] = profile
            active = self._active_id or profile.profile_id
            self._write(profiles, active, self._feedback_id)
            return profile

    def delete(self, profile_id: str) -> bool:
        # 功能：删除档案；删的是激活/反馈档案 → 对应指针清空（回落 config.yaml，不自动改指他人）
        with self._lock:
            self._refresh()
            if profile_id not in self._profiles:
                return False
            profiles = {k: v for k, v in self._profiles.items() if k != profile_id}
            active = "" if self._active_id == profile_id else self._active_id
            feedback = "" if self._feedback_id == profile_id else self._feedback_id
            self._write(profiles, active, feedback)
            return True

    def activate(self, profile_id: str) -> bool:
        with self._lock:
            self._refresh()
            if profile_id not in self._profiles:
                return False
            self._write(dict(self._profiles), profile_id, self._feedback_id)
            return True

    def deactivate(self) -> None:
        with self._lock:
            self._refresh()
            self._write(dict(self._profiles), "", self._feedback_id)

    def set_feedback(self, profile_id: str) -> bool:
        # 功能：指定反馈模型档案（反思验收 LLM-as-Judge）；空串 = 清除指定；档案不存在返回 False
        with self._lock:
            self._refresh()
            if profile_id and profile_id not in self._profiles:
                return False
            self._write(dict(self._profiles), self._active_id, profile_id)
            return True


def _apply_profile(base: LLMConfig, profile: LlmProfile) -> LLMConfig:
    # 功能：档案覆盖 base 的模型/凭证/地址/白名单高级项（仅覆盖档案**有值**的字段
    # 空串不覆盖——避免「档案没填 base_url 却清掉配置里的地址」）
    patch: dict[str, Any] = {"default_model": profile.model}
    if profile.provider.strip():
        patch["provider"] = profile.provider.strip()
    if profile.api_key.strip():
        patch["api_key"] = profile.api_key.strip()
    if profile.base_url.strip():
        patch["base_url"] = profile.base_url.strip()
    for key, value in profile.extra.items():
        patch[key] = value
    return base.model_copy(update=patch)


def resolve_llm_config(base: LLMConfig) -> LLMConfig:
    # 功能：**主链路唯一解析入口**——激活档案覆盖 base 的模型/凭证/地址/白名单高级项
    #
    # 无激活档案 / 档案文件损坏 → 原样返回（fail-open：坏文件不得阻断对话主链路）。
    store = store_singleton()
    try:
        profile = store.active_profile()
    except ProfilesFileError as exc:
        logger.warning("llm profiles 不可用，回退 config.yaml：%s", exc)
        return base
    if profile is None:
        return base
    return _apply_profile(base, profile)


def resolve_feedback_llm_config(base: LLMConfig) -> LLMConfig | None:
    # 功能：反馈模型（反思验收 LLM-as-Judge）档案解析——未指定反馈档案 / 档案无 key /
    # 文件损坏 → None（调用方回落到 judge_model / 主模型，fail-open 不阻断主链路）
    store = store_singleton()
    try:
        profile = store.feedback_profile()
    except ProfilesFileError as exc:
        logger.warning("llm profiles 不可用（反馈模型），忽略：%s", exc)
        return None
    if profile is None or not profile.api_key.strip():
        return None
    return _apply_profile(base, profile)


def with_resolved_llm(config: Any) -> Any:
    # 功能：把 resolve_llm_config 施加到整个 BandiConfig（返回副本，不改原对象）
    resolved = resolve_llm_config(config.llm)
    if resolved is config.llm:
        return config
    return config.model_copy(update={"llm": resolved})


def active_profile_summary() -> dict[str, Any] | None:
    # 功能：激活档案摘要（API 出参用；key 经掩码，绝不出明文）
    store = store_singleton()
    try:
        profile = store.active_profile()
    except ProfilesFileError:
        return None
    if profile is None:
        return None
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "provider": profile.provider,
        "model": profile.model,
        "base_url": profile.base_url,
        "api_key_masked": mask_api_key(profile.api_key),
    }


_STORE_LOCK = threading.Lock()
_STORE: ProfileStore | None = None


def store_singleton() -> ProfileStore:
    # 功能：进程内单例（mtime 缓存下每次调用都很轻；测试可经 reset_store_singleton 隔离）
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ProfileStore()
        return _STORE


def reset_store_singleton(path: Path | str | None = None) -> ProfileStore:
    # 功能：重置单例（测试隔离用；path=None 回落默认路径）
    global _STORE
    with _STORE_LOCK:
        _STORE = ProfileStore(path)
        return _STORE


def profile_for_api(profile: LlmProfile) -> dict[str, Any]:
    # 功能：档案出参（key 掩码；含 has_api_key 供前端判断是否已配置）
    return {
        **profile.model_dump(),
        "api_key": mask_api_key(profile.api_key),
        "has_api_key": bool(profile.api_key.strip()),
    }
