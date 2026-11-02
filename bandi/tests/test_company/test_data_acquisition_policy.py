from __future__ import annotations

from types import SimpleNamespace

from bandi.company.data_acquisition_policy import (
    ACQUISITION_SHELL_PREFIXES,
    is_acquisition_shell_command,
    is_acquisition_specialist_projection,
    is_download_command,
    is_media_acquisition,
    is_projection_scoped_acquisition,
    is_projection_scoped_acquisition_shell_command,
    load_download_manifest_entries,
    split_shell_command_segments,
    task_projection_id,
    task_role_id,
    workspace_root_for_task,
)


class TestDataAcquisitionPolicy:
    def test_acquisition_prefixes_and_command_detection(self):
        assert "curl" in ACQUISITION_SHELL_PREFIXES
        assert "wget" in ACQUISITION_SHELL_PREFIXES
        assert is_acquisition_shell_command("curl http://example.com")
        assert not is_acquisition_shell_command("ls -la")

    def test_acquisition_prefix_whitespace_and_token_edges(self):
        assert is_acquisition_shell_command("  curl   http://example.com  ")
        assert is_acquisition_shell_command("wget")
        assert not is_acquisition_shell_command("   ")
        assert not is_acquisition_shell_command("")
        assert not is_acquisition_shell_command("git clone http://example.com")

    def test_is_download_command(self):
        assert is_download_command("curl http://example.com/file.zip")
        assert not is_download_command("curl")
        assert not is_download_command("ls -la")

    def test_is_media_acquisition(self):
        assert is_media_acquisition("curl http://example.com/video.mp4")
        assert not is_media_acquisition("curl http://example.com/file.zip")

    def test_is_projection_scoped_acquisition(self):
        assert is_projection_scoped_acquisition("curl http://example.com", "data_acquisition")
        assert not is_projection_scoped_acquisition("curl http://example.com", "other")
        assert not is_projection_scoped_acquisition("ls -la", "data_acquisition")


def _work_item(**kwargs):
    metadata = kwargs.pop("metadata", {})
    return SimpleNamespace(
        id=kwargs.pop("id", "wi-1"),
        title=kwargs.pop("title", "Acquire assets"),
        description=kwargs.pop("description", "Download media"),
        assignee_role=kwargs.pop("assignee_role", "acquisition_specialist"),
        metadata=metadata,
    )


def _acquisition_item(**kwargs):
    return _work_item(
        metadata={
            "work_item_projection_id": "data_acquisition",
            "workspace_manifest": {"root_path": "/workspace/wi-1"},
            **kwargs.pop("metadata", {}),
        },
        **kwargs,
    )


class TestTaskProjectionHelpers:
    def test_task_projection_id_from_metadata_and_none(self):
        wi = _work_item(metadata={"work_item_projection_id": "data_acquisition"})
        assert task_projection_id(wi) == "data_acquisition"
        assert task_projection_id(None) == ""

    def test_task_role_id_assignee_and_metadata_fallback(self):
        wi = _work_item(assignee_role="acquisition_specialist")
        assert task_role_id(wi) == "acquisition_specialist"
        fallback = _work_item(
            assignee_role="", metadata={"work_item_role_id": "acquisition_specialist"}
        )
        assert task_role_id(fallback) == "acquisition_specialist"

    def test_task_role_id_none(self):
        assert task_role_id(None) == ""


class TestIsAcquisitionSpecialistProjection:
    def test_matches_projection_and_role_from_task_and_explicit_args(self):
        assert is_acquisition_specialist_projection(
            task=_acquisition_item(), projection_id="", role_id=""
        )
        assert is_acquisition_specialist_projection(
            projection_id="data_acquisition", role_id="acquisition_specialist"
        )

    def test_rejects_wrong_projection_or_role(self):
        assert not is_acquisition_specialist_projection(
            projection_id="other", role_id="acquisition_specialist"
        )
        assert not is_acquisition_specialist_projection(
            projection_id="data_acquisition", role_id="worker"
        )

    def test_no_task_no_args(self):
        assert not is_acquisition_specialist_projection()


class TestWorkspaceRootForTask:
    def test_resolves_from_manifest_or_target_output_dir(self):
        wi = _work_item(metadata={"workspace_manifest": {"root_path": "/workspace/wi-1"}})
        root = workspace_root_for_task(wi)
        assert root is not None
        assert str(root).replace("\\", "/").endswith("/workspace/wi-1")
        fallback = _work_item(metadata={"target_output_dir": "/out/wi-1"})
        root = workspace_root_for_task(fallback)
        assert root is not None
        assert str(root).replace("\\", "/").endswith("/out/wi-1")

    def test_none_when_missing(self):
        assert workspace_root_for_task(_work_item()) is None

    def test_none_task(self):
        assert workspace_root_for_task(None) is None


class TestLoadDownloadManifestEntries:
    def test_empty_or_invalid_json_returns_empty_entries(self, tmp_path):
        assert load_download_manifest_entries("") == []
        p = tmp_path / "manifest.json"
        p.write_text("not json", encoding="utf-8")
        assert load_download_manifest_entries(str(p)) == []

    def test_loads_flat_and_nested_entries(self, tmp_path):
        p = tmp_path / "manifest.json"
        p.write_text(
            '[{"status": "downloaded", "local_path": "/w/a.mp4", "media_kind": "video"}]',
            encoding="utf-8",
        )
        entries = load_download_manifest_entries(str(p))
        assert len(entries) == 1
        assert entries[0]["media_kind"] == "video"
        nested = tmp_path / "nested.json"
        nested.write_text(
            '{"entries": [{"status": "pending", "local_path": "/w/b.mp4"}]}', encoding="utf-8"
        )
        entries = load_download_manifest_entries(str(nested))
        assert len(entries) == 1
        assert entries[0]["status"] == "pending"


