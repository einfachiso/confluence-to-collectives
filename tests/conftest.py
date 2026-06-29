"""Shared fixtures for migration tests."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_page_html():
    return (FIXTURES_DIR / "sample_page.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_tables_html():
    return (FIXTURES_DIR / "sample_page_tables.html").read_text(encoding="utf-8")


@pytest.fixture
def sample_comments():
    return json.loads((FIXTURES_DIR / "sample_comments.json").read_text(encoding="utf-8"))


@pytest.fixture
def tmp_state(tmp_path):
    """Return a MigrationState using a temp directory."""
    from migrate import MigrationState

    state = MigrationState(path=tmp_path / ".migration-state.json")
    return state


@pytest.fixture
def sample_page_data(sample_page_html, sample_comments):
    """A fully formed page data dict as stored in export JSON."""
    return {
        "page_id": "12345",
        "title": "Sample Page",
        "space_key": "TEAM",
        "parent_id": None,
        "has_children": False,
        "body": sample_page_html,
        "comments": sample_comments,
        "attachments": [
            {"title": "screenshot.png", "size": 102400, "mediaType": "image/png"},
            {"title": "document.pdf", "size": 512000, "mediaType": "application/pdf"},
        ],
    }


@pytest.fixture
def sample_template():
    """A Confluence v1 space page-template (storage body with a template variable,
    an <at:declarations> block, and an unexpanded macro)."""
    return {
        "templateId": "987654321",
        "name": "Risk Assessment",
        "templateType": "page",
        "editorVersion": "v2",
        "labels": [],
        "space": {"key": "TEAM"},
        "body": {
            "storage": {
                "value": (
                    "<at:declarations>"
                    '<at:string at:name="Risk Owner" />'
                    "</at:declarations>"
                    "<h2>Risk Assessment</h2>"
                    '<p>Owner: <at:var at:name="Risk Owner" /></p>'
                    '<ac:structured-macro ac:name="info">'
                    "<ac:rich-text-body><p>Fill this in.</p></ac:rich-text-body>"
                    "</ac:structured-macro>"
                ),
                "representation": "storage",
            }
        },
    }


@pytest.fixture
def mock_confluence_response():
    """Factory for mock Confluence API responses."""

    def _make(results, next_link=None):
        data = {"results": results}
        if next_link:
            data["_links"] = {"next": next_link}
        else:
            data["_links"] = {}
        return data

    return _make


@pytest.fixture
def mock_session():
    """A mocked requests.Session."""
    session = MagicMock()
    return session
