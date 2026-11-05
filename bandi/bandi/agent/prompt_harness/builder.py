# 功能：系统提示词组装器（静态段前置/动态段后置分区；工具/Skill/恢复点已抽离为工件消息通道）
# 分区语义（prompt-cache 前缀稳定性）：static（身份/框架/本体/模式——跨会话/跨员工稳定）
# 连续在最前，dynamic（领域/项目惯例/记忆/实体——逐轮可变）居中，tail（安全/输出/体验——
# 静态但需在动态之后沉底，SAFETY 显著性纪律）收尾。
# ⚠️ 领域指令**不进 static**：领域由 DomainRouter 按用户输入关键词逐轮路由（同一会话
# 内可变），一旦进 static，不同领域/不同员工的静态前缀就不一致 → provider 前缀缓存
# 的跨 Agent 共享被打碎（命中率崩塌）。领域指令本身逐轮可变，放动态段零损失。

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bandi.agent.prompt_harness.prompt_loader import PromptLoader

# 10.10：static 段稳定前缀 LRU（openclaw system-prompt 稳定前缀语义）——
# 同 (language, mode, ontology, 身份变体) 在**同一轮内**复用静态串，避免重复拼接。
# ⚠️ 不跨轮：`prompt_harness_from_config` 在 `core/agent_runner.py::run_agent_turn`
_STATIC_CACHE_MAX = 64

if TYPE_CHECKING:
    from bandi.perception.context_loader import ContextBundle

_SEPARATOR_TOKENS = 2

# 不得吃满共享池，同时保留交叉余量避免任一段被压到最低保底）
_RULES_SECTION_MAX_RATIO = 0.6
_MEMORY_SECTION_MAX_RATIO = 0.6

PROJECT_MEMORY_RULES = """\
## 项目记忆规则

项目记忆用于沉淀跨会话的长期经验与事实。**MEMORY.md 是索引，不是正文仓库**：
- 记忆正文写入独立文件 `.bandi/memory/entries/<id>.md`；MEMORY.md 只保留一行索引
  `- [标题](entries/<id>.md) — 一行钩子（≤150 字）`。不要把正文直接写进 MEMORY.md。
- 沉淀前先用 memory_search 查重，避免重复条目。
- 需要原文时用 memory_read 按 entry_id 展开，不要凭钩子猜内容。
- 仅当内容一行内说得清时，才可直接写进 MEMORY.md 正文区。
- 用户要求遗忘某条记忆时用 memory_forget（scope=project_entry 或 long_term）。
- 跨项目通用的事实由长期记忆自动沉淀（long_term 全局层），无需手动写入。
- 跨事物之间的结构关系（A 依赖 B / A 属于 B / A 取代 B）用 memory_assert 写三元组，谓词必须取自关系词汇表。
- 禁止写入密钥、口令、token 等敏感信息。
- 记忆是补充信息，不替代正常回复或交付物。

记忆卫生（写入前逐条自检）：
- 只记四类：用户偏好、反馈教训、项目事实、参考资料；代码结构、文件路径、git 历史、调试过程和临时任务不写。
- 反馈类同时记录「为什么」和「下次怎么用」，成功与失败都记。
- 涉及文件、函数或开关的记忆，推荐前先验证当前代码；已失效的内容标注或遗忘。
- 用户明确说「忽略记忆」时，本次回答按空记忆处理，不引用、不对比、不提及任何记忆内容。
- 相对日期（今天/昨天/下周）写入时转换为绝对日期（YYYY-MM-DD）。"""


def _user_desktop() -> Path | None:
    """功能：探测当前用户的桌面目录（Windows 走注册表权威值）。

    不按目录名猜：非英语系统桌面不叫 `Desktop`，OneDrive 重定向后桌面在
    `OneDrive\\Desktop` 下，猜名字会漏。`User Shell Folders\\Desktop` 是
    Windows 自己解析出的真实路径（含本地化与重定向），值里带 `%...%`
    环境变量需展开。非 Windows 或读取失败时回退 `~/Desktop`。
    """
    import os

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = os.path.expandvars(str(raw)).strip()
            if expanded:
                return Path(expanded)
        except (OSError, ImportError, ValueError):
            pass
    try:
        return Path.home() / "Desktop"
    except (OSError, RuntimeError):
        return None


