# 功能：契约——episodic 召回相关度门槛（B2 遗留①：recall 噪音根治）
#
# 行为不变式（钉行为，不钉实现）：
#   1. 通用词查询不得召回任何条目——「什么 / 是什么 / 用户 / 回答 / 请问」这类
# 单/双 token 通用词零区分度，命中即噪音
#   2. 真相关短查询不得误杀——「端口」这类精准短词照常命中（区分度在此）
#   3. 混合查询只取有效词——「我们的部署端口是什么」去掉停用词后仍能召回
#   4. 命中占比门槛可调——min_hit_ratio=1.0 时部分命中被挡（默认口径宽松）
#   5. 召回计数只随「返回的命中」写回——被门槛挡下的噪音不喂 consolidation
#
# 背景：20 轮普通对话中 5 条被召回 2~4 次，全部走
# 通用 gram 搭桥（「什么」「回答」「用户」），recall 虚高喂到 consolidation
# multi_query 通道。

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.memory.episodic import Episode, EpisodicStore

_SAMPLES = (
    ("e0", "用户: DNA 的中文全称是什么？ → 回答: 脱氧核糖核酸。"),
    ("e1", "用户: 太阳系一共有几颗行星？ → 回答: 8 颗。"),
    ("e2", "用户: 圆周率小数点后五位是什么？ → 回答: 14159"),
    ("e3", "用户: 第 1 题：请算 12 加 1 等于多少？只用一句话回答数字。 → 回答: 13"),
    ("e4", "用户: 把苹果这个单词翻译成英文怎么写？ → 回答: apple"),
    ("e5", "用户: 我们项目的部署端口是 7421，上线时间定在周三。 → 回答: 收到，已记录端口 7421"),
)


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    instance = EpisodicStore(str(tmp_path / "ep.db"))
    for episode_id, summary in _SAMPLES:
        instance.record(Episode(episode_id=episode_id, summary=summary, importance=0.4))
    yield instance
    instance.close()


class TestStopwordQueryGate:
    @pytest.mark.parametrize(
        "query",
        ["什么", "是什么", "是什么？", "用户", "回答", "请问", "用户: ", "现在"],
        ids=["what", "is-what", "is-what-q", "user", "answer", "please", "user-colon", "now"],
    )
    def test_stopword_query_returns_nothing(self, store: EpisodicStore, query: str) -> None:
        """破坏者：通用词查询零返回——这类召回是 recall 噪音的唯一源头。"""
        assert store.search(query, limit=5) == [], f"{query!r} 不得召回任何条目"

    def test_genuine_short_query_kept(self, store: EpisodicStore) -> None:
        """真相关短查询不得误杀（区分度在此，不在 token 数）。"""
        hits = store.search("部署端口", limit=5)
        assert [e.episode_id for e, _ in hits] == ["e5"]

    def test_genuine_short_query_kept_ascii(self, store: EpisodicStore) -> None:
        hits = store.search("英文", limit=5)
        assert [e.episode_id for e, _ in hits] == ["e4"]

    def test_mixed_query_uses_meaningful_tokens(self, store: EpisodicStore) -> None:
        """混合查询：停用词被剔除后有效词仍可召回（短查询形态）。"""
        hits = store.search("部署端口 是什么", limit=5)
        assert [e.episode_id for e, _ in hits] == ["e5"]

    def test_genuine_long_query_kept(self, store: EpisodicStore) -> None:
        hits = store.search("我们项目的部署端口是 7421，上线时间定在周三", limit=5)
        assert "e5" in [e.episode_id for e, _ in hits]


