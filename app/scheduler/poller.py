"""Polling-Loop für automatische Dokumenterkennung und -verarbeitung.

Läuft als asyncio-Task im Hintergrund und prüft in konfigurierbaren
Intervallen ob neue Dokumente mit Tag "NEU" in Paperless vorliegen.

Gefundene Dokumente werden sequenziell über die ClassificationPipeline
verarbeitet.  Fehler bei einzelnen Dokumenten stoppen den Loop nicht.

Prüft zusätzlich bei jedem Durchlauf ob die Schema-Analyse
ausgelöst werden soll (AP-10, Phase 3).

Steuerung über die Web-UI (Start/Stop/Pause) wird über Methoden am
Poller-Objekt ermöglicht – die UI selbst kommt in einem späteren AP.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

from app.classifier.pipeline import ClassificationPipeline, PipelineResult
from app.classifier.resolver import TAG_NEU_ID
from app.claude.client import ClaudeAPIError, CostLimitReachedError
from app.logging_config import get_logger
from app.schema_matrix.trigger import SchemaTrigger

if TYPE_CHECKING:
    from app.claude.cost_tracker import CostTracker
    from app.config import Settings
    from app.db.database import Database
    from app.paperless.client import PaperlessClient

logger = get_logger("scheduler")

# Pause zwischen zwei aufeinanderfolgenden Dokumenten (Sekunden).
# Verhindert Rate-Limit-Fehler bei Batch-Verarbeitung (z.B. initialer Import).
# Im Normalbetrieb (1-3 Dokumente pro Zyklus) vernachlässigbar.
DOCUMENT_DELAY_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Poller-Status
# ---------------------------------------------------------------------------

class PollerState(str, Enum):
    """Mögliche Zustände des Pollers."""
    STOPPED = "stopped"       # Nicht gestartet oder beendet
    RUNNING = "running"       # Aktiv, wartet auf nächsten Zyklus oder verarbeitet
    PAUSED = "paused"         # Pausiert durch Nutzer oder Kostenlimit
    PROCESSING = "processing" # Gerade bei der Verarbeitung eines Dokuments


@dataclass
class PollerStatus:
    """Aktueller Status des Pollers für Dashboard und API.

    Wird bei jedem Zykluswechsel aktualisiert und kann von der
    Web-UI jederzeit abgefragt werden.
    """
    state: PollerState = PollerState.STOPPED
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    current_document_id: int | None = None
    documents_processed: int = 0
    documents_errored: int = 0
    last_error: str | None = None
    cost_limit_paused: bool = False

    # Ergebnisse des letzten Laufs
    last_run_results: list[PipelineResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

class Poller:
    """Polling-basierte Dokumenterkennung und -verarbeitung.

    Der Poller fragt Paperless in regelmäßigen Abständen nach Dokumenten
    mit Tag "NEU" (ID 12) ab und schickt jedes durch die Pipeline.

    Verwendung:
        poller = Poller(paperless, pipeline, settings)
        task = poller.start()   # Gibt asyncio.Task zurück
        ...
        await poller.stop()     # Graceful Shutdown

    Die Instanz hält ihren eigenen Status (PollerStatus), der von der
    Web-UI abgefragt werden kann.
    """

    def __init__(
        self,
        paperless: PaperlessClient,
        pipeline: ClassificationPipeline,
        settings: Settings,
        cost_tracker: CostTracker | None = None,
        database: Database | None = None,
    ) -> None:
        """Initialisiert den Poller.

        Args:
            paperless: Initialisierter PaperlessClient.
            pipeline: Konfigurierte ClassificationPipeline.
            settings: Anwendungseinstellungen (Intervall, Kostenlimit).
            cost_tracker: Optionaler CostTracker für Kostenlimit-Prüfung.
            database: Optionale Database-Instanz für Schema-Trigger (AP-10).
        """
        self._paperless = paperless
        self._pipeline = pipeline
        self._settings = settings
        self._cost_tracker = cost_tracker
        self._database = database

        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        # Nicht gesetzt = nicht pausiert → Verarbeitung läuft
        self._pause_event.set()

        # Schema-Trigger nur erstellen wenn DB verfügbar (AP-10)
        self._schema_trigger: SchemaTrigger | None = None
        if database is not None:
            self._schema_trigger = SchemaTrigger(database, settings)

        self.status = PollerStatus()

    # --- Steuerung (für Web-UI) ---

    def start(self) -> asyncio.Task[None]:
        """Startet den Polling-Loop als asyncio Background-Task.

        Returns:
            Der erstellte asyncio.Task.

        Raises:
            RuntimeError: Wenn der Poller bereits läuft.
        """
        if self._task is not None and not self._task.done():
            raise RuntimeError("Poller läuft bereits")

        self._stop_event.clear()
        self._pause_event.set()
        self.status.state = PollerState.RUNNING
        self.status.cost_limit_paused = False

        self._task = asyncio.create_task(
            self._run_loop(),
            name="poller-loop",
        )
        # Fehler im Task loggen statt stillschweigend verschlucken
        self._task.add_done_callback(self._on_task_done)

        logger.info(
            "Poller gestartet: Intervall=%ds, Kostenlimit=$%.2f",
            self._settings.polling_interval_seconds,
            self._settings.monthly_cost_limit_usd,
        )
        return self._task

    async def stop(self) -> None:
        """Stoppt den Polling-Loop graceful.

        Wartet bis der aktuelle Verarbeitungsschritt abgeschlossen ist,
        bricht aber den Sleep zwischen Zyklen sofort ab.
        """
        if self._task is None or self._task.done():
            logger.debug("Poller.stop() aufgerufen, aber kein aktiver Task")
            return

        logger.info("Poller wird gestoppt...")
        self._stop_event.set()
        # Falls pausiert: Pause aufheben, damit der Loop das Stop-Event sieht
        self._pause_event.set()

        try:
            await asyncio.wait_for(self._task, timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning("Poller-Task hat nach 60s nicht beendet – wird abgebrochen")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self.status.state = PollerState.STOPPED
        self.status.next_run_at = None
        self.status.current_document_id = None
        logger.info("Poller gestoppt")

    def pause(self) -> None:
        """Pausiert den Poller nach dem aktuellen Dokument.

        Der laufende Verarbeitungsschritt wird noch abgeschlossen.
        Fortsetzen mit resume().
        """
        if self.status.state in (PollerState.STOPPED,):
            logger.debug("Poller.pause() aufgerufen, aber Poller ist gestoppt")
            return

        self._pause_event.clear()
        self.status.state = PollerState.PAUSED
        logger.info("Poller pausiert")

    def resume(self) -> None:
        """Setzt einen pausierten Poller fort."""
        if self.status.state != PollerState.PAUSED:
            logger.debug("Poller.resume() aufgerufen, aber Poller ist nicht pausiert")
            return

        self._pause_event.set()
        self.status.state = PollerState.RUNNING
        self.status.cost_limit_paused = False
        logger.info("Poller fortgesetzt")

    @property
    def is_running(self) -> bool:
        """True wenn der Poller aktiv ist (nicht gestoppt)."""
        return self._task is not None and not self._task.done()

    # --- Hauptschleife ---

    async def _run_loop(self) -> None:
        """Endlosschleife: Dokumente suchen → verarbeiten → warten → wiederholen."""
        logger.info("Polling-Loop gestartet")

        while not self._stop_event.is_set():
            try:
                # Pause-Check: blockiert hier bis resume() aufgerufen wird
                await self._wait_for_resume_or_stop()
                if self._stop_event.is_set():
                    break

                # Kostenlimit prüfen bevor wir Dokumente suchen
                if await self._is_cost_limit_reached():
                    # Nächsten Zyklus abwarten – vielleicht ist nächsten Monat Budget da
                    await self._sleep_until_next_cycle()
                    continue

                # Dokumente mit Tag "NEU" suchen
                await self._process_pending_documents()

                # Schema-Analyse-Trigger prüfen (AP-10)
                await self._check_schema_trigger()

                # Bis zum nächsten Zyklus warten
                await self._sleep_until_next_cycle()

            except asyncio.CancelledError:
                logger.info("Polling-Loop abgebrochen (CancelledError)")
                raise
            except Exception as exc:
                # Unerwarteter Fehler im Loop selbst (nicht in der Pipeline) –
                # loggen und trotzdem weiterlaufen
                logger.exception("Unerwarteter Fehler im Polling-Loop: %s", exc)
                self.status.last_error = f"Loop-Fehler: {exc}"
                await self._sleep_until_next_cycle()

        logger.info("Polling-Loop beendet")

    async def _process_pending_documents(self) -> None:
        """Sucht und verarbeitet alle Dokumente mit Tag 'NEU'.

        Sequenzielle Verarbeitung: ein Dokument nach dem anderen.
        Fehler bei einem Dokument werden geloggt, stoppen aber nicht den Loop.
        """
        # Dokumente mit Tag "NEU" abrufen
        try:
            documents = await self._paperless.get_documents(tags=[TAG_NEU_ID])
        except Exception as exc:
            logger.error("Fehler beim Abrufen neuer Dokumente: %s", exc)
            self.status.last_error = f"Abruf-Fehler: {exc}"
            return

        if not documents:
            logger.debug("Keine neuen Dokumente gefunden")
            self.status.last_run_at = datetime.now(timezone.utc)
            return

        logger.info("%d Dokument(e) mit Tag 'NEU' gefunden", len(documents))

        run_results: list[PipelineResult] = []

        for i, doc in enumerate(documents):
            # Vor jedem Dokument: Stop/Pause prüfen
            if self._stop_event.is_set():
                logger.info("Stop-Signal empfangen – breche Verarbeitung ab")
                break

            await self._wait_for_resume_or_stop()
            if self._stop_event.is_set():
                break

            # Kostenlimit vor jedem Dokument prüfen
            if await self._is_cost_limit_reached():
                logger.warning(
                    "Kostenlimit erreicht – verbleibende Dokumente werden übersprungen"
                )
                break

            # Delay zwischen Dokumenten (nicht vor dem ersten)
            if i > 0:
                logger.debug(
                    "Warte %.1fs vor nächstem Dokument", DOCUMENT_DELAY_SECONDS,
                )
                await asyncio.sleep(DOCUMENT_DELAY_SECONDS)

            # Dokument verarbeiten
            self.status.state = PollerState.PROCESSING
            self.status.current_document_id = doc.id

            logger.info("Verarbeite Dokument %d: '%s'", doc.id, doc.title[:60])

            try:
                result = await self._pipeline.classify_document(doc.id)
                run_results.append(result)

                if result.success:
                    self.status.documents_processed += 1
                    logger.info(
                        "Dokument %d erfolgreich: %s (%.1fs, $%.6f)",
                        doc.id,
                        result.confidence.level.value if result.confidence else "?",
                        result.duration_seconds,
                        result.cost_usd,
                    )
                else:
                    self.status.documents_errored += 1
                    self.status.last_error = (
                        f"Dokument {doc.id}: {result.error}"
                    )
                    logger.warning(
                        "Dokument %d fehlgeschlagen: %s", doc.id, result.error,
                    )

            except ClaudeAPIError as exc:
                if exc.status_code in (429, 529):
                    # Rate-Limit oder Überlast: Zyklus abbrechen.
                    # Das Dokument wurde NICHT als Error markiert (Pipeline
                    # hat re-raised), NEU-Tag bleibt, ki_status bleibt null.
                    # Beim nächsten Zyklus wird es erneut versucht.
                    remaining = len(documents) - i - 1
                    logger.warning(
                        "Rate-Limit (HTTP %d) bei Dokument %d – "
                        "Zyklus wird abgebrochen, %d Dokument(e) verbleiben "
                        "für nächsten Zyklus",
                        exc.status_code, doc.id, remaining,
                    )
                    self.status.last_error = (
                        f"Rate-Limit bei Dokument {doc.id} (HTTP {exc.status_code})"
                    )
                    break
                # Andere ClaudeAPIErrors (nicht 429/529) sollten nicht hier
                # landen (Pipeline fängt sie ab), aber für den Fall:
                self.status.documents_errored += 1
                self.status.last_error = f"Dokument {doc.id}: {exc}"
                logger.exception(
                    "Unerwarteter Claude-Fehler bei Dokument %d: %s",
                    doc.id, exc,
                )

            except CostLimitReachedError:
                # Pipeline hat CostLimitReachedError geworfen –
                # Poller pausiert sich selbst
                logger.warning(
                    "Kostenlimit während Verarbeitung von Dokument %d erreicht",
                    doc.id,
                )
                self._pause_for_cost_limit()
                break

            except Exception as exc:
                # Sollte nicht vorkommen (Pipeline fängt alles),
                # aber sicher ist sicher
                self.status.documents_errored += 1
                self.status.last_error = f"Dokument {doc.id}: {exc}"
                logger.exception(
                    "Unerwarteter Fehler bei Dokument %d: %s", doc.id, exc,
                )

        # Zyklus abschließen
        self.status.current_document_id = None
        self.status.last_run_at = datetime.now(timezone.utc)
        self.status.last_run_results = run_results

        if not self._stop_event.is_set() and self.status.state != PollerState.PAUSED:
            self.status.state = PollerState.RUNNING

        processed = sum(1 for r in run_results if r.success)
        errored = sum(1 for r in run_results if not r.success)
        total_cost = sum(r.cost_usd for r in run_results)
        logger.info(
            "Zyklus abgeschlossen: %d verarbeitet, %d Fehler, $%.6f Kosten",
            processed, errored, total_cost,
        )

    # --- Hilfsmethoden ---

    async def _is_cost_limit_reached(self) -> bool:
        """Prüft ob das monatliche Kostenlimit erreicht ist.

        Nutzt den CostTracker direkt, nicht den ClaudeClient –
        der Poller soll schon VOR dem API-Call entscheiden können.

        Async seit AP-06: CostTracker liest Monatsdaten aus SQLite.
        """
        if not self._cost_tracker:
            return False

        limit = self._settings.monthly_cost_limit_usd
        if limit <= 0:
            return False

        if await self._cost_tracker.is_limit_reached(limit):
            if not self.status.cost_limit_paused:
                current = await self._cost_tracker.get_monthly_cost()
                logger.warning(
                    "Monatliches Kostenlimit erreicht: $%.2f / $%.2f",
                    current, limit,
                )
                self._pause_for_cost_limit()
            return True

        return False

    def _pause_for_cost_limit(self) -> None:
        """Pausiert den Poller wegen Kostenlimit-Überschreitung."""
        self._pause_event.clear()
        self.status.state = PollerState.PAUSED
        self.status.cost_limit_paused = True
        logger.warning("Poller pausiert: Kostenlimit erreicht")

    async def _wait_for_resume_or_stop(self) -> None:
        """Wartet bis der Poller fortgesetzt oder gestoppt wird.

        Kehrt sofort zurück wenn nicht pausiert.
        """
        if self._pause_event.is_set():
            return

        logger.debug("Poller pausiert – warte auf resume() oder stop()")

        # Warten bis entweder Pause aufgehoben oder Stop signalisiert wird
        stop_task = asyncio.create_task(self._stop_event.wait())
        resume_task = asyncio.create_task(self._pause_event.wait())

        try:
            done, pending = await asyncio.wait(
                {stop_task, resume_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Nicht abgeschlossene Tasks aufräumen
            for task in (stop_task, resume_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def _check_schema_trigger(self) -> None:
        """Prüft ob die Schema-Analyse ausgelöst werden soll.

        In AP-10 wird nur geloggt – die eigentliche Analyse (Collector +
        Opus-Aufruf) wird in AP-11 implementiert.  Fehler hier dürfen
        den Polling-Loop nicht unterbrechen.
        """
        if self._schema_trigger is None:
            return

        try:
            should_run, reason = await self._schema_trigger.should_run()

            if should_run:
                logger.info(
                    "Schema-Trigger ausgelöst: %s – "
                    "Analyse noch nicht implementiert (→ AP-11)",
                    reason,
                )
                # TODO AP-11: Hier Collector + Opus-Analyse starten
                #   collector = SchemaCollector(self._paperless, cache)
                #   result = await collector.collect(...)
                #   ... Opus-Analyse + Storage ...
            else:
                logger.debug("Schema-Trigger: %s", reason)

        except Exception as exc:
            # Schema-Trigger-Fehler darf Poller nie stoppen
            logger.warning(
                "Schema-Trigger-Prüfung fehlgeschlagen: %s",
                exc,
                exc_info=True,
            )

    async def _sleep_until_next_cycle(self) -> None:
        """Wartet das konfigurierte Polling-Intervall ab.

        Kann durch stop() vorzeitig abgebrochen werden.
        """
        interval = self._settings.polling_interval_seconds
        self.status.next_run_at = datetime.now(timezone.utc).replace(
            microsecond=0,
        )
        self.status.next_run_at += timedelta(seconds=interval)

        logger.debug(
            "Nächster Zyklus in %ds (um %s)",
            interval,
            self.status.next_run_at.isoformat(),
        )

        # asyncio.wait_for mit Stop-Event – bricht ab wenn stop() gerufen wird
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=interval,
            )
            # Wenn wir hier ankommen, wurde stop() aufgerufen
            logger.debug("Sleep abgebrochen durch Stop-Signal")
        except asyncio.TimeoutError:
            # Normaler Fall: Timeout = Intervall abgelaufen
            pass

    @staticmethod
    def _on_task_done(task: asyncio.Task[None]) -> None:
        """Callback für den asyncio-Task: loggt unerwartete Fehler."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Poller-Task unerwartet beendet: %s: %s",
                type(exc).__name__, exc,
            )
