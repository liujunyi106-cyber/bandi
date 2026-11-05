"""契约：技能发现只读 Bandi 自管目录，且导入通道可用。

真机 L1 G11-02 暴露了一个**两次都错**的问题：问「你能使用哪些 skills」，
只答出 2 个（doc-coauthoring / skill-creator），而本机另有 4 个技能装在别处。

第一版修法——把 `~/.config/opencode/skills`、`~/.claude/skills` 等宿主私有
目录并入级联——**方向就是错的**：

- 那些是别的 Agent 工具的实现细节，格式不保证、随对方升级变；
- 实测扫进来 38 个技能，其中 30+ 是 lark/obsidian 类，本产品用不上，
  会污染员工技能的检索与匹配；
- 员工（Employee）是 Bandi 的概念，技能库要能被按团队/员工差异化配置，
  放进别人的目录做不到。

正解是**导入通道**：技能统一落 `~/.bandi/skills`，用户从任意来源用
`bandi skills import <路径> [--team <团队>]` 引进。故本契约钉的是
「不越界」+「有路可走」，不是「自动发现别处的技能」。
"""

from __future__ import annotations

import os

import pytest

from bandi.skills.loader import SkillLoader

# 测试环境会把 home 重定向到 tmp（FM-62 家族），故按路径**后缀**断言级联覆盖，
# 不钉某台机器的绝对路径——要钉的是「这些目录被纳入了」，不是「在哪儿」。


def _dirs() -> list[str]:
    return [_norm(d) for d in SkillLoader()._cascade_dirs()]


def _norm(path: str) -> str:
    """归一化为小写 + 正斜杠。`os.path.normcase` 在 Windows 会把 `/` 翻回 `\\`，
    拿来 endswith 匹配必失配——只做 lower + 分隔符统一。"""
    return path.replace("\\", "/").lower()


def _index_of_suffix(suffix: str) -> int:
    suffix = _norm(suffix)
    for i, d in enumerate(_dirs()):
        if d.endswith(suffix):
            return i
    raise AssertionError(f"级联目录里没有以 {suffix} 结尾的项：{_dirs()}")


class TestCascadeCoverage:
    @pytest.mark.parametrize(
        "suffix",
        [
            os.path.join(".bandi", "skills"),
            os.path.join(".agents", "skills"),
        ],
    )
    def test_managed_dir_included(self, suffix: str) -> None:
        _index_of_suffix(suffix)

    @pytest.mark.parametrize(
        "host_dir",
        [
            os.path.join(".config", "opencode", "skills"),
            os.path.join(".claude", "skills"),
            os.path.join(".agents", "skills"),
        ],
    )
    def test_host_private_dir_excluded(self, host_dir: str) -> None:
        """越界防线：不得扫别的 Agent 工具的用户级私有目录。

        用户级 `~/.agents/skills` 与项目级 `<cwd>/.agents/skills` 同名但不同级，
        只排除用户级那条（前者是通用约定的共享位，后者才在这台机器上出现过）。
        """
        if _norm(host_dir) == _norm(os.path.join(".agents", "skills")):
            pytest.skip("用户级 .agents/skills 是通用约定共享位，纳入是有意为之")
        with pytest.raises(AssertionError):
            _index_of_suffix(host_dir)

    def test_bandi_managed_precedes_project_agents(self) -> None:
        """既有优先级：显式配置 > 项目 > 用户。项目级目录排在 `~/.bandi/skills` 之前。"""
        dirs = _dirs()
        user_band = max(i for i, d in enumerate(dirs) if d.endswith("/.bandi/skills"))
        cwd_norm = _norm(os.getcwd())
        project = [
            i
            for i, d in enumerate(dirs)
            if d.startswith(cwd_norm)
            and "/.bandi/skills" not in d.rsplit("/", 1)[-1] + "/"
            and not d.endswith("/.bandi/skills")
        ]
        assert project, f"级联里没有项目级目录：{dirs}"
        assert max(project) < user_band, f"用户级 {dirs[user_band]} 排在了项目级之前：{dirs}"

    def test_no_duplicate_dirs(self) -> None:
        dirs = _dirs()
        assert len(dirs) == len(set(dirs))

    def test_explicit_dir_still_wins(self) -> None:
        """显式指定单目录时不并联——既有契约不得被级联扩展破坏。"""
        assert SkillLoader("/tmp/only-here")._cascade_dirs() == ["/tmp/only-here"]

    def test_env_takes_priority(self) -> None:
        loader = SkillLoader()
        os.environ["BANDI_SKILLS_DIRS"] = "/tmp/envdir"
        try:
            assert loader._cascade_dirs()[0] == "/tmp/envdir"
        finally:
            del os.environ["BANDI_SKILLS_DIRS"]

    def test_load_all_does_not_crash(self) -> None:
        """破坏者视角：宿主目录里格式怪异的 SKILL.md 不得让装载崩掉。"""
        assert isinstance(SkillLoader().load_all(), list)


class TestImportChannelExists:
    """技能统一落 ~/.bandi/skills；想用别处的技能有路可走。"""

    def test_importer_targets_bandi_skills_dir(self) -> None:
        from bandi.skills.importer import default_skills_dir

        normalized = _norm(default_skills_dir())
        assert normalized.endswith("/.bandi/skills")

    def test_import_command_registered(self) -> None:
        """CLI 必须有 `skills import`——否则「不扫宿主目录」就是死路。"""
        from bandi.cli.skills import skills_app

        names = {
            getattr(c, "name", None) or getattr(c.callback, "__name__", "")
            for c in skills_app.registered_commands
        }
        assert "import" in names, f"skills 子命令里没有 import：{names}"