def _accessible_paths_section() -> str:
    """功能：可访问路径清单（动态段）——告诉模型工作区的真实绝对路径。

    缺这段时模型只能猜用户目录：实测会猜出 `C:/Users/Administrator/Desktop`
    这类不存在的路径，然后把自己的臆测当成访问失败的事实报给用户。
    路径逐机器/逐项目可变（workspace_root 受 BANDI_WORKSPACE_ROOT 与 project_root
    影响），故归动态段，不进 static 前缀。
    """
    from bandi.tools.fs_tool import workspace_root

    entries: list[str] = []
    roots: list[Path] = []
    try:
        roots.append(Path(workspace_root()))
    except Exception:  # noqa: BLE001 — 环境信息缺失不得影响主链路
        pass
    try:
        roots.append(Path.home())
    except (OSError, RuntimeError):
        pass
    desktop = _user_desktop()
    if desktop is not None:
        roots.append(desktop)
    for root in roots:
        try:
            if not root.exists():
                continue
            resolved = str(root.resolve())
        except OSError:
            continue
        if resolved not in entries:
            entries.append(resolved)
    if not entries:
        return ""
    lines = "\n".join(f"- `{p}`" for p in entries)
    return (
        "## 可访问路径\n\n"
        f"{lines}\n\n"
        "以上为本机真实路径。访问用户目录下的文件（如桌面）时直接使用上述路径，"
        "**不要臆造 `C:/Users/<用户名>/...` 之类的目录名**；若某路径确实不存在，"
        "如实说明并列出上方可用路径供用户确认，不得把猜测当作访问失败的事实。"
    )


@dataclass(frozen=True)
class PromptSections:
    """功能：System Prompt 三段分区——static 为可缓存前缀（跨轮字节稳定）
    dynamic/tail 为可变与安全收尾段。build_sections 的组装产物。"""

    static: str
    dynamic: str
    tail: str


def prompt_harness_from_config(bandi_config: object) -> PromptHarness:
    """功能：按 BandiConfig 装配 PromptHarness（preferred_language 的单一透传点）。

    历史缺陷（全量审查发现）：生产两处构造（`core/agent_runner.py` 主链
    `agent/subagent.py` 子代理）此前均为 `PromptHarness()` 裸调用 → `BandiConfig.
    preferred_language` 零消费者，用户配 `en` 仍按 zh-CN 作答。集中于此便于单点验证。
    """
    language = str(getattr(bandi_config, "preferred_language", "") or "zh-CN")
    return PromptHarness(preferred_language=language)


