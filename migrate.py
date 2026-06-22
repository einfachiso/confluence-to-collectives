#!/usr/bin/env python3
"""Confluence Cloud → Nextcloud Collectives Migration CLI."""

__version__ = "0.6.0"

import json
import logging
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, unquote, quote

import click
import html2text
import requests
from bs4 import BeautifulSoup, Comment, Tag
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
REDACT_PATTERNS = []


class RedactFilter(logging.Filter):
    """Redact sensitive values from log records."""

    def filter(self, record):
        msg = record.getMessage()
        for pattern in REDACT_PATTERNS:
            if pattern:
                msg = msg.replace(pattern, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


def setup_logging(debug=False, log_file=None):
    """Configure console + optional file logging."""
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # Collect secrets to redact
    REDACT_PATTERNS.clear()
    for key in ("CONFLUENCE_API_TOKEN", "NEXTCLOUD_PASSWORD"):
        val = os.getenv(key)
        if val:
            REDACT_PATTERNS.append(val)

    redact = RedactFilter()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console.addFilter(redact)
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        fh.addFilter(redact)
        logger.addHandler(fh)

    return logger


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FAILURE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4

# ---------------------------------------------------------------------------
# MigrationState
# ---------------------------------------------------------------------------

STATE_FILE = ".migration-state.json"


class MigrationState:
    """Persistent per-page migration state backed by JSON file."""

    def __init__(self, path=None):
        self.path = Path(path if path is not None else STATE_FILE)
        self.pages = {}

    def load(self):
        if self.path.exists():
            self.pages = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.pages, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get_page(self, page_id):
        return self.pages.get(str(page_id))

    def set_page(self, page_id, data):
        self.pages[str(page_id)] = data
        self.save()

    def get_pages_by_status(self, status):
        return {pid: p for pid, p in self.pages.items() if p.get("status") == status}

    def summary(self):
        counts = {}
        for p in self.pages.values():
            s = p.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        return counts

    @staticmethod
    def new_page_record(page_id, title, space_key, parent_id=None, has_children=False):
        return {
            "page_id": str(page_id),
            "title": title,
            "space_key": space_key,
            "parent_id": str(parent_id) if parent_id else None,
            "has_children": has_children,
            "status": "pending",
            "export_path": None,
            "convert_path": None,
            "upload_path": None,
            "error": None,
            "attachments": [],
            "comments": [],
        }


# ---------------------------------------------------------------------------
# ConfluenceClient
# ---------------------------------------------------------------------------


class ConfluenceClient:
    """Confluence Cloud REST API v2 client."""

    MAX_RETRIES = 5
    INITIAL_BACKOFF = 1  # seconds

    def __init__(self, base_url, username, api_token):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, api_token)
        self.session.headers.update({"Accept": "application/json"})

    # -- low-level --------------------------------------------------------

    def _request(self, method, url, **kwargs):
        """Make an HTTP request with retry + backoff on 429/5xx."""
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"

        backoff = self.INITIAL_BACKOFF
        for attempt in range(1, self.MAX_RETRIES + 1):
            log.debug("%s %s (attempt %d)", method, url, attempt)
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                log.warning("HTTP %d on %s — retrying in %ds", resp.status_code, url, retry_after)
                time.sleep(retry_after)
                backoff = min(backoff * 2, 32)
                continue

            resp.raise_for_status()
            return resp

        resp.raise_for_status()
        return resp  # unreachable but satisfies linters

    def _get_json(self, url, **params):
        return self._request("GET", url, params=params).json()

    def _paginate(self, url, **params):
        """Yield all results using cursor-based pagination."""
        while url:
            data = self._get_json(url, **params)
            params = {}  # params only on first call; next URL has them
            yield from data.get("results", [])
            url = data.get("_links", {}).get("next")

    # -- auth -------------------------------------------------------------

    def verify_auth(self):
        """Verify credentials are valid (not anonymous)."""
        data = self._get_json("/wiki/rest/api/user/current")
        if data.get("type") == "anonymous":
            raise click.ClickException(
                "Confluence authentication failed — token resolved as anonymous. "
                "Check CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN."
            )
        log.info("Authenticated as: %s", data.get("displayName", data.get("username", "?")))
        return data

    # -- spaces -----------------------------------------------------------

    def get_space_by_key(self, key):
        data = self._get_json("/wiki/api/v2/spaces", keys=key)
        results = data.get("results", [])
        if not results:
            raise click.ClickException(f"Space '{key}' not found.")
        return results[0]

    def get_all_spaces(self):
        return list(self._paginate("/wiki/api/v2/spaces"))

    # -- pages ------------------------------------------------------------

    def get_space_pages(self, space_id):
        """Get all pages in a space. Lists pages first, then fetches each with export_view body."""
        # v2 API doesn't support body-format on list endpoint — list first, fetch individually
        page_list = list(
            self._paginate(
                "/wiki/api/v2/spaces/{}/pages".format(space_id),
                limit=50,
            )
        )
        log.info("Found %d pages in space, fetching bodies...", len(page_list))
        pages = []
        for i, p in enumerate(page_list, 1):
            log.debug("Fetching page %d/%d: %s", i, len(page_list), p.get("title", "?"))
            full = self.get_page(p["id"])
            pages.append(full)
        return pages

    def get_page(self, page_id):
        return self._get_json(
            f"/wiki/api/v2/pages/{page_id}",
            **{"body-format": "export_view"},
        )

    def get_pages_by_ids(self, page_ids):
        """Fetch multiple pages by ID."""
        pages = []
        for pid in page_ids:
            pages.append(self.get_page(pid))
        return pages

    def get_page_children(self, page_id):
        """Get direct children of a page."""
        return list(self._paginate(f"/wiki/api/v2/pages/{page_id}/children"))

    # -- comments ---------------------------------------------------------

    def get_page_comments(self, page_id):
        top_level = list(
            self._paginate(
                f"/wiki/api/v2/pages/{page_id}/footer-comments",
                **{"body-format": "storage"},
            )
        )
        # Recursively fetch reply chains
        all_comments = []
        for comment in top_level:
            all_comments.append(comment)
            all_comments.extend(self._get_comment_replies(comment["id"]))
        return all_comments

    def _get_comment_replies(self, comment_id):
        """Recursively fetch replies (children) of a footer comment."""
        children = list(
            self._paginate(
                f"/wiki/api/v2/footer-comments/{comment_id}/children",
                **{"body-format": "storage"},
            )
        )
        all_replies = []
        for child in children:
            all_replies.append(child)
            all_replies.extend(self._get_comment_replies(child["id"]))
        return all_replies

    # -- attachments ------------------------------------------------------

    def get_page_attachments(self, page_id):
        return list(self._paginate(f"/wiki/api/v2/pages/{page_id}/attachments"))

    def download_attachment(self, content_id, attachment_id, fallback_url=None):
        """Download an attachment binary.

        Uses the REST route /wiki/rest/api/content/{id}/child/attachment/{attId}/download,
        which honours API-token Basic auth and 302-redirects to the media binary.
        The attachment's own `downloadLink` instead points at the legacy
        /wiki/download servlet, which rejects API tokens (HTTP 401 + "OAuth"
        challenge) on Cloud sites — so it is only used as a last-resort fallback.
        """
        try:
            url = (
                f"/wiki/rest/api/content/{content_id}"
                f"/child/attachment/{attachment_id}/download"
            )
            return self._request("GET", url).content
        except requests.HTTPError:
            if fallback_url:
                if fallback_url.startswith("/download/") or fallback_url.startswith("/rest/"):
                    fallback_url = f"/wiki{fallback_url}"
                return self._request("GET", fallback_url).content
            raise


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------


