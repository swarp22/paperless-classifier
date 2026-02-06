# Paperless Claude Classifier

KI-gestützte Dokumentenklassifizierung für [Paperless-ngx](https://docs.paperless-ngx.com/) mit der Claude API.

## Überblick

Analysiert PDF-Dokumente aus Paperless-ngx visuell und inhaltlich über die Claude API und setzt automatisch:
- Dokumenttyp, Korrespondent, Tags, Speicherpfad
- Personenzuordnung (Haushaltsmitglieder)
- Steuerrelevanz und Steuerjahr
- Haus-Ordner-Zuordnung (physisches Aktenregister)
- Dokumentenverknüpfungen (z.B. Arztrechnung ↔ Erstattung)

## Zielplattform

- Raspberry Pi 4 (ARM64)
- Docker / Docker Compose
- Paperless-ngx v2.20.6

## Tech Stack

- Python 3.11+
- [NiceGUI](https://nicegui.io/) – Web-UI
- [Anthropic SDK](https://docs.anthropic.com/) – Claude API
- httpx (async) – Paperless-ngx API
- Pydantic v2 – Validierung & Settings

## Projektstruktur

```
paperless-classifier/
├── app/
│   ├── __init__.py
│   ├── main.py              # NiceGUI Einstiegspunkt
│   ├── config.py             # Settings (Pydantic)
│   ├── claude/               # AP-03: Claude API Client
│   │   ├── __init__.py
│   │   ├── client.py         # ClaudeClient, ClassificationResult
│   │   ├── cost_tracker.py   # Preistabelle, TokenUsage, CostTracker
│   │   └── prompts.py        # System-Prompt-Builder
│   └── paperless/            # AP-02: Paperless API Client
│       ├── __init__.py
│       └── client.py         # PaperlessClient
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── ERRATA.md                 # Abweichungen zur Design-Dokumentation
├── .gitignore
└── README.md
```

## Setup

```bash
# .env anlegen (nicht im Repo!)
cp .env.example .env
nano .env

# Container bauen und starten
docker compose up -d --build

# Web-UI öffnen
# http://<pi-ip>:8501
```

## Umgebungsvariablen (.env)

| Variable | Beschreibung | Beispiel |
|---|---|---|
| `PAPERLESS_URL` | Paperless-ngx URL | `http://192.168.178.xx:8000` |
| `PAPERLESS_TOKEN` | API-Token | `abc123...` |
| `ANTHROPIC_API_KEY` | Claude API Key | `sk-ant-...` |

## Arbeitspakete

- [x] AP-00: Setup & Custom Fields
- [x] AP-01: Container & NiceGUI
- [x] AP-02: Paperless API Client
- [x] AP-03: Claude API Client
- [ ] AP-04: Classifier Core (Pipeline, Model Router)
- [ ] AP-05: Web-UI
- [ ] ...

## Hinweise

- **Privates Repo** – Konfiguration enthält haushaltsspezifische Daten
- **Design-Dokumentation** liegt im Claude-Projektwissen, nicht im Repo
- **ERRATA.md** dokumentiert alle Abweichungen zur Design-Doku
