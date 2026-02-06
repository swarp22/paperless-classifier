# Errata & Erkenntnisse – Paperless Claude Classifier

Laufende Sammlung von Korrekturen und Abweichungen gegenüber der Design-Dokumentation.

---

## E-001: Custom Field Select-Options API-Format (AP-00, 2025-02-06)

**Betrifft:** `paperless-claude-classifier-design_4.md`, `00-setup-custom-fields.md`

**Problem:** Die API-Beispiele nutzen einfache Strings für `select_options`:
```json
"select_options": ["Max", "Melanie", "Kilian"]
```

**Korrekt für Paperless-ngx v2.20.6:** Select-Options müssen als Objekte mit `label` übergeben werden. Die `id` wird serverseitig automatisch generiert:
```json
"select_options": [
  {"label": "Max"},
  {"label": "Melanie"},
  {"label": "Kilian"}
]
```

**Auswirkung:** POST auf `/api/custom_fields/` gibt 500er zurück, wenn das alte Format verwendet wird.

**Relevanz für Classifier-Code:** Beim Setzen von Custom Field Werten muss ggf. die interne `id` der Option verwendet werden, nicht der Label-String. Beim Lesen kommen Objekte mit `id` und `label` zurück.

---

## E-002: Tatsächliche Custom Field IDs (AP-00, 2025-02-06)

**Betrifft:** `00-setup-custom-fields.md`, Tabelle in Aufgabe 0.4

| Custom Field | ID | Typ | Optionen |
|---|---|---|---|
| Dokumenteverknüpfung | 1 | documentlink | — |
| Ordnerarchiv Paginierung | 2 | integer | — |
| Ordner Haus Ordnungszahl | 4 | integer | — |
| Ordner Haus Register | 5 | select | 1–11 (Haus-Kategorien) |
| zusammenhängender Vorgang | 6 | select | (dynamisch) |
| **Person** | **7** | select | Max, Melanie, Kilian |
| **ki_status** | **8** | select | classified, review, error, manual, skipped |

**Hinweis:** ID 3 fehlt – vermutlich ein früher gelöschtes Feld.

---

## E-003: Heredoc funktioniert nicht zuverlässig über Terminus/SSH (AP-00, 2025-02-06)

**Betrifft:** Alle Arbeitspakete mit mehrzeiligen Shell-Befehlen

