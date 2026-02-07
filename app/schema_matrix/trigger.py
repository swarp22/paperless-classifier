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

        Returns:
            Tuple (should_run, reason):
            - should_run: True wenn die Analyse ausgelöst werden soll
            - reason: Menschenlesbare Begründung (für Logging/UI)
        """
        # Schema-Analyse im Manuell-Modus? Dann keine automatischen Trigger.
        if self._settings.schema_matrix_schedule == SchemaMatrixSchedule.MANUAL:
            return (False, "Zeitplan auf 'manual' gesetzt")

        last_run = await self._db.get_last_schema_analysis_run()

        # Noch nie gelaufen → sofort auslösen
        if last_run is None:
            logger.info(
                "Schema-Trigger: Noch nie gelaufen – Erstlauf wird ausgelöst",
            )
            return (True, "Erstlauf (noch nie ausgeführt)")

        # Zeitpunkt des letzten Laufs parsen
        last_run_at = last_run.get("run_at", "")
        try:
            last_run_dt = datetime.fromisoformat(last_run_at)
            # Falls kein Timezone-Info: UTC annehmen
            if last_run_dt.tzinfo is None:
                last_run_dt = last_run_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning(
                "Schema-Trigger: run_at nicht parsbar: '%s' – Erstlauf wird ausgelöst",
                last_run_at,
            )
            return (True, "Letzter Lauf-Zeitpunkt nicht parsbar")

        now = datetime.now(timezone.utc)
        hours_since_last = (now - last_run_dt).total_seconds() / 3600

        # Mindestabstand: N Stunden zwischen automatischen Läufen
        min_interval = self._settings.schema_matrix_min_interval_h
        if hours_since_last < min_interval:
            return (
                False,
                f"Mindestabstand nicht erreicht: "
                f"{hours_since_last:.1f}h / {min_interval}h",
            )

        # Trigger 1: Wöchentlicher Zeitplan (≥168h = 7 Tage)
        if hours_since_last >= 7 * 24:
            logger.info(
                "Schema-Trigger: Wöchentlicher Zeitplan erreicht "
                "(%.1f Stunden seit letztem Lauf)",
                hours_since_last,
            )
            return (True, "Wöchentlicher Zeitplan")

        # Trigger 2: Schwellwert neue Dokumente
        threshold = self._settings.schema_matrix_threshold
        docs_since = await self._db.get_documents_processed_since(last_run_at)

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
            f"{hours_since_last:.1f}h seit Lauf, "
            f"{docs_since}/{threshold} neue Dokumente",
        )

    async def get_status(self) -> dict[str, Any]:
        """Aktueller Trigger-Status für UI-Anzeige.

        Returns:
            Dict mit Informationen zum Trigger-Zustand.
        """
        last_run = await self._db.get_last_schema_analysis_run()
        threshold = self._settings.schema_matrix_threshold

        status: dict[str, Any] = {
            "schedule": self._settings.schema_matrix_schedule.value,
            "threshold": threshold,
            "min_interval_h": self._settings.schema_matrix_min_interval_h,
            "last_run": None,
            "docs_since_last_run": 0,
            "threshold_progress_pct": 0.0,
            "next_scheduled": None,
        }

        if last_run:
            last_run_at = last_run.get("run_at", "")
            status["last_run"] = last_run_at

            docs_since = await self._db.get_documents_processed_since(
                last_run_at,
            )
            status["docs_since_last_run"] = docs_since
            status["threshold_progress_pct"] = min(
                100.0, (docs_since / threshold) * 100.0,
            )

            # Nächster geplanter Lauf (Sonntag 03:00)
            try:
                last_dt = datetime.fromisoformat(last_run_at)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)

                # Nächsten Sonntag 03:00 berechnen
                days_until_sunday = (6 - last_dt.weekday()) % 7
                if days_until_sunday == 0:
                    # Wenn letzter Lauf auch Sonntag war, nächsten Sonntag
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
