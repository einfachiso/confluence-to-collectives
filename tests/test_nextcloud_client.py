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
    def test_dav_base_defaults_to_hidden_collectives(self):
        nc = NextcloudClient(
            "https://nc.example.com/",  # trailing slash
            "alice",
            "pass",
            "Team Notes",
        )
        # Modern Collectives (4.x) stores pages under the hidden ".Collectives".
        assert nc.dav_base == "https://nc.example.com/remote.php/dav/files/alice/.Collectives/Team Notes"

    def test_verify_connection_falls_back_to_legacy_folder(self, nc):
        """If the OCS API has no match and '.Collectives' 404s, use 'Collectives'."""
        def fake_request(method, url, **kwargs):
            resp = MagicMock()
            resp.status_code = 207 if "/.Collectives/" not in url else 404
            return resp

        no_ocs = MagicMock(status_code=404)  # API unavailable -> probe folders
        with patch.object(nc.session, "get", return_value=no_ocs), \
                patch.object(nc.session, "request", side_effect=fake_request):
            nc.verify_connection()
        assert nc.collectives_dir == "Collectives"
        assert nc.dav_base.endswith("/Collectives/MyCollective")

    def test_verify_connection_prefers_hidden_folder(self, nc):
        no_ocs = MagicMock(status_code=404)
        ok = MagicMock(status_code=207)
        with patch.object(nc.session, "get", return_value=no_ocs), \
                patch.object(nc.session, "request", return_value=ok):
            nc.verify_connection()
        assert nc.collectives_dir == ".Collectives"


class TestResolveCollective:
    def _client(self, configured):
        return NextcloudClient("https://nc.example.com", "user", "pw", configured)

    def _ocs(self, collectives, pages):
        lst = MagicMock(status_code=200)
        lst.json.return_value = {"ocs": {"data": {"collectives": collectives}}}
        pg = MagicMock(status_code=200)
        pg.json.return_value = {"ocs": {"data": {"pages": pages}}}
        return lambda url, **kw: pg if "/pages" in url else lst

    def test_resolves_localised_folder_name_and_segment(self):
        nc = self._client("MyTeam-7")  # configured as <slug>-<id>
        get = self._ocs(
            [{"id": 7, "name": "My Team", "slug": "MyTeam"}],
            [{"collectivePath": ".Kollektive/My Team"}],
        )
        with patch.object(nc.session, "get", side_effect=get):
            assert nc.resolve_collective() is True
        assert nc.collective == "My Team"           # WebDAV folder = display name
        assert nc.collective_segment == "MyTeam-7"   # link URL segment
        assert nc.collectives_dir == ".Kollektive"   # localised storage folder
        assert nc.dav_base.endswith("/.Kollektive/My Team")

    def test_matches_by_display_name(self):
        nc = self._client("My Team")
        get = self._ocs([{"id": 7, "name": "My Team", "slug": "MyTeam"}],
                        [{"collectivePath": ".Collectives/My Team"}])
        with patch.object(nc.session, "get", side_effect=get):
            assert nc.resolve_collective() is True
        assert nc.collective_segment == "MyTeam-7"

    def test_no_match_returns_false(self):
        nc = self._client("Nope")
        lst = MagicMock(status_code=200)
        lst.json.return_value = {"ocs": {"data": {"collectives": [
            {"id": 1, "name": "Other", "slug": "Other"}]}}}
        with patch.object(nc.session, "get", return_value=lst):
            assert nc.resolve_collective() is False


class TestEnsureTemplatesFolder:
    def test_creates_and_seeds_index(self, nc):
        def req(method, url, **kw):
            r = MagicMock()
            r.status_code = 201 if method == "MKCOL" else 404  # PROPFIND → not found
            return r

        put_resp = MagicMock(status_code=201)
        with patch.object(nc.session, "request", side_effect=req) as mock_req, \
             patch.object(nc.session, "put", return_value=put_resp) as mock_put:
            nc.ensure_templates_folder()
            mkcol = [c for c in mock_req.call_args_list if c[0][0] == "MKCOL"]
            assert len(mkcol) == 1
            assert mkcol[0][0][1].endswith("/.templates")
            assert mock_put.call_count == 1
            assert mock_put.call_args[0][0].endswith("/.templates/Readme.md")
            assert b"template files for the collective" in mock_put.call_args.kwargs["data"]

    def test_skips_index_when_present(self, nc):
        def req(method, url, **kw):
            r = MagicMock()
            r.status_code = 201 if method == "MKCOL" else 207  # PROPFIND → exists
            return r

        with patch.object(nc.session, "request", side_effect=req), \
             patch.object(nc.session, "put") as mock_put:
            nc.ensure_templates_folder()
            mock_put.assert_not_called()


class TestUploadTemplate:
    def test_puts_to_templates_path(self, nc):
        put_resp = MagicMock(status_code=201)
        with patch.object(nc.session, "put", return_value=put_resp) as mock_put:
            remote = nc.upload_template("My Template", "# Body")
            assert remote == ".templates/My Template.md"
            assert mock_put.call_args[0][0].endswith("/.templates/My Template.md")
            assert mock_put.call_args.kwargs["data"] == b"# Body"

    def test_raises_on_bad_status(self, nc):
        put_resp = MagicMock(status_code=500)
        with patch.object(nc.session, "put", return_value=put_resp):
            with pytest.raises(click.ClickException, match="Upload failed"):
                nc.upload_template("X", "y")
