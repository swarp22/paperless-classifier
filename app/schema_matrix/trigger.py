"""Trigger-Logik für die Schema-Analyse.

Drei Auslöser, je nachdem was zuerst eintritt:

1. **Zeitplan**: Wöchentlich (Sonntag 03:00 Uhr)
2. **Schwellwert**: ≥20 neue Dokumente seit letztem Lauf
3. **Manuell**: Via Web-UI-Button (kommt in AP-12)

Mindestabstand: 24h zwischen automatischen Läufen.
Der Trigger wird bei jedem Poller-Durchlauf geprüft.

AP-10: Collector & Datenmodell (Phase 3)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.config import SchemaMatrixSchedule, Settings
from app.db.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trigger-Prüfung
# ---------------------------------------------------------------------------

class SchemaTrigger:
    """Prüft ob eine Schema-Analyse ausgelöst werden soll.

    Wird vom Poller bei jedem Durchlauf aufgerufen.  Die eigentliche
    Analyse wird NICHT hier durchgeführt – nur die Entscheidung ob
    sie nötig ist.

    Verwendung:
        trigger = SchemaTrigger(database, settings)
        should_run, reason = await trigger.should_run()
        if should_run:
            # Schema-Analyse starten (AP-11)
            pass
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
    ) -> None:
        self._db = database
        self._settings = settings

    async def should_run(self) -> tuple[bool, str]:
        """Prüft alle automatischen Trigger-Bedingungen.

        Verwendet den letzten Analyse-*Versuch* (nicht nur den letzten
        Erfolg) für den Cooldown-Timer.  Damit wird verhindert, dass
        fehlgeschlagene Läufe eine Endlosschleife auslösen.  (AP-11)

        Returns:
            Tuple (should_run, reason):
            - should_run: True wenn die Analyse ausgelöst werden soll
            - reason: Menschenlesbare Begründung (für Logging/UI)
        """
        # Schema-Analyse im Manuell-Modus? Dann keine automatischen Trigger.
        if self._settings.schema_matrix_schedule == SchemaMatrixSchedule.MANUAL:
            return (False, "Zeitplan auf 'manual' gesetzt")

        # Cooldown basiert auf dem letzten VERSUCH (inkl. Fehler)
        last_attempt = await self._db.get_last_schema_analysis_attempt()

        # Noch nie gelaufen → sofort auslösen
        if last_attempt is None:
            logger.info(
                "Schema-Trigger: Noch nie gelaufen – Erstlauf wird ausgelöst",
            )
            return (True, "Erstlauf (noch nie ausgeführt)")

        # Zeitpunkt des letzten Versuchs parsen (für Cooldown)
        attempt_at = last_attempt.get("run_at", "")
        try:
            attempt_dt = datetime.fromisoformat(attempt_at)
            if attempt_dt.tzinfo is None:
                attempt_dt = attempt_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning(
                "Schema-Trigger: run_at nicht parsbar: '%s' – Erstlauf wird ausgelöst",
                attempt_at,
            )
            return (True, "Letzter Lauf-Zeitpunkt nicht parsbar")

        now = datetime.now(timezone.utc)
        hours_since_attempt = (now - attempt_dt).total_seconds() / 3600

        # Mindestabstand: N Stunden zwischen Versuchen (auch fehlgeschlagenen)
        min_interval = self._settings.schema_matrix_min_interval_h
        if hours_since_attempt < min_interval:
            last_status = last_attempt.get("status", "?")
            return (
                False,
                f"Mindestabstand nicht erreicht: "
                f"{hours_since_attempt:.1f}h / {min_interval}h "
                f"(letzter Versuch: {last_status})",
            )

        # Ab hier: Cooldown ist abgelaufen, prüfe ob Trigger-Bedingung erfüllt

        # Für Zeitplan und Schwellwert brauchen wir den letzten ERFOLG
        last_success = await self._db.get_last_schema_analysis_run()

        # Noch nie erfolgreich gelaufen → auslösen
        if last_success is None:
            logger.info(
                "Schema-Trigger: Noch kein erfolgreicher Lauf – wird ausgelöst",
            )
            return (True, "Kein erfolgreicher Lauf vorhanden")

        success_at = last_success.get("run_at", "")
        try:
            success_dt = datetime.fromisoformat(success_at)
            if success_dt.tzinfo is None:
                success_dt = success_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return (True, "Letzter Erfolg-Zeitpunkt nicht parsbar")

        hours_since_success = (now - success_dt).total_seconds() / 3600

        # Trigger 1: Wöchentlicher Zeitplan (≥168h = 7 Tage seit letztem Erfolg)
        if hours_since_success >= 7 * 24:
            logger.info(
                "Schema-Trigger: Wöchentlicher Zeitplan erreicht "
                "(%.1f Stunden seit letztem Erfolg)",
                hours_since_success,
            )
            return (True, "Wöchentlicher Zeitplan")

        # Trigger 2: Schwellwert neue Dokumente (seit letztem Erfolg)
        threshold = self._settings.schema_matrix_threshold
        docs_since = await self._db.get_documents_processed_since(success_at)

        if docs_since >= threshold:
            logger.info(
                "Schema-Trigger: Schwellwert erreicht – "
                "%d neue Dokumente (Schwelle: %d)",
                docs_since, threshold,
            )
            return (True, f"Schwellwert: {docs_since}/{threshold} neue Dokumente")

        return (
            False,
            f"Kein Trigger aktiv: "
            f"{hours_since_success:.1f}h seit Erfolg, "
            f"{docs_since}/{threshold} neue Dokumente",
        )

    async def get_status(self) -> dict[str, Any]:
        """Aktueller Trigger-Status für UI-Anzeige.

        Returns:
            Dict mit Informationen zum Trigger-Zustand.
        """
        last_success = await self._db.get_last_schema_analysis_run()
        last_attempt = await self._db.get_last_schema_analysis_attempt()
        threshold = self._settings.schema_matrix_threshold

        status: dict[str, Any] = {
            "schedule": self._settings.schema_matrix_schedule.value,
            "threshold": threshold,
            "min_interval_h": self._settings.schema_matrix_min_interval_h,
            "last_run": None,
            "last_attempt": None,
            "last_attempt_status": None,
            "docs_since_last_run": 0,
            "threshold_progress_pct": 0.0,
            "next_scheduled": None,
        }

        # Letzter Versuch (für Cooldown-Anzeige)
        if last_attempt:
            status["last_attempt"] = last_attempt.get("run_at", "")
            status["last_attempt_status"] = last_attempt.get("status", "?")

        # Letzter Erfolg (für Zeitplan + Schwellwert)
        if last_success:
            last_run_at = last_success.get("run_at", "")
            status["last_run"] = last_run_at

            docs_since = await self._db.get_documents_processed_since(
                last_run_at,
            )
            status["docs_since_last_run"] = docs_since
            status["threshold_progress_pct"] = min(
                100.0, (docs_since / threshold) * 100.0,
            )

            # Nächsten geplanter Lauf (Sonntag 03:00)
            try:
                last_dt = datetime.fromisoformat(last_run_at)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)

                days_until_sunday = (6 - last_dt.weekday()) % 7
                if days_until_sunday == 0:
                    days_until_sunday = 7
                from datetime import timedelta
                next_sunday = last_dt.replace(
                    hour=3, minute=0, second=0, microsecond=0,
                ) + timedelta(days=days_until_sunday)
                status["next_scheduled"] = next_sunday.isoformat()
            except (ValueError, TypeError):
                pass

            remaining_docs = max(0, threshold - docs_since)
            status["remaining_docs_to_threshold"] = remaining_docs

        return status
