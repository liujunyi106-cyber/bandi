class BandiError(Exception):  # 功能：全局异常基类，标记所有 Bandi 自定义异常起源
    """Base exception for all Bandi errors."""


class ConfigError(BandiError):  # 功能：配置读取/校验失败（YAML 解析、字段缺失、类型不匹配）
    """Configuration loading or validation error."""


class LLMError(BandiError):  # 功能：LLM 提供方调用失败（网络、认证、限流、模型不可用）
    """LLM provider call error."""


class LLMUnavailableError(LLMError):  # 功能：无可用 LLM 候选（模型无法解析 / 候选全失败）
    """No viable LLM candidate: unresolvable model or every candidate failed.

    与普通 LLMError 的区别在**可恢复性**：这类失败重试同一个配置毫无意义
    （模型名不在目录、无 base_url、或上游整体不可用），恢复面判定按 FATAL 立即
    终止并回明确文案，而不是按 RECOVERABLE 反复重试整个回合。
    """


def llm_error_with_status(message: str, cause: BaseException) -> LLMError:
    # 功能：构造**保留上游 HTTP 状态码**的 LLMError
    # 恢复面判定（agent/recovery_manager）按 status_code >= 400 归 FATAL；跨层包装
    # 若只带消息字符串，4xx/5xx 会长得像普通可重试错误 ⇒ 整个回合被反复重试
    error = LLMError(message)
    status = upstream_status(cause)
    if status is not None:
        error.status_code = status
    return error


def upstream_status(error: BaseException | None) -> int | None:
    # 功能：沿异常链提取上游 HTTP 状态码——配置类错误（4xx 非 429）判 FATAL，
    # 暂时故障（429 / 5xx）判可重试；链上无状态则返回 None（调用方按配置类处理）。
    #
    # 为什么沿链找：候选池聚合（ProviderChainExhaustedError）与能力层包装都会新建异常对象，
    # 状态码留在内层（`last_error` 属性或 `__cause__`/`__context__`），逐层解引用才能取到。
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None) or getattr(
            getattr(current, "response", None), "status_code", None
        )
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                pass
        last = getattr(current, "last_error", None)
        if isinstance(last, BaseException) and id(last) not in seen:
            current = last
            continue
        current = current.__cause__ or current.__context__
    return None


def is_transient_upstream(error: BaseException | None) -> bool:
    # 功能：上游状态是否属**暂时故障**（429 限流 / 5xx 服务端故障）——这类失败重试有意义。
    status = upstream_status(error)
    return status is not None and (status == 429 or status >= 500)


class ToolError(BandiError):  # 功能：工具执行异常（超时、命令失败、不支持的平台）
    """Tool execution error."""


class OntologyError(BandiError):  # 功能：本体操作异常（实体 CRUD、关系校验、查询失败）
    """Ontology operation error."""


class SessionError(BandiError):  # 功能：Session 持久化异常（读写失败、格式损坏、版本不兼容）
    """Session persistence error."""


class SafetyError(BandiError):  # 功能：内容安全违规（模型拒绝回答、内容过滤命中）
    """Content safety violation."""


class AgentError(BandiError):  # 功能：Agent 生命周期异常（状态机转换失败、非法状态）
    """Agent lifecycle error."""