**Problem:** `cat > datei << 'EOF'` und mehrzeilige curl-Befehle mit `\`-Zeilenumbrüchen werden in Terminus (SSH-Client) nicht korrekt übernommen.

**Workaround:** Dateien direkt mit `nano` bearbeiten statt heredoc zu nutzen. Mehrzeilige curl-Befehle in eine einzelne Zeile zusammenfassen.

**Relevanz:** Betrifft alle zukünftigen Anleitungen, die Shell-Snippets enthalten. Befehle immer als Einzeiler formulieren oder `nano`-Anweisungen geben.

---

## E-004: Projektpfad angepasst für CasaOS-Backup (AP-00, 2025-02-06)

**Betrifft:** Alle Arbeitspakete und Design-Dokumentation

**Dokumentiert:** `~/docker/paperless-classifier`

**Tatsächlich:** `/DATA/AppData/paperless-classifier`

**Grund:** Unter `/DATA/AppData/` liegende Verzeichnisse werden vom CasaOS-Backup-Konzept erfasst.

**Relevanz:** Alle Pfadangaben in Anleitungen und Code (Docker-Volumes, Mounts, etc.) müssen den tatsächlichen Pfad verwenden.

---

## E-005: libgl1-mesa-glx nicht mehr verfügbar in Debian Trixie (AP-01, 2025-02-06)

**Betrifft:** `Dockerfile`

**Problem:** `python:3.11-slim` basiert inzwischen auf Debian Trixie. Das Paket `libgl1-mesa-glx` wurde entfernt und ist nicht mehr installierbar. `docker compose build` bricht mit `E: Package 'libgl1-mesa-glx' has no installation candidate` ab.

**Lösung:** Paketname im Dockerfile ersetzen:
```
# Alt (funktioniert nicht mehr):
libgl1-mesa-glx
# Neu:
libgl1
```

**Relevanz:** Betrifft den Dockerfile-Build auf allen Plattformen (ARM64 und x86). Bei zukünftigen Änderungen am Dockerfile beachten.

---

## E-006: Model Router aus AP-03 nach AP-04 verschoben (AP-03, 2026-02-06)

**Betrifft:** `naechster-chat-kontext-ap03_1.md` (Kernaufgaben-Liste), Design-Dokument Abschnitt 5.4

**Problem:** Das Kontext-Dokument listet "Model Router: Dokumenteigenschaften → Modellwahl" als Kernaufgabe von AP-03 (Claude API Client). Der Model Router benötigt jedoch:

1. **PyMuPDF (`fitz`)** für lokale PDF-Analyse (`is_image_pdf`, `page_count`) – neue Dependency
2. **Zugriff auf Paperless-Metadaten** (`correspondent_known`, `expects_stamp`) – Abhängigkeit zum Paperless-Client
3. **Architektonisch:** Das Design-Dokument platziert den Router unter `classifier/model_router.py`, nicht unter `claude/`

Der Model Router gehört zur Classifier-Pipeline, nicht zum API-Client.

**Entscheidung:** Model Router wird in AP-04 (Classifier Core) implementiert, wo er architektonisch hingehört. Der `ClaudeClient` in AP-03 akzeptiert ein beliebiges Modell als Parameter – die Entscheidung *welches* Modell trifft der Aufrufer.

**Batch API:** Die Methodensignaturen `batch_classify()` und `get_batch_results()` sind im Client als Schnittstelle definiert, der Body ist als `TODO Phase 4` markiert (`NotImplementedError`). So ist die Schnittstelle dokumentiert, ohne dass Phase-4-Logik in Phase-1-Code landet.

**Relevanz:** AP-04 muss den Model Router (`classifier/model_router.py`) und die PyMuPDF-Dependency umsetzen. Die `requirements.txt` wird erst dann um `PyMuPDF` erweitert.

---

## E-007: Preistabelle aktualisiert – Opus massiv günstiger, Opus 4.6 neu (AP-03, 2026-02-06)

**Betrifft:** `cost_tracker.py`, Design-Dokument Abschnitte 2.3 und 5.5

**Problem:** Das Design-Dokument enthält veraltete Preise für Opus 4.5:

| Modell | Design-Dokument | Tatsächlich (06.02.2026) |
|---|---|---|
| Opus 4.5 Input | $15.00/MTok | **$5.00/MTok** |
| Opus 4.5 Output | $75.00/MTok | **$25.00/MTok** |
| Opus 4.5 Cache Read | $1.50/MTok | **$0.50/MTok** |
| Opus 4.5 Cache Write | $18.75/MTok | **$6.25/MTok (5m) / $10.00/MTok (1h)** |

Opus ist damit um Faktor 3 günstiger als angenommen. Die Kosten pro Schema-Analyse-Lauf sinken von ~$0.73 auf ~$0.24.

**Neue Modelle:**
- **Claude Opus 4.6** (`claude-opus-4-6`): Gleiche Preise wie Opus 4.5. Neues Flaggschiff-Modell, heute veröffentlicht.

**Strukturelle Änderung Cache Write:**
Anthropic hat zwei Cache-Write-Stufen eingeführt:
- **5m** (5 Minuten, `cache_control: {"type": "ephemeral"}`): Günstiger, unser Standard
- **1h** (1 Stunde): Teurer, aktuell nicht genutzt

`ModelPricing` hat jetzt `cache_write_5m_per_mtok` und `cache_write_1h_per_mtok` statt eines einzelnen `cache_write_per_mtok`. `calculate_cost()` akzeptiert `cache_ttl="5m"|"1h"`.

**Änderung in config.py (durchgeführt):** `schema_matrix_model` von `claude-opus-4-5-20251101` auf `claude-opus-4-6` geändert. Gleiches Preisniveau, neueres Modell. Beide bleiben in der Preistabelle hinterlegt.