class PromptHarness:
    _MAX_ENTITY_NAME_CHARS = 80

    def __init__(
        self,
        preferred_language: str = "zh-CN",
        prompt_loader: PromptLoader | None = None,
    ):
        self._language = preferred_language
        self._loader = prompt_loader or PromptLoader()
        self._static_cache: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    @property
    def static_cache_size(self) -> int:
        """功能（10.10）：静态前缀缓存当前条目数（有界 ≤ _STATIC_CACHE_MAX）。"""
        return len(self._static_cache)

    def _static_prompt(self, mode: str, ontology_schema: str, is_secretary: bool = False) -> str:
        """功能（10.10）：static 段构建（LRU 复用）——键含语言/本体模式哈希/
        身份变体（秘书=无项目会话）；命中把条目移到队尾（最近使用），超限淘汰最旧。

        ⚠️ 键与内容**不含领域**（修）与**不含 mode**（修）：
        领域逐轮关键词路由、mode 可切换——二者占前缀会让静态段随运行状态漂移
        跨 Agent 共享命中率被稀释；领域指令与 mode 指令分别由 `_domain_instruction`
        与 `build_sections` 注入**动态段**（mode 参数保留仅为调用签名兼容）。
        """
        schema_hash = (
            hashlib.sha256(ontology_schema.encode("utf-8")).hexdigest()[:16]
            if ontology_schema
            else ""
        )
        key = (
            self._language,
            schema_hash,
            "secretary" if is_secretary else "member",
        )
        cached = self._static_cache.get(key)
        if cached is not None:
            self._static_cache.move_to_end(key)
            return cached
        identity = (
            self._loader.get_secretary_identity() if is_secretary else self._loader.get_identity()
        )
        static_parts: list[str] = [identity.format(language=self._language)]
        static_parts.append(self._loader.get_framework())
        if ontology_schema:
            static_parts.append(ontology_schema)
        static_text = "\n\n".join(static_parts)
        self._static_cache[key] = static_text
        if len(self._static_cache) > _STATIC_CACHE_MAX:
            self._static_cache.popitem(last=False)
        return static_text

    @staticmethod
    def _domain_instruction(domain_label: str) -> str:
        """功能：领域指令文本（**动态段**）——领域由 DomainRouter 逐轮关键词路由
        故其指令只约束本轮工具面，不得占用可缓存前缀（见文件头注记）。"""
        if not domain_label:
            return ""
        return (
            f"## 当前领域\n\n你正在处理「{domain_label}」相关工作。只使用下方列出的工具完成需求。"
        )

    def build_sections(
        self,
        mode: str,
        context: ContextBundle,
        degraded_capabilities: dict[str, str] | None = None,
        domain_label: str = "",
        project_instructions_max_tokens: int | None = None,
    ) -> PromptSections:
        static_text = self._static_prompt(
            mode, context.ontology_schema or "", not context.project_scoped
        )

        dynamic_parts: list[str] = []
        # 占用可缓存前缀（切模式不废 prefix cache）；先于领域/项目惯例被读到
        mode_prompt = self._loader.get_mode_prompts().get(mode)
        if mode_prompt:
            dynamic_parts.append(mode_prompt)
        # 领域指令置动态段次位：它是工具面约束，应先于易变的项目惯例/记忆被读到
        domain_instruction = self._domain_instruction(domain_label)
        if domain_instruction:
            dynamic_parts.append(domain_instruction)
        if context.project_instructions or context.memory_context:
            instructions = self._trim_project_sections(
                context.project_instructions,
                context.memory_context,
                project_instructions_max_tokens,
            )
            if instructions:
                dynamic_parts.append(f"## 项目惯例\n\n{instructions}")

        if context.memory_rules_active:
            dynamic_parts.append(PROJECT_MEMORY_RULES)

        accessible_paths = _accessible_paths_section()
        if accessible_paths:
            dynamic_parts.append(accessible_paths)

        if context.referenced_entities:
            safe_lines = []
            for e in context.referenced_entities:
                safe_name = e.name.replace("\n", " ").replace("\r", " ")
                if len(safe_name) > self._MAX_ENTITY_NAME_CHARS:
                    safe_name = safe_name[: self._MAX_ENTITY_NAME_CHARS] + "..."
                safe_lines.append(
                    f"- [{getattr(e, 'entity_type', '') or '未知'}] {safe_name} (id: {e.id})"
                )
            entity_lines = "\n".join(safe_lines)
            dynamic_parts.append(f"## 当前上下文实体\n\n{entity_lines}")

        tail_parts: list[str] = [self._loader.get_safety()]
        tail_parts.append(self._loader.get_output_control())
        tail_parts.append(self._loader.get_user_experience())

        if degraded_capabilities:
            degraded_lines = "\n".join(
                f"- {layer}: {reason}" for layer, reason in degraded_capabilities.items()
            )
            tail_parts.append(
                self._loader.get_fallback_template().format(degraded_list=degraded_lines)
            )

        return PromptSections(
            static=static_text,
            dynamic="\n\n".join(dynamic_parts),
            tail="\n\n".join(tail_parts),
        )

    def build(
        self,
        mode: str,
        context: ContextBundle,
        degraded_capabilities: dict[str, str] | None = None,
        domain_label: str = "",
        project_instructions_max_tokens: int | None = None,
    ) -> str:
        sections = self.build_sections(
            mode,
            context,
            degraded_capabilities=degraded_capabilities,
            domain_label=domain_label,
            project_instructions_max_tokens=project_instructions_max_tokens,
        )
        return "\n\n".join(p for p in (sections.static, sections.dynamic, sections.tail) if p)

    @classmethod
    def _trim_project_instructions(cls, instructions: str, max_tokens: int | None) -> str:
        """按预算语义块裁剪 project_instructions——禁用字符二分切（不切断 Markdown
        结构：半标题/代码围栏孤儿）；仅当整段无块边界可用时才退 token 前缀兜底。"""
        if max_tokens is None or max_tokens <= 0:
            return instructions
        from bandi.utils.token_counter import TokenCounter

        if TokenCounter.estimate(instructions) <= max_tokens:
            return instructions
        return cls._semantic_trim(instructions, max_tokens)

    @classmethod
    def _trim_project_sections(
        cls,
        rules: str,
        memory: str,
        max_tokens: int | None,
    ) -> str:
        """
        不再「后缀二分把层级记忆整段裁掉」。

        语义：规则优先（放得下则完整保留），记忆侧语义装箱绝不整段丢失——
          1. 合并未超预算 → 原样；
          2. 规则放得下 → 规则完整，记忆装剩余预算（语义装箱）；
          3. 规则本身即占满/超预算 → 给记忆保底（至多半预算），规则后缀裁剪补足。
        输出恒守 规则 → 记忆 顺序。
        """
        from bandi.utils.token_counter import TokenCounter

        if max_tokens is None or max_tokens <= 0:
            return cls._link_sections(rules, memory)
        # （原共享池下膨胀源可把对方挤到最低保底；现各 ≤60%，保留交叉余量）
        rules_cap = max(1, int(max_tokens * _RULES_SECTION_MAX_RATIO))
        memory_cap = max(1, int(max_tokens * _MEMORY_SECTION_MAX_RATIO))
        if rules and TokenCounter.estimate(rules) > rules_cap:
            rules = cls._trim_project_instructions(rules, rules_cap)
        if memory and TokenCounter.estimate(memory) > memory_cap:
            memory = cls._semantic_trim(memory, memory_cap)
        combined = cls._link_sections(rules, memory)
        if TokenCounter.estimate(combined) <= max_tokens:
            return combined
        if not memory:
            return cls._trim_project_instructions(rules, max_tokens)
        if not rules:
            return cls._semantic_trim(memory, max_tokens)
        rules_cost = TokenCounter.estimate(rules)
        if rules_cost < max_tokens:
            # 规则完整保留，记忆装剩余（语义装箱，扣分隔符开销）
            memory_kept = cls._semantic_trim(memory, max_tokens - rules_cost - _SEPARATOR_TOKENS)
            return cls._link_sections(rules, memory_kept)
        # 规则本身即占满/超预算：记忆保底（至多半预算），规则语义裁剪补足
        mem_floor = min(TokenCounter.estimate(memory), max_tokens // 2)
        memory_kept = cls._semantic_trim(memory, mem_floor)
        remaining = max(1, max_tokens - TokenCounter.estimate(memory_kept) - _SEPARATOR_TOKENS)
        rules_kept = cls._trim_project_instructions(rules, remaining)
        return cls._link_sections(rules_kept, memory_kept)

    @staticmethod
    def _link_sections(rules: str, memory: str) -> str:
        if rules and memory:
            return f"{rules}\n\n{memory}"
        return rules or memory

    @classmethod
    def _semantic_trim(cls, text: str, budget: int) -> str:
        """语义块装箱：按空行拆块贪心装入；块放不下时保留必留首行标识（代码围栏
        块不可安全截断→整块丢弃）；无任何块可装时退 token 前缀截断兜底。"""
        from bandi.utils.token_counter import TokenCounter

        if budget <= 0:
            return ""
        blocks = [b for b in (text or "").split("\n\n") if b.strip()]
        if not blocks:
            return ""
        kept: list[str] = []
        remaining = budget
        for blk in blocks:
            cost = max(1, TokenCounter.estimate(blk)) + (_SEPARATOR_TOKENS if kept else 0)
            if cost <= remaining:
                kept.append(blk)
                remaining -= cost
                continue
            lines = blk.splitlines()
            if len(lines) > 1 and not blk.lstrip().startswith("```"):
                prefix = lines[0].strip()
                p_cost = max(1, TokenCounter.estimate(prefix)) + (_SEPARATOR_TOKENS if kept else 0)
                if prefix and p_cost <= remaining:
                    kept.append(prefix)
                    remaining -= p_cost
        if not kept and (text or "").strip():
            return TokenCounter.truncate_to_token_budget((text or "").strip(), budget)
        return "\n\n".join(kept)
