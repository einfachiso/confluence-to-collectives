#!/usr/bin/env python3
"""draw.io → mermaid map builder (FlowForge-based).

Produces ``mermaid-map.json`` — ``{image_filename: mermaid_text}`` — consumed by
migrate.py's weber transform (``WCEXTEND_MERMAID_MAP``). The key is the *rendered
image* filename referenced in the exported page HTML, so a ``Foo.drawio`` source
maps to key ``Foo.drawio.png`` and an extension-less source ``Bar`` to ``Bar.png``.

Conversion uses **FlowForge** (https://github.com/genkinsforge/FlowForge) to parse
the .drawio — its decompression and group/swimlane detection are solid — but
replaces FlowForge's emitter, which has bugs for these ISMS swimlane diagrams:
emits each nested lane twice (invalid mermaid) and leaks raw HTML (`<br style…>`,
`&nbsp;`) into labels. The corrected emitter here emits top-level subgraphs only,
sanitises ids, and strips HTML from labels (keeping `<br/>` line breaks).

Kept SEPARATE from migrate.py (which stays dependency-light). Requires FlowForge:
    pip install ./tools/FlowForge      # vendored under the workspace
    # or: pip install . from a clone of the FlowForge repo

Usage:
    python drawio_to_mermaid.py --input-dir export_data/<SPACE>/attachments --out mermaid-map.json
"""

import argparse
import json
import logging
import re
from pathlib import Path

try:
    from flowforge import FlowForgeConverter
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        "FlowForge is required. Install it with:\n"
        "    pip install ./tools/FlowForge\n"
        "or from https://github.com/genkinsforge/FlowForge"
    ) from exc

log = logging.getLogger("drawio_to_mermaid")

_HTML_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
                  "&quot;": "'", "&#39;": "'"}


def clean_label(label):
    """Make an mxCell value safe for a quoted mermaid label: keep `<br/>` line
    breaks, strip all other HTML, unescape entities, and neutralise the `"`/`|`
    characters that would break `["…"]` / `-->|…|` syntax."""
    if not label:
        return "?"
    s = re.sub(r"<br\s*[^>]*?>", "\x00", label, flags=re.I)   # any <br…> → sentinel
    s = re.sub(r"<[^>]+>", "", s)                              # strip remaining tags
    for ent, ch in _HTML_ENTITIES.items():
        s = s.replace(ent, ch)
    s = s.replace('"', "'").replace("|", "/")
    s = re.sub(r"[ \t]+", " ", s)
    s = s.replace("\x00", "<br/>").strip()
    return s or "?"


def _sid(cell_id):
    """A mermaid-safe node/subgraph id (draw.io ids contain hyphens)."""
    return "n" + re.sub(r"\W", "_", cell_id)


class _MermaidEmitter(FlowForgeConverter):
    """FlowForge with a corrected Mermaid emitter (see module docstring)."""

    def _parse_xml(self, xml_data):
        root = super()._parse_xml(xml_data)
        if root is not None:
            self._flatten_wrappers(root)
        return root

    @staticmethod
    def _flatten_wrappers(root):
        """draw.io wraps any cell carrying a hyperlink/custom attributes in a
        <UserObject>/<object> element: the id and label sit on the wrapper while
        the geometry/style/connections sit on an inner <mxCell>. FlowForge only
        scans <mxCell>, so these nodes — and every edge touching them, including
        cross-swimlane routes — get dropped. Promote each wrapper's inner cell,
        copying the wrapper's id and label onto it, so they survive parsing."""
        parent_map = {child: parent for parent in root.iter() for child in parent}
        for wrapper, parent in list(parent_map.items()):
            if wrapper.tag.split("}")[-1] not in ("UserObject", "object"):
                continue
            cell = wrapper.find("mxCell")
            if cell is None:
                continue
            cell.set("id", wrapper.get("id", cell.get("id", "")))
            label = wrapper.get("label")
            if label is not None and not cell.get("value"):
                cell.set("value", label)
            parent[list(parent).index(wrapper)] = cell

    def _format_node_safe(self, node):
        label = clean_label(node.get("label", ""))
        nid = _sid(node["id"])
        # Inspect the raw style string — draw.io encodes shapes in many ways
        # (bare `rhombus`, `shape=mxgraph.bpmn.gateway2;perimeter=rhombusPerimeter`,
        # `shape=mxgraph.flowchart.terminator`, …), so substring checks are robust.
        raw = (node.get("style", "") or "").lower()
        if "rhombus" in raw or "gateway" in raw:
            return f'{nid}{{"{label}"}}'               # decision
        if "ellipse" in raw or "mxgraph.flowchart.start" in raw:
            return f'{nid}(("{label}"))'               # circle (start/end event)
        if "rounded=1" in raw or "terminator" in raw or "stadium" in raw:
            return f'{nid}("{label}")'                 # stadium
        return f'{nid}["{label}"]'                     # process / default

    def _emit_mermaid(self, diagram, direction="TD", diagram_type="flowchart"):
        groups = diagram.get("groups", {})
        node_map = self.node_map
        lines = [f"flowchart {direction}"]
        emitted = set()

        # `edgeLabel` cells are an edge's caption (parent = the edge id), not real
        # nodes — collect them as edge labels and suppress them as standalone nodes.
        edge_labels = {}
        for n in diagram.get("nodes", []):
            if "edgelabel" in (n.get("style", "") or "").lower():
                lab = clean_label(n.get("label", ""))
                if lab and lab != "?":
                    edge_labels[n.get("parent")] = lab
                emitted.add(n["id"])

        def emit_group(group_id, level):
            group = groups[group_id]
            indent = "    " * level
            lines.append(f'{indent}subgraph {_sid(group_id)}["{clean_label(group["label"])}"]')
            # Force vertical flow inside each lane/pool; without this mermaid lays
            # swimlane subgraphs out side-by-side (the diagram reads left-to-right).
            lines.append(f"{indent}    direction TB")
            for child in group.get("children", []):
                cid = child["id"]
                if cid in groups:
                    emit_group(cid, level + 1)
                else:
                    lines.append(indent + "    " + self._format_node_safe(child))
                    emitted.add(cid)
            lines.append(indent + "end")
            emitted.add(group_id)

        # Top-level groups only; nested lanes are handled by the recursion.
        for group_id in groups:
            parent = node_map.get(group_id, {}).get("parent")
            if parent not in groups:
                emit_group(group_id, 0)

        for node in diagram.get("nodes", []):
            if node["id"] not in emitted and node["id"] not in groups:
                lines.append(self._format_node_safe(node))
                emitted.add(node["id"])

        for edge in diagram.get("edges", []):
            if edge["source"] in node_map and edge["target"] in node_map:
                src, tgt = _sid(edge["source"]), _sid(edge["target"])
                label = clean_label(edge["label"]) if edge.get("label") else edge_labels.get(edge["id"], "")
                lines.append(f"{src} -->|{label}| {tgt}" if label else f"{src} --> {tgt}")

        return "\n".join(lines)