class Converter:
    """Convert Confluence export_view HTML to Markdown."""

    UNSAFE_FILENAME_RE = re.compile(r'[/\\:*?"<>|]')

    # Matches a Confluence page id in an internal link, e.g.
    #   /wiki/spaces/TEAM/pages/67890/Other+Page
    #   https://x.atlassian.net/wiki/spaces/TEAM/pages/67890
    PAGE_LINK_RE = re.compile(r"/pages/(\d+)")

    def __init__(self, exclude_images=False, exclude_attachments=False, strip_patterns=None):
        self.exclude_images = exclude_images
        self.exclude_attachments = exclude_attachments
        # Text substrings; any block (table/div/etc.) whose text contains one is
        # removed during preprocessing. Used to drop boilerplate such as a
        # per-page copyright box. Configured via STRIP_CONTENT_PATTERNS.
        self.strip_patterns = [p for p in (strip_patterns or []) if p]
        # page_id -> output path relative to the space root (e.g. "Section/Leaf.md").
        # Populated via set_link_map() so internal page links can be rewritten to
        # relative links between the migrated Markdown files.
        self.page_link_map = {}
        self._h2t = html2text.HTML2Text()
        self._h2t.body_width = 0
        self._h2t.protect_links = True
        self._h2t.unicode_snob = True
        self._h2t.wrap_links = False
        self._h2t.wrap_list_items = False
        self._h2t.pad_tables = True

    # -- preprocessing ----------------------------------------------------

    def preprocess_html(self, html, attachment_names=None, current_path=None):
        """Clean Confluence HTML before markdown conversion.

        current_path: output path of the page being converted (relative to the
        space root); enables internal-link rewriting when a link map is set.
        """
        soup = BeautifulSoup(html, "html.parser")

        # Drop configured boilerplate blocks (e.g. a per-page copyright box)
        self._strip_configured_content(soup)

        # Status macros (lozenges, e.g. "IN ARBEIT" / "ABGELEHNT") → inline code.
        # Runs early so status keywords inside panels and table cells are caught
        # before those blocks are flattened/converted.
        self._replace_status_macros(soup)

        # Remove leading <hr> tags — html2text converts them to "---" which
        # Nextcloud Collectives misinterprets as YAML front matter
        for el in soup.find_all("hr"):
            el.decompose()

        # Remove attachment management UI + preceding "Attachments" heading
        for el in soup.select("div.plugin_attachments_container"):
            prev = el.previous_sibling
            # Walk back over whitespace NavigableStrings
            while prev and not isinstance(prev, Tag):
                prev = prev.previous_sibling
            if prev and prev.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if "attachment" in prev.get_text(strip=True).lower():
                    prev.decompose()
            el.decompose()

        # Tables: expand colspan into duplicate cells so markdown column count is correct
        for cell in soup.find_all(["th", "td"]):
            span = int(cell.get("colspan", 1))
            if span > 1:
                del cell["colspan"]
                for _ in range(span - 1):
                    empty = soup.new_tag(cell.name)
                    cell.insert_after(empty)

        # Tables: flatten block elements inside cells so html2text keeps table intact
        for tag_name in ("th", "td"):
            for cell in soup.find_all(tag_name):
                # Replace headings with <strong>
                for heading in cell.find_all(re.compile(r"^h[1-6]$")):
                    strong = soup.new_tag("strong")
                    strong.string = heading.get_text()
                    heading.replace_with(strong)

                # Flatten block content in cells to keep table rows intact
                block_children = cell.find_all(["p", "ul", "ol"])
                if len(block_children) <= 1 and not cell.find("br"):
                    # Simple case: single <p> or single list — just unwrap
                    for lst in cell.find_all(["ul", "ol"]):
                        items = [li.get_text(strip=True) for li in lst.find_all("li")]
                        lst.replace_with(", ".join(items) if items else "")
                    for p in cell.find_all("p"):
                        p.unwrap()
                elif block_children:
                    # Complex case: multiple blocks — collapse to inline text in DOM order
                    parts = []
                    for child in list(cell.children):
                        if isinstance(child, Tag):
                            if child.name in ("ul", "ol"):
                                items = [li.get_text(strip=True) for li in child.find_all("li")]
                                if items:
                                    parts.append(", ".join(items))
                                child.decompose()
                            elif child.name == "p":
                                text = child.get_text(strip=True)
                                if text:
                                    parts.append(text)
                                child.decompose()
                            elif child.name == "br":
                                child.decompose()
                        else:
                            text = str(child).strip()
                            if text:
                                parts.append(text)
                            child.extract()
                    if parts:
                        cell.insert(0, " — ".join(parts))

        # Info / warning / note panels → blockquotes. No "Info:"/"Note:" label is
        # prepended: Collectives renders the blockquote distinctly, and Confluence
        # panel bodies frequently already start with their own label, so a prefix
        # just duplicates it.
        for panel in soup.find_all("div", class_=re.compile(r"(confluence-information-macro)")):
            body = panel.find("div", class_="confluence-information-macro-body")
            if body:
                bq = soup.new_tag("blockquote")
                for child in list(body.children):
                    bq.append(child.extract() if isinstance(child, Tag) else child)
                panel.replace_with(bq)

        # Code blocks — preserve language hints
        for code_macro in soup.find_all("div", class_="code-block"):
            lang = code_macro.get("data-language", "")
            pre = code_macro.find("pre")
            if pre:
                new_pre = soup.new_tag("pre")
                code_tag = soup.new_tag("code", attrs={"class": f"language-{lang}"} if lang else {})
                code_tag.string = pre.get_text()
                new_pre.append(code_tag)
                code_macro.replace_with(new_pre)

        # ac:structured-macro and data-macro-name → HTML comments, BUT keep any
        # rendered image inside (draw.io / Gliffy / etc. embed a PNG preview in
        # export_view) so diagrams still show up in Collectives.
        for macro in soup.find_all("ac:structured-macro"):
            name = macro.get("ac:name", "unknown")
            self._replace_unsupported_macro(soup, macro, name)

        for macro in soup.find_all("div", attrs={"data-macro-name": True}):
            # Skip already-handled panels and code blocks
            if macro.get("class") and any(
                "confluence-information-macro" in c or "code-block" in c
                for c in macro.get("class", [])
            ):
                continue
            name = macro.get("data-macro-name", "unknown")
            self._replace_unsupported_macro(soup, macro, name)

        # User mentions → @DisplayName
        for mention in soup.find_all("a", class_="confluence-userlink"):
            display = mention.get_text(strip=True)
            if display:
                mention.replace_with(f"@{display}")

        # Rewrite image src to local filenames; remove non-image files (e.g. .mp4)
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
        if not self.exclude_images:
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "/attachments/" in src or "/attachment/" in src:
                    filename = unquote(src.split("/")[-1].split("?")[0])
                    ext = Path(filename).suffix.lower()
                    if ext in image_exts:
                        img["src"] = quote(filename)
                    else:
                        # Non-image (video, etc.) — remove inline embed;
                        # file will appear in ## Attachments section instead
                        img.decompose()
                elif src.startswith("data:"):
                    pass  # inline base64, leave as-is
        else:
            for img in soup.find_all("img"):
                img.decompose()

        # Rewrite internal page-to-page links to relative Markdown links so they
        # keep working inside Collectives (no-op unless a link map + current path
        # are supplied via set_link_map() / convert_page(current_page_id=...)).
        if self.page_link_map and current_path is not None:
            self._rewrite_internal_links(soup, current_path)

        return str(soup)

    def _strip_configured_content(self, soup):
        """Remove blocks whose text contains a configured strip pattern.

        Targets the nearest enclosing table (incl. its `table-wrap` div) — or, if
        not in a table, the nearest block element — so e.g. a per-page copyright
        box (a one-cell table) is dropped wholesale. Customer-specific patterns
        live in config, never in code.
        """
        if not self.strip_patterns:
            return
        removed = set()
        for pattern in self.strip_patterns:
            for node in soup.find_all(string=lambda s, p=pattern: s and p in s):
                block = node.find_parent("table") or node.find_parent(
                    ["blockquote", "div", "p", "li"])
                if block is None:
                    continue
                target = block.find_parent("div", class_="table-wrap") or block
                if id(target) in removed:
                    continue
                removed.add(id(target))
                target.decompose()

    def _replace_status_macros(self, soup):
        """Confluence status macros render as a lozenge span in export_view
        (`<span class="status-macro aui-lozenge …">IN ARBEIT</span>`). Collectives
        has no equivalent, so turn each into an inline `<code>` element — html2text
        emits it as backtick-wrapped inline code (e.g. `IN ARBEIT`), keeping the
        keyword visually distinct.
        """
        for span in soup.find_all("span", class_="status-macro"):
            text = span.get_text(strip=True)
            if not text:
                span.decompose()
                continue
            code = soup.new_tag("code")
            code.string = text
            span.replace_with(code)

    def _replace_unsupported_macro(self, soup, macro, name):
        """Drop an unsupported macro, but lift out any rendered <img> it wraps.

        Confluence renders draw.io/Gliffy/etc. macros to a PNG preview inside the
        macro element in export_view. Keeping that image lets the diagram render
        in Collectives; the later image-src rewrite turns it into a local file
        reference. Macros with no image become an HTML comment as before.
        """
        imgs = macro.find_all("img")
        if imgs:
            wrapper = soup.new_tag("p")
            for img in imgs:
                wrapper.append(img.extract())
            macro.replace_with(wrapper)
        else:
            macro.replace_with(Comment(f" Unsupported macro: {name} "))

    def _rewrite_internal_links(self, soup, current_path):
        """Mark <a> links that target a migrated Confluence page with a
        `cpage:<id>` placeholder. The placeholder is resolved to a real
        Collectives page URL at upload time (`resolve_page_links`), because the
        URL depends on the upload target parent and collective name, which the
        converter does not know. Links to pages outside the migrated set (or
        non-page links) are left untouched."""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = self.PAGE_LINK_RE.search(href)
            if not m:
                continue
            target_id = m.group(1)
            if target_id not in self.page_link_map:
                continue  # target page not part of this migration — leave as-is
            anchor = "#" + href.split("#", 1)[1] if "#" in href else ""
            a["href"] = f"cpage:{target_id}{anchor}"

    # -- conversion -------------------------------------------------------

    def set_link_map(self, page_link_map):
        """Provide {page_id: output_path} so internal links can be rewritten."""
        self.page_link_map = page_link_map or {}

    def html_to_markdown(self, html):
        """Convert HTML to markdown via html2text."""
        return self._h2t.handle(html).strip()

    def convert_page(self, page_data, attachments_dir=None, current_page_id=None):
        """Full page conversion: preprocess → markdown → comments → attachments.

        current_page_id: the Confluence page id being converted; used with the
        link map to rewrite internal page links relative to this page.
        """
        html = page_data.get("body", "")
        attachment_names = [a["title"] for a in page_data.get("attachments", [])]
        current_path = self.page_link_map.get(str(current_page_id)) if current_page_id else None
        processed = self.preprocess_html(html, attachment_names, current_path=current_path)
        md = self.html_to_markdown(processed)

        # Append comments
        comments = page_data.get("comments", [])
        if comments:
            md += "\n\n" + self.format_comments(comments)

        # Append attachment links (non-image only)
        attachments = page_data.get("attachments", [])
        if attachments and not self.exclude_attachments:
            section = self.generate_attachment_section(attachments)
            if section:
                md += "\n\n" + section

        return md

    def format_comments(self, comments):
        """Format comments as a ## Comments section."""
        if not comments:
            return ""
        parts = ["## Comments", ""]
        for c in comments:
            author = "Unknown"
            version = c.get("version", {})
            if version and version.get("authorId"):
                author = version.get("authorId")
            # Try to get display name from nested author object
            if version and isinstance(version.get("author"), dict):
                author = version["author"].get("displayName", author)
            date = version.get("createdAt", "") if version else ""
            if date:
                # Format: strip timezone info for readability
                date = date.replace("T", " ").split(".")[0]
            parts.append(f"### {author} — {date}")
            parts.append("")
            body_html = ""
            body = c.get("body", {})
            if isinstance(body, dict):
                body_html = body.get("storage", {}).get("value", "")
            elif isinstance(body, str):
                body_html = body
            if body_html:
                parts.append(self.html_to_markdown(body_html))
            parts.append("")
        return "\n".join(parts)

    def generate_attachment_section(self, attachments):
        """Generate ## Attachments section for non-image files."""
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
        non_image = []
        for a in attachments:
            title = a.get("title", "")
            ext = Path(title).suffix.lower()
            if ext not in image_exts:
                non_image.append(title)
        if not non_image:
            return ""
        lines = ["## Attachments", ""]
        for name in non_image:
            lines.append(f"- [{name}]({quote(name)})")
        return "\n".join(lines)

    # -- filename / tree --------------------------------------------------

    def sanitize_filename(self, name, existing=None):
        """Sanitize a filename: strip unsafe chars, cap length, dedupe."""
        name = self.UNSAFE_FILENAME_RE.sub("", name).strip()
        if not name:
            name = "untitled"
        if len(name) > 200:
            name = name[:200]
        if existing is not None:
            base = name
            counter = 2
            while name in existing:
                name = f"{base}-{counter}"
                counter += 1
        return name

    def build_output_tree(self, state):
        """Build mapping of page_id → output path relative to space root.

        Returns dict: {page_id: {"path": "relative/path.md", "dir": "relative/dir"}}
        """
        pages = state.pages
        if not pages:
            return {}

        # Find the space key and homepage
        space_key = None
        homepage_id = None
        children_map = {}  # parent_id → [child_ids]
        root_pages = []  # pages with no parent

        for pid, p in pages.items():
            space_key = p.get("space_key", space_key)
            parent = p.get("parent_id")
            if parent and parent in pages:
                children_map.setdefault(parent, []).append(pid)
            else:
                root_pages.append(pid)

        # Determine which pages have children (in our page set)
        has_children = set(children_map.keys())

        # Find homepage: root page with most children, or first root page
        if root_pages:
            homepage_id = max(root_pages, key=lambda pid: len(children_map.get(pid, [])))

        output = {}
        used_names = {}  # dir_path → set of names used

        def assign_paths(page_ids, dir_prefix):
            used_names.setdefault(dir_prefix, set())
            for pid in page_ids:
                p = pages[pid]
                title = p.get("title", "untitled")

                if pid == homepage_id and dir_prefix == "":
                    # Homepage → root Readme.md
                    output[pid] = {"path": "Readme.md", "dir": ""}
                    # Homepage children go in root dir
                    if pid in children_map:
                        assign_paths(children_map[pid], "")
                elif pid in has_children:
                    # Parent page → folder/Readme.md
                    folder = self.sanitize_filename(title, used_names[dir_prefix])
                    used_names[dir_prefix].add(folder)
                    folder_path = f"{dir_prefix}/{folder}" if dir_prefix else folder
                    output[pid] = {"path": f"{folder_path}/Readme.md", "dir": folder_path}
                    if pid in children_map:
                        assign_paths(children_map[pid], folder_path)
                else:
                    # Leaf page → title.md
                    name = self.sanitize_filename(title, used_names[dir_prefix])
                    used_names[dir_prefix].add(name)
                    path = f"{dir_prefix}/{name}.md" if dir_prefix else f"{name}.md"
                    output[pid] = {"path": path, "dir": dir_prefix}

        assign_paths(root_pages, "")
        return output

    def copy_attachments(self, page_data, src_dir, dest_dir):
        """Copy attachment files from export dir to convert output dir.

        Returns list of copied filenames.
        """
        copied = []
        if self.exclude_attachments:
            return copied

        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
        attachments = page_data.get("attachments", [])

        for a in attachments:
            title = a.get("title", "")
            ext = Path(title).suffix.lower()

            if self.exclude_images and ext in image_exts:
                continue

            src_file = Path(src_dir) / title
            if src_file.exists():
                dest_file = Path(dest_dir) / title
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_file), str(dest_file))
                copied.append(title)
            else:
                log.warning("Attachment file not found: %s", src_file)

        return copied


