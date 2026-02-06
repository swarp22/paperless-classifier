# Projektstatus – Paperless Claude Classifier

Dieses Dokument dient als Chat-übergreifender Kontext für die Entwicklung
mit Claude. Es wird nach jedem abgeschlossenen Arbeitspaket aktualisiert.

**Letzte Aktualisierung:** 2026-02-06, nach AP-04

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

## Nächstes Arbeitspaket

**AP-05: Poller & Scheduler**
- Polling-basierte Dokumenterkennung (Tag "NEU" / ki_status=null)
- asyncio-Task mit konfigurierbarem Intervall
- Webhook-Endpunkt (optional, Phase 2)
- Integration mit ClassificationPipeline

## Konfiguration (config.py – aktuelle Werte)

```python
default_model = "claude-sonnet-4-5-20250929"
batch_model = "claude-sonnet-4-5-20250929"
schema_matrix_model = "claude-opus-4-6"        # geändert von opus-4-5, siehe ERRATA E-007
monthly_cost_limit_usd = 25.0
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
│   ├── claude/                    # AP-03
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── cost_tracker.py
│   │   └── prompts.py
│   ├── classifier/                # AP-04
│   │   ├── __init__.py
│   │   ├── model_router.py        # PDF-Analyse + Modellwahl
│   │   ├── resolver.py            # Name→ID Mapping (Fuzzy)
│   │   ├── confidence.py          # Confidence-Bewertung
│   │   └── pipeline.py            # Orchestrierung (10-Schritte-Flow)
│   └── paperless/                 # AP-02
│       ├── __init__.py
│       ├── cache.py
│       ├── client.py
│       ├── exceptions.py
│       └── models.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── ERRATA.md                      # Abweichungen zur Design-Doku
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
