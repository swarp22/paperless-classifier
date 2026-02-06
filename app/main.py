"""Einstiegspunkt des Paperless Claude Classifiers.

Startet den NiceGUI-Server mit integriertem Health-Check-Endpoint.
NiceGUI bringt FastAPI/Uvicorn mit – kein separater Server nötig.

Lifecycle:
1. startup()        – Logging, Config-Validierung (synchron)
2. async_startup()  – Clients initialisieren, Cache laden, Poller starten
3. ... Server läuft ...
4. shutdown()       – Poller stoppen, Clients schließen
"""

import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from nicegui import app, ui

from app.config import Settings, get_settings
from app.logging_config import get_logger, setup_logging

logger = get_logger("app")


# ---------------------------------------------------------------------------
# Laufzeit-Objekte (werden in async_startup initialisiert)
# ---------------------------------------------------------------------------
# Modul-Level-Referenzen, damit Health-Check und zukünftige UI darauf
# zugreifen können.  Vor async_startup() sind alle None.

_paperless_client: Any = None   # PaperlessClient | None
_claude_client: Any = None      # ClaudeClient | None
_cost_tracker: Any = None       # CostTracker | None
_pipeline: Any = None           # ClassificationPipeline | None
_poller: Any = None             # Poller | None


def get_poller() -> Any:
    """Gibt die Poller-Instanz zurück (für Web-UI in späteren APs)."""
    return _poller


def get_pipeline() -> Any:
    """Gibt die Pipeline-Instanz zurück (für Web-UI in späteren APs)."""
    return _pipeline


def get_cost_tracker() -> Any:
    """Gibt den CostTracker zurück (für Kosten-Dashboard in späteren APs)."""
    return _cost_tracker


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

    # Poller-Status einbeziehen
    poller_info: dict[str, Any]
    if _poller is not None:
        poller_info = {
            "status": _poller.status.state.value,
            "documents_processed": _poller.status.documents_processed,
            "documents_errored": _poller.status.documents_errored,
            "last_run_at": (
                _poller.status.last_run_at.isoformat()
                if _poller.status.last_run_at
                else None
            ),
            "cost_limit_paused": _poller.status.cost_limit_paused,
        }
    else:
        poller_info = {"status": "not_initialized"}

    # Gesamtstatus: healthy wenn DB schreibbar und API-Key vorhanden,
    # degraded wenn Paperless nicht erreichbar, unhealthy bei DB/Key-Problemen
    checks = {
        "paperless": paperless,
        "anthropic_api_key": api_key,
        "database": database,
        "poller": poller_info,
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
    """Wird beim Serverstart ausgeführt – initialisiert Logging und prüft Config.

    Synchroner Handler: Läuft vor async_startup().
    """
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


async def async_startup() -> None:
    """Asynchrone Initialisierung: Clients erstellen, Cache laden, Poller starten.

    Wird nach startup() ausgeführt, wenn der Event-Loop bereits läuft.
    Fehler hier sind nicht fatal – der Container läuft weiter im
    degraded-Modus (Health-Check zeigt den Zustand an).
    """
    global _paperless_client, _claude_client, _cost_tracker, _pipeline, _poller

    settings = get_settings()

    # --- PaperlessClient ---
    try:
        from app.paperless.client import PaperlessClient

        _paperless_client = PaperlessClient(
            base_url=settings.paperless_url,
            token=settings.paperless_api_token,
        )
        await _paperless_client.__aenter__()
        logger.info("PaperlessClient initialisiert")

        # Stammdaten-Cache laden (Korrespondenten, Tags, Typen, Pfade)
        stats = await _paperless_client.load_cache()
        logger.info(
            "Stammdaten-Cache geladen: %s",
            ", ".join(f"{k}={v}" for k, v in stats.items()),
        )
    except Exception as exc:
        logger.error("PaperlessClient konnte nicht initialisiert werden: %s", exc)
        _paperless_client = None
        return  # Ohne Paperless-Client kein Poller möglich

    # --- ClaudeClient + CostTracker ---
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY nicht konfiguriert – "
            "Poller wird nicht gestartet (Klassifizierung nicht möglich)"
        )
        return

    try:
        from app.claude.client import ClaudeClient
        from app.claude.cost_tracker import CostTracker

        _cost_tracker = CostTracker()

        _claude_client = ClaudeClient(
            api_key=settings.anthropic_api_key,
            default_model=settings.default_model,
            cost_tracker=_cost_tracker,
            monthly_cost_limit_usd=settings.monthly_cost_limit_usd,
        )
        await _claude_client.__aenter__()
        logger.info("ClaudeClient initialisiert")
    except Exception as exc:
        logger.error("ClaudeClient konnte nicht initialisiert werden: %s", exc)
        _claude_client = None
        return  # Ohne Claude-Client kein Poller möglich

    # --- Pipeline ---
    try:
        from app.classifier.pipeline import ClassificationPipeline, PipelineConfig

        _pipeline = ClassificationPipeline(
            paperless=_paperless_client,
            claude=_claude_client,
            config=PipelineConfig(),
        )
        logger.info("ClassificationPipeline erstellt")
    except Exception as exc:
        logger.error("Pipeline konnte nicht erstellt werden: %s", exc)
        return

    # --- Poller ---
    try:
        from app.scheduler.poller import Poller

        _poller = Poller(
            paperless=_paperless_client,
            pipeline=_pipeline,
            settings=settings,
            cost_tracker=_cost_tracker,
        )
        _poller.start()
        logger.info("Poller gestartet")
    except Exception as exc:
        logger.error("Poller konnte nicht gestartet werden: %s", exc)


async def shutdown() -> None:
    """Graceful Shutdown: Poller stoppen, Clients schließen.

    Wird beim Container-Stop (SIGTERM) aufgerufen.  Reihenfolge:
    1. Poller stoppen (wartet auf aktuelles Dokument)
    2. ClaudeClient schließen
    3. PaperlessClient schließen
    """
    global _poller, _claude_client, _paperless_client

    logger.info("Shutdown eingeleitet...")

    # Poller stoppen
    if _poller is not None:
        try:
            await _poller.stop()
            logger.info("Poller gestoppt")
        except Exception as exc:
            logger.error("Fehler beim Stoppen des Pollers: %s", exc)
        _poller = None

    # ClaudeClient schließen
    if _claude_client is not None:
        try:
            await _claude_client.__aexit__(None, None, None)
            logger.info("ClaudeClient geschlossen")
        except Exception as exc:
            logger.error("Fehler beim Schließen des ClaudeClients: %s", exc)
        _claude_client = None

    # PaperlessClient schließen
    if _paperless_client is not None:
        try:
            await _paperless_client.__aexit__(None, None, None)
            logger.info("PaperlessClient geschlossen")
        except Exception as exc:
            logger.error("Fehler beim Schließen des PaperlessClients: %s", exc)
        _paperless_client = None

    logger.info("=" * 60)
    logger.info("Paperless Claude Classifier beendet")
    logger.info("=" * 60)


app.on_startup(startup)
app.on_startup(async_startup)
app.on_shutdown(shutdown)


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