# ---------------------------------------------------------------------------
# NextcloudClient
# ---------------------------------------------------------------------------


class NextcloudClient:
    """Nextcloud WebDAV client for Collectives."""

    # Collectives stores pages in a folder inside the user's files. Recent
    # versions (Collectives 4.x / Nextcloud 28+) use the HIDDEN ".Collectives";
    # older versions used "Collectives". Probed in verify_connection().
    COLLECTIVES_DIRS = (".Collectives", "Collectives")

    def __init__(self, base_url, username, password, collective):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.collective = collective
        # URL segment for internal page links; slug-id form after resolution.
        self.collective_segment = collective
        self.files_base = f"{self.base_url}/remote.php/dav/files/{username}"
        # Default to the modern hidden folder; resolution may switch it (incl.
        # localised names like '.Kollektive').
        self.collectives_dir = self.COLLECTIVES_DIRS[0]
        self.session = requests.Session()
        self.session.auth = (username, password)

    @property
    def dav_base(self):
        return f"{self.files_base}/{self.collectives_dir}/{self.collective}"

    def _ocs_get(self, path):
        return self.session.get(
            f"{self.base_url}{path}",
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )

    def resolve_collective(self):
        """Resolve the configured collective via the Collectives OCS API.

        Matches the configured NEXTCLOUD_COLLECTIVE against each collective's
        name, slug, or '<slug>-<id>'. On a match, sets the WebDAV folder name
        (the display name), the storage folder read from a page's
        `collectivePath` (so localised folders like '.Kollektive' work without
        guessing), and the '<slug>-<id>' URL segment used for internal links.
        Returns True on success, False if the API/match is unavailable.
        """
        try:
            resp = self._ocs_get("/ocs/v2.php/apps/collectives/api/v1.0/collectives")
            if resp.status_code != 200:
                return False
            collectives = resp.json().get("ocs", {}).get("data", {}).get("collectives", [])
        except Exception:
            return False

        match = next(
            (c for c in collectives
             if self.collective in (c.get("name"), c.get("slug"), f"{c.get('slug')}-{c.get('id')}")),
            None,
        )
        if not match:
            return False

        cid = match["id"]
        self.collective = match.get("name")               # WebDAV folder = display name
        self.collective_segment = f"{match.get('slug')}-{cid}"
        try:  # localised storage folder, read rather than guessed
            pages = (self._ocs_get(
                f"/ocs/v2.php/apps/collectives/api/v1.0/collectives/{cid}/pages")
                .json().get("ocs", {}).get("data", {}).get("pages", []))
            for p in pages:
                cp = p.get("collectivePath")
                if cp and "/" in cp:
                    self.collectives_dir = cp.split("/", 1)[0]
                    break
        except Exception:
            pass
        return True

    def verify_connection(self):
        """Verify we can reach the collective.

        Prefers the Collectives OCS API (handles localised storage folders and
        display-name vs slug). Falls back to probing '.Collectives'/'Collectives'
        for older servers without the API.
        """
        if self.resolve_collective():
            resp = self.session.request("PROPFIND", self.dav_base, headers={"Depth": "0"})
            if resp.status_code == 401:
                raise click.ClickException("Nextcloud authentication failed — check credentials.")
            if resp.status_code in (200, 207):
                log.info("Connected to Nextcloud collective: %s (under %s)",
                         self.collective, self.collectives_dir)
                return
            raise click.ClickException(
                f"Collective '{self.collective}' resolved but {self.dav_base} "
                f"returned HTTP {resp.status_code}")

        last_status = None
        for candidate in self.COLLECTIVES_DIRS:
            self.collectives_dir = candidate
            resp = self.session.request("PROPFIND", self.dav_base, headers={"Depth": "0"})
            if resp.status_code == 401:
                raise click.ClickException("Nextcloud authentication failed — check credentials.")
            if resp.status_code in (200, 207):
                log.info("Connected to Nextcloud collective: %s (under %s)",
                         self.collective, candidate)
                return
            last_status = resp.status_code

        if last_status == 404:
            raise click.ClickException(
                f"Collective '{self.collective}' not found (no OCS match; probed "
                f"{'/'.join(self.COLLECTIVES_DIRS)}) for user {self.username}.")
        raise click.ClickException(f"Nextcloud PROPFIND failed with status {last_status}")

    @staticmethod
    def _remote(path):
        """Normalise a remote path for WebDAV URLs (Windows backslashes → '/')."""
        return path.replace("\\", "/")

    def mkdir_p(self, path):
        """Recursively create directories via MKCOL."""
        parts = self._remote(path).strip("/").split("/")
        current = self.dav_base
        for part in parts:
            if not part:
                continue
            current = f"{current}/{part}"
            resp = self.session.request("MKCOL", current)
            if resp.status_code not in (201, 405):  # 405 = already exists
                log.warning("MKCOL %s returned %d", current, resp.status_code)

    def upload_file(self, local_path, remote_path):
        """Upload a file via PUT."""
        import mimetypes
        remote_path = self._remote(remote_path)
        url = f"{self.dav_base}/{remote_path.lstrip('/')}"
        content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        with open(local_path, "rb") as f:
            resp = self.session.put(url, data=f, headers={"Content-Type": content_type})
        if resp.status_code not in (200, 201, 204):
            raise click.ClickException(
                f"Upload failed for {remote_path}: HTTP {resp.status_code}"
            )
        log.debug("Uploaded: %s", remote_path)

    def get_file_id(self, remote_path):
        """Get Nextcloud file ID via PROPFIND."""
        import xml.etree.ElementTree as ET

        remote_path = self._remote(remote_path)
        url = f"{self.dav_base}/{remote_path.lstrip('/')}"
        body = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop><oc:fileid/></d:prop>"
            "</d:propfind>"
        )
        resp = self.session.request(
            "PROPFIND", url,
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data=body,
        )
        if resp.status_code in (200, 207):
            root = ET.fromstring(resp.text)
            fileid_el = root.find(".//{http://owncloud.org/ns}fileid")
            if fileid_el is not None and fileid_el.text:
                return fileid_el.text
        log.warning("Could not get file ID for %s (HTTP %d)", remote_path, resp.status_code)
        return None

    def file_url(self, file_id, remote_dir=None):
        """Nextcloud "open file by id" URL. Resolves regardless of where the file
        lives — important because collectives live under the hidden .Collectives
        folder, which a path-based Files URL (dir=...) cannot navigate into."""
        return f"{self.base_url}/f/{file_id}"

    def exists(self, path):
        """Check if a remote path exists via PROPFIND depth 0."""
        url = f"{self.dav_base}/{self._remote(path).lstrip('/')}"
        resp = self.session.request("PROPFIND", url, headers={"Depth": "0"})
        return resp.status_code in (200, 207)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def strip_patterns_from_env():
    """Content-strip patterns from STRIP_CONTENT_PATTERNS (||| separated)."""
    raw = os.getenv("STRIP_CONTENT_PATTERNS", "")
    return [p.strip() for p in raw.split("|||") if p.strip()]


