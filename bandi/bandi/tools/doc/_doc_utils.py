# 功能：doc 工具族共享辅助函数（惰性 provider）

"""tools/doc 层跨文件重复辅助的公共宿主。

阶段 E DRY 复扫提取：
- ``lazy_provider``：doc_classifier / pdf_research 逐字重复（219 chars）。
子模块以 ``from bandi.tools.doc._doc_utils import ...`` 引用，保持 monkeypatch 路径兼容。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bandi.core.config import BandiConfig

if TYPE_CHECKING:
    from bandi.llm.base import BaseProvider

# 单例复用，配合 close_lazy_provider 显式释放
_lazy_provider_cache: BaseProvider | None = None


def lazy_provider(provider: BaseProvider | None) -> BaseProvider | None:
    global _lazy_provider_cache
    if provider is not None:
        return provider
    if _lazy_provider_cache is None:
        from bandi.agent.factory import build_provider

        _lazy_provider_cache = build_provider(BandiConfig.load())
    return _lazy_provider_cache


async def close_lazy_provider() -> None:
    """关闭缓存的懒加载 provider（释放 AsyncClient 连接池）。"""
    global _lazy_provider_cache
    if _lazy_provider_cache is not None:
        try:
            await _lazy_provider_cache.close()
        finally:
            _lazy_provider_cache = None


def import_fitz():
    """懒加载 PyMuPDF——被 pdf_reader 以 `as _import_fitz` 别名导入。"""
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PyMuPDF is not installed. Install with: pip install pymupdf")
    return fitz