def convert_file(path, direction="TD"):
    """Convert one .drawio/mxfile to mermaid text, or None if empty/unconvertible."""
    converter = _MermaidEmitter(log_level=logging.CRITICAL, strict_mode=False)
    try:
        content = converter.load_file(str(path))
        mermaid = converter.convert(content, diagram_index=0, direction=direction)
    except Exception as e:  # FlowForge raises on malformed input in some paths
        log.warning("FlowForge failed on %s: %s", path, e)
        return None
    body = [ln for ln in mermaid.splitlines() if ln.strip()]
    if len(body) <= 1:
        return None  # only the `flowchart TD` header — empty diagram
    return mermaid


# --------------------------------------------------------------------------
# Scanning / CLI
# --------------------------------------------------------------------------

def _looks_like_mxfile(path):
    """True if a file (used for extension-less sources) sniffs as mxGraph XML."""
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:200].lstrip()
    except OSError:
        return False
    return head.startswith("<mxfile") or head.startswith("<mxGraphModel")


def find_source_files(input_dir):
    """draw.io source files under input_dir: ``*.drawio`` plus extension-less
    mxGraph sources. Skips ``~…`` autosave and ``.tmp`` files."""
    root = Path(input_dir)
    found = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("~"):
            continue
        if p.suffix.lower() == ".tmp":
            continue
        if p.suffix.lower() == ".drawio":
            found.append(p)
        elif p.suffix == "" and _looks_like_mxfile(p):
            found.append(p)
    return found


def image_key(path):
    """Page-scoped rendered-image key for a source file: ``<page-id>/<name>.png``.

    Attachments live in ``…/attachments/<page-id>/<name>.drawio``, so the parent
    directory name is the Confluence page id. The same diagram filename recurs
    across pages with different content, so the page id is needed to disambiguate;
    migrate.py builds the identical key from the page's `<img>` src URL.
    """
    return f"{path.parent.name}/{path.name}.png"


def build_map(input_dir, direction="TD"):
    """Return {<page-id>/<image_filename>: mermaid_text} for every convertible source."""
    result = {}
    for src in find_source_files(input_dir):
        mermaid = convert_file(src, direction=direction)
        key = image_key(src)
        if mermaid:
            result[key] = mermaid
            log.info("Converted %s -> %s", src.name, key)
        else:
            log.warning("Skipped (empty/unconvertible): %s", src.name)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Convert draw.io diagrams to a mermaid JSON map (FlowForge).")
    parser.add_argument("--input-dir", default=".",
                        help="Directory to scan recursively for .drawio sources (default: .).")
    parser.add_argument("--out", default="mermaid-map.json",
                        help="Output JSON map path (default: mermaid-map.json).")
    parser.add_argument("--direction", default="TD",
                        help="Mermaid flow direction: TD or LR (default: TD).")
    parser.add_argument("--debug", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s")

    mapping = build_map(args.input_dir, direction=args.direction)
    Path(args.out).write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote %d diagram(s) to %s", len(mapping), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