def require_env(*keys):
    """Validate required environment variables are set."""
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        raise click.ClickException(
            f"Missing required environment variables: {', '.join(missing)}"
        )


# Placeholder emitted by the converter for an internal page link; resolved here.
CPAGE_LINK_RE = re.compile(r"cpage:(\d+)(#[^)>\s]*)?")


def page_route_segments(output_rel_path):
    """Collectives route segments (page titles) for a converted page file path,
    relative to the space root. A page's title in Collectives is its file/folder
    name, so the route is the path with the `.md` stripped and the `Readme.md`
    of a parent page collapsed to its folder.

    e.g. "Drafts/Proc/CAPA.md" -> ["Drafts", "Proc", "CAPA"]
         "Drafts/Proc/Readme.md" -> ["Drafts", "Proc"]   (parent/folder page)
         "Readme.md" -> []                                (space homepage)
    """
    p = output_rel_path.replace("\\", "/")
    if p == "Readme.md":
        return []
    if p.endswith("/Readme.md"):
        p = p[: -len("/Readme.md")]
    elif p.endswith(".md"):
        p = p[:-3]
    return [seg for seg in p.split("/") if seg]


def build_page_routes(state, target_parent):
    """Map Confluence page_id -> Collectives route (page-title segments from the
    collective root). target_parent is the optional top folder; '' imports
    directly at the collective base (the space homepage becomes the landing page).
    """
    prefix = [target_parent] if target_parent else []
    routes = {}
    for pid, rec in state.pages.items():
        convert_path = rec.get("convert_path")
        if not convert_path:
            continue
        space_key = rec.get("space_key", "default")
        rel = Path(convert_path).relative_to(Path("convert_data") / space_key).as_posix()
        routes[pid] = prefix + page_route_segments(rel)
    return routes


