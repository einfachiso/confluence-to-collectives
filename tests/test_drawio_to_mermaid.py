"""Tests for the FlowForge-based drawio_to_mermaid converter."""

import json

import pytest

pytest.importorskip("flowforge", reason="FlowForge not installed (pip install ./tools/FlowForge)")

import drawio_to_mermaid as d2m


SWIMLANE = """<mxfile><diagram name="Page-1"><mxGraphModel><root>
  <mxCell id="0"/>
  <mxCell id="1" parent="0"/>
  <mxCell id="lane" value="Role A" style="swimlane" vertex="1" parent="1"/>
  <mxCell id="a" value="Start &amp; go" style="rounded=1" vertex="1" parent="lane"/>
  <mxCell id="b" value="Decide&lt;br&gt;now?" style="rhombus" vertex="1" parent="lane"/>
  <mxCell id="c" value="End" vertex="1" parent="1"/>
  <mxCell id="e1" value="yes" edge="1" parent="1" source="a" target="b"/>
  <mxCell id="e2" edge="1" parent="1" source="b" target="c"/>
</root></mxGraphModel></diagram></mxfile>"""

EMPTY = ("<mxfile><diagram><mxGraphModel><root>"
         "<mxCell id='0'/><mxCell id='1' parent='0'/>"
         "</root></mxGraphModel></diagram></mxfile>")


class TestCleanLabel:
    def test_strips_html_keeps_br(self):
        assert d2m.clean_label("Line<br/>two") == "Line<br/>two"
        assert d2m.clean_label('A<br style="x">B') == "A<br/>B"

    def test_unescapes_entities(self):
        assert d2m.clean_label("a &amp; b &nbsp;c") == "a & b c"

    def test_neutralises_quotes_and_pipes(self):
        out = d2m.clean_label('say "hi" | bye')
        assert '"' not in out and "|" not in out

    def test_empty_becomes_placeholder(self):
        assert d2m.clean_label("") == "?"

    def test_sid_is_mermaid_safe(self):
        assert d2m._sid("vOra-1") == "nvOra_1"
        assert d2m._sid("a-b-c").replace("n", "", 1).find("-") == -1


