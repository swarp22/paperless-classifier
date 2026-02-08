"""Einstiegspunkt des Paperless Claude Classifiers.

Startet den NiceGUI-Server mit integriertem Health-Check-Endpoint.
NiceGUI bringt FastAPI/Uvicorn mit – kein separater Server nötig.

Lifecycle:
1. startup()        – Logging, Config-Validierung (synchron)
2. async_startup()  – Clients initialisieren, Cache laden, Poller starten
3. ... Server läuft ...
4. shutdown()       – Poller stoppen, Clients schließen
"""

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

from nicegui import app, ui

from app.config import Settings, get_settings
from app.logging_config import get_logger, setup_logging
import app.state as state

logger = get_logger("app")


# ---------------------------------------------------------------------------
# Getter-Funktionen (Delegieren an app.state, Rückwärtskompatibilität)
# ---------------------------------------------------------------------------

def get_database() -> Any:
    """Gibt die Database-Instanz zurück."""
    return state.database


def get_poller() -> Any:
    """Gibt die Poller-Instanz zurück."""
    return state.poller


def get_pipeline() -> Any:
    """Gibt die Pipeline-Instanz zurück."""
    return state.pipeline


def get_cost_tracker() -> Any:
    """Gibt den CostTracker zurück."""
    return state.cost_tracker


# --- Health-Check Logik ---

