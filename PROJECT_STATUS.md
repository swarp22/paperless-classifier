# Projektstatus – Paperless Claude Classifier

Dieses Dokument dient als Chat-übergreifender Kontext für die Entwicklung
mit Claude. Es wird nach jedem abgeschlossenen Arbeitspaket aktualisiert.

**Letzte Aktualisierung:** 2026-02-07, nach AP-10

---

## Plattform & Umgebung

| Komponente | Details |
|---|---|
| Hardware | Raspberry Pi 4 (ARM64) |
| OS | CasaOS / Debian |
| Paperless-ngx | v2.20.6, 162 Dokumente, Port 8000 |
| Container | `paperless-classifier`, Port 8501, Python 3.11-slim |
| Repo | `github.com/swarp22/paperless-classifier` (privat) |
| Projektpfad Pi | `/DATA/AppData/paperless-classifier` |

## Abgeschlossene Arbeitspakete

### AP-00: Setup & Custom Fields ✓
- 6 Custom Fields in Paperless angelegt (IDs: 1, 2, 4, 5, 6, 7, 8)
- Person (ID 7): Max, Melanie, Kilian
- ki_status (ID 8): classified, review, error, manual, skipped
- Haus-Register (ID 5): 11 Kategorien

### AP-01: Container & NiceGUI ✓
- Dockerfile, docker-compose.yml, requirements.txt
- NiceGUI Health-Check auf Port 8501
- Hinweis: `libgl1` statt `libgl1-mesa-glx` (Debian Trixie), siehe ERRATA E-005

### AP-02: Paperless API Client ✓
- `app/paperless/client.py`: Async httpx Client
- CRUD für Dokumente, Korrespondenten, Tags, Dokumenttypen, Speicherpfade
- PDF-Download, Custom-Field-Zugriff
- Keine externen Abhängigkeiten über httpx/pydantic hinaus

### AP-03: Claude API Client ✓
- `app/claude/client.py`: AsyncAnthropic, PDF→Base64, Prompt Caching, JSON-Parsing
- `app/claude/cost_tracker.py`: Preistabelle (Opus 4.6/4.5, Sonnet 4.5, Haiku 4.5), TokenUsage, CostTracker
- `app/claude/prompts.py`: System-Prompt-Builder mit Stammdaten + Regelwerk
- Batch-API-Signaturen als Stubs (TODO Phase 4)
- Model Router bewusst auf AP-04 verschoben (ERRATA E-006)
- Preistabelle aktualisiert: Opus 4.5 = $5/$25 statt $15/$75 (ERRATA E-007)
- Cache Write mit zwei Stufen: 5min (ephemeral) und 1h

### AP-04: Classifier Core ✓
- `app/classifier/model_router.py`: Lokale PDF-Analyse (PyMuPDF), Modellwahl (Sonnet/Haiku)
- `app/classifier/resolver.py`: Name→ID Mapping mit Fuzzy-Matching (difflib, Threshold 0.85)
  - Select-Option-Auflösung für Custom Fields (ERRATA E-001)
  - Steuer-Tag-Ableitung aus tax_relevant + tax_year
  - Neuanlage-Tracking (create_new aus Claude-Antwort)
- `app/classifier/confidence.py`: Gewichtete Confidence-Bewertung (4 Signale)
  - HIGH → auto_apply (ki_status=classified), MEDIUM → apply_review, LOW → review_only
- `app/classifier/pipeline.py`: 10-Schritte-Orchestrierung (Design-Dokument Abschnitt 6)
  - Dependency Injection (PaperlessClient, ClaudeClient, PipelineConfig)
  - System-Prompt-Caching, Neuanlage-Handling, Fehler-Recovery
- Keine neuen Dependencies außer PyMuPDF (bereits in requirements.txt)

### AP-05: Poller & Scheduler ✓
- `app/scheduler/poller.py`: asyncio-Polling-Loop mit konfigurierbarem Intervall
  - PollerState: STOPPED/RUNNING/PAUSED/PROCESSING
  - Kostenlimit-Prüfung vor jedem Dokument
  - Graceful Shutdown über asyncio.Event
  - Health-Check-Integration