class TestSplitShellCommandSegments:
    def test_single_and_ampersand_segments(self):
        assert split_shell_command_segments("curl http://x.com") == [["curl", "http://x.com"]]
        segments = split_shell_command_segments("curl http://a.com && curl http://b.com")
        assert segments == [["curl", "http://a.com"], ["curl", "http://b.com"]]

    def test_empty_returns_empty(self):
        assert split_shell_command_segments("") == []

    def test_semicolon_separated(self):
        segments = split_shell_command_segments("echo hi ; echo bye")
        assert len(segments) == 2

    def test_pipe_control(self):
        segments = split_shell_command_segments("curl http://x.com | grep video")
        assert len(segments) == 2


class TestIsProjectionScopedAcquisitionShellCommand:
    def test_allows_in_workspace_downloads(self):
        wi = _acquisition_item()
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -O http://example.com/video.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="curl --output-dir work/out -O http://example.com/video.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )

    def test_rejects_role_escape_multi_segment_and_non_http(self):
        worker = _work_item(
            assignee_role="worker",
            metadata={"work_item_projection_id": "data_acquisition"},
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4",
            task=worker,
            target_output_dir="/workspace/wi-1",
        )
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o /etc/evil.sh http://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl http://a.com && curl http://b.com",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 ftp://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )

    def test_rejects_wget_and_ytdlp_capital_flags_outside_workspace(self):
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="wget -O /etc/evil.sh http://example.com/x.zip",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="yt-dlp -P /tmp/evil -o /etc/evil.mp4 http://example.com/x",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )

    def test_rejects_curl_output_dir_outside_workspace(self):
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl --output-dir /tmp/evil -O http://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl --output-dir /tmp/evil -o f.mp4 http://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )


class TestExtractOutputTargetsP1:
    def test_curl_remote_name_downloads_in_workspace(self):
        wi = _acquisition_item()
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -O http://example.com/video.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -O http://example.com/a.mp4 http://example.com/b.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )

    def test_non_output_flags_and_ffmpeg_allowed(self):
        wi = _acquisition_item()
        assert is_projection_scoped_acquisition_shell_command(
            command="wget -o /var/log/wget.log http://example.com/x.zip",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="wget -p http://example.com/page.html",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="yt-dlp -O '%(title)s' http://example.com/video",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="ffmpeg -i in.mp4 out1.mp4 out2.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )

    def test_explicit_output_flags_outside_blocked(self):
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o /etc/evil.sh http://example.com/video.mp4",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="wget -O /etc/evil.sh http://example.com/x.zip",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )

    def test_type_prefix_and_ffmpeg_outside_blocked(self):
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="yt-dlp -P video:/tmp/evil -o '%(title)s.mp4' http://example.com/video",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="ffmpeg -i in.mp4 /etc/evil.mp4 out.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )


class TestP2Batch1DA:
    def test_quoted_http_urls_detected(self):
        assert is_download_command('curl "https://example.com/a.mp4"')
        assert is_download_command("curl 'https://example.com/a.mp4'")

    def test_ytdlp_paths_windows_drive_not_truncated(self):
        from bandi.company.data_acquisition_policy import _extract_output_targets

        targets = _extract_output_targets(
            "yt-dlp",
            ["yt-dlp", "-P", "D:\\downloads", "-o", "out.mp4", "http://example.com/x"],
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert any("D:" in str(t) for t in targets)

    def test_ytdlp_paths_type_prefix_escape_still_detected(self):
        wi = _acquisition_item()
        assert not is_projection_scoped_acquisition_shell_command(
            command="yt-dlp -P video:/tmp/evil -o out.mp4 http://example.com/video",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )

    def test_ffmpeg_input_and_outputs_scoped(self):
        wi = _acquisition_item()
        assert is_projection_scoped_acquisition_shell_command(
            command="ffmpeg -i /outside/in.mp4 out.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="ffmpeg -i in.mp4 /etc/evil.mp4 out.mp4",
            task=wi,
            working_directory="/workspace/wi-1",
            target_output_dir="/workspace/wi-1",
        )

    def test_fd_redirect_not_split_but_plain_redirect_splits(self):
        segments = split_shell_command_segments(
            "curl -o a.mp4 http://example.com/video.mp4 2> err.log"
        )
        assert len(segments) == 1
        plain = split_shell_command_segments("curl http://example.com/x > out.txt")
        assert len(plain) == 2

    def test_fd_redirect_scope_allows_dup_rejects_escape(self):
        wi = _acquisition_item()
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4 2> err.log",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4 2> /etc/evil.log",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert not is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4 &> /etc/evil.log",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
        assert is_projection_scoped_acquisition_shell_command(
            command="curl -o a.mp4 http://example.com/video.mp4 2>&1",
            task=wi,
            target_output_dir="/workspace/wi-1",
        )
