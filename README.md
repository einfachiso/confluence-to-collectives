# Confluence Cloud → Nextcloud Collectives Migration Tool

CLI tool to migrate pages and attachments from Confluence Cloud to Nextcloud Collectives. Converts Confluence HTML to clean Markdown with preserved hierarchy, images, tables, comments, and attachments.

## How It Works

The tool runs a three-phase pipeline:

1. **Export** — Fetches pages via the Confluence Cloud REST API v2 (`body-format=export_view` for clean HTML), downloads attachments, and stores everything locally in `export_data/`.
2. **Convert** — Transforms Confluence HTML to Markdown using BeautifulSoup (preprocessing) + html2text (conversion). Builds the output directory tree matching page hierarchy. Copies attachments alongside their pages. Links to other migrated pages are tagged with a `cpage:<id>` placeholder for the upload phase to resolve, and diagram/image macro previews are lifted out so they render as static images.
3. **Upload** — Resolves the target collective via the Collectives OCS API, then pushes the converted Markdown files and attachments to Nextcloud Collectives via WebDAV (`MKCOL` for directories, `PUT` for files). The `cpage:` placeholders are resolved per source page into **relative** links between the output pages, so they survive renaming the collective or moving its pages.

Each phase tracks per-page status in `.migration-state.json`, enabling resumable migrations and independent re-runs of any phase.

### Internal Links & Diagrams

- **Internal page links** between migrated pages are preserved as relative links between the output pages. Relative links are collective-agnostic, so they keep working if the collective is renamed or its pages are moved to another collective. The collective's root/landing page is the one exception (it has no trailing URL segment to be relative to), and uses an absolute `/apps/collectives/<slug>-<id>/...` link instead.
- **Diagrams and images** from draw.io / Gliffy / etc. macros render as static images: the macro's rendered `<img>` preview (embedded by `export_view`) is lifted out before the macro itself is dropped. SVG previews stay demoted to an attachment link, since Collectives blocks SVG rendering.

### Modern / Localized / Shared Collectives

`verify_connection` resolves the target collective via the Collectives OCS API, matching `NEXTCLOUD_COLLECTIVE` by display name, slug, or `<slug>-<id>`. This handles:

- The hidden, **localized** storage folder — a German Nextcloud stores collectives under `.Kollektive` rather than `.Collectives` (read from a page's `collectivePath`).
- The WebDAV folder being the collective's display name, while the link URL segment is `<slug>-<id>`.

If the OCS API is unavailable, it falls back to probing `.Collectives` then `Collectives`. Attachment links use the open-by-id `.../f/<id>` URL so they resolve even from the hidden storage folder.

### Page Hierarchy Mapping

| Confluence Structure | Collectives Output |
|---|---|
| Space homepage | `Readme.md` (root) |
| Page with children | `{title}/Readme.md` |
| Leaf page (no children) | `{title}.md` |

Attachments are placed flat alongside their page's Markdown file in the same directory. Image references in Markdown use relative paths (`![alt](image.png)`).

## Prerequisites

- Python 3.11+
- Confluence Cloud account with API access
- Nextcloud instance with the Collectives app installed

## Getting API Tokens

### Confluence Cloud API Token

1. Go to [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a label (e.g., "Migration Tool") and click **Create**
4. Copy the token — you won't be able to see it again

The API uses Basic Auth with your email as the username and the API token as the password.

### Nextcloud Credentials

If your Nextcloud instance uses two-factor authentication:

1. Go to **Settings → Security → Devices & sessions**
2. Enter a name for the app password (e.g., "Migration Tool")
3. Click **Create new app password**
4. Copy the generated password

Otherwise, use your regular Nextcloud username and password.

## Installation

```bash
git clone <repository-url>
cd confluence-to-collectives
python -m venv venv
source venv/bin/activate    # Linux/macOS
# venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

```env
# Confluence Cloud
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net
CONFLUENCE_USERNAME=your-email@example.com
CONFLUENCE_API_TOKEN=your-api-token

# Nextcloud
NEXTCLOUD_URL=https://your-nextcloud.com
NEXTCLOUD_USERNAME=your-username
NEXTCLOUD_PASSWORD=your-password-or-app-password
NEXTCLOUD_COLLECTIVE=your-collective-name
```

## Usage

### Full Migration (recommended)

```bash
# Migrate an entire Confluence space
python migrate.py migrate --space SPACE_KEY

# Migrate specific pages
python migrate.py migrate --pages 12345,67890

# Preview without making changes
python migrate.py migrate --space SPACE_KEY --dry-run

# With debug logging
python migrate.py migrate --space SPACE_KEY --debug --log-file migration.log

# Skip images or all attachments
python migrate.py migrate --space SPACE_KEY --exclude-images
python migrate.py migrate --space SPACE_KEY --exclude-attachments

# Specify target parent page in Collectives
python migrate.py migrate --space SPACE_KEY --target-parent "Imported from Confluence"

# Import directly at the collective base (space homepage becomes the landing page)
python migrate.py migrate --space SPACE_KEY --target-parent ''
```

### Individual Phases

Run each phase independently for more control:

```bash
# Phase 1: Export from Confluence
python migrate.py export --space SPACE_KEY

# Phase 2: Convert HTML to Markdown
python migrate.py convert

# Phase 3: Upload to Nextcloud
python migrate.py upload --target-parent MigratedPages

# Check progress at any point
python migrate.py status
```

### Space Templates

Confluence space page-templates can be imported as native Collectives page
templates (they land in the collective's hidden `.templates/` folder and show up
in the New-page template picker):

```bash
# Import a space's templates (standalone)
python migrate.py import-templates --space SPACE_KEY

# Preview without writing (needs only Confluence credentials)
python migrate.py import-templates --space SPACE_KEY --dry-run

# Or fold templates into a full migration run
python migrate.py migrate --space SPACE_KEY --include-templates
```

Template bodies come from Confluence in *storage* format (templates have no
rendered `export_view`). Template variables such as `<at:var at:name="Owner"/>`
are rendered as `{{Owner}}` placeholders. See **Limitations** for macro handling.

### Command Options

| Option | Commands | Description |
|---|---|---|
| `--space KEY` | export, migrate | Confluence space key |
| `--pages ID1,ID2` | export, migrate | Specific page IDs (comma-separated) |
| `--all-spaces` | export, migrate | Migrate all accessible spaces |
| `--exclude-images` | export, convert, migrate | Skip image attachments |
| `--exclude-attachments` | export, convert, migrate | Skip all attachments |
| `--target-parent NAME` | upload, migrate | Top folder in the collective (default: `MigratedPages`). Use `''` to import at the collective base — the space homepage becomes the landing page |
| `--include-templates` | migrate | Also import the space's page-templates into the collective's `.templates/` folder (requires `--space`) |
| `--space KEY` | import-templates | Confluence space whose page-templates to import (required) |
| `--dry-run` | all | Preview actions without changes |
| `--debug` | all | Enable debug logging |
| `--log-file PATH` | all | Write logs to file |

## File Structure

```
export_data/{space_key}/          # Phase 1 output
  pages/{page_id}.json            # Page metadata + HTML body + comments
  attachments/{page_id}/{file}    # Downloaded attachment files

convert_data/{space_key}/         # Phase 2 output (uploaded in Phase 3)
  Readme.md                       # Space homepage
  ParentPage/
    Readme.md                     # Parent page content
    image.png                     # Parent's attachment
    ChildPage.md                  # Leaf child page
    child-image.png               # Child's attachment

.migration-state.json             # Per-page status tracking
```

## Content Conversion Details

| Confluence Element | Markdown Result |
|---|---|
| Page body | Clean Markdown |
| Tables (even with headings in cells) | Markdown tables |
| Images | `![alt](image.png)` with relative paths |
| Diagram macros (Draw.io, Gliffy, etc.) | Rendered preview lifted out as a static `![](…)` image |
| Internal page links | Relative links between the migrated pages (move/rename-safe) |
| Code blocks | Fenced code blocks with language hints |
| Info/warning/note/tip panels | Plain blockquotes (no duplicated `Info:`/`Note:` label) |
| User mentions | `@DisplayName` |
| Footer comments | `## Comments` section with author and date |
| Non-image attachments | `## Attachments` section with `-` bulleted links |
| Other unsupported macros (Jira, etc.) | HTML comment: `<!-- Unsupported macro: name -->` |
| Attachment management UI | Removed (tool generates its own section) |

## Limitations

- **Confluence Cloud only** — Confluence Server/Data Center API differences are not handled.
- **Footer comments only** — Inline comments (annotations on specific text) are not migrated; footer comments and their reply chains are fully supported.
- **No permission migration** — Page-level permissions from Confluence are not transferred.
- **Unsupported macros** — Diagram macros (Draw.io, Gliffy, etc.) render via their static image preview, but other third-party macro content (e.g. Jira) is replaced with HTML comments. SVG diagram previews are demoted to attachment links, since Collectives blocks SVG rendering.
- **Space templates** — Imported template bodies are storage-format XHTML (templates have no rendered `export_view`), so any macro inside a template is *unexpanded*: it is surfaced as a visible `[Unsupported macro: NAME]` marker and its body is dropped. Template variables become `{{Name}}` placeholders. A renamed template leaves its old file behind (uploads overwrite by path; they do not purge).
- **Sequential processing** — Pages are processed one at a time (no parallel downloads).
- **Filename length** — Titles are capped at 200 characters; special characters are stripped.

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | All pages migrated successfully |
| 1 | Partial failure (some pages failed, others succeeded) |
| 2 | Complete failure (all pages failed) |
| 3 | Configuration error (missing environment variables) |
| 4 | Authentication error |

## Development

```bash
# Install test dependencies
pip install pytest

# Run all tests
python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_converter.py -v

# Run specific test
python -m pytest tests/test_converter.py -k "test_macro"
```

### MCP Servers (Development Only)

The `.mcp.json` file configures two MCP servers for development and testing:

- **`mcp-atlassian`** — Query real Confluence pages, inspect hierarchies, fetch HTML for test fixtures
- **`nextcloud-mcp-server`** — Verify WebDAV uploads, check directory structures

These are development tools only — `migrate.py` has zero MCP dependency and uses direct HTTP requests.

## Changelog

### Unreleased

- Internal page links between migrated pages are preserved as relative links (survive renaming the collective or moving its pages); the collective's landing page uses an absolute `/apps/collectives/<slug>-<id>/...` link
- Diagram macros (Draw.io, Gliffy, etc.) now render as static images — the rendered preview is lifted out of the macro instead of being dropped
- Resolve the target collective via the Collectives OCS API: supports modern/localized/shared collectives (e.g. a German Nextcloud's `.Kollektive` storage folder), matches by name / slug / `<slug>-<id>`, and falls back to probing `.Collectives`/`Collectives`
- Attachment links use the open-by-id `.../f/<id>` URL so they resolve even from the hidden storage folder
- `--target-parent ''` imports directly at the collective base (the space homepage becomes the landing page) instead of nesting under a top folder
- Fix: attachment list `-` bullets are preserved; Info/Warning/Note/Tip panels become plain blockquotes without a duplicated `Info:`/`Note:` label

### 0.6.0

- Fix: SVG files are no longer embedded inline via `![](file.svg)` — Nextcloud Collectives blocks SVG rendering for security. SVGs now appear in the `## Attachments` section as clickable links instead (fixes #3)
- Fix: File uploads now include correct `Content-Type` headers (e.g. `image/svg+xml` for SVGs) — previously Nextcloud defaulted to `text/plain` for unrecognized types

### 0.5.0

- Fix: Confluence `<hr/>` tags are now stripped during preprocessing — they were converted to `---` which Nextcloud Collectives misinterpreted as YAML front matter, breaking table rendering at the start of pages (fixes #1)
- Fix: `load_dotenv(override=True)` ensures `.env` values take precedence over stale shell environment variables

### 0.4.0

- Fix: Nested comment replies are now fetched recursively via `/wiki/api/v2/footer-comments/{id}/children` — previously only top-level footer comments were exported, missing reply chains (fixes #2)

### 0.3.0

- Fix: Non-image files (`.mp4`, `.mov`, etc.) wrapped in `<img>` tags by Confluence are no longer treated as inline images — they appear in the Attachments section with proper Nextcloud file links instead
- Fix: Attachment links now use absolute Nextcloud file-ID URLs (`/apps/files/files/{id}?openfile=true`) instead of relative paths that Collectives misinterprets as page links
- Fix: Tables with `colspan` header cells no longer produce mismatched column counts that break Markdown rendering
- Fix: Filenames with spaces in attachment links are now URL-encoded

### 0.2.0

- Checkpoint: Added correct handling of attachments URLs logic + handling white spaces

### 0.1.0

- Initial commit — Confluence to Collectives migration pipeline