- **Erster Live-Test:** 11 Dokumente erfolgreich, $0.31 Gesamtkosten
  - Model Routing validiert: 7× Haiku ($0.09), 3× Sonnet ($0.20)
  - Mapping-Auflösung: 100% exakt, kein Fuzzy-Match nötig
- **Fix E-009:** Race Condition bei Multi-PATCH → Single-PATCH-Architektur
- **Fix E-010:** Rate-Limit (429/529) → Zyklusabbruch statt Error-Markierung
- **Fix E-011:** Haus-Register/Ordnungszahl bei digitalen Dokumenten unterdrückt
- **E-012 (Eselsohr):** Steuer-Tag-Neuanlage nutzt nicht create_new-Mechanismus → Phase 3

### AP-06: SQLite State-Management ✓
- `app/db/database.py`: SQLite-Schema (WAL-Modus, Indizes), async via aiosqlite
  - Tabelle `processed_documents`: Verarbeitungshistorie pro Pipeline-Durchlauf
  - Tabelle `daily_costs`: Aggregierte Tageskosten (UPSERT bei jedem Insert)
- Pipeline-Integration: `_persist_result()` im finally-Block (Schritt 10)
- CostTracker-Migration: `is_limit_reached()`, `get_monthly_cost()` etc. jetzt async
- **ERRATA:** E-013 bis E-015

### AP-07: Web-UI Basis ✓
- NiceGUI-Grundgerüst mit Sidebar-Navigation und Header
- Dashboard, Kosten, Settings, Log-Viewer
- **ERRATA:** E-016 bis E-022

### AP-08: Review-Queue ✓
- Vollständige Review-Queue mit Approve/Reject/Edit
- Direkte Neuanlage von Korrespondenten/Dokumenttypen/Tags
- **ERRATA:** E-023 bis E-029

### AP-09: Kosten-Dashboard & UI-Polish ✓
- Modell-Kosten mit Prozentanteil, Cache-Ersparnis, Auto-Refresh
- DB-Migration: daily_costs um sonnet_cost_usd/haiku_cost_usd
- **ERRATA:** E-030 bis E-032

### AP-10: Schema-Analyse – Collector & Datenmodell ✓
- **SQLite-Schema:** 4 neue Tabellen + 3 Indizes
  - `schema_title_patterns`: Titel-Schemata pro (Dokumenttyp, Korrespondent)
  - `schema_path_rules`: Pfad-Organisationsregeln pro Topic
  - `schema_mapping_matrix`: Zuordnungen (Korrespondent + Typ) → Pfad
  - `schema_analysis_runs`: Audit-Log mit Token/Kosten/Status
- **Storage-Modul** (`app/schema_matrix/storage.py`):
  - SchemaStorage: Upsert mit is_manual-Schutz (force=False überspringt manuelle Einträge)
  - CRUD für alle 3 Tabellen + Statistiken + set_manual_flag()
- **Collector-Modul** (`app/schema_matrix/collector.py`):
  - Datensammlung aus Paperless (alle Dokumente, ungefiltert)
  - Ebene 1: Titel-Gruppierung nach (Dokumenttyp, Korrespondent)
  - Ebene 2: Pfad-Hierarchie-Zerlegung (" / "-Separator), Topic-Gruppierung
  - Ebene 3: Zuordnungstabelle mit Eindeutigkeitserkennung
  - Änderungserkennung + `serialize_for_prompt()` für Opus-Prompt
- **Trigger-Modul** (`app/schema_matrix/trigger.py`):
  - 3 Auslöser: Wöchentlich (168h), Schwellwert (≥20 Docs), Manuell (AP-12)
  - Mindestabstand 24h, `get_status()` für UI
- **Poller-Integration:**
  - `_check_schema_trigger()` in `_run_loop()` – nur Logging bis AP-11
  - `main.py` übergibt `database=state.database` an Poller

## Nächstes Arbeitspaket

