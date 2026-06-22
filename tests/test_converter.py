"""Tests for Converter — the critical test file for HTML edge cases."""

import json
from pathlib import Path

import pytest
from migrate import Converter, resolve_page_links, weber_config_from_env


@pytest.fixture
def converter():
    return Converter()


TOC_URL = "https://nc.example/index.php/apps/wcextend/smartpicker/wcextend-toc"


@pytest.fixture
def converter_weber():
    return Converter(
        mermaid_map={"CAPA.drawio.png": "flowchart TD\n  A-->B"},
        toc_url=TOC_URL,
        dokinfo_patterns=["Dokumentenlenkung-Header"],
    )


@pytest.fixture
def converter_no_images():
    return Converter(exclude_images=True)


@pytest.fixture
def converter_no_attachments():
    return Converter(exclude_attachments=True)


# -- Preprocessing tests ---------------------------------------------------


class TestPreprocessTableHeaders:
    def test_h3_in_th_becomes_strong(self, converter):
        html = '<table><tr><th><h3>Header</h3></th></tr></table>'
        result = converter.preprocess_html(html)
        assert "<h3>" not in result
        assert "<strong>" in result
        assert "Header" in result

    def test_h2_in_td_becomes_strong(self, converter):
        html = '<table><tr><td><h2>Cell Title</h2></td></tr></table>'
        result = converter.preprocess_html(html)
        assert "<h2>" not in result
        assert "<strong>" in result

    def test_multiple_headings_in_table(self, converter):
        html = '''<table>
            <tr><th><h1>A</h1></th><th><h6>B</h6></th></tr>
            <tr><td>1</td><td>2</td></tr>
        </table>'''
        result = converter.preprocess_html(html)
        assert "<h1>" not in result
        assert "<h6>" not in result
        assert result.count("<strong>") == 2

    def test_complex_table_fixture(self, converter, sample_tables_html):
        result = converter.preprocess_html(sample_tables_html)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(result, "html.parser")
        # Headings inside table cells should be replaced with <strong>
        for cell in soup.find_all(["th", "td"]):
            assert cell.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is None
        assert "<strong>" in result


class TestStripConfiguredContent:
    def test_removes_table_with_pattern(self):
        c = Converter(strip_patterns=["CONFIDENTIAL"])
        html = ('<p>Keep me</p>'
                '<div class="table-wrap"><table><tr><td>'
                '<p>This is CONFIDENTIAL boilerplate</p></td></tr></table></div>'
                '<p>Keep me too</p>')
        result = c.preprocess_html(html)
        assert "CONFIDENTIAL" not in result
        assert "<table" not in result
        assert "Keep me" in result and "Keep me too" in result

    def test_removes_non_table_block(self):
        c = Converter(strip_patterns=["REMOVE"])
        html = '<p>Stay</p><div><p>REMOVE this block</p></div><p>Stay2</p>'
        result = c.preprocess_html(html)
        assert "REMOVE" not in result
        assert "Stay" in result and "Stay2" in result

    def test_no_patterns_keeps_everything(self):
        c = Converter()
        html = '<table><tr><td>Copyright box</td></tr></table>'
        assert "Copyright box" in c.preprocess_html(html)


class TestPreprocessAttachmentContainer:
    def test_remove_plugin_attachments_container(self, converter):
        html = '<p>Content</p><div class="plugin_attachments_container"><h2>Attachments</h2></div>'
        result = converter.preprocess_html(html)
        assert "plugin_attachments_container" not in result
        assert "Content" in result

    def test_sample_page_removes_container(self, converter, sample_page_html):
        result = converter.preprocess_html(sample_page_html)
        assert "plugin_attachments_container" not in result


class TestPreprocessImageUrls:
    def test_rewrite_attachment_image_url(self, converter):
        html = '<img src="/download/attachments/12345/logo.png?version=1" alt="Logo" />'
        result = converter.preprocess_html(html)
        assert 'src="logo.png"' in result

    def test_rewrite_url_with_path_segments(self, converter):
        html = '<img src="/wiki/rest/api/content/123/child/attachment/456/download/photo.jpg" />'
        result = converter.preprocess_html(html)
        assert 'src="photo.jpg"' in result

    def test_exclude_images_removes_tags(self, converter_no_images):
        html = '<p>Text</p><img src="/download/attachments/1/img.png" /><p>More</p>'
        result = converter_no_images.preprocess_html(html)
        assert "<img" not in result
        assert "Text" in result
        assert "More" in result

    def test_data_uri_images_unchanged(self, converter):
        html = '<img src="data:image/png;base64,abc123" />'
        result = converter.preprocess_html(html)
        assert "data:image/png;base64" in result


