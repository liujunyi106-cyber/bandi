from __future__ import annotations

from bandi.company.output_contract import (
    RuntimeArtifact,
    artifact_content_hash,
    build_runtime_artifact_manifest,
    changed_artifacts,
    render_runtime_artifact_messages,
)


class TestArtifactContentHash:
    def test_hash_is_deterministic(self) -> None:
        assert artifact_content_hash("hello") == artifact_content_hash("hello")

    def test_hash_differs_by_content(self) -> None:
        assert artifact_content_hash("hello") != artifact_content_hash("world")

    def test_hash_differs_by_metadata(self) -> None:
        assert artifact_content_hash("hello", {"turn": 1}) != artifact_content_hash("hello")

    def test_hash_chinese_content(self) -> None:
        assert artifact_content_hash("可用工具：搜索") == artifact_content_hash("可用工具：搜索")
        assert artifact_content_hash("可用工具：搜索") != artifact_content_hash("可用工具：读取")

    def test_hash_empty_content(self) -> None:
        assert artifact_content_hash("") == artifact_content_hash(None)

    def test_hash_metadata_key_order_insensitive(self) -> None:
        assert artifact_content_hash("x", {"a": 1, "b": 2}) == artifact_content_hash(
            "x", {"b": 2, "a": 1}
        )

    def test_hash_hex_sha256(self) -> None:
        assert len(artifact_content_hash("hello")) == 64


class TestBuildRuntimeArtifactManifest:
    def _tools(self) -> list[RuntimeArtifact]:
        return [
            RuntimeArtifact(
                artifact_type="tool_surface",
                title="Tools",
                content="search, read",
                scope="session-1",
                metadata={"turn": 1},
            ),
            RuntimeArtifact(
                artifact_type="skills",
                title="Skills",
                content="pandas",
                scope="session-1",
            ),
        ]

    def test_manifest_and_hashes(self) -> None:
        manifest, hashes = build_runtime_artifact_manifest(self._tools())
        assert len(manifest) == 2
        assert set(hashes) == {"tool_surface", "skills"}
        assert hashes["tool_surface"] != hashes["skills"]

    def test_record_fields(self) -> None:
        manifest, hashes = build_runtime_artifact_manifest([self._tools()[0]])
        record = manifest[0]
        assert record["type"] == "tool_surface"
        assert record["title"] == "Tools"
        assert record["content"] == "search, read"
        assert record["scope"] == "session-1"
        assert "content_hash" in record
        assert record["content_hash"] == hashes["tool_surface"]
        assert record["metadata"]["turn"] == 1

    def test_hashes_embed_in_metadata(self) -> None:
        manifest, hashes = build_runtime_artifact_manifest([self._tools()[0]])
        assert manifest[0]["content_hash"] == hashes["tool_surface"]

    def test_manifest_empty(self) -> None:
        manifest, hashes = build_runtime_artifact_manifest([])
        assert manifest == []
        assert hashes == {}

    def test_same_content_same_hash(self) -> None:
        m1, h1 = build_runtime_artifact_manifest(self._tools())
        m2, h2 = build_runtime_artifact_manifest(self._tools())
        assert h1 == h2
        assert m1 == m2


class TestChangedArtifacts:
    def _artifacts(self) -> list[RuntimeArtifact]:
        return [
            RuntimeArtifact(artifact_type="tool_surface", title="Tools", content="search"),
            RuntimeArtifact(artifact_type="skills", title="Skills", content="pandas"),
        ]

    def test_no_previous_all_marked_delta(self) -> None:
        changed = changed_artifacts(self._artifacts(), None)
        assert [(a.artifact_type, is_delta) for a, is_delta in changed] == [
            ("tool_surface", False),
            ("skills", False),
        ]

    def test_same_hash_no_delta(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        changed = changed_artifacts(self._artifacts(), hashes)
        assert all(not is_delta for _, is_delta in changed)

    def test_changed_content_is_delta(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        changed_artifact = [
            RuntimeArtifact(artifact_type="tool_surface", title="Tools", content="search, write")
        ]
        changed = changed_artifacts(changed_artifact, hashes)
        assert changed[0][0].artifact_type == "tool_surface"
        assert changed[0][1]

    def test_new_artifact_type_not_delta(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        added = [RuntimeArtifact(artifact_type="team_memory", title="Mem", content="note")]
        changed = changed_artifacts(added, hashes)
        assert [a.artifact_type for a, _ in changed] == ["team_memory"]
        assert not changed[0][1]

    def test_removed_type_not_in_changed(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        subset = [RuntimeArtifact(artifact_type="skills", title="Skills", content="pandas")]
        changed = changed_artifacts(subset, hashes)
        assert [a.artifact_type for a, _ in changed] == ["skills"]
        assert not changed[0][1]

    def test_uses_embedded_metadata_hash(self) -> None:
        previous = {"tool_surface": "abc123"}
        artifacts = [
            RuntimeArtifact(
                artifact_type="tool_surface",
                title="Tools",
                content="search, write",
                metadata={"content_hash": "abc123"},
            )
        ]
        changed = changed_artifacts(artifacts, previous)
        assert not changed[0][1]

    def test_embedded_metadata_hash_delta(self) -> None:
        previous = {"tool_surface": "abc123"}
        artifacts = [
            RuntimeArtifact(
                artifact_type="tool_surface",
                title="Tools",
                content="search, write",
                metadata={"content_hash": "def456"},
            )
        ]
        changed = changed_artifacts(artifacts, previous)
        assert changed[0][1]


class TestRenderRuntimeArtifactMessages:
    def _artifacts(self) -> list[RuntimeArtifact]:
        return [
            RuntimeArtifact(artifact_type="tool_surface", title="可用工具", content="search"),
            RuntimeArtifact(artifact_type="skills", title="技能", content="pandas"),
        ]

    def test_initial_render_full_header(self) -> None:
        messages = render_runtime_artifact_messages(self._artifacts(), previous_hashes=None)
        assert len(messages) == 2
        assert all(m["role"] == "system" for m in messages)
        assert messages[0]["content"].startswith("## Runtime Artifact:")
        assert "search" in messages[0]["content"]
        assert "可用工具" in messages[0]["content"]

    def test_unchanged_render_still_emits(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        messages = render_runtime_artifact_messages(self._artifacts(), previous_hashes=hashes)
        assert len(messages) == 2

    def test_changed_content_uses_delta_header(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        changed = [
            RuntimeArtifact(artifact_type="tool_surface", title="可用工具", content="search, write")
        ]
        messages = render_runtime_artifact_messages(changed, previous_hashes=hashes)
        assert messages[0]["content"].startswith("## Runtime Artifact Delta:")
        assert "search, write" in messages[0]["content"]

    def test_emit_delta_false_uses_full_header(self) -> None:
        _, hashes = build_runtime_artifact_manifest(self._artifacts())
        changed = [
            RuntimeArtifact(artifact_type="tool_surface", title="可用工具", content="search, write")
        ]
        messages = render_runtime_artifact_messages(
            changed, previous_hashes=hashes, emit_delta_messages=False
        )
        assert messages[0]["content"].startswith("## Runtime Artifact:")

    def test_empty_artifacts(self) -> None:
        assert render_runtime_artifact_messages([]) == []
