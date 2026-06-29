"""Tests for CLI commands."""

import os
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from migrate import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestHelp:
    def test_main_help(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "export" in result.output
        assert "convert" in result.output
        assert "upload" in result.output
        assert "migrate" in result.output
        assert "status" in result.output

    def test_export_help(self, runner):
        result = runner.invoke(cli, ["export", "--help"])
        assert result.exit_code == 0
        assert "--space" in result.output
        assert "--pages" in result.output
        assert "--dry-run" in result.output
        assert "--debug" in result.output

    def test_convert_help(self, runner):
        result = runner.invoke(cli, ["convert", "--help"])
        assert result.exit_code == 0
        assert "--exclude-images" in result.output
        assert "--exclude-attachments" in result.output

    def test_upload_help(self, runner):
        result = runner.invoke(cli, ["upload", "--help"])
        assert result.exit_code == 0
        assert "--target-parent" in result.output

    def test_migrate_help(self, runner):
        result = runner.invoke(cli, ["migrate", "--help"])
        assert result.exit_code == 0
        assert "--space" in result.output
        assert "--target-parent" in result.output
        assert "--include-templates" in result.output

    def test_import_templates_help(self, runner):
        result = runner.invoke(cli, ["import-templates", "--help"])
        assert result.exit_code == 0
        assert "page-templates" in result.output
        assert "--space" in result.output

    def test_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestMissingConfig:
    def test_export_missing_env(self, runner):
        env = {
            "CONFLUENCE_BASE_URL": "",
            "CONFLUENCE_USERNAME": "",
            "CONFLUENCE_API_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = runner.invoke(cli, ["export", "--space", "TEST"])
            assert result.exit_code != 0
            assert "Missing required" in result.output or "Error" in result.output

    def test_upload_missing_env(self, runner):
        env = {
            "NEXTCLOUD_URL": "",
            "NEXTCLOUD_USERNAME": "",
            "NEXTCLOUD_PASSWORD": "",
            "NEXTCLOUD_COLLECTIVE": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = runner.invoke(cli, ["upload"])
            assert result.exit_code != 0


class TestScopeValidation:
    def test_export_no_scope_fails(self, runner):
        env = {
            "CONFLUENCE_BASE_URL": "https://test.atlassian.net",
            "CONFLUENCE_USERNAME": "user",
            "CONFLUENCE_API_TOKEN": "token",
        }
        with patch.dict(os.environ, env, clear=False):
            # Mock auth verification
            with patch("migrate.ConfluenceClient.verify_auth"):
                result = runner.invoke(cli, ["export"])
                assert result.exit_code != 0
                assert "Specify --space" in result.output


class TestDryRun:
    def test_convert_dry_run_no_data(self, runner, tmp_path):
        """Convert with dry-run and no exported data should exit cleanly."""
        with patch("migrate.STATE_FILE", str(tmp_path / ".migration-state.json")):
            result = runner.invoke(cli, ["convert", "--dry-run"])
            assert "No exported pages" in result.output

    def test_status_no_data(self, runner, tmp_path):
        """Status with no state file should show helpful message."""
        with patch("migrate.STATE_FILE", str(tmp_path / ".migration-state.json")):
            result = runner.invoke(cli, ["status"])
            assert "No migration state" in result.output


class TestImportTemplatesCLI:
    def test_space_required(self, runner):
        result = runner.invoke(cli, ["import-templates"])
        assert result.exit_code != 0

    def test_dry_run_lists_without_nextcloud(self, runner):
        """--dry-run needs only Confluence creds and never touches Nextcloud."""
        env = {
            "CONFLUENCE_BASE_URL": "https://test.atlassian.net",
            "CONFLUENCE_USERNAME": "user",
            "CONFLUENCE_API_TOKEN": "token",
        }
        templates = [{"templateId": "1", "name": "Risk Assessment", "templateType": "page"}]
        with patch.dict(os.environ, env, clear=False), \
             patch("migrate.ConfluenceClient.verify_auth"), \
             patch("migrate.ConfluenceClient.get_space_templates", return_value=templates), \
             patch("migrate.NextcloudClient") as mock_nc:
            result = runner.invoke(cli, ["import-templates", "--space", "TEAM", "--dry-run"])
            assert result.exit_code == 0
            assert "Risk Assessment" in result.output
            mock_nc.assert_not_called()
