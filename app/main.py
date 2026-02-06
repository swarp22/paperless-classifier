"""Einstiegspunkt des Paperless Claude Classifiers.

Startet den NiceGUI-Server mit integriertem Health-Check-Endpoint.
NiceGUI bringt FastAPI/Uvicorn mit – kein separater Server nötig.
"""

import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from nicegui import app, ui

from app.config import Settings, get_settings
from app.logging_config import get_logger, setup_logging

logger = get_logger("app")


# --- Health-Check Logik ---

async def check_paperless_reachable(settings: Settings) -> dict[str, Any]:
    """Prüft ob die Paperless-ngx API erreichbar ist.

    Kein harter Fehler – der Classifier kann auch bei Paperless-Downtime
    laufen (wartet dann auf nächsten Polling-Zyklus).
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(
                f"{settings.paperless_url}/api/",
                headers={"Authorization": f"Token {settings.paperless_api_token}"},
            )
            if response.status_code == 200:
                return {"status": "ok", "url": settings.paperless_url}
            return {
                "status": "error",
                "url": settings.paperless_url,
                "http_status": response.status_code,
            }
    except httpx.RequestError as e:
        return {"status": "unreachable", "url": settings.paperless_url, "error": str(e)}


def check_api_key_present(settings: Settings) -> dict[str, Any]:
    """Prüft ob der Anthropic API-Key konfiguriert ist.

    Validiert nur das Vorhandensein, nicht die Gültigkeit
    (das würde einen API-Call kosten).
    """
    if settings.anthropic_api_key and settings.anthropic_api_key.startswith("sk-ant-"):
        return {"status": "ok", "key_prefix": settings.anthropic_api_key[:12] + "..."}
    return {"status": "not_configured"}


def check_sqlite_writable(settings: Settings) -> dict[str, Any]:
    """Prüft ob das Datenverzeichnis beschreibbar ist."""
    try:
        data_dir = settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        # Testdatei schreiben und sofort löschen
        test_file = data_dir / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return {"status": "ok", "path": str(data_dir)}
    except OSError as e:
        return {"status": "error", "path": str(settings.data_dir), "error": str(e)}


# --- FastAPI-Endpoint auf dem NiceGUI-Server ---

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health-Check-Endpoint für Docker und Monitoring.

    Gibt HTTP 200 zurück solange der Service grundsätzlich läuft.
    Einzelne Subsysteme können 'degraded' sein ohne den Container zu killen.

    Returns:
        JSON mit Status jeder Komponente und Gesamtstatus.
    """
    settings = get_settings()

    paperless = await check_paperless_reachable(settings)
    api_key = check_api_key_present(settings)
    database = check_sqlite_writable(settings)

    # Gesamtstatus: healthy wenn DB schreibbar und API-Key vorhanden,
    # degraded wenn Paperless nicht erreichbar, unhealthy bei DB/Key-Problemen
    checks = {
        "paperless": paperless,
        "anthropic_api_key": api_key,
        "database": database,
    }

    critical_ok = database["status"] == "ok"
    all_ok = (
        critical_ok
        and api_key["status"] == "ok"
        and paperless["status"] == "ok"
    )

    if all_ok:
        overall = "healthy"
    elif critical_ok:
        # DB schreibbar, aber Paperless oder API-Key fehlt → lauffähig mit Einschränkungen
        overall = "degraded"
    else:
        overall = "unhealthy"

    return {
        "status": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "checks": checks,
    }


# --- Startup / Shutdown ---

def startup() -> None:
    """Wird beim Serverstart ausgeführt – initialisiert Logging und prüft Config."""
    try:
        settings = get_settings()
    except Exception as e:
        # Ohne gültige Config kann der Container nicht starten
        print(f"FATAL: Konfigurationsfehler – {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(
        log_level=settings.log_level.value,
        log_dir=settings.log_dir,
    )

    logger.info("=" * 60)
    logger.info("Paperless Claude Classifier v0.1.0 startet")
    logger.info("=" * 60)
    logger.info("Paperless-URL: %s", settings.paperless_url)
    logger.info("Standard-Modell: %s", settings.default_model)
    logger.info("Verarbeitungsmodus: %s", settings.processing_mode.value)
    logger.info("Polling-Intervall: %ds", settings.polling_interval_seconds)
    logger.info("Kostenlimit: $%.2f/Monat", settings.monthly_cost_limit_usd)
    logger.info("Log-Level: %s", settings.log_level.value)
    logger.info("Datenverzeichnis: %s", settings.data_dir)


app.on_startup(startup)


# --- Platzhalter UI (wird in späteren APs ausgebaut) ---

@ui.page("/")
def index_page() -> None:
    """Startseite – Platzhalter bis AP-06 (Web-UI Basis)."""
    ui.label("Paperless Claude Classifier").classes("text-2xl font-bold")
    ui.label("Dashboard kommt in AP-06.").classes("text-gray-500")
    ui.link("Health-Check →", "/health")


# --- Haupteinstiegspunkt ---

def main() -> None:
    """Startet den NiceGUI-Server."""
    ui.run(
        host="0.0.0.0",
        port=8501,
        title="Paperless Classifier",
        # Kein automatisches Browser-Öffnen im Container
        show=False,
        # Reload nur in Entwicklung, nicht in Produktion
        reload=False,
        # Favicon kommt später
        favicon=None,
    )


if __name__ == "__main__":
    main()