# Health-Check-Funktionen (ausgelagert, um zirkuläre Imports zu vermeiden)
from app.health import check_api_key_present, check_paperless_reachable, check_sqlite_writable


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
    if state.poller is not None:
        poller_info = {
            "status": state.poller.status.state.value,
            "documents_processed": state.poller.status.documents_processed,
            "documents_errored": state.poller.status.documents_errored,
            "last_run_at": (
                state.poller.status.last_run_at.isoformat()
                if state.poller.status.last_run_at
                else None
            ),
            "cost_limit_paused": state.poller.status.cost_limit_paused,
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

async def _paperless_reconnect_loop(settings: Settings) -> None:
    """Hintergrund-Task: Versucht periodisch Paperless zu erreichen.

    Wird gestartet wenn der initiale Verbindungsaufbau fehlschlägt
    (z.B. weil Paperless nach einem Backup noch nicht hochgefahren ist).
    Bei Erfolg werden alle Clients und der Poller initialisiert.

    (E-033: Startup-Retry bei Paperless-Ausfall)
    """
    reconnect_interval = 60.0  # Sekunden zwischen Versuchen
    attempt = 0

    while state.paperless_client is None:
        await asyncio.sleep(reconnect_interval)
        attempt += 1

        client: PaperlessClient | None = None
        try:
            from app.paperless.client import PaperlessClient

            client = PaperlessClient(
                base_url=settings.paperless_url,
                token=settings.paperless_api_token,
            )
            await client.__aenter__()
            stats = await client.load_cache()

            state.paperless_client = client
            logger.info(
                "Paperless-Reconnect erfolgreich nach %d Versuchen: %s",
                attempt,
                ", ".join(f"{k}={v}" for k, v in stats.items()),
            )

            # Jetzt den Rest der Initialisierung nachholen
            await _initialize_remaining_services(settings)
            return

        except Exception as exc:
            logger.warning(
                "Paperless-Reconnect Versuch %d fehlgeschlagen: %s",
                attempt, exc,
            )
            if client is not None:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:
                    pass


async def _initialize_remaining_services(settings: Settings) -> None:
    """Initialisiert Claude-Client, Pipeline und Poller.

    Wird sowohl vom normalen Startup als auch vom Reconnect aufgerufen,
    um Duplikation zu vermeiden.  Prüft ob Services bereits initialisiert
    sind und überspringt sie gegebenenfalls.

    (E-033: Gemeinsame Init-Logik für Startup und Reconnect)
    """
    # --- ClaudeClient + CostTracker ---
    if state.claude_client is None:
        if not settings.anthropic_api_key:
            logger.warning(
                "ANTHROPIC_API_KEY nicht konfiguriert – "
                "Poller wird nicht gestartet (Klassifizierung nicht möglich)"
            )
            return

        try:
            from app.claude.client import ClaudeClient
            from app.claude.cost_tracker import CostTracker

            if state.cost_tracker is None:
                state.cost_tracker = CostTracker()
                if state.database:
                    state.cost_tracker.set_database(state.database)

            state.claude_client = ClaudeClient(
                api_key=settings.anthropic_api_key,
                default_model=settings.default_model,
                cost_tracker=state.cost_tracker,
                monthly_cost_limit_usd=settings.monthly_cost_limit_usd,
            )
            await state.claude_client.__aenter__()
            logger.info("ClaudeClient initialisiert")
        except Exception as exc:
            logger.error("ClaudeClient konnte nicht initialisiert werden: %s", exc)
            state.claude_client = None
            return

    # --- Pipeline ---
    if state.pipeline is None:
        try:
            from app.classifier.pipeline import ClassificationPipeline, PipelineConfig

            state.pipeline = ClassificationPipeline(
                paperless=state.paperless_client,
                claude=state.claude_client,
                config=PipelineConfig(),
                database=state.database,
            )
            logger.info("ClassificationPipeline erstellt")
        except Exception as exc:
            logger.error("Pipeline konnte nicht erstellt werden: %s", exc)
            return

    # --- Poller ---
    if state.poller is None:
        try:
            from app.scheduler.poller import Poller

            state.poller = Poller(
                paperless=state.paperless_client,
                pipeline=state.pipeline,
                settings=settings,
                cost_tracker=state.cost_tracker,
                database=state.database,
            )
            state.poller.start()
            logger.info("Poller gestartet")
        except Exception as exc:
            logger.error("Poller konnte nicht gestartet werden: %s", exc)

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
    """Asynchrone Initialisierung: DB, Clients, Cache, Poller starten.

    Wird nach startup() ausgeführt, wenn der Event-Loop bereits läuft.
    Fehler hier sind nicht fatal – der Container läuft weiter im
    degraded-Modus (Health-Check zeigt den Zustand an).
    """
    # State-Variablen werden über app.state gesetzt
    

    settings = get_settings()

    # --- SQLite-Datenbank (AP-06) ---
    try:
        from app.db.database import Database

        state.database = Database(settings.db_path)
        await state.database.initialize()
        logger.info("SQLite-Datenbank initialisiert: %s", settings.db_path)
    except Exception as exc:
        logger.error("Datenbank konnte nicht initialisiert werden: %s", exc)
        state.database = None
        # Kein Return – der Classifier kann ohne DB laufen (Degraded-Modus)

    # --- PaperlessClient (mit Retry bei Verbindungsfehler, E-033) ---
    paperless_initialized = False
    max_retry_seconds = 600  # 10 Minuten Gesamtzeit
    retry_interval = 10.0    # Start: 10 Sekunden
    max_interval = 60.0      # Deckel: 60 Sekunden
    total_waited = 0.0
    attempt = 0

    while not paperless_initialized and total_waited < max_retry_seconds:
        attempt += 1
        try:
            from app.paperless.client import PaperlessClient

            if state.paperless_client is None:
                state.paperless_client = PaperlessClient(
                    base_url=settings.paperless_url,
                    token=settings.paperless_api_token,
                )
                await state.paperless_client.__aenter__()

            # Stammdaten-Cache laden (Korrespondenten, Tags, Typen, Pfade)
            stats = await state.paperless_client.load_cache()

            paperless_initialized = True
            logger.info("PaperlessClient initialisiert")
            logger.info(
                "Stammdaten-Cache geladen: %s",
                ", ".join(f"{k}={v}" for k, v in stats.items()),
            )
        except Exception as exc:
            if attempt == 1:
                logger.warning(
                    "Paperless nicht erreichbar (Versuch %d): %s – "
                    "Retry mit Backoff (max. %ds)",
                    attempt, exc, max_retry_seconds,
                )
            else:
                logger.warning(
                    "Paperless nicht erreichbar (Versuch %d, %.0fs/%ds): %s – "
                    "nächster Versuch in %.0fs",
                    attempt, total_waited, max_retry_seconds, exc,
                    retry_interval,
                )

            # PaperlessClient aufräumen falls teilweise initialisiert
            if state.paperless_client is not None:
                try:
                    await state.paperless_client.__aexit__(None, None, None)
                except Exception:
                    pass
                state.paperless_client = None

            await asyncio.sleep(retry_interval)
            total_waited += retry_interval
            retry_interval = min(retry_interval * 2, max_interval)

    if not paperless_initialized:
        logger.error(
            "Paperless nach %ds nicht erreichbar – "
            "Container läuft im Degraded-Modus (kein Poller). "
            "Reconnect wird im Hintergrund versucht.",
            max_retry_seconds,
        )
        state.paperless_client = None
        # Hintergrund-Task für periodische Reconnect-Versuche starten
        asyncio.create_task(
            _paperless_reconnect_loop(settings),
            name="paperless-reconnect",
        )
        return

    # --- ClaudeClient, Pipeline, Poller (gemeinsame Init-Logik, E-033) ---
    await _initialize_remaining_services(settings)


async def shutdown() -> None:
    """Graceful Shutdown: Poller stoppen, Clients und DB schließen.

    Wird beim Container-Stop (SIGTERM) aufgerufen.  Reihenfolge:
    1. Poller stoppen (wartet auf aktuelles Dokument)
    2. ClaudeClient schließen
    3. PaperlessClient schließen
    4. Datenbank schließen
    """
    # State-Variablen werden über app.state zurückgesetzt

    logger.info("Shutdown eingeleitet...")

    # Poller stoppen
    if state.poller is not None:
        try:
            await state.poller.stop()
            logger.info("Poller gestoppt")
        except Exception as exc:
            logger.error("Fehler beim Stoppen des Pollers: %s", exc)
        state.poller = None

    # ClaudeClient schließen
    if state.claude_client is not None:
        try:
            await state.claude_client.__aexit__(None, None, None)
            logger.info("ClaudeClient geschlossen")
        except Exception as exc:
            logger.error("Fehler beim Schließen des ClaudeClients: %s", exc)
        state.claude_client = None

    # PaperlessClient schließen
    if state.paperless_client is not None:
        try:
            await state.paperless_client.__aexit__(None, None, None)
            logger.info("PaperlessClient geschlossen")
        except Exception as exc:
            logger.error("Fehler beim Schließen des PaperlessClients: %s", exc)
        state.paperless_client = None

    # Datenbank schließen (AP-06)
    if state.database is not None:
        try:
            await state.database.close()
            logger.info("Datenbank geschlossen")
        except Exception as exc:
            logger.error("Fehler beim Schließen der Datenbank: %s", exc)
        state.database = None

    logger.info("=" * 60)
    logger.info("Paperless Claude Classifier beendet")
    logger.info("=" * 60)


app.on_startup(startup)
app.on_startup(async_startup)
app.on_shutdown(shutdown)


# --- UI-Seiten registrieren (AP-07) ---
# Muss vor ui.run() aufgerufen werden, damit die @ui.page-Routen
# beim Server-Start bekannt sind.
from app.ui import register_pages

register_pages()


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