**AP-11: Schema-Analyse – Opus-Analyse & Prompt-Builder**
- Opus-API-Aufruf: Analyse-Prompt mit vorverarbeiteten Collector-Daten
- Response-Parsing: JSON → SQLite (Titel-Schemata, Pfad-Regeln, Matrix)
- Prompt-Builder: Schema-Regeln in den Klassifizierungs-System-Prompt einbetten
- Upsert-Logik: Manuelle Einträge (is_manual=TRUE) nicht überschreiben
- Poller-Integration: Trigger → Collector → Opus → Storage → Audit-Log

**AP-12: Schema-Analyse – Web-UI**
- Drei Tabs: Titel-Schemata, Pfad-Regeln, Zuordnungsmatrix
- Bearbeiten/Fixieren einzelner Einträge (is_manual setzen)
- Manueller Trigger-Button für Schema-Analyse-Lauf
- Anzeige des letzten Lauf-Ergebnisses (Audit-Log)

## Konfiguration (config.py – aktuelle Werte)

```python
default_model = "claude-sonnet-4-5-20250929"
batch_model = "claude-sonnet-4-5-20250929"
schema_matrix_model = "claude-opus-4-6"
monthly_cost_limit_usd = 25.0
polling_interval_seconds = 300
schema_matrix_schedule = "weekly"
schema_matrix_threshold = 20
schema_matrix_min_interval_h = 24
```

## Paperless-Stammdaten (Kurzfassung)

- 29 Korrespondenten, 23 Dokumenttypen, 7 Tags, 30 Speicherpfade
- Vollständige Struktur: `paperless-struktur-20260202-184522.json` im Projektwissen

## Projektdateien-Übersicht

```
paperless-classifier/
├── app/
│   ├── __init__.py
│   ├── main.py                    # NiceGUI Einstiegspunkt
│   ├── config.py                  # Pydantic Settings
│   ├── logging_config.py          # Logging-Konfiguration
│   ├── state.py                   # Globale Laufzeit-Objekte + Getter
│   ├── health.py                  # Health-Check-Logik
│   ├── claude/                    # AP-03
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── cost_tracker.py
│   │   └── prompts.py
│   ├── db/                        # AP-06 + AP-09 + AP-10
│   │   ├── __init__.py
│   │   └── database.py
│   ├── classifier/                # AP-04 + AP-05
│   │   ├── __init__.py
│   │   ├── model_router.py
│   │   ├── resolver.py
│   │   ├── confidence.py
│   │   └── pipeline.py
│   ├── paperless/                 # AP-02
│   │   ├── __init__.py
│   │   ├── cache.py
│   │   ├── client.py
│   │   ├── exceptions.py
│   │   └── models.py
│   ├── scheduler/                 # AP-05 + AP-10
│   │   ├── __init__.py
│   │   └── poller.py
│   ├── schema_matrix/             # AP-10
│   │   ├── __init__.py
│   │   ├── collector.py
│   │   ├── storage.py
│   │   └── trigger.py
│   └── ui/                        # AP-07 + AP-08 + AP-09
│       ├── __init__.py
│       ├── layout.py
│       ├── dashboard.py
│       ├── costs.py
│       ├── review.py
│       ├── settings.py
│       └── logs.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── ERRATA.md                      # E-001 bis E-032
├── PROJECT_STATUS.md
└── README.md
```

## Referenzen

- **Design-Dokumentation:** Im Claude-Projektwissen (`project_knowledge_search`)
- **ERRATA:** `ERRATA.md` im Repo (Source of Truth, nicht im Projektwissen)
- **Code:** GitHub Repo (Source of Truth, lesbar per `web_fetch` auf raw.githubusercontent.com)

## Hinweise für den Chat-Start

Zu Beginn eines neuen Chats:
1. `PROJECT_STATUS.md` und `ERRATA.md` aus dem Repo lesen
2. Design-Dokumentation per `project_knowledge_search` nach Bedarf nachschlagen
3. Aktuellen Code bei Bedarf per `web_fetch` aus dem Repo holen
