"""Tests for NextcloudClient."""

from unittest.mock import MagicMock, patch, call

import click
import pytest

from migrate import NextcloudClient


@pytest.fixture
def nc():
    return NextcloudClient(
        "https://cloud.example.com",
        "testuser",
        "testpass",
        "MyCollective",
    )


class TestVerifyConnection:
    def test_success(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 207
        with patch.object(nc.session, "request", return_value=mock_resp):
            nc.verify_connection()  # Should not raise

    def test_auth_failure(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch.object(nc.session, "request", return_value=mock_resp):
            with pytest.raises(click.ClickException, match="authentication failed"):
                nc.verify_connection()

    def test_not_found(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(nc.session, "request", return_value=mock_resp):
            with pytest.raises(click.ClickException, match="not found"):
                nc.verify_connection()


class TestMkdirP:
    def test_creates_nested_dirs(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 201

        with patch.object(nc.session, "request", return_value=mock_resp) as mock_req:
            nc.mkdir_p("a/b/c")
            # Should make 3 MKCOL calls
            assert mock_req.call_count == 3
            urls = [c[0][1] for c in mock_req.call_args_list]
            assert urls[0].endswith("/a")
            assert urls[1].endswith("/a/b")
            assert urls[2].endswith("/a/b/c")

    def test_backslash_path_normalised(self, nc):
        """Windows path separators must become '/' in WebDAV URLs (HTTP 400 bug)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        with patch.object(nc.session, "request", return_value=mock_resp) as mock_req:
            nc.mkdir_p("a\\b\\c")
            urls = [c[0][1] for c in mock_req.call_args_list]
            assert mock_req.call_count == 3
            assert "\\" not in urls[-1]
            assert urls[-1].endswith("/a/b/c")

    def test_ignores_405_already_exists(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 405  # Already exists

        with patch.object(nc.session, "request", return_value=mock_resp):
            nc.mkdir_p("existing/dir")  # Should not raise


class TestUploadFile:
    def test_successful_upload(self, nc, tmp_path):
        test_file = tmp_path / "test.md"
        test_file.write_text("# Hello")

        mock_resp = MagicMock()
        mock_resp.status_code = 201

        with patch.object(nc.session, "put", return_value=mock_resp):
            nc.upload_file(str(test_file), "MigratedPages/test.md")

    def test_upload_failure_raises(self, nc, tmp_path):
        test_file = tmp_path / "test.md"
        test_file.write_text("content")

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(nc.session, "put", return_value=mock_resp):
            with pytest.raises(click.ClickException, match="Upload failed"):
                nc.upload_file(str(test_file), "path/test.md")


class TestExists:
    def test_exists_true(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 207
        with patch.object(nc.session, "request", return_value=mock_resp):
            assert nc.exists("some/path") is True

    def test_exists_false(self, nc):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch.object(nc.session, "request", return_value=mock_resp):
            assert nc.exists("missing/path") is False


class TestDavBasePath:
    def test_dav_base_construction(self):
        nc = NextcloudClient(
            "https://nc.example.com/",  # trailing slash
            "alice",
            "pass",
            "Team Notes",
        )
        assert nc.dav_base == "https://nc.example.com/remote.php/dav/files/alice/Collectives/Team Notes"
