# 功能：产物守卫契约——完成态假完成兜底（「零落盘假完成」）。完成判定前
# 校验声称产物可读回且落在 project_root 内；缺失时生成校正指令驱动补齐轮。
from pathlib import Path

from bandi.agent.artifact_guard import ArtifactGuard, correction_prompt


class TestArtifactGuardExistence:
    def test_existing_artifact_is_clean(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.txt").write_text("A", encoding="utf-8")
        guard = ArtifactGuard(tmp_path)
        assert guard.missing(["alpha.txt"]) == []

    def test_missing_artifacts_reported(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.txt").write_text("A", encoding="utf-8")
        guard = ArtifactGuard(tmp_path)
        missing = guard.missing(["alpha.txt", "beta.txt", "gamma/gamma.txt"])
        assert sorted(missing) == ["beta.txt", "gamma/gamma.txt"]

    def test_dir_nested_artifact(self, tmp_path: Path) -> None:
        (tmp_path / "gamma").mkdir()
        (tmp_path / "gamma" / "gamma.txt").write_text("C", encoding="utf-8")
        guard = ArtifactGuard(tmp_path)
        assert guard.missing(["gamma/gamma.txt"]) == []


class TestArtifactGuardEscape:
    def test_parent_traversal_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "leak.txt"
        outside.write_text("secret", encoding="utf-8")
        guard = ArtifactGuard(tmp_path)
        # ../leak.txt 在根外不可达：判 missing（不做根外读取）
        assert "../leak.txt" in guard.missing(["../leak.txt"])
        assert not guard.is_within(outside)

    def test_absolute_outside_root_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "abs.txt"
        outside.write_text("x", encoding="utf-8")
        guard = ArtifactGuard(tmp_path)
        assert not guard.is_within(outside)
        # 绝对外部路径不属于项目根产物集
        assert guard.missing([str(outside)]) == [str(outside)]

    def test_within_root_accepted(self, tmp_path: Path) -> None:
        guard = ArtifactGuard(tmp_path)
        assert guard.is_within(tmp_path / "ok.txt")


class TestArtifactGuardSemantics:
    def test_empty_expected_never_missing(self, tmp_path: Path) -> None:
        guard = ArtifactGuard(tmp_path)
        assert guard.missing([]) == []

    def test_correct_prompt_names_missing_and_uses_write(self) -> None:
        prompt = correction_prompt(["beta.txt", "gamma/gamma.txt"])
        assert "beta.txt" in prompt and "gamma/gamma.txt" in prompt
        assert "write" in prompt