def _relative_route_link(target_route, source_route):
    """Relative href from a source page to a target page (both route-segment
    lists). The browser resolves a relative link against the source page's URL
    with its last segment dropped, so the base is source_route[:-1]; a shared
    parent prefix cancels out, leaving a collective-agnostic link."""
    base = "/".join(source_route[:-1]) or "."
    target = "/".join(target_route) or "."
    rel = posixpath.relpath(target, base)
    return "/".join(quote(seg) for seg in rel.split("/"))


def resolve_page_links(content, source_route, page_routes, collective_segment):
    """Replace `cpage:<id>` placeholders with links to other migrated pages.

    Links are RELATIVE to the source page so they survive moving/renaming the
    collective (the collective is inherited from the current page's URL). The one
    exception is the collective root/landing page (source_route == []): its URL
    has no trailing path segment to be relative to, so its links fall back to the
    absolute '/apps/collectives/<segment>/...' form.
    """
    root_source = not source_route

    def _sub(m):
        target_route = page_routes.get(m.group(1))
        if target_route is None:
            return m.group(0)  # target not migrated — leave the placeholder's link alone
        anchor = m.group(2) or ""
        if root_source:
            href = "/apps/collectives/" + "/".join(
                quote(s) for s in [collective_segment, *target_route])
        else:
            href = _relative_route_link(target_route, source_route)
        return href + anchor

    return CPAGE_LINK_RE.sub(_sub, content)


def determine_exit_code(state):
    """Determine process exit code from migration state."""
    summary = state.summary()
    total = sum(summary.values())
    if total == 0:
        return EXIT_SUCCESS
    uploaded = summary.get("uploaded", 0)
    failed = summary.get("failed", 0)
    if uploaded == total:
        return EXIT_SUCCESS
    if failed == total:
        return EXIT_FAILURE
    if failed > 0 or uploaded > 0:
        return EXIT_PARTIAL
    return EXIT_SUCCESS


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

COMMON_OPTIONS = [
    click.option("--dry-run", is_flag=True, help="Preview actions without making changes."),
    click.option("--debug", is_flag=True, help="Enable debug logging."),
    click.option("--log-file", type=click.Path(), default=None, help="Log file path."),
]

SCOPE_OPTIONS = [
    click.option("--space", "space_key", default=None, help="Confluence space key."),
    click.option("--pages", default=None, help="Comma-separated page IDs."),
    click.option("--all-spaces", is_flag=True, help="Migrate all accessible spaces."),
]

ATTACHMENT_OPTIONS = [
    click.option("--exclude-images", is_flag=True, help="Skip image attachments."),
    click.option("--exclude-attachments", is_flag=True, help="Skip all attachments."),
]


def add_options(options):
    """Decorator to apply a list of click options."""
    def decorator(func):
        for option in reversed(options):
            func = option(func)
        return func
    return decorator


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """Confluence Cloud → Nextcloud Collectives migration tool."""
    pass


# -- export ---------------------------------------------------------------


