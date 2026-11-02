# 功能：契约异常终态下模型真实输出必须落库，系统替代文案不落库
#
# 行为不变式（钉行为，不钉实现）：
#   1. 正常终态（final_message 为 None 或与模型输出一致）→ 落库模型真实输出
#   2. 异常终态（final_message 为系统生成文案且与模型输出不同）→ 落库模型真实输出
#      （current_content），系统文案绝不进历史
#   3. 异常终态且模型无任何输出（current_content 为空）→ 不落库空消息
#   4. 破坏者视角：模型输出了内容但系统同时给了终止文案时，历史里必须能找到
#      模型说过的话——刷新页面不能把用户已看到的回复抹掉

from __future__ import annotations

from bandi.core.agent_runner import _assistant_persist_content


class TestTerminalPersist:
    def test_normal_terminal_persists_model_output(self) -> None:
        """功能：正常终态（final_message=None）落库模型输出。"""
        assert _assistant_persist_content(None, "模型真实回复") == "模型真实回复"

    def test_normal_terminal_persists_when_final_equals_current(self) -> None:
        """功能：final_message 与模型输出一致时按模型输出落库。"""
        assert _assistant_persist_content("同一句", "同一句") == "同一句"

    def test_abnormal_terminal_persists_model_output_not_system_text(self) -> None:
        """功能：异常终态落库 current_content，不得把系统文案写进历史。

        这是 BC-006 的核心不变式：模型已流式送达用户的回复必须留存。
        """
        system_text = "工具 read 在被暂停后仍被反复调用（第 5 次）——已中止本轮。"
        model_text = "好的老板，我已经记住了！"

        got = _assistant_persist_content(system_text, model_text)

        assert got == model_text
        assert got is not None
        assert system_text not in (got or "")

    def test_abnormal_terminal_without_model_output_skips(self) -> None:
        """功能：异常终态且模型无输出时不落空消息。"""
        assert _assistant_persist_content("已中止本轮。", "") is None
        assert _assistant_persist_content("已中止本轮。", None) is None

    def test_system_text_never_persisted_even_when_only_source(self) -> None:
        """功能：破坏者视角——只有系统文案、模型未产出时，绝不能把系统文案落库。

        防回归：历史含系统文案会让后续轮模型模仿复读（冒烟 r3 复读 r2 拦截文案）。
        """
        assert _assistant_persist_content("检测到注入攻击", "") is None
        assert _assistant_persist_content("会话超时（60 秒），已停止本轮。", None) is None

    def test_blank_model_output_treated_as_empty(self) -> None:
        """功能：模型输出为纯空白时按无输出处理，不落空白消息。"""
        assert _assistant_persist_content("已中止本轮。", "   \n  ") is None

    def test_normal_terminal_blank_output_returns_none(self) -> None:
        """功能：正常终态但双方均无内容时不落库。"""
        assert _assistant_persist_content(None, "") is None
        assert _assistant_persist_content(None, None) is None


class TestPersistedContentIsRealModelOutput:
    def test_multi_line_model_output_preserved_verbatim(self) -> None:
        """功能：破坏者视角——多段模型输出须原样留存（含换行与标点）。"""
        model_text = "好的老板，我已经记住了！\n\n你在意 88 与靛蓝色。\n还要记住别的吗？"
        got = _assistant_persist_content("已中止本轮。", model_text)
        assert got == model_text
        assert "\n" in (got or "")