def _md(converter, html):
    """Convert a body of HTML to final markdown (preprocess → html2text →
    sentinel-token restore) the way convert_page does."""
    return converter.convert_page({"body": html, "attachments": [], "comments": []})


class TestPreprocessPanels:
    """Confluence info panels → Nextcloud Text callout blocks (`::: <type> … :::`)."""

    def test_info_panel(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-information">
            <span class="aui-icon confluence-information-macro-icon"></span>
            <div class="confluence-information-macro-body"><p>Info text</p></div>
        </div>'''
        md = _md(converter, html)
        assert "::: info\n\nInfo text\n\n:::" in md
        assert "<blockquote>" not in md
        assert "WCEXTEND" not in md

    def test_warning_panel_maps_to_error(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-warning">
            <div class="confluence-information-macro-body"><p>Danger!</p></div>
        </div>'''
        md = _md(converter, html)
        assert "::: error\n\nDanger!\n\n:::" in md

    def test_note_panel_maps_to_warn(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-note">
            <div class="confluence-information-macro-body"><p>Remember this.</p></div>
        </div>'''
        md = _md(converter, html)
        assert "::: warn\n\nRemember this.\n\n:::" in md

    def test_tip_panel_maps_to_success(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-tip">
            <div class="confluence-information-macro-body"><p>Pro tip!</p></div>
        </div>'''
        md = _md(converter, html)
        assert "::: success\n\nPro tip!\n\n:::" in md

    def test_panel_body_with_list_preserved(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-information">
            <div class="confluence-information-macro-body"><p>Items:</p>
            <ul><li>one</li><li>two</li></ul></div>
        </div>'''
        md = _md(converter, html)
        assert md.startswith("::: info")
        assert "one" in md and "two" in md
        assert md.rstrip().endswith(":::")


class TestPreprocessCodeBlocks:
    def test_code_block_with_language(self, converter):
        html = '<div class="code-block" data-language="java"><pre>System.out.println("hi");</pre></div>'
        result = converter.preprocess_html(html)
        assert 'class="language-java"' in result
        assert "System.out.println" in result

    def test_code_block_without_language(self, converter):
        html = '<div class="code-block"><pre>some code</pre></div>'
        result = converter.preprocess_html(html)
        assert "<pre>" in result
        assert "some code" in result


class TestPreprocessStatusMacros:
    def test_status_macro_becomes_code(self, converter):
        html = '<p>Status: <span class="status-macro aui-lozenge aui-lozenge-current">IN ARBEIT</span></p>'
        result = converter.preprocess_html(html)
        assert "<code>IN ARBEIT</code>" in result

    def test_status_macro_inline_code_markdown(self, converter):
        html = '<p>Status: <span class="status-macro aui-lozenge aui-lozenge-error">ABGELEHNT</span></p>'
        md = converter.html_to_markdown(converter.preprocess_html(html))
        assert "`ABGELEHNT`" in md

    def test_empty_status_macro_removed(self, converter):
        html = '<p>x<span class="status-macro aui-lozenge"></span>y</p>'
        result = converter.preprocess_html(html)
        assert "status-macro" not in result
        assert "<code>" not in result


class TestPreprocessMacros:
    def test_unsupported_structured_macro(self, converter):
        html = '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PROJ-1</ac:parameter></ac:structured-macro>'
        result = converter.preprocess_html(html)
        assert "Unsupported macro: jira" in result

    def test_data_macro_name_div(self, converter):
        html = '<div data-macro-name="drawio"><p>diagram</p></div>'
        result = converter.preprocess_html(html)
        assert "Unsupported macro: drawio" in result

    def test_info_panel_not_double_processed_as_macro(self, converter):
        """Info panels have data-macro-name but should be handled as panels (callouts), not generic macros."""
        html = '''<div class="confluence-information-macro confluence-information-macro-information" data-macro-name="info">
            <div class="confluence-information-macro-body"><p>Info</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        # Tokenized as a callout, not turned into an unsupported-macro comment
        assert "Unsupported macro" not in result
        assert "::: info" in converter._restore_md_tokens(result)


class TestPreprocessUserMentions:
    def test_user_mention_replaced(self, converter):
        html = '<a class="confluence-userlink" href="/wiki/people/abc">Jane Doe</a>'
        result = converter.preprocess_html(html)
        assert "@Jane Doe" in result
        assert "<a" not in result


# -- Full conversion tests -------------------------------------------------


class TestHtmlToMarkdown:
    def test_basic_html(self, converter):
        md = converter.html_to_markdown("<h1>Title</h1><p>Paragraph</p>")
        assert "# Title" in md
        assert "Paragraph" in md

    def test_body_width_zero(self, converter):
        """Lines should not be wrapped."""
        long_text = "A " * 200
        md = converter.html_to_markdown(f"<p>{long_text}</p>")
        # Should be a single long line, not wrapped
        lines = [l for l in md.split("\n") if l.strip()]
        assert len(lines) == 1


class TestPreserveDiagramImages:
    """draw.io / Gliffy etc. render a PNG preview inside the macro in
    export_view; that image must survive instead of being dropped."""

    def test_drawio_image_preserved(self, converter):
        html = (
            '<div data-macro-name="drawio">'
            '<img src="/download/attachments/12345/architecture.png?version=2" />'
            '</div>'
        )
        result = converter.preprocess_html(html)
        assert 'src="architecture.png"' in result
        assert "Unsupported macro" not in result

    def test_structured_macro_image_preserved(self, converter):
        html = (
            '<ac:structured-macro ac:name="drawio">'
            '<img src="/download/attachments/1/flow.png" />'
            '</ac:structured-macro>'
        )
        result = converter.preprocess_html(html)
        assert 'src="flow.png"' in result
        assert "Unsupported macro" not in result

    def test_macro_without_image_still_becomes_comment(self, converter):
        html = '<div data-macro-name="drawio"><p>diagram</p></div>'
        result = converter.preprocess_html(html)
        assert "Unsupported macro: drawio" in result


class TestRewriteInternalLinks:
    LINK_MAP = {
        "67890": "Section/Other Page.md",
        "111": "Readme.md",
    }

    def test_link_to_migrated_page_becomes_placeholder(self, converter):
        converter.set_link_map(self.LINK_MAP)
        html = '<p>See <a href="/wiki/spaces/TEAM/pages/67890/Other+Page">Other Page</a></p>'
        result = converter.preprocess_html(html, current_path="Readme.md")
        assert 'href="cpage:67890"' in result

    def test_anchor_preserved_in_placeholder(self, converter):
        converter.set_link_map(self.LINK_MAP)
        html = '<a href="/wiki/spaces/TEAM/pages/67890/Other+Page#Heading">x</a>'
        result = converter.preprocess_html(html, current_path="Readme.md")
        assert 'href="cpage:67890#Heading"' in result

    def test_link_to_unmigrated_page_untouched(self, converter):
        converter.set_link_map(self.LINK_MAP)
        html = '<a href="/wiki/spaces/TEAM/pages/99999/Gone">x</a>'
        result = converter.preprocess_html(html, current_path="Readme.md")
        assert "/wiki/spaces/TEAM/pages/99999/Gone" in result
        assert "cpage:" not in result

    def test_external_link_untouched(self, converter):
        converter.set_link_map(self.LINK_MAP)
        html = '<a href="https://example.com/page">x</a>'
        result = converter.preprocess_html(html, current_path="Readme.md")
        assert 'href="https://example.com/page"' in result

    def test_no_rewrite_without_link_map(self, converter):
        html = '<a href="/wiki/spaces/TEAM/pages/67890/Other+Page">x</a>'
        result = converter.preprocess_html(html, current_path="Readme.md")
        assert "/wiki/spaces/TEAM/pages/67890" in result
        assert "cpage:" not in result

    def test_convert_page_emits_placeholder_via_current_page_id(self, converter):
        converter.set_link_map({"1": "Readme.md", "67890": "Section/Other Page.md"})
        page_data = {
            "body": '<a href="/wiki/spaces/TEAM/pages/67890/Other+Page">Other</a>',
            "comments": [],
            "attachments": [],
        }
        md = converter.convert_page(page_data, current_page_id="1")
        assert "cpage:67890" in md


class TestResolvePageLinks:
    def test_route_segments(self):
        from migrate import page_route_segments
        assert page_route_segments("Drafts/Proc/CAPA.md") == ["Drafts", "Proc", "CAPA"]
        assert page_route_segments("Drafts/Proc/Readme.md") == ["Drafts", "Proc"]
        assert page_route_segments("Readme.md") == []

    def test_build_routes_with_and_without_top_folder(self, tmp_path):
        from migrate import MigrationState, build_page_routes
        state = MigrationState(path=tmp_path / ".migration-state.json")
        state.set_page("1", {"page_id": "1", "space_key": "TEAM", "status": "converted",
                             "convert_path": str(Path("convert_data/TEAM/Section/Other Page.md"))})
        state.set_page("2", {"page_id": "2", "space_key": "TEAM", "status": "converted",
                             "convert_path": str(Path("convert_data/TEAM/Readme.md"))})
        with_top = build_page_routes(state, "Top")
        assert with_top["1"] == ["Top", "Section", "Other Page"]
        assert with_top["2"] == ["Top"]            # homepage sits under the wrapper
        base = build_page_routes(state, "")
        assert base["1"] == ["Section", "Other Page"]
        assert base["2"] == []                      # homepage IS the collective root

    def test_relative_route_link(self):
        from migrate import _relative_route_link
        # leaf -> sibling leaf
        assert _relative_route_link(["A", "B", "D"], ["A", "B", "C"]) == "D"
        # folder/parent page (route A/B) -> its child D
        assert _relative_route_link(["A", "B", "D"], ["A", "B"]) == "B/D"
        # any page -> the collective root (homepage)
        assert _relative_route_link([], ["A", "B"]) == ".."
        # segments are URL-encoded
        assert _relative_route_link(["Other Page"], ["Sub", "Leaf"]) == "../Other%20Page"

    def test_resolve_relative_from_normal_page(self):
        routes = {"1": ["Sec", "Other"], "2": ["Sec", "Cur"]}
        md = "see [X](<cpage:1>) and [Y](<cpage:1#H>) and [Z](<cpage:99>)"
        out = resolve_page_links(md, ["Sec", "Cur"], routes, "Coll-9")
        assert "[X](<Other>)" in out              # relative, collective-agnostic
        assert "Other#H" in out                   # anchor preserved
        assert "cpage:99" in out                  # unmigrated target left as-is

    def test_resolve_absolute_from_root_homepage(self):
        # The collective root page (source_route == []) can't be relative.
        out = resolve_page_links("[X](<cpage:1>)", [], {"1": ["Vorgabe", "Glossar"]}, "Coll-9")
        assert "/apps/collectives/Coll-9/Vorgabe/Glossar" in out


class TestConvertPage:
    def test_full_page_conversion(self, converter, sample_page_data):
        md = converter.convert_page(sample_page_data)
        # Should contain markdown content
        assert "Sample Page Title" in md
        # Should contain comments section
        assert "## Comments" in md
        assert "Alice Smith" in md
        # Should contain attachment section (PDF, not images)
        assert "## Attachments" in md
        assert "document.pdf" in md

    def test_page_without_comments(self, converter):
        page_data = {
            "body": "<p>Simple page</p>",
            "comments": [],
            "attachments": [],
        }
        md = converter.convert_page(page_data)
        assert "Simple page" in md
        assert "## Comments" not in md

    def test_page_without_attachments(self, converter):
        page_data = {
            "body": "<p>Content</p>",
            "comments": [],
            "attachments": [],
        }
        md = converter.convert_page(page_data)
        assert "## Attachments" not in md

    def test_exclude_attachments(self, converter_no_attachments):
        page_data = {
            "body": "<p>Content</p>",
            "comments": [],
            "attachments": [
                {"title": "doc.pdf", "mediaType": "application/pdf"},
            ],
        }
        md = converter_no_attachments.convert_page(page_data)
        assert "## Attachments" not in md


# -- Comments formatting ---------------------------------------------------


class TestFormatComments:
    def test_format_with_display_name(self, converter, sample_comments):
        result = converter.format_comments(sample_comments)
        assert "## Comments" in result
        assert "### Alice Smith" in result
        assert "### Bob Jones" in result
        assert "Add more examples" in result

    def test_empty_comments(self, converter):
        assert converter.format_comments([]) == ""

    def test_comment_date_formatting(self, converter, sample_comments):
        result = converter.format_comments(sample_comments)
        assert "2024-01-15 10:30:00" in result


# -- Attachment section ---------------------------------------------------


class TestGenerateAttachmentSection:
    def test_only_non_image_files(self, converter):
        attachments = [
            {"title": "logo.png"},
            {"title": "report.pdf"},
            {"title": "data.xlsx"},
            {"title": "photo.jpg"},
        ]
        result = converter.generate_attachment_section(attachments)
        assert "report.pdf" in result
        assert "data.xlsx" in result
        assert "logo.png" not in result
        assert "photo.jpg" not in result

    def test_all_images_returns_empty(self, converter):
        attachments = [
            {"title": "a.png"},
            {"title": "b.jpg"},
        ]
        assert converter.generate_attachment_section(attachments) == ""

    def test_link_format(self, converter):
        attachments = [{"title": "file.pdf"}]
        result = converter.generate_attachment_section(attachments)
        assert "- [file.pdf](file.pdf)" in result


# -- Filename sanitization ------------------------------------------------


class TestSanitizeFilename:
    def test_strip_unsafe_chars(self, converter):
        assert converter.sanitize_filename('file/name:with*bad?"chars') == "filenamewithbadchars"

    def test_cap_200_chars(self, converter):
        name = "a" * 300
        assert len(converter.sanitize_filename(name)) == 200

    def test_empty_becomes_untitled(self, converter):
        assert converter.sanitize_filename("***") == "untitled"

    def test_dedupe_with_suffix(self, converter):
        existing = {"report", "report-2"}
        result = converter.sanitize_filename("report", existing)
        assert result == "report-3"

    def test_no_collision(self, converter):
        existing = {"other"}
        result = converter.sanitize_filename("report", existing)
        assert result == "report"

    def test_whitespace_stripped(self, converter):
        assert converter.sanitize_filename("  name  ") == "name"

    def test_pipe_and_angle_brackets(self, converter):
        assert converter.sanitize_filename("a|b<c>d") == "abcd"


# -- Output tree building -------------------------------------------------


class TestBuildOutputTree:
    def _make_state(self, pages_data, tmp_path):
        """Helper to create a MigrationState with test pages."""
        from migrate import MigrationState

        state = MigrationState(path=tmp_path / ".migration-state.json")
        for p in pages_data:
            state.set_page(p["page_id"], p)
        return state

    def test_single_page_is_readme(self, converter, tmp_path):
        state = self._make_state(
            [
                {
                    "page_id": "1",
                    "title": "Home",
                    "space_key": "SP",
                    "parent_id": None,
                    "has_children": False,
                    "status": "exported",
                }
            ],
            tmp_path,
        )
        tree = converter.build_output_tree(state)
        assert tree["1"]["path"] == "Readme.md"

    def test_parent_child_structure(self, converter, tmp_path):
        state = self._make_state(
            [
                {
                    "page_id": "1",
                    "title": "Home",
                    "space_key": "SP",
                    "parent_id": None,
                    "has_children": True,
                    "status": "exported",
                },
                {
                    "page_id": "2",
                    "title": "Child Page",
                    "space_key": "SP",
                    "parent_id": "1",
                    "has_children": False,
                    "status": "exported",
                },
            ],
            tmp_path,
        )
        tree = converter.build_output_tree(state)
        assert tree["1"]["path"] == "Readme.md"
        assert tree["2"]["path"] == "Child Page.md"

    def test_nested_hierarchy(self, converter, tmp_path):
        state = self._make_state(
            [
                {
                    "page_id": "1",
                    "title": "Root",
                    "space_key": "SP",
                    "parent_id": None,
                    "has_children": True,
                    "status": "exported",
                },
                {
                    "page_id": "2",
                    "title": "Section",
                    "space_key": "SP",
                    "parent_id": "1",
                    "has_children": True,
                    "status": "exported",
                },
                {
                    "page_id": "3",
                    "title": "Leaf",
                    "space_key": "SP",
                    "parent_id": "2",
                    "has_children": False,
                    "status": "exported",
                },
            ],
            tmp_path,
        )
        tree = converter.build_output_tree(state)
        assert tree["1"]["path"] == "Readme.md"
        assert tree["2"]["path"] == "Section/Readme.md"
        assert tree["3"]["path"] == "Section/Leaf.md"

    def test_name_collision_deduped(self, converter, tmp_path):
        state = self._make_state(
            [
                {
                    "page_id": "1",
                    "title": "Root",
                    "space_key": "SP",
                    "parent_id": None,
                    "has_children": True,
                    "status": "exported",
                },
                {
                    "page_id": "2",
                    "title": "Report",
                    "space_key": "SP",
                    "parent_id": "1",
                    "has_children": False,
                    "status": "exported",
                },
                {
                    "page_id": "3",
                    "title": "Report",
                    "space_key": "SP",
                    "parent_id": "1",
                    "has_children": False,
                    "status": "exported",
                },
            ],
            tmp_path,
        )
        tree = converter.build_output_tree(state)
        paths = [tree["2"]["path"], tree["3"]["path"]]
        # One should be Report.md and the other Report-2.md
        assert "Report.md" in paths
        assert "Report-2.md" in paths

    def test_empty_state(self, converter, tmp_path):
        from migrate import MigrationState

        state = MigrationState(path=tmp_path / ".migration-state.json")
        tree = converter.build_output_tree(state)
        assert tree == {}


# -- Integration: full HTML to final MD ------------------------------------


class TestFullConversion:
    def test_sample_page_produces_clean_markdown(self, converter, sample_page_data):
        md = converter.convert_page(sample_page_data)

        # No raw HTML should remain (except HTML comments for unsupported macros)
        import re

        html_tags = re.findall(r"<(?!!)(?!/!)[a-zA-Z][^>]*>", md)
        assert html_tags == [], f"Raw HTML tags found in output: {html_tags}"

        # Key content preserved
        assert "screenshot.png" in md  # image reference
        assert "## Comments" in md
        assert "## Attachments" in md
        assert "document.pdf" in md

    def test_tables_in_complex_fixture(self, converter, sample_tables_html):
        page_data = {
            "body": sample_tables_html,
            "comments": [],
            "attachments": [],
        }
        md = converter.convert_page(page_data)
        # Table content should be present
        assert "Authentication" in md
        assert "API Gateway" in md
        # No block-level headings should remain in table
        assert "<h2>" not in md


# -- Weber-specific transforms --------------------------------------------


class TestDrawioMermaid:
    """draw.io <img> → ```mermaid block when a mapping exists; else PNG fallback."""

    def test_mapped_image_becomes_mermaid(self, converter_weber):
        html = '<p><img src="/wiki/download/attachments/123/CAPA.drawio.png?api=v2"/></p>'
        md = _md(converter_weber, html)
        assert "```mermaid\nflowchart TD\n  A-->B\n```" in md
        assert "CAPA.drawio.png" not in md  # the image reference is gone

    def test_unmapped_drawio_falls_back_to_png(self, converter_weber, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            html = '<p><img src="/wiki/download/attachments/9/Other.drawio.png"/></p>'
            md = _md(converter_weber, html)
        assert "![](Other.drawio.png)" in md
        assert "```mermaid" not in md
        assert any("No mermaid mapping" in r.message for r in caplog.records)

    def test_no_mermaid_map_is_noop(self, converter):
        html = '<p><img src="/wiki/download/attachments/123/CAPA.drawio.png"/></p>'
        md = _md(converter, html)
        assert "![](CAPA.drawio.png)" in md
        assert "```mermaid" not in md

    def test_page_scoped_key_disambiguates(self):
        # Same filename on two pages → page id in the src picks the right diagram.
        c = Converter(mermaid_map={
            "100/Untitled Diagram.drawio.png": "flowchart TD\n  A-->B",
            "200/Untitled Diagram.drawio.png": "flowchart TD\n  C-->D",
        })
        md100 = _md(c, '<p><img src="/wiki/download/attachments/100/Untitled Diagram.drawio.png"/></p>')
        md200 = _md(c, '<p><img src="/wiki/download/attachments/200/Untitled Diagram.drawio.png"/></p>')
        assert "A-->B" in md100 and "C-->D" not in md100
        assert "C-->D" in md200 and "A-->B" not in md200


class TestDrawioAttachmentOmission:
    DRAWIO_ATTS = [
        {"title": "CAPA.drawio", "mediaType": "application/vnd.jgraph.mxfile"},
        {"title": "Dokumentenlenkung", "mediaType": "application/vnd.jgraph.mxfile"},
        {"title": "~CAPA.drawio.tmp", "mediaType": "application/vnd.jgraph.mxfile"},
        {"title": "CAPA.drawio.png", "mediaType": "image/png"},
        {"title": "report.pdf", "mediaType": "application/pdf"},
    ]

    def test_mapped_diagram_omits_sources_and_preview(self, converter_weber):
        # Diagram replaced by mermaid → its preview + all sources are omitted.
        converter_weber._drawio_replaced = {"CAPA.drawio.png"}
        omit = converter_weber._drawio_omit_names(self.DRAWIO_ATTS)
        assert omit == {"CAPA.drawio", "Dokumentenlenkung", "~CAPA.drawio.tmp", "CAPA.drawio.png"}
        section = converter_weber.generate_attachment_section(self.DRAWIO_ATTS, omit=omit)
        assert "report.pdf" in section
        assert "CAPA.drawio" not in section and "Dokumentenlenkung" not in section

    def test_non_embedded_preview_is_omitted(self, converter_weber):
        # Preview that is only an attachment (never embedded) → omitted as junk.
        converter_weber._drawio_replaced = set()
        converter_weber._drawio_fallback = set()
        omit = converter_weber._drawio_omit_names(self.DRAWIO_ATTS)
        assert "CAPA.drawio.png" in omit
        assert {"CAPA.drawio", "Dokumentenlenkung", "~CAPA.drawio.tmp"} <= omit

    def test_inline_fallback_preview_is_kept(self, converter_weber):
        # Embedded diagram with no mermaid mapping → kept inline, NOT omitted.
        converter_weber._drawio_fallback = {"CAPA.drawio.png"}
        omit = converter_weber._drawio_omit_names(self.DRAWIO_ATTS)
        assert "CAPA.drawio.png" not in omit
        assert {"CAPA.drawio", "Dokumentenlenkung", "~CAPA.drawio.tmp"} <= omit

    def test_replaced_preview_with_mismatched_name_omitted(self, converter_weber):
        # Rendered preview whose name differs from the source mxfile — only
        # _drawio_replaced (the embedded filename) identifies it.
        atts = [
            {"title": "Prozess Informationssicherheitsereignis und -vorfall",
             "mediaType": "application/vnd.jgraph.mxfile"},
            {"title": "Vorfallmanagement und CAPA.png", "mediaType": "image/png"},
            {"title": "echtes-bild.png", "mediaType": "image/png"},
        ]
        converter_weber._drawio_replaced = {"Vorfallmanagement und CAPA.png"}
        omit = converter_weber._drawio_omit_names(atts)
        assert "Vorfallmanagement und CAPA.png" in omit
        assert "Prozess Informationssicherheitsereignis und -vorfall" in omit
        assert "echtes-bild.png" not in omit  # genuine screenshot kept

    def test_extensionless_source_png_twin_omitted(self, converter_weber):
        atts = [
            {"title": "Dok", "mediaType": "application/vnd.jgraph.mxfile"},
            {"title": "Dok.png", "mediaType": "image/png"},
            {"title": "real-photo.png", "mediaType": "image/png"},
        ]
        omit = converter_weber._drawio_omit_names(atts)
        assert "Dok" in omit and "Dok.png" in omit
        assert "real-photo.png" not in omit  # genuine image untouched

    def test_copy_attachments_skips_omitted(self, converter_weber, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        for a in self.DRAWIO_ATTS:
            (src / a["title"]).write_text("x", encoding="utf-8")
        converter_weber._drawio_replaced = {"CAPA.drawio.png"}
        copied = converter_weber.copy_attachments(
            {"attachments": self.DRAWIO_ATTS}, src, dest)
        assert copied == ["report.pdf"]
        assert (dest / "report.pdf").exists()
        assert not (dest / "CAPA.drawio").exists()


class TestStaticToc:
    TOC_HTML = (
        "<style type='text/css'>div.rbtoc123 {padding: 0px;}</style>"
        "<div class='toc-macro rbtoc123'><ul class='toc-indentation'>"
        "<li><a href='#x'>Section One</a></li></ul></div>"
    )

    def test_toc_becomes_link_preview(self, converter_weber):
        md = _md(converter_weber, self.TOC_HTML)
        assert f"[{TOC_URL}]({TOC_URL} (preview))" in md
        assert "Section One" not in md
        assert "WCEXTEND" not in md

    def test_toc_style_block_removed(self, converter_weber):
        result = converter_weber.preprocess_html(self.TOC_HTML)
        assert "rbtoc123" not in result
        assert "<style" not in result

    def test_no_toc_url_degrades(self, converter):
        result = converter.preprocess_html(self.TOC_HTML)
        assert "toc-macro" in result  # untouched without config


class TestDokinfoRemove:
    DOKINFO_HTML = (
        "<div class='table-wrap'><table><tr><td>"
        "<p>Dokumentenlenkung-Header content here</p></td></tr></table></div>"
        "<p>Body stays</p>"
    )

    def test_dokinfo_removed(self, converter_weber):
        md = _md(converter_weber, self.DOKINFO_HTML)
        # Header table is dropped, not replaced with anything
        assert "Dokumentenlenkung-Header" not in md
        assert "wcextend" not in md
        assert "Body stays" in md

    def test_no_config_leaves_table(self, converter):
        result = converter.preprocess_html(self.DOKINFO_HTML)
        assert "Dokumentenlenkung-Header" in result
        assert "<table" in result


class TestTokenRestore:
    def test_token_is_alphanumeric(self, converter):
        tok = converter._new_token()
        assert tok.isalnum()
        assert tok.startswith("WCEXTEND")

    def test_combined_transforms_all_restored(self, converter_weber):
        html = (
            "<div class='table-wrap'><table><tr><td>"
            "<p>Dokumentenlenkung-Header</p></td></tr></table></div>"
            "<div class='confluence-information-macro confluence-information-macro-information'>"
            "<div class='confluence-information-macro-body'><p>Hint</p></div></div>"
            "<p><img src='/wiki/download/attachments/1/CAPA.drawio.png'/></p>"
        )
        md = _md(converter_weber, html)
        assert "WCEXTEND" not in md
        assert "Dokumentenlenkung-Header" not in md  # header removed, not replaced
        assert "::: info\n\nHint\n\n:::" in md
        assert "```mermaid" in md


class TestWeberConfigFromEnv:
    def test_reads_env(self, monkeypatch, tmp_path):
        mmap = tmp_path / "m.json"
        mmap.write_text('{"X.drawio.png": "flowchart TD\\n A-->B"}', encoding="utf-8")
        monkeypatch.setenv("WCEXTEND_TOC_URL", "http://toc")
        monkeypatch.setenv("WCEXTEND_DOKINFO_PATTERNS", "A|||B")
        monkeypatch.setenv("WCEXTEND_MERMAID_MAP", str(mmap))
        cfg = weber_config_from_env()
        assert cfg["toc_url"] == "http://toc"
        assert cfg["dokinfo_patterns"] == ["A", "B"]
        assert cfg["mermaid_map"]["X.drawio.png"].startswith("flowchart")

    def test_absent_env_disables(self, monkeypatch, tmp_path):
        for k in ("WCEXTEND_TOC_URL", "WCEXTEND_DOKINFO_PATTERNS"):
            monkeypatch.delenv(k, raising=False)
        # Point the map at a non-existent path so the result is cwd-independent.
        monkeypatch.setenv("WCEXTEND_MERMAID_MAP", str(tmp_path / "absent.json"))
        cfg = weber_config_from_env()
        assert cfg["toc_url"] is None
        assert cfg["dokinfo_patterns"] == [] and cfg["mermaid_map"] == {}

    def test_malformed_map_is_empty(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("WCEXTEND_MERMAID_MAP", str(bad))
        assert weber_config_from_env()["mermaid_map"] == {}


class TestMoveConfig:
    """Output-tree restructuring (dissolve folders / move pages / strip prefix)."""

    PAGES = [
        {"page_id": "1", "title": "Home", "space_key": "SP", "parent_id": None},
        {"page_id": "2", "title": "Drafts", "space_key": "SP", "parent_id": "1"},
        {"page_id": "3", "title": "Draft Prozesse", "space_key": "SP", "parent_id": "2"},
        {"page_id": "4", "title": "[DRAFT] Prozess CAPA", "space_key": "SP", "parent_id": "3"},
        {"page_id": "5", "title": "[DRAFT] Leitlinie", "space_key": "SP", "parent_id": "2"},
        {"page_id": "6", "title": "Vorgabedokumente", "space_key": "SP", "parent_id": "1"},
        {"page_id": "7", "title": "Prozesse", "space_key": "SP", "parent_id": "6"},
    ]
    MOVES = {
        "strip_title_prefix": "[DRAFT] ",
        "dissolve_folders": [
            {"folder": ["Drafts", "Draft Prozesse"], "into": ["Vorgabedokumente", "Prozesse"]}
        ],
        "move_pages": [
            {"page": ["Drafts", "[DRAFT] Leitlinie"], "into": ["Vorgabedokumente"]}
        ],
        "remove_folders": [["Drafts"]],
    }

    def _state(self, tmp_path):
        from migrate import MigrationState
        s = MigrationState(path=tmp_path / ".migration-state.json")
        for p in self.PAGES:
            s.set_page(p["page_id"], p)
        return s

    def test_restructure(self, tmp_path):
        from migrate import Converter
        tree = Converter(move_config=self.MOVES).build_output_tree(self._state(tmp_path))
        # relocated + prefix stripped
        assert tree["4"]["path"] == "Vorgabedokumente/Prozesse/Prozess CAPA.md"
        # the former leaf "Prozesse" is now a folder page
        assert tree["7"]["path"] == "Vorgabedokumente/Prozesse/Readme.md"
        # loose page moved directly under Vorgabedokumente, prefix stripped
        assert tree["5"]["path"] == "Vorgabedokumente/Leitlinie.md"
        # dropped folders are gone
        assert "2" not in tree and "3" not in tree
        # no [DRAFT] anywhere
        assert not any("[DRAFT]" in v["path"] for v in tree.values())

    def test_noop_without_config(self, tmp_path):
        from migrate import Converter
        tree = Converter().build_output_tree(self._state(tmp_path))
        assert any("Drafts" in v["path"] for v in tree.values())  # tree unchanged
        assert tree["4"]["path"].endswith("[DRAFT] Prozess CAPA.md")
