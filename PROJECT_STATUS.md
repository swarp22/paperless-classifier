# Projektstatus – Paperless Claude Classifier

Dieses Dokument dient als Chat-übergreifender Kontext für die Entwicklung
mit Claude. Es wird nach jedem abgeschlossenen Arbeitspaket aktualisiert.

**Letzte Aktualisierung:** 2026-02-07, nach AP-09

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
  - `_apply_result()` sendet alle Änderungen in einem PATCH
- **Fix E-010:** Rate-Limit (429/529) → Zyklusabbruch statt Error-Markierung
  - 2s Delay zwischen Dokumenten (`DOCUMENT_DELAY_SECONDS`)
  - ClaudeAPIError mit status_code wird an Poller weitergereicht
- **Fix E-011:** Haus-Register/Ordnungszahl bei digitalen Dokumenten unterdrückt
  - Resolver prüft jetzt `is_scanned_document` + `pagination_stamp is None`
  - Pipeline entfernt Haus-Felder bei digitalen PDFs (analog Paginierung)
- **E-012 (Eselsohr):** Steuer-Tag-Neuanlage nutzt nicht create_new-Mechanismus → Phase 3

### AP-06: SQLite State-Management ✓
- `app/db/database.py`: SQLite-Schema (WAL-Modus, Indizes), async via aiosqlite
  - Tabelle `processed_documents`: Verarbeitungshistorie pro Pipeline-Durchlauf
  - Tabelle `daily_costs`: Aggregierte Tageskosten (UPSERT bei jedem Insert)
  - Abfrage-Methoden: get_monthly_cost, get_daily_cost, get_model_breakdown, get_recent_documents
- Pipeline-Integration: `_persist_result()` im finally-Block (Schritt 10)
  - Schreibt bei Erfolg und Fehler (sofern API-Aufruf stattfand)
- CostTracker-Migration: `is_limit_reached()`, `get_monthly_cost()` etc. jetzt async
  - SQLite als primäre Quelle, In-Memory-Fallback ohne DB
  - `set_database()` wird in main.py nach DB-Init aufgerufen
- Aufrufer-Anpassungen: `ClaudeClient._check_cost_limit()` und `Poller._is_cost_limit_reached()` jetzt async
- **ERRATA E-013:** DB-Modul in `app/db/` statt `app/database.py`
- **ERRATA E-014:** CostTracker-Methoden async (Breaking Change, alle Aufrufer angepasst)
- **ERRATA E-015:** Schema-Abweichungen (paperless_id nicht UNIQUE, duration_seconds + error_message ergänzt, Cache-Token in daily_costs)

### AP-07: Web-UI Basis ✓
- NiceGUI-Grundgerüst mit Sidebar-Navigation und Header
- `app/ui/layout.py`: Wiederverwendbares Layout (Sidebar + Content)
- `app/ui/dashboard.py`: Poller-Status, Zähler-Karten (Heute/Woche/Monat), Dokumenten-Tabelle
- `app/ui/costs.py`: Kosten-Übersicht mit ECharts-Tagesdiagramm, Modell-Badges
- `app/ui/settings.py`: Verbindungsstatus-Prüfung, Config-Anzeige, Poller-Steuerung
- `app/ui/logs.py`: Log-Viewer mit Level-Filter, Traceback-Gruppierung
- `app/state.py`: Getter-Funktionen ausgelagert (E-017: verhindert doppelte Startup-Registrierung)
- **ERRATA:** E-016 bis E-022

### AP-08: Review-Queue ✓
- `app/ui/review.py`: Vollständige Review-Queue mit Approve/Reject/Edit
- Vorschau aller Claude-Zuordnungen (Korrespondent, Typ, Tags, Pfad, Custom Fields, Titel)
- Direkte Neuanlage von Korrespondenten/Dokumenttypen/Tags aus der Review-Queue
- Freitext-Bearbeitung aller Felder vor der Anwendung
- Badge-Zähler im Sidebar-Menü, Poller-Status-Chip im Header
- **ERRATA:** E-023 bis E-029

