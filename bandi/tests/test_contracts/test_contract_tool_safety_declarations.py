"""
契约族：工具安全声明完整性（同族补齐，免疫）。

背景：协作工具族已钉「写工具必须声明 retryable=False」（test_contract_collab_tools.py
契约 12），但同族的 todo_write/config_file/archive_manager/pdf_reader/request_user_input
与纯只读族（read_document/entity_query/ontology_query/*_processor/skill_*）仍无声明——
安全仅靠「失败走 _err dict → executor 标 non_retryable」的隐式兜底，一旦未来改成
抛异常失败即落入默认重试重放（与协作工具修复前完全同构）。

本契约钉的行为族（全部生产调用路径 = registry 注册的工具类元数据）：
- 写/混合/交互工具：必须显式声明 retry_policy=RetryPolicy(retryable=False)（权威
  独立于隐式 dict 兜底）——重试会重放副作用（todo 重复写入 / 重复发消息 / 重复解压 /
  PDF split-merge 重复产出 / 重复打扰用户）
- 纯只读工具：必须标 is_read_only=True——去重（executor._is_dedup_eligible）与
  超时重试（retry_mw ）都依赖此标记；未标则只读工具不去重、超时被禁重试（可用性损失）

断言「最终产物」：工具类元数据声明（retry_policy / is_read_only）——这是后续
executor 去重与 retry 决策链的行为前提。
"""

from __future__ import annotations

import pytest

from bandi.tools.base import RetryPolicy
from bandi.tools.doc.archive_manager import ArchiveManagerTool
from bandi.tools.doc.config_file_tool import ConfigFileTool
from bandi.tools.doc.csv_processor import CSVProcessorTool
from bandi.tools.doc.html_processor import HTMLProcessorTool
from bandi.tools.doc.markdown_processor import MarkdownProcessorTool
from bandi.tools.doc.pdf_reader import PDFReaderTool
from bandi.tools.doc.text_processor import TextFileProcessorTool
from bandi.tools.doc_tool import DocumentReadTool
from bandi.tools.skill_tool import SkillDiscoverTool, SkillTool
from bandi.tools.todo import TodoWriteTool
from bandi.tools.user_input import UserInputTool

# 写/混合/交互工具——失败不得自动重试（重放副作用或重复打扰用户）
_WRITE_TOOLS = [
    TodoWriteTool,  # todo_write：写任务列表
    ConfigFileTool,  # config_file：write/merge/convert 写文件
    ArchiveManagerTool,  # archive_manager：extract/create 写文件系统
    PDFReaderTool,  # pdf_reader：split/merge/extract_pages/convert_to_images 写文件
    UserInputTool,  # request_user_input：交互，重试=重复询问用户
    CSVProcessorTool,  # csv_processor：_op_write/_op_convert 写文件
    TextFileProcessorTool,  # text_processor：8 个 _op_* 辅助方法 write_text 写文件
    HTMLProcessorTool,  # html_processor：_op_convert + _write_output 写输出文件
    MarkdownProcessorTool,  # markdown_processor：_op_write + _write_file 写文件
]


def _ontology_query_tool():
    """功能：三元组本体查询工具实例（六模式只读查询）。"""
    import tempfile
    from pathlib import Path

    from bandi.ontology.spec import open_ontology
    from bandi.ontology.tools import OntologyToolContext
    from bandi.ontology.triple_store import OntologyStore
    from bandi.tools.ontology_tool_adapter import build_ontology_tools

    store = OntologyStore(open_ontology(str(Path(tempfile.mkdtemp()) / "onto.db")))
    for tool in build_ontology_tools(OntologyToolContext(store=store, inference=True)):
        if tool.name == "ontology_query":
            return tool
    raise AssertionError("ontology_query 未构建")


# 纯只读工具——必须标 is_read_only（去重/超时重试语义正确）
_READ_TOOLS = [
    DocumentReadTool,  # read_document
    _ontology_query_tool(),  # ontology_query：三元组六模式只读查询
    SkillTool,  # skill_invoke：读 skill 定义
    SkillDiscoverTool,  # skill_discover
]


@pytest.mark.parametrize("tool_cls", _WRITE_TOOLS, ids=lambda c: c.name)
def test_contract_write_tools_declare_non_retryable(tool_cls) -> None:
    """写/混合/交互工具必须显式声明 retryable=False（同族补齐 ）。"""
    policy = getattr(tool_cls, "retry_policy", None)
    assert policy is not None, (
        f"{tool_cls.name} 未声明 retry_policy——安全仅靠隐式 dict 兜底，改抛异常即裸奔"
    )
    assert isinstance(policy, RetryPolicy) and policy.retryable is False, (
        f"{tool_cls.name} 必须显式 retryable=False"
    )


@pytest.mark.parametrize("tool_cls", _READ_TOOLS, ids=lambda c: c.name)
def test_contract_read_tools_declare_read_only(tool_cls) -> None:
    """纯只读工具必须标 is_read_only=True（去重/超时重试语义正确）。"""
    assert getattr(tool_cls, "is_read_only", False) is True, (
        f"{tool_cls.name} 必须标 is_read_only=True——否则不去重且超时被禁重试"
    )
