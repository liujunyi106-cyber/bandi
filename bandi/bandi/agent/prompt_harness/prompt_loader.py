# 功能：提示词模板读取（单一真相源 = artifacts.py 常量）
#
# 历史：曾支持 prompts/*.yaml 覆盖（loader 优先读 YAML、常量兜底），但生产无任何
# 调用方使用该能力——同一段提示词存两份，改常量而跑另一份，是B/B/
# B修复时踩到的坑。现删除 YAML 层，常量为唯一真相源。
from __future__ import annotations

from bandi.agent.prompt_harness.artifacts import (
    CODE_EDIT_PROMPT,
    FALLBACK_PROMPT_TEMPLATE,
    FRAMEWORK_PROMPT,
    IDENTITY_PROMPT,
    MODE_PROMPTS,
    OUTPUT_CONTROL_PROMPT,
    SAFETY_PROMPT,
    TASK_EXECUTION_PROMPT,
    TOOL_USAGE_PROMPT,
    USER_EXPERIENCE_PROMPT,
)

# 功能：无项目会话的「助理」身份模板——普通会话（L1）与成员会话说不同的话术，
# 但身份自称与禁述底层模型的约束一致。
_SECRETARY_IDENTITY = """你是 Bandi 助理，隶属 Bandi 数字员工组织，自称 Bandi 助手，是用户的通用助理（无项目归属的会话由你接管）。

被问及你是谁、你叫什么、由什么驱动时，只回答 Bandi 助手或 Bandi 数字员工，不要提及底层模型名称与厂商。

你的职责：
- 直接回答用户的问题、分析需求，并调用工具完成各类事务（检索、文件、数据处理等）
- 帮助用户组建 AI 员工团队（可引导到「团队」页创建，或按团队模板实例化）
- 当用户把任务归属到某个「项目」时，由该项目的工作流接管；未归属的任务在你这里完成
- 过程中沉淀的长期经验会自动进入记忆系统，不需用户手动整理

能力清单以本次列出的工具为唯一依据。清单里没有的能力一律回答「暂不支持」，不要凭想象列举你并不具备的功能。被问「你能做什么」时按框架层给定的固定分档逐档作答。

用 {language} 回复，务实、直接。"""


class PromptLoader:
    def get_identity(self) -> str:
        return IDENTITY_PROMPT

    def get_secretary_identity(self) -> str:
        return _SECRETARY_IDENTITY

    def get_framework(self) -> str:
        return FRAMEWORK_PROMPT

    def get_task_execution(self) -> str:
        return TASK_EXECUTION_PROMPT

    def get_tool_usage(self) -> str:
        return TOOL_USAGE_PROMPT

    def get_code_edit(self) -> str:
        return CODE_EDIT_PROMPT

    def get_mode_prompts(self) -> dict[str, str]:
        return MODE_PROMPTS

    def get_safety(self) -> str:
        return SAFETY_PROMPT

    def get_output_control(self) -> str:
        return OUTPUT_CONTROL_PROMPT

    def get_user_experience(self) -> str:
        return USER_EXPERIENCE_PROMPT

    def get_fallback_template(self) -> str:
        return FALLBACK_PROMPT_TEMPLATE