@cli.command()
@add_options(COMMON_OPTIONS)
@add_options(SCOPE_OPTIONS)
@add_options(ATTACHMENT_OPTIONS)
def export(space_key, pages, all_spaces, exclude_images, exclude_attachments, dry_run, debug, log_file):
    """Export pages from Confluence Cloud to local disk."""
    setup_logging(debug, log_file)

    require_env("CONFLUENCE_BASE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN")

    client = ConfluenceClient(
        os.getenv("CONFLUENCE_BASE_URL"),
        os.getenv("CONFLUENCE_USERNAME"),
        os.getenv("CONFLUENCE_API_TOKEN"),
    )

    try:
        client.verify_auth()
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Authentication failed: {e}")

    state = MigrationState().load()

    # Resolve scope
    space_ids_keys = []  # list of (space_id, space_key)
    if pages:
        # Fetch individual pages, group by space
        page_ids = [p.strip() for p in pages.split(",")]
        fetched = client.get_pages_by_ids(page_ids)
        for pg in fetched:
            sk = pg.get("spaceId", "")
            space_ids_keys.append((sk, pg.get("_expandable", {}).get("space", sk)))
        # Deduplicate
        space_ids_keys = list(set(space_ids_keys))
        all_pages = fetched
    elif all_spaces:
        spaces = client.get_all_spaces()
        space_ids_keys = [(s["id"], s["key"]) for s in spaces]
        all_pages = None  # will fetch per-space
    elif space_key:
        space = client.get_space_by_key(space_key)
        space_ids_keys = [(space["id"], space["key"])]
        all_pages = None
    else:
        raise click.ClickException("Specify --space, --pages, or --all-spaces.")

    for space_id, sk in space_ids_keys:
        if not sk or sk == space_id:
            # Try to resolve the space key from space_id
            try:
                space_data = client._get_json(f"/wiki/api/v2/spaces/{space_id}")
                sk = space_data.get("key", str(space_id))
            except Exception:
                sk = str(space_id)

        log.info("Processing space: %s", sk)

        if all_pages is None:
            fetched_pages = client.get_space_pages(space_id)
        else:
            fetched_pages = [p for p in all_pages if str(p.get("spaceId")) == str(space_id)]

        if dry_run:
            click.echo(f"\n[DRY RUN] Space: {sk} — {len(fetched_pages)} page(s)")
            for pg in fetched_pages:
                click.echo(f"  Page: {pg.get('title', '?')} (ID: {pg['id']})")
            continue

        # Determine which pages have children
        page_ids_set = {str(p["id"]) for p in fetched_pages}
        parent_ids_set = set()
        for p in fetched_pages:
            pid = p.get("parentId")
            if pid and str(pid) in page_ids_set:
                parent_ids_set.add(str(pid))

        export_base = Path("export_data") / sk

        for pg in fetched_pages:
            page_id = str(pg["id"])
            title = pg.get("title", "untitled")
            parent_id = pg.get("parentId")
            has_children = page_id in parent_ids_set

            log.info("Exporting page: %s (ID: %s)", title, page_id)

            try:
                # Get body HTML
                body_html = ""
                body = pg.get("body", {})
                if isinstance(body, dict):
                    body_html = body.get("export_view", {}).get("value", "")
                elif isinstance(body, str):
                    body_html = body

                # Get comments
                comments = []
                try:
                    comments = client.get_page_comments(page_id)
                except Exception as e:
                    log.warning("Failed to fetch comments for page %s: %s", page_id, e)

                # Get and download attachments
                attachments = []
                if not exclude_attachments:
                    try:
                        att_list = client.get_page_attachments(page_id)
                        att_dir = export_base / "attachments" / page_id
                        for att in att_list:
                            att_title = att.get("title", "")
                            image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
                            ext = Path(att_title).suffix.lower()

                            if exclude_images and ext in image_exts:
                                continue

                            att_id = att.get("id")
                            fallback = att.get("downloadLink") or att.get("_links", {}).get("download")

                            if att_id or fallback:
                                try:
                                    data = client.download_attachment(page_id, att_id, fallback_url=fallback)
                                    att_dir.mkdir(parents=True, exist_ok=True)
                                    (att_dir / att_title).write_bytes(data)
                                    attachments.append({
                                        "title": att_title,
                                        "size": len(data),
                                        "mediaType": att.get("mediaType", ""),
                                    })
                                    log.debug("Downloaded attachment: %s", att_title)
                                except Exception as e:
                                    log.warning("Failed to download %s: %s", att_title, e)
                                    attachments.append({
                                        "title": att_title,
                                        "error": str(e),
                                        "mediaType": att.get("mediaType", ""),
                                    })
                    except Exception as e:
                        log.warning("Failed to fetch attachments for page %s: %s", page_id, e)

                # Save page data
                page_data = {
                    "page_id": page_id,
                    "title": title,
                    "space_key": sk,
                    "parent_id": str(parent_id) if parent_id else None,
                    "has_children": has_children,
                    "body": body_html,
                    "comments": comments,
                    "attachments": attachments,
                }

                pages_dir = export_base / "pages"
                pages_dir.mkdir(parents=True, exist_ok=True)
                export_path = pages_dir / f"{page_id}.json"
                export_path.write_text(
                    json.dumps(page_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                # Update state
                record = MigrationState.new_page_record(
                    page_id, title, sk, parent_id, has_children
                )
                record["status"] = "exported"
                record["export_path"] = str(export_path)
                record["attachments"] = attachments
                record["comments"] = [{"id": c.get("id")} for c in comments]
                state.set_page(page_id, record)

                log.info("Exported: %s (%d attachments, %d comments)", title, len(attachments), len(comments))

            except Exception as e:
                log.error("Failed to export page %s: %s", page_id, e)
                record = MigrationState.new_page_record(
                    page_id, title, sk, parent_id, has_children
                )
                record["status"] = "failed"
                record["error"] = str(e)
                state.set_page(page_id, record)

    if not dry_run:
        summary = state.summary()
        click.echo(f"\nExport complete: {summary}")

    sys.exit(determine_exit_code(state) if not dry_run else EXIT_SUCCESS)


# -- convert --------------------------------------------------------------


@cli.command()
@add_options(COMMON_OPTIONS)
@add_options(ATTACHMENT_OPTIONS)
def convert(exclude_images, exclude_attachments, dry_run, debug, log_file):
    """Convert exported HTML pages to Markdown."""
    setup_logging(debug, log_file)

    state = MigrationState().load()
    exported = state.get_pages_by_status("exported")

    if not exported:
        click.echo("No exported pages to convert. Run 'export' first.")
        sys.exit(EXIT_SUCCESS)

    converter = Converter(exclude_images=exclude_images, exclude_attachments=exclude_attachments,
                          strip_patterns=strip_patterns_from_env())
    tree = converter.build_output_tree(state)
    converter.set_link_map({pid: info["path"] for pid, info in tree.items()})

    if dry_run:
        click.echo(f"\n[DRY RUN] {len(exported)} page(s) to convert:")
        for pid, info in tree.items():
            p = state.get_page(pid)
            if p and p.get("status") == "exported":
                click.echo(f"  {p['title']} → {info['path']}")
        sys.exit(EXIT_SUCCESS)

    # Determine space key from first page
    first_page = next(iter(exported.values()))
    space_key = first_page.get("space_key", "default")
    convert_base = Path("convert_data") / space_key

    for pid, page_rec in exported.items():
        title = page_rec.get("title", "?")
        log.info("Converting: %s", title)

        try:
            # Load export data
            export_path = page_rec.get("export_path")
            if not export_path or not Path(export_path).exists():
                raise FileNotFoundError(f"Export file not found: {export_path}")

            page_data = json.loads(Path(export_path).read_text(encoding="utf-8"))

            # Get output path from tree
            path_info = tree.get(pid)
            if not path_info:
                log.warning("No output path computed for page %s, skipping", pid)
                continue

            output_path = convert_base / path_info["path"]
            output_dir = convert_base / path_info["dir"] if path_info["dir"] else convert_base

            # Convert to markdown
            md = converter.convert_page(page_data, current_page_id=pid)

            # Write markdown
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(md, encoding="utf-8")

            # Copy attachments
            att_src = Path("export_data") / space_key / "attachments" / pid
            if att_src.exists():
                converter.copy_attachments(page_data, att_src, output_dir)

            # Update state
            page_rec["status"] = "converted"
            page_rec["convert_path"] = str(output_path)
            state.set_page(pid, page_rec)

            log.info("Converted: %s → %s", title, path_info["path"])

        except Exception as e:
            log.error("Failed to convert page %s: %s", pid, e)
            page_rec["status"] = "failed"
            page_rec["error"] = str(e)
            state.set_page(pid, page_rec)

    summary = state.summary()
    click.echo(f"\nConvert complete: {summary}")
    sys.exit(determine_exit_code(state))


# -- upload ---------------------------------------------------------------


@cli.command()
@add_options(COMMON_OPTIONS)
@click.option("--target-parent", default="MigratedPages",
              help="Top folder in the collective. Use '' to import at the collective "
                   "base (the space homepage becomes the landing page).")
def upload(target_parent, dry_run, debug, log_file):
    """Upload converted files to Nextcloud Collectives."""
    setup_logging(debug, log_file)

    require_env("NEXTCLOUD_URL", "NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD", "NEXTCLOUD_COLLECTIVE")

    nc = NextcloudClient(
        os.getenv("NEXTCLOUD_URL"),
        os.getenv("NEXTCLOUD_USERNAME"),
        os.getenv("NEXTCLOUD_PASSWORD"),
        os.getenv("NEXTCLOUD_COLLECTIVE"),
    )

    state = MigrationState().load()
    converted = state.get_pages_by_status("converted")

    if not converted:
        click.echo("No converted pages to upload. Run 'convert' first.")
        sys.exit(EXIT_SUCCESS)

    if dry_run:
        click.echo(f"\n[DRY RUN] {len(converted)} page(s) to upload:")
        click.echo(f"  Target parent: {target_parent}/")
        for pid, page_rec in converted.items():
            cp = page_rec.get("convert_path", "?")
            click.echo(f"  {page_rec['title']} → {target_parent}/{Path(cp).name}")
        sys.exit(EXIT_SUCCESS)

    try:
        nc.verify_connection()
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Nextcloud connection failed: {e}")

    # Create target parent directory
    nc.mkdir_p(target_parent)

    # Walk the convert_data directory and upload everything
    first_page = next(iter(converted.values()))
    space_key = first_page.get("space_key", "default")
    convert_base = Path("convert_data") / space_key

    if not convert_base.exists():
        raise click.ClickException(f"Convert data directory not found: {convert_base}")

    # Collect all files, split into attachments and markdown pages
    all_files = sorted(f for f in convert_base.rglob("*") if f.is_file())
    attachment_files = [f for f in all_files if f.suffix != ".md"]
    md_files = [f for f in all_files if f.suffix == ".md"]

    # Ensure all parent directories are created
    created_dirs = set()
    for local_file in all_files:
        parent_dirs = local_file.relative_to(convert_base).parent.as_posix()
        if parent_dirs and parent_dirs != "." and parent_dirs not in created_dirs:
            nc.mkdir_p(f"{target_parent}/{parent_dirs}")
            created_dirs.add(parent_dirs)

    # Pass 1: Upload attachments and collect file IDs
    # Maps: remote_dir -> {encoded_filename: file_url}
    attachment_urls = {}
    for local_file in attachment_files:
        relative = local_file.relative_to(convert_base)
        remote_path = f"{target_parent}/{relative.as_posix()}"
        try:
            nc.upload_file(str(local_file), remote_path)
            log.info("Uploaded attachment: %s", remote_path)
            file_id = nc.get_file_id(remote_path)
            if file_id:
                remote_dir = relative.parent.as_posix() if str(relative.parent) != "." else ""
                full_remote_dir = f"{target_parent}/{remote_dir}".rstrip("/")
                encoded_name = quote(local_file.name)
                attachment_urls.setdefault(full_remote_dir, {})[encoded_name] = \
                    nc.file_url(file_id, full_remote_dir)
                log.debug("File ID %s for %s", file_id, remote_path)
        except Exception as e:
            log.error("Failed to upload attachment %s: %s", remote_path, e)

    # Pass 2: Patch markdown with file-ID links + internal page links, then upload
    page_routes = build_page_routes(state, target_parent)
    id_by_path = {Path(rec["convert_path"]): pid
                  for pid, rec in state.pages.items() if rec.get("convert_path")}
    uploaded_pages = set()
    for local_file in md_files:
        relative = local_file.relative_to(convert_base)
        remote_path = f"{target_parent}/{relative.as_posix()}"
        remote_dir = relative.parent.as_posix() if str(relative.parent) != "." else ""
        full_remote_dir = f"{target_parent}/{remote_dir}".rstrip("/")

        try:
            content = local_file.read_text(encoding="utf-8")

            # Resolve internal page-link placeholders (relative to this page)
            source_route = page_routes.get(id_by_path.get(local_file), [])
            content = resolve_page_links(content, source_route, page_routes, nc.collective_segment)

            # Replace attachment links with absolute Nextcloud file URLs
            dir_urls = attachment_urls.get(full_remote_dir, {})
            if dir_urls:
                def _replace_link(m):
                    label, href = m.group(1), m.group(2)
                    if href in dir_urls:
                        return f"- [{label}]({dir_urls[href]})"  # keep the list bullet
                    return m.group(0)

                content = re.sub(
                    r"^- \[([^\]]+)\]\(([^)]+)\)$",
                    _replace_link,
                    content,
                    flags=re.MULTILINE,
                )

            # Upload patched content
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                nc.upload_file(tmp_path, remote_path)
            finally:
                os.unlink(tmp_path)

            log.info("Uploaded: %s", remote_path)

            for pid, page_rec in converted.items():
                if page_rec.get("convert_path") and Path(page_rec["convert_path"]) == local_file:
                    page_rec["status"] = "uploaded"
                    page_rec["upload_path"] = remote_path
                    state.set_page(pid, page_rec)
                    uploaded_pages.add(pid)

        except Exception as e:
            log.error("Failed to upload %s: %s", remote_path, e)
            for pid, page_rec in converted.items():
                if page_rec.get("convert_path") and Path(page_rec["convert_path"]) == local_file:
                    page_rec["status"] = "failed"
                    page_rec["error"] = f"Upload failed: {e}"
                    state.set_page(pid, page_rec)

    summary = state.summary()
    click.echo(f"\nUpload complete: {summary}")
    sys.exit(determine_exit_code(state))


# -- migrate (full pipeline) ---------------------------------------------


@cli.command()
@add_options(COMMON_OPTIONS)
@add_options(SCOPE_OPTIONS)
@add_options(ATTACHMENT_OPTIONS)
@click.option("--target-parent", default="MigratedPages",
              help="Top folder in the collective. Use '' to import at the collective "
                   "base (the space homepage becomes the landing page).")
def migrate(space_key, pages, all_spaces, exclude_images, exclude_attachments,
            target_parent, dry_run, debug, log_file):
    """Run full migration pipeline: export → convert → upload."""
    setup_logging(debug, log_file)

    # --- Export phase ---
    click.echo("=" * 60)
    click.echo("Phase 1: Export")
    click.echo("=" * 60)

    require_env("CONFLUENCE_BASE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN")

    conf_client = ConfluenceClient(
        os.getenv("CONFLUENCE_BASE_URL"),
        os.getenv("CONFLUENCE_USERNAME"),
        os.getenv("CONFLUENCE_API_TOKEN"),
    )

    try:
        conf_client.verify_auth()
    except click.ClickException:
        sys.exit(EXIT_AUTH)
    except Exception as e:
        log.error("Authentication failed: %s", e)
        sys.exit(EXIT_AUTH)

    state = MigrationState().load()

    # Resolve scope
    space_ids_keys = []
    if pages:
        page_ids = [p.strip() for p in pages.split(",")]
        fetched = conf_client.get_pages_by_ids(page_ids)
        # Group by space
        seen = set()
        for pg in fetched:
            si = str(pg.get("spaceId", ""))
            if si not in seen:
                seen.add(si)
                try:
                    sd = conf_client._get_json(f"/wiki/api/v2/spaces/{si}")
                    space_ids_keys.append((si, sd.get("key", si)))
                except Exception:
                    space_ids_keys.append((si, si))
        all_pages = fetched
    elif all_spaces:
        spaces = conf_client.get_all_spaces()
        space_ids_keys = [(s["id"], s["key"]) for s in spaces]
        all_pages = None
    elif space_key:
        space = conf_client.get_space_by_key(space_key)
        space_ids_keys = [(space["id"], space["key"])]
        all_pages = None
    else:
        click.echo("Error: Specify --space, --pages, or --all-spaces.")
        sys.exit(EXIT_CONFIG)

    # Export each space
    for space_id, sk in space_ids_keys:
        log.info("Exporting space: %s", sk)

        if all_pages is None:
            fetched_pages = conf_client.get_space_pages(space_id)
        else:
            fetched_pages = [p for p in all_pages if str(p.get("spaceId")) == str(space_id)]

        if dry_run:
            click.echo(f"[DRY RUN] Space: {sk} — {len(fetched_pages)} page(s)")
            for pg in fetched_pages:
                click.echo(f"  Page: {pg.get('title', '?')} (ID: {pg['id']})")
            continue

        page_ids_set = {str(p["id"]) for p in fetched_pages}
        parent_ids_set = {str(p.get("parentId")) for p in fetched_pages if p.get("parentId") and str(p.get("parentId")) in page_ids_set}
        export_base = Path("export_data") / sk

        for pg in fetched_pages:
            page_id = str(pg["id"])
            title = pg.get("title", "untitled")
            parent_id = pg.get("parentId")
            has_children = page_id in parent_ids_set

            # Skip if already exported or beyond
            existing = state.get_page(page_id)
            if existing and existing.get("status") in ("exported", "converted", "uploaded"):
                log.info("Skipping already processed page: %s", title)
                continue

            log.info("Exporting page: %s (ID: %s)", title, page_id)

            try:
                body_html = ""
                body = pg.get("body", {})
                if isinstance(body, dict):
                    body_html = body.get("export_view", {}).get("value", "")
                elif isinstance(body, str):
                    body_html = body

                comments = []
                try:
                    comments = conf_client.get_page_comments(page_id)
                except Exception as e:
                    log.warning("Failed to fetch comments for %s: %s", page_id, e)

                attachments = []
                if not exclude_attachments:
                    try:
                        att_list = conf_client.get_page_attachments(page_id)
                        att_dir = export_base / "attachments" / page_id
                        for att in att_list:
                            att_title = att.get("title", "")
                            image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}
                            ext = Path(att_title).suffix.lower()
                            if exclude_images and ext in image_exts:
                                continue
                            att_id = att.get("id")
                            fallback = att.get("downloadLink") or att.get("_links", {}).get("download")
                            if att_id or fallback:
                                try:
                                    data = conf_client.download_attachment(page_id, att_id, fallback_url=fallback)
                                    att_dir.mkdir(parents=True, exist_ok=True)
                                    (att_dir / att_title).write_bytes(data)
                                    attachments.append({"title": att_title, "size": len(data), "mediaType": att.get("mediaType", "")})
                                except Exception as e:
                                    log.warning("Failed to download %s: %s", att_title, e)
                                    attachments.append({"title": att_title, "error": str(e), "mediaType": att.get("mediaType", "")})
                    except Exception as e:
                        log.warning("Failed to fetch attachments for %s: %s", page_id, e)

                page_data = {
                    "page_id": page_id,
                    "title": title,
                    "space_key": sk,
                    "parent_id": str(parent_id) if parent_id else None,
                    "has_children": has_children,
                    "body": body_html,
                    "comments": comments,
                    "attachments": attachments,
                }
                pages_dir = export_base / "pages"
                pages_dir.mkdir(parents=True, exist_ok=True)
                export_path = pages_dir / f"{page_id}.json"
                export_path.write_text(json.dumps(page_data, indent=2, ensure_ascii=False), encoding="utf-8")

                record = MigrationState.new_page_record(page_id, title, sk, parent_id, has_children)
                record["status"] = "exported"
                record["export_path"] = str(export_path)
                record["attachments"] = attachments
                record["comments"] = [{"id": c.get("id")} for c in comments]
                state.set_page(page_id, record)
                log.info("Exported: %s", title)

            except Exception as e:
                log.error("Failed to export page %s: %s", page_id, e)
                record = MigrationState.new_page_record(page_id, title, sk, parent_id, has_children)
                record["status"] = "failed"
                record["error"] = str(e)
                state.set_page(page_id, record)

    # Check if export produced anything
    exported_count = len(state.get_pages_by_status("exported"))
    if exported_count == 0 and not dry_run:
        summary = state.summary()
        if summary.get("failed", 0) == sum(summary.values()):
            click.echo("Export failed for all pages.")
            sys.exit(EXIT_FAILURE)

    if dry_run:
        click.echo("\n[DRY RUN] Pipeline preview complete.")
        sys.exit(EXIT_SUCCESS)

    # --- Convert phase ---
    click.echo("\n" + "=" * 60)
    click.echo("Phase 2: Convert")
    click.echo("=" * 60)

    converter = Converter(exclude_images=exclude_images, exclude_attachments=exclude_attachments,
                          strip_patterns=strip_patterns_from_env())
    exported = state.get_pages_by_status("exported")
    tree = converter.build_output_tree(state)
    converter.set_link_map({pid: info["path"] for pid, info in tree.items()})

    for pid, page_rec in exported.items():
        title = page_rec.get("title", "?")
        log.info("Converting: %s", title)

        try:
            export_path = page_rec.get("export_path")
            if not export_path or not Path(export_path).exists():
                raise FileNotFoundError(f"Export file not found: {export_path}")

            page_data = json.loads(Path(export_path).read_text(encoding="utf-8"))
            path_info = tree.get(pid)
            if not path_info:
                log.warning("No output path for page %s, skipping", pid)
                continue

            sk = page_rec.get("space_key", "default")
            convert_base = Path("convert_data") / sk
            output_path = convert_base / path_info["path"]
            output_dir = convert_base / path_info["dir"] if path_info["dir"] else convert_base

            md = converter.convert_page(page_data, current_page_id=pid)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(md, encoding="utf-8")

            att_src = Path("export_data") / sk / "attachments" / pid
            if att_src.exists():
                converter.copy_attachments(page_data, att_src, output_dir)

            page_rec["status"] = "converted"
            page_rec["convert_path"] = str(output_path)
            state.set_page(pid, page_rec)
            log.info("Converted: %s → %s", title, path_info["path"])

        except Exception as e:
            log.error("Failed to convert page %s: %s", pid, e)
            page_rec["status"] = "failed"
            page_rec["error"] = str(e)
            state.set_page(pid, page_rec)

    # --- Upload phase ---
    click.echo("\n" + "=" * 60)
    click.echo("Phase 3: Upload")
    click.echo("=" * 60)

    require_env("NEXTCLOUD_URL", "NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD", "NEXTCLOUD_COLLECTIVE")

    nc = NextcloudClient(
        os.getenv("NEXTCLOUD_URL"),
        os.getenv("NEXTCLOUD_USERNAME"),
        os.getenv("NEXTCLOUD_PASSWORD"),
        os.getenv("NEXTCLOUD_COLLECTIVE"),
    )

    try:
        nc.verify_connection()
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Nextcloud connection failed: {e}")

    nc.mkdir_p(target_parent)

    converted = state.get_pages_by_status("converted")
    page_routes = build_page_routes(state, target_parent)
    for pid, page_rec in converted.items():
        sk = page_rec.get("space_key", "default")
        convert_base = Path("convert_data") / sk
        convert_path = page_rec.get("convert_path")

        if not convert_path or not Path(convert_path).exists():
            log.warning("Convert path not found for %s, skipping upload", pid)
            continue

        convert_file = Path(convert_path)
        relative = convert_file.relative_to(convert_base)
        remote_path = f"{target_parent}/{relative.as_posix()}"

        parent_dirs = relative.parent.as_posix()
        if parent_dirs and parent_dirs != ".":
            nc.mkdir_p(f"{target_parent}/{parent_dirs}")

        try:
            # Resolve internal page-link placeholders (relative to this page)
            content = resolve_page_links(convert_file.read_text(encoding="utf-8"),
                                         page_routes.get(pid, []), page_routes, nc.collective_segment)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                nc.upload_file(tmp_path, remote_path)
            finally:
                os.unlink(tmp_path)

            # Also upload attachments in the same directory
            output_dir = convert_file.parent
            for f in output_dir.iterdir():
                if f.is_file() and f != convert_file and f.suffix != ".md":
                    f_relative = f.relative_to(convert_base)
                    f_remote = f"{target_parent}/{f_relative.as_posix()}"
                    nc.upload_file(str(f), f_remote)
                    log.debug("Uploaded attachment: %s", f_remote)

            page_rec["status"] = "uploaded"
            page_rec["upload_path"] = remote_path
            state.set_page(pid, page_rec)
            log.info("Uploaded: %s → %s", page_rec["title"], remote_path)

        except Exception as e:
            log.error("Failed to upload %s: %s", pid, e)
            page_rec["status"] = "failed"
            page_rec["error"] = f"Upload failed: {e}"
            state.set_page(pid, page_rec)

    # Final summary
    summary = state.summary()
    click.echo("\n" + "=" * 60)
    click.echo("Migration complete")
    click.echo("=" * 60)
    click.echo(f"Results: {summary}")

    sys.exit(determine_exit_code(state))


# -- status ---------------------------------------------------------------


@cli.command()
@add_options(COMMON_OPTIONS)
def status(dry_run, debug, log_file):
    """Show migration progress."""
    setup_logging(debug, log_file)

    state = MigrationState().load()

    if not state.pages:
        click.echo("No migration state found. Run 'export' first.")
        sys.exit(EXIT_SUCCESS)

    summary = state.summary()
    total = sum(summary.values())

    click.echo("\nMigration Status")
    click.echo("=" * 40)
    for status_name in ("pending", "exported", "converted", "uploaded", "failed"):
        count = summary.get(status_name, 0)
        pct = (count / total * 100) if total else 0
        bar = "#" * int(pct / 5)
        click.echo(f"  {status_name:<12} {count:>4}  {pct:5.1f}%  {bar}")
    click.echo(f"  {'total':<12} {total:>4}")

    # Show failed pages
    failed = state.get_pages_by_status("failed")
    if failed:
        click.echo("\nFailed pages:")
        for pid, p in failed.items():
            click.echo(f"  [{pid}] {p['title']}: {p.get('error', 'unknown error')}")

    sys.exit(EXIT_SUCCESS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