class TestRatioGate:
    def test_ratio_gate_applies_on_like_fallback(
        self, store: EpisodicStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """命中占比门槛在 LIKE 兜底路径生效——FTS 主链是全命中 AND，
        占比门槛只在兜底（部分命中）路径起作用。"""
        store.record(
            Episode(
                episode_id="e6",
                summary="用户: 部署端口上线时间 已通知运维组 → 回答: 收到",
                importance=0.4,
            )
        )
        # FTS MATCH 语法错误 → sqlite 抛异常 → 走 LIKE 兜底（OR 语义，部分命中可进入）
        monkeypatch.setattr(
            type(store),
            "_fts_query_for",
            staticmethod(lambda *a, **k: "AND"),
        )
        relaxed = store.search("部署端口 上线时间 运维组 结算周期 归档策略", limit=5)
        strict = store.search(
            "部署端口 上线时间 运维组 结算周期 归档策略", limit=5, min_hit_ratio=1.0
        )

        assert "e6" in [e.episode_id for e, _ in relaxed], "宽松口径保留部分命中"
        assert strict == [], "严格占比门槛下部分命中被挡"

    def test_stopword_gate_not_overridable_by_ratio(self, store: EpisodicStore) -> None:
        """停用词层独立于 ratio 参数——放宽 ratio 也不放行通用词召回。"""
        hits = store.search("是什么", limit=5, min_hit_ratio=0.0, min_hits=0)
        assert hits == []


class TestRecallWriteback:
    def test_gated_noise_never_bumps_recall(self, store: EpisodicStore) -> None:
        """端到端：门槛挡下的噪音不产生召回写回（recall 不被顶高）。"""
        for query in ("是什么", "用户", "回答"):
            for episode, _score in store.search(query, limit=5):
                store.note_recall(episode.episode_id, query=query)

        assert all(store.get(eid).recall_count == 0 for eid, _ in _SAMPLES)

    def test_genuine_hit_bumps_recall(self, store: EpisodicStore) -> None:
        hits = store.search("部署端口", limit=5)
        for episode, _score in hits:
            store.note_recall(episode.episode_id, query="部署端口")

        assert store.get("e5").recall_count == 1
        assert store.get("e0").recall_count == 0


class TestFunctionWordGramGate:
    """中文 2-gram 跨词切分会产出「高的 / 的代 / 有哪」这类只含虚词的 gram。

    证据（recall 分布复验）：查询「梵高的代表作有哪些？」的 gram 集合为
    ['梵高','高的','的代','代表','表作','作有','有哪','哪些？']，整词停用表一个都
    滤不掉，于是「世界上**高的**山峰」只凭 1 个 gram「高的」即被召回，
    recall_count 被顶到 15；「彩虹**有哪**几种颜色」同理。"""

    @pytest.fixture()
    def gram_store(self, tmp_path: Path) -> Any:
        instance = EpisodicStore(str(tmp_path / "gram.db"))
        samples = (
            ("g0", "用户: 世界上最高的山峰叫什么？ → 回答: 珠穆朗玛峰。"),
            ("g1", "用户: 彩虹有哪几种颜色？ → 回答: 红橙黄绿蓝靛紫。"),
            ("g2", "用户: 地球上面积最大的海洋是哪个？ → 回答: 太平洋。"),
            ("g3", "用户: 梵高的代表作有哪些？ → 回答: 向日葵、星月夜。"),
        )
        for episode_id, summary in samples:
            instance.record(Episode(episode_id=episode_id, summary=summary, importance=0.4))
        yield instance
        instance.close()

    def test_single_function_word_gram_does_not_recall(self, gram_store: EpisodicStore) -> None:
        """破坏者：只靠「高的」一个虚词 gram 搭桥的召回必须被挡——那是噪音。"""
        hits = gram_store.search("梵高的代表作有哪些？", limit=5)
        recalled = {e.episode_id for e, _ in hits}
        assert "g0" not in recalled, "「最高的山峰」不得被「高的」召回"
        assert "g1" not in recalled, "「有哪几种颜色」不得被「有哪」召回"
        assert "g2" not in recalled, "「最大的海洋」不得被「大的」召回"

    def test_content_gram_query_still_recalls(self, gram_store: EpisodicStore) -> None:
        """闸门不得误杀真相关——实词 gram 命中仍照常召回。"""
        hits = gram_store.search("珠穆朗玛峰", limit=5)
        assert [e.episode_id for e, _ in hits] == ["g0"]

    def test_two_content_grams_recall(self, gram_store: EpisodicStore) -> None:
        """实词 gram 达门槛即可召回（≥2 个实词 gram 是最低区分度要求）。"""
        hits = gram_store.search("梵高 代表作", limit=5)
        assert "g3" in {e.episode_id for e, _ in hits}

    @pytest.mark.parametrize(
        ("query", "must_not", "why"),
        [
            (
                "法国的首都是哪座城市？",
                "g2",
                "「X的首都是哪座城市」模板与「面积最大的海洋」只共享虚词 gram",
            ),
            ("莫比乌斯环有几个面？", "g2", "「有几个面」不得召回「面积最大的海洋」"),
            ("太阳系有几颗行星？", "g0", "「有几颗」不得召回「世界上最高的山峰」"),
        ],
        ids=["city-template", "how-many-faces", "how-many-planets"],
    )
    def test_question_template_does_not_cross_recall(
        self, gram_store: EpisodicStore, query: str, must_not: str, why: str
    ) -> None:
        """破坏者：问句模板相同不等于内容相关——不得跨条目召回。"""
        recalled = {e.episode_id for e, _ in gram_store.search(query, limit=5)}
        assert must_not not in recalled, why

    def test_same_template_different_subject_not_recalled(self, tmp_path: Path) -> Any:
        """同模板不同主体（中国的首都 vs 法国的首都）——共享实词仍不足以召回。"""
        instance = EpisodicStore(str(tmp_path / "capital.db"))
        instance.record(
            Episode(
                episode_id="c0",
                summary="用户: 中国的首都是哪座城市？ → 回答: 北京",
                importance=0.4,
            )
        )
        try:
            recalled = {e.episode_id for e, _ in instance.search("法国的首都是哪座城市？", limit=5)}
            assert recalled == set(), "仅共享「首都」一个实词不足以召回别国条目"
            recalled_own = {
                e.episode_id for e, _ in instance.search("中国的首都是哪座城市？", limit=5)
            }
            assert recalled_own == {"c0"}, "真相关（自身）必须召回"
        finally:
            instance.close()

    def test_no_single_gram_bridge_raises_recall(self, gram_store: EpisodicStore) -> None:
        """端到端：虚词搭桥的跨条目召回为 0——别条目的 recall 不被顶高。

        证据（recall_queries 反查）：修复前「梵高的代表作有哪些？」曾召回
        「世界上最高的山峰叫什么？」（recall 顶到 15）；「莫比乌斯环有几个面？」
        曾召回「太阳系一共有几颗行星？」——两侧共享「的有」「有几」这类
        虚词 gram 即被判相关。修复后只有真相关条目被召回。"""
        for query in (
            "梵高的代表作有哪些？",
            "人体最大的器官是什么？",
            "莫比乌斯环有几个面？",
            "彩虹有哪几种颜色？",
        ):
            for episode, _score in gram_store.search(query, limit=5):
                gram_store.note_recall(episode.episode_id, query=query)

        assert gram_store.get("g3").recall_count == 1, "「梵高的代表作有哪些？」召回自身"
        assert gram_store.get("g1").recall_count == 1, "「彩虹有哪几种颜色？」召回自身"
        assert gram_store.get("g0").recall_count == 0, "「最高的山峰」不得被虚词搭桥召回"
        assert gram_store.get("g2").recall_count == 0, "「最大的海洋」不得被虚词搭桥召回"


class TestLikeFallbackFunctionWordGate:
    """FTS 主链是全 gram AND（精确）；LIKE 兜底是 OR 语义——单个虚词 gram 即可命中。

    证据：episodic 与 long_term/working 同库，FTS5 方言异常时回落 LIKE
    （bandi.log 里 long_term/working 已在刷「FTS5 search failed, falling back」）。
    兜底路径若不过滤虚词 gram，「梵高的代表作有哪些？」会因「高的」命中
    「世界上最高的山峰叫什么？」——即修复前 recall 顶到 15 的那条路径。"""

    @pytest.fixture()
    def fallback_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        instance = EpisodicStore(str(tmp_path / "fb.db"))
        for episode_id, summary in (
            ("f0", "用户: 世界上最高的山峰叫什么？ → 回答: 珠穆朗玛峰。"),
            ("f1", "用户: 梵高的代表作有哪些？ → 回答: 向日葵、星月夜。"),
        ):
            instance.record(Episode(episode_id=episode_id, summary=summary, importance=0.4))
        monkeypatch.setattr(type(instance), "_fts_query_for", staticmethod(lambda *a, **k: "AND"))
        yield instance
        instance.close()

    def test_function_word_gram_not_used_as_bridge(self, fallback_store: EpisodicStore) -> None:
        """破坏者：LIKE OR 兜底下「高的」不得成为召回桥梁。"""
        recalled = {e.episode_id for e, _ in fallback_store.search("梵高的代表作有哪些？", limit=5)}
        assert "f0" not in recalled, "「最高的山峰」不得只靠「高的」搭桥召回"
        assert "f1" in recalled, "真相关（自身）必须召回"

    def test_single_content_word_query_still_works(self, fallback_store: EpisodicStore) -> None:
        """单实词 gram 查询不得被 min_hits 误杀——「珠峰」这类精准查询照常召回。"""
        recalled = {e.episode_id for e, _ in fallback_store.search("珠穆朗玛峰", limit=5)}
        assert recalled == {"f0"}, "单个实词 gram 的精准查询必须可召回"
