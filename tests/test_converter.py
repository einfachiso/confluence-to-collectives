"""Tests for Converter — the critical test file for HTML edge cases."""

import json
from pathlib import Path

import pytest
from migrate import Converter, resolve_page_links


@pytest.fixture
def converter():
    return Converter()


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


class TestPreprocessPanels:
    def test_info_panel(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-information">
            <span class="aui-icon confluence-information-macro-icon"></span>
            <div class="confluence-information-macro-body"><p>Info text</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        assert "<blockquote>" in result
        assert "Info text" in result
        # No duplicate type label is prepended (the body often self-labels)
        assert "Info:" not in result

    def test_warning_panel(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-warning">
            <div class="confluence-information-macro-body"><p>Danger!</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        assert "<blockquote>" in result
        assert "Danger!" in result
        assert "Warning:" not in result

    def test_note_panel(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-note">
            <div class="confluence-information-macro-body"><p>Remember this.</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        assert "<blockquote>" in result
        assert "Remember this." in result

    def test_tip_panel(self, converter):
        html = '''<div class="confluence-information-macro confluence-information-macro-tip">
            <div class="confluence-information-macro-body"><p>Pro tip!</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        assert "<blockquote>" in result
        assert "Pro tip!" in result


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
        """Info panels have data-macro-name but should be handled as panels, not generic macros."""
        html = '''<div class="confluence-information-macro confluence-information-macro-information" data-macro-name="info">
            <div class="confluence-information-macro-body"><p>Info</p></div>
        </div>'''
        result = converter.preprocess_html(html)
        # Should be a blockquote, not an unsupported macro comment
        assert "<blockquote>" in result
        assert "Unsupported macro" not in result


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


class TestTemplateConversion:
    def test_at_var_becomes_placeholder(self, converter):
        out = converter._substitute_template_variables(
            'x <at:var at:name="Owner"/> y')
        assert out == "x {{Owner}} y"

    def test_at_var_paired_form(self, converter):
        out = converter._substitute_template_variables(
            'x <at:var at:name="Owner"></at:var> y')
        assert out == "x {{Owner}} y"

    def test_at_var_name_with_spaces(self, converter):
        out = converter._substitute_template_variables(
            'Owner: <at:var at:name="Risk Owner" />')
        assert out == "Owner: {{Risk Owner}}"

    def test_declarations_stripped(self, converter):
        out = converter._substitute_template_variables(
            '<at:declarations><at:string at:name="Owner"/></at:declarations><p>Hi</p>')
        assert "<at:declarations>" not in out
        assert "<at:string" not in out
        assert "<p>Hi</p>" in out

    def test_convert_template_renders_variable_and_macro(self, converter, sample_template):
        md = converter.convert_template(sample_template)
        assert "{{Risk Owner}}" in md   # template variable rendered
        assert "<at:" not in md          # no leftover template tags
        assert "<ac:" not in md          # macro consumed by preprocess_html
        # Unexpanded storage macro surfaced as a visible marker (not silently
        # dropped). Its body ("Fill this in.") is still lost — known limitation.
        assert "[Unsupported macro: info]" in md
        assert "Fill this in." not in md

    def test_convert_template_no_attachment_or_comment_section(self, converter, sample_template):
        md = converter.convert_template(sample_template)
        assert "## Attachments" not in md
        assert "## Comments" not in md

    def test_convert_template_empty_body(self, converter):
        assert converter.convert_template({}) == ""
        assert converter.convert_template({"body": {}}) == ""

    def test_details_macro_table_extracted(self, converter):
        """The details (page-properties) macro's inner table is preserved (default)."""
        det = ('<ac:structured-macro ac:name="details"><ac:rich-text-body>'
               '<table><tr><th>Verantwortlicher</th><td>Max</td></tr></table>'
               '</ac:rich-text-body></ac:structured-macro>')
        md = converter.convert_template({"body": {"storage": {"value": det}}})
        assert "Verantwortlicher" in md and "Max" in md
        assert "Unsupported macro: details" not in md
        assert "<ac:" not in md

    def test_details_macro_excluded_when_flagged(self):
        """exclude_details drops the macro and its table entirely."""
        c = Converter(exclude_details=True)
        det = ('<ac:structured-macro ac:name="details"><ac:rich-text-body>'
               '<table><tr><th>Verantwortlicher</th><td>Max</td></tr></table>'
               '</ac:rich-text-body></ac:structured-macro>')
        md = c.convert_template({"body": {"storage": {"value": det}}})
        assert "Verantwortlicher" not in md
        assert "Unsupported macro: details" not in md

    def test_details_with_nested_macro(self, converter):
        """A details box nesting another macro (e.g. status) must not error when
        the parent is processed (regression: decomposing invalidated the nested)."""
        det = ('<ac:structured-macro ac:name="details"><ac:rich-text-body><table>'
               '<tr><th>Status</th><td><ac:structured-macro ac:name="status">'
               '<ac:parameter ac:name="title">offen</ac:parameter>'
               '</ac:structured-macro></td></tr></table></ac:rich-text-body>'
               '</ac:structured-macro>')
        # extract (default) keeps the table; exclude drops it — neither errors
        assert "Status" in converter.convert_template({"body": {"storage": {"value": det}}})
        Converter(exclude_details=True).convert_template({"body": {"storage": {"value": det}}})