class TestConvert:
    def test_swimlane_to_subgraph(self, tmp_path):
        p = tmp_path / "Flow.drawio"
        p.write_text(SWIMLANE, encoding="utf-8")
        mm = d2m.convert_file(p)
        assert mm.startswith("flowchart TD")
        assert 'subgraph nlane["Role A"]' in mm
        assert "direction TB" in mm   # lanes flow top-to-bottom, not side-by-side
        assert '("Start & go")' in mm          # rounded → stadium, entity unescaped
        assert '{"Decide<br/>now?"}' in mm     # rhombus → decision, <br/> kept
        assert "-->|yes|" in mm                # labelled edge

    def test_no_duplicate_subgraph(self, tmp_path):
        """FlowForge's own emitter repeats nested lanes; ours must not."""
        nested = """<mxfile><diagram><mxGraphModel><root>
          <mxCell id="0"/><mxCell id="1" parent="0"/>
          <mxCell id="pool" value="Pool" style="swimlane" vertex="1" parent="1"/>
          <mxCell id="lane" value="Lane" style="swimlane" vertex="1" parent="pool"/>
          <mxCell id="t" value="Task" vertex="1" parent="lane"/>
        </root></mxGraphModel></diagram></mxfile>"""
        p = tmp_path / "N.drawio"
        p.write_text(nested, encoding="utf-8")
        mm = d2m.convert_file(p)
        assert mm.count('subgraph nlane["Lane"]') == 1
        assert mm.count("Task") == 1

    def test_empty_returns_none(self, tmp_path):
        p = tmp_path / "Empty.drawio"
        p.write_text(EMPTY, encoding="utf-8")
        assert d2m.convert_file(p) is None

    def test_userobject_node_and_crosslane_edge_preserved(self, tmp_path):
        """Nodes wrapped in <UserObject> (hyperlinked cells) and the edges that
        touch them — incl. cross-swimlane routes — must survive."""
        xml = """<mxfile><diagram><mxGraphModel><root>
          <mxCell id="0"/><mxCell id="1" parent="0"/>
          <mxCell id="laneA" value="Lane A" style="swimlane" vertex="1" parent="1"/>
          <mxCell id="laneB" value="Lane B" style="swimlane" vertex="1" parent="1"/>
          <UserObject label="Open" id="open"><mxCell style="rounded=0" vertex="1" parent="laneA"><mxGeometry/></mxCell></UserObject>
          <UserObject label="Investigate" id="inv"><mxCell style="rounded=0" vertex="1" parent="laneB"><mxGeometry/></mxCell></UserObject>
          <mxCell id="e" edge="1" parent="1" source="open" target="inv"/>
        </root></mxGraphModel></diagram></mxfile>"""
        p = tmp_path / "U.drawio"
        p.write_text(xml, encoding="utf-8")
        mm = d2m.convert_file(p)
        assert 'nopen["Open"]' in mm and 'ninv["Investigate"]' in mm
        assert "nopen --> ninv" in mm          # cross-lane edge preserved

    def test_edgelabel_reattached_not_floating(self, tmp_path):
        xml = """<mxfile><diagram><mxGraphModel><root>
          <mxCell id="0"/><mxCell id="1" parent="0"/>
          <mxCell id="a" value="A" vertex="1" parent="1"/>
          <mxCell id="b" value="B" vertex="1" parent="1"/>
          <mxCell id="e" edge="1" parent="1" source="a" target="b"/>
          <mxCell id="lbl" value="why" style="edgeLabel" vertex="1" parent="e"/>
        </root></mxGraphModel></diagram></mxfile>"""
        p = tmp_path / "L.drawio"
        p.write_text(xml, encoding="utf-8")
        mm = d2m.convert_file(p)
        assert "na -->|why| nb" in mm
        assert '["why"]' not in mm              # not emitted as a floating node


def _make_export(tmp_path, page_id, diagrams):
    """Build a minimal export dir: pages/<id>.json + diagrams/<id>/<n>.drawio.
    `diagrams` = list of (diagram_name, drawio_xml)."""
    (tmp_path / "pages").mkdir(exist_ok=True)
    manifest = []
    for i, (name, xml) in enumerate(diagrams):
        rel = f"diagrams/{page_id}/{i}.drawio"
        (tmp_path / "diagrams" / page_id).mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(xml, encoding="utf-8")
        manifest.append({"diagram_name": name, "source": rel})
    (tmp_path / "pages" / f"{page_id}.json").write_text(
        json.dumps({"page_id": page_id, "diagrams": manifest}), encoding="utf-8")


class TestBuildMap:
    def test_key_is_diagram_name_based(self, tmp_path):
        # source file name differs from diagramName → key uses diagramName
        _make_export(tmp_path, "138", [("Vorfallmanagement und CAPA", SWIMLANE)])
        mapping = d2m.build_map(tmp_path)
        assert "138/Vorfallmanagement und CAPA.png" in mapping
        assert mapping["138/Vorfallmanagement und CAPA.png"].startswith("flowchart TD")

    def test_empty_diagram_skipped(self, tmp_path):
        _make_export(tmp_path, "1", [("Leer", EMPTY)])
        assert d2m.build_map(tmp_path) == {}

    def test_same_name_different_pages_no_collision(self, tmp_path):
        _make_export(tmp_path, "100", [("Untitled Diagram", SWIMLANE)])
        _make_export(tmp_path, "200", [("Untitled Diagram", SWIMLANE)])
        mapping = d2m.build_map(tmp_path)
        assert "100/Untitled Diagram.png" in mapping
        assert "200/Untitled Diagram.png" in mapping

    def test_diagram_key_helper(self):
        assert d2m.diagram_key("138", "Vorfallmanagement und CAPA") == "138/Vorfallmanagement und CAPA.png"
