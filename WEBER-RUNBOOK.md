# Weber-Migration — Ex-/Import-Runbook

Operativer Ablauf für die Migration der ISMS-Vorlagen aus Confluence Cloud in das
Weber-Nextcloud (Collectives). Dieser Ablauf ist **bewusst mehrstufig und nicht in
einem einzigen Skript** gekapselt — die draw.io→mermaid-Konvertierung ist ein
eigener Schritt (separates Tool + externe Abhängigkeit). Dieses Dokument hält den
Ablauf fest.

Gilt für Branch **`weber-migration`**.

---

## 1. Voraussetzungen (einmalig)

```powershell
# Tool-Setup (im Fork-Verzeichnis)
cd confluence-to-collectives
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# FlowForge (NUR für den Mermaid-Schritt benötigt; migrate.py selbst braucht es nicht)
#   Liegt unter ../tools/FlowForge (aus https://github.com/genkinsforge/FlowForge geklont)
pip install ../tools/FlowForge
```

### `.env` (gitignored, enthält Secrets + kundenspezifische Werte)

```ini
# Quelle: Confluence Cloud (einfachISO-Tenant)
CONFLUENCE_BASE_URL=https://einfachiso.atlassian.net
CONFLUENCE_USERNAME=<user>@einfachiso.de
CONFLUENCE_API_TOKEN=<api-token>

# Ziel: Weber-Nextcloud
NEXTCLOUD_URL=https://nextcloud.weber.digital
NEXTCLOUD_USERNAME=<user>
NEXTCLOUD_PASSWORD=<app-passwort>
NEXTCLOUD_COLLECTIVE=ISMS-ISO-27001

# einfachISO: Copyright-/Lizenz-Box entfernen
STRIP_CONTENT_PATTERNS=Alle Rechte vorbehalten|||Nutzungserlaubnis|||Alle Rechte sind vorbehalten

# weber (wcextend-SmartPicker). Leer = Transform aus.
WCEXTEND_DOKINFO_URL=https://nextcloud.weber.digital/index.php/apps/wcextend/smartpicker/wcextend-isms-dokinfo-prozess
WCEXTEND_DOKINFO_PATTERNS=Reviewzyklus|||Training erforderlich für
WCEXTEND_TOC_URL=https://nextcloud.weber.digital/index.php/apps/wcextend/smartpicker/wcextend-toc
WCEXTEND_MERMAID_MAP=mermaid-map.json
```

> Auth prüfen: `migrate.py` verifiziert den Confluence-Token automatisch
> (anonym = Fehler). Bei Nextcloud ein **App-Passwort** verwenden.

---

## 2. Ablauf (4 Schritte)

Variablen: `SPACE` = Confluence Space Key (z. B. `ISMS2022DE`).

```powershell
# 1) Export aus Confluence — lädt Seiten, Anhänge UND löst draw.io-Diagramme
#    über die echte Verknüpfung auf (siehe Abschnitt 4). Ablage: export_data/<SPACE>/
python migrate.py export --space <SPACE>

# 2) Mermaid-Map bauen (FlowForge). Liest export_data/<SPACE>/pages + diagrams,
#    erzeugt mermaid-map.json (Key = <page-id>/<diagramName>.png)
python drawio_to_mermaid.py --export-dir export_data/<SPACE> --out mermaid-map.json

# 3) Konvertieren (HTML→Markdown + weber-Transforms; liest mermaid-map.json automatisch)
python migrate.py convert

# 4) Hochladen ins Collective. --target-parent "" = Import auf Collective-Ebene
#    (Space-Homepage wird Landing-Page)
python migrate.py upload --target-parent ""
```

> **Wichtig:** Der Einzeiler `migrate.py migrate` (export→convert→upload) enthält den
> **Mermaid-Schritt nicht**. Immer die 4 Schritte einzeln fahren, sonst fallen
> Diagramme auf das statische PNG zurück.

---

## 3. Erneuter Import / Bereinigung

Vor jedem Re-Import **beide** Caches leeren, sonst bleiben veraltete Inhalte liegen:

```powershell
# a) Lokales Convert-Verzeichnis leeren (sonst werden alte/entfernte Anhänge mit-hochgeladen)
Remove-Item -Recurse -Force convert_data/<SPACE>

# b) Ziel-Collective leeren — den Inhalt unter .Collectives/<NAME> löschen
#    (Ordner '.templates' stehen lassen). Sonst bleiben alte Seiten mit alten Links liegen.
```

Danach Schritte 2–4 wiederholen (bei reiner Convert-Änderung) bzw. 1–4 (bei Quell-Änderung).
Lokales Komplett-Reset des Test-Nextclouds: `pwsh docker/setup.ps1 -Fresh` (nur Testumgebung).

---

## 4. Wie die draw.io→mermaid-Konvertierung funktioniert (Hintergrund)

draw.io-Diagramme werden **nicht über Dateinamen** zugeordnet (die sind unzuverlässig:
umbenannt, veraltet, teils korrupte Lokalkopien), sondern über die **maßgebliche
Verknüpfung** in Confluence:

```
Seite (Storage-Format)
 └─ <ac:structured-macro ac:name="drawio">
      ├─ diagramName    → die Seite bettet das Bild <diagramName>.png ein
      └─ custContentId  → Custom Content (Typ …diagramly:drawio-diagram)
            └─ pageId    → Ursprungsseite (kann abweichen, wenn das Diagramm wiederverwendet wird)
                 └─ Attachment mit Kommentar "draw.io Diagramm" = editierbare mxfile-Quelle
```

- **`migrate.py export`** wertet pro Seite das Storage-Makro aus, lädt die echte Quelle
  und legt sie unter `export_data/<SPACE>/diagrams/<page-id>/` ab; pro Seite steht ein
  `diagrams`-Manifest in `pages/<page-id>.json`.
- **`drawio_to_mermaid.py`** (FlowForge-basiert) konvertiert diese Quellen und schreibt
  `mermaid-map.json`, Key = `<page-id>/<diagramName>.png`. Korrekturen ggü. FlowForge:
  verschachtelte Lanes nur einmal, IDs bereinigt, HTML aus Labels entfernt (`<br/>`
  bleibt), `<UserObject>`/`<object>`-Wrapper aufgelöst (verlinkte Knoten + deren Kanten),
  edgeLabel-Zellen an die Kante gehängt, `direction TB` je Subgraph.
- **`migrate.py convert`** ersetzt das eingebettete `<img>` durch einen ```mermaid```-Block
  (Lookup per `<page-id>/<diagramName>.png`), entfernt draw.io-Anhänge aus dem Upload und
  fällt auf das statische PNG zurück, wenn keine Mermaid-Quelle existiert.

---

## 5. Verifikation nach dem Import

- Seitenzahl, Mermaid-Seiten und „keine draw.io-Artefakte" prüfen (PROPFIND/Skript).
- Liste aller Seiten mit Diagrammen + Collective-Links erzeugen (siehe
  `diagramm-seiten.md` im Workspace; Generator-Skript in der Projekt-Historie).
- Stichproben: Info-Panels = `::: …`-Callouts, TOC/Dokinfo = wcextend-Link-Preview,
  Diagramme = Mermaid.

---

## 6. Bekannte Einschränkungen

- **Mermaid-Layout = dagre.** Im Nextcloud-Text-Bundle ist **kein ELK** enthalten
  (nur der Name registriert), d. h. Swimlanes lassen sich nicht weiter ausrichten.
- Kanten, die im Original an Lane-Container/Wegpunkte andocken, fallen weg (kein
  Knoten-zu-Knoten) — Knoten, Labels, Lanes und Entscheidungen bleiben erhalten.
- Diagramme, die nur als Anhang vorliegen (nicht inline eingebettet), werden nicht
  konvertiert.
- Mehrere gleichnamige „draw.io Diagramm"-Quellen auf **einer** Ursprungsseite werden per
  Best-Effort (Titelabgleich) zugeordnet; sonst Warnung im Export-Log.