### AP-09: Kosten-Dashboard & UI-Polish ✓
- Kosten-Seite: Woche-Karte, Modell-Kosten mit Prozentanteil, Ø Tokens/Dokument, Cache-Ersparnis
- DB-Migration: daily_costs um sonnet_cost_usd/haiku_cost_usd (ALTER TABLE, idempotent)
- Neue DB-Methoden: get_avg_tokens_per_document(), get_cache_savings()
- Auto-Refresh: Dashboard (30s), Kosten (30s), Log-Viewer (5s Toggle)
- Dashboard: Paperless-ID als klickbarer Link zur Dokumentdetailseite
- Log-Viewer: Textsuche, Component-Filter, Auto-Refresh-Switch
- Settings: Header-Chip synchronisiert sich nach Poller-Steuerung (Delayed Reload)
- **ERRATA:** E-030 bis E-032

## Nächstes Arbeitspaket

**AP-10: Schema-Analyse – Collector & Datenmodell (Phase 3)**
- SQLite-Tabellen: schema_title_patterns, schema_path_rules, schema_mapping_matrix, schema_analysis_runs
- Collector: Titel/Typ/Korrespondent/Pfad aus Paperless sammeln und vorverarbeiten
- Gruppierung: Titel nach (Dokumenttyp, Korrespondent), Pfad-Hierarchie extrahieren
- Trigger-Logik: Wöchentlich + Schwellwert (≥20 neue Docs) + manuell
- Design-Dokument: Abschnitt 8 (Schema-Analyse) + Abschnitt 6 (Datenmodell)

**AP-11: Schema-Analyse – Opus-Analyse & Prompt-Builder**
- Opus-API-Aufruf: Analyse-Prompt mit vorverarbeiteten Collector-Daten
- Response-Parsing: JSON → SQLite (Titel-Schemata, Pfad-Regeln, Matrix)
- Prompt-Builder: Schema-Regeln in den Klassifizierungs-System-Prompt einbetten
- Upsert-Logik: Manuelle Einträge (is_manual=TRUE) nicht überschreiben

**AP-12: Schema-Analyse – Web-UI**
- Drei Tabs: Titel-Schemata, Pfad-Regeln, Zuordnungsmatrix
- Bearbeiten/Fixieren einzelner Einträge (is_manual setzen)
- Manueller Trigger-Button für Schema-Analyse-Lauf
- Anzeige des letzten Lauf-Ergebnisses (Audit-Log)

## Konfiguration (config.py – aktuelle Werte)

```python
default_model = "claude-sonnet-4-5-20250929"
batch_model = "claude-sonnet-4-5-20250929"
schema_matrix_model = "claude-opus-4-6"        # geändert von opus-4-5, siehe ERRATA E-007
monthly_cost_limit_usd = 25.0
polling_interval_seconds = 300                  # 5 Minuten
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
│   ├── state.py                   # Globale Laufzeit-Objekte + Getter (AP-07)
│   ├── health.py                  # Health-Check-Logik
│   ├── claude/                    # AP-03
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── cost_tracker.py
│   │   └── prompts.py
│   ├── db/                        # AP-06 + AP-09
│   │   ├── __init__.py
│   │   └── database.py           # SQLite (daily_costs mit per-Modell-Kosten)
│   ├── classifier/                # AP-04 + Fixes aus AP-05
│   │   ├── __init__.py
│   │   ├── model_router.py        # PDF-Analyse + Modellwahl
│   │   ├── resolver.py            # Name→ID Mapping (Fuzzy)
│   │   ├── confidence.py          # Confidence-Bewertung
│   │   └── pipeline.py            # Orchestrierung (10-Schritte-Flow)
│   ├── paperless/                 # AP-02
│   │   ├── __init__.py
│   │   ├── cache.py
│   │   ├── client.py
│   │   ├── exceptions.py
│   │   └── models.py
│   ├── scheduler/                 # AP-05
│   │   ├── __init__.py
│   │   └── poller.py
│   └── ui/                        # AP-07 + AP-08 + AP-09
│       ├── __init__.py            # register_pages()
│       ├── layout.py              # Sidebar + Header Layout
│       ├── dashboard.py           # Startseite (Auto-Refresh 30s)
│       ├── costs.py               # Kosten-Dashboard (Auto-Refresh 30s)
│       ├── review.py              # Review-Queue mit Approve/Reject/Edit
│       ├── settings.py            # Config-Anzeige + Poller-Steuerung
│       └── logs.py                # Log-Viewer (Suche, Filter, Auto-Refresh)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── ERRATA.md                      # Abweichungen zur Design-Doku (E-001 bis E-032)
├── PROJECT_STATUS.md              # ← Dieses Dokument
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
