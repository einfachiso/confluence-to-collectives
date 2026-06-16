"""Tests for ConfluenceClient."""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import click
import pytest
import requests

from migrate import ConfluenceClient


@pytest.fixture
def client():
    return ConfluenceClient("https://test.atlassian.net", "user@test.com", "test-token")


class TestConfluenceAuth:
    def test_verify_auth_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "type": "known",
            "displayName": "Test User",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp):
            result = client.verify_auth()
            assert result["displayName"] == "Test User"

    def test_verify_auth_anonymous_raises(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"type": "anonymous"}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp):
            with pytest.raises(click.ClickException, match="anonymous"):
                client.verify_auth()


class TestPagination:
    def test_paginate_single_page(self, client, mock_confluence_response):
        responses = [mock_confluence_response([{"id": "1"}, {"id": "2"}])]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = responses
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp):
            results = list(client._paginate("/wiki/api/v2/test"))
            assert len(results) == 2

    def test_paginate_multiple_pages(self, client):
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = {
            "results": [{"id": "1"}],
            "_links": {"next": "/wiki/api/v2/test?cursor=abc"},
        }
        page1_resp.raise_for_status = MagicMock()

        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = {
            "results": [{"id": "2"}],
            "_links": {},
        }
        page2_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", side_effect=[page1_resp, page2_resp]):
            results = list(client._paginate("/wiki/api/v2/test"))
            assert len(results) == 2
            assert results[0]["id"] == "1"
            assert results[1]["id"] == "2"


class TestRateLimiting:
    def test_retry_on_429(self, client):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "0"}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"ok": True}
        success.raise_for_status = MagicMock()

        with patch.object(client.session, "request", side_effect=[rate_limited, success]):
            result = client._get_json("/wiki/api/v2/test")
            assert result == {"ok": True}

    def test_retry_on_500(self, client):
        error = MagicMock()
        error.status_code = 500
        error.headers = {}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"ok": True}
        success.raise_for_status = MagicMock()

        with patch.object(client.session, "request", side_effect=[error, success]):
            with patch("time.sleep"):  # Skip actual sleep
                result = client._get_json("/wiki/api/v2/test")
                assert result == {"ok": True}


class TestDownloadURL:
    def test_uses_rest_child_attachment_route(self, client):
        """Primary download must use the REST content/child/attachment route,
        which honours API-token Basic auth (the legacy /wiki/download servlet
        rejects API tokens with HTTP 401)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"file-data"
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp) as mock_req:
            data = client.download_attachment("123", "att999", fallback_url="/download/attachments/123/file.png")
            assert data == b"file-data"
            call_url = mock_req.call_args[0][1]
            assert call_url.endswith("/wiki/rest/api/content/123/child/attachment/att999/download")

    def test_falls_back_to_download_link_on_error(self, client):
        import requests

        ok = MagicMock(status_code=200, content=b"img", raise_for_status=MagicMock())

        def side_effect(method, url, **kwargs):
            if "/child/attachment/" in url:
                raise requests.HTTPError("404")
            return ok

        with patch.object(client.session, "request", side_effect=side_effect) as mock_req:
            data = client.download_attachment("123", "att999", fallback_url="/download/attachments/123/file.png")
            assert data == b"img"
            # fallback prepends /wiki to the servlet link
            assert mock_req.call_args[0][1].startswith("https://test.atlassian.net/wiki/download/")


class TestSpaceResolution:
    def test_get_space_by_key(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"id": "100", "key": "TEAM", "name": "Team Space"}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp):
            space = client.get_space_by_key("TEAM")
            assert space["key"] == "TEAM"

    def test_space_not_found_raises(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client.session, "request", return_value=mock_resp):
            with pytest.raises(click.ClickException, match="not found"):
                client.get_space_by_key("NOPE")
