"""SQLite State-Management für den Paperless Claude Classifier.

Verwaltet die persistente Speicherung von Verarbeitungshistorie und
Kostenaggregation.  Nutzt aiosqlite für async Zugriff.

Schema-Migrationen erfolgen über CREATE TABLE IF NOT EXISTS.
Die Datenbank liegt unter /app/data/classifier.db (Docker-Volume).

Tabellen (Phase 1):
- processed_documents: Verarbeitungshistorie pro Dokument-Versuch
- daily_costs: Aggregierte Tageskosten für schnelle Dashboard-Abfragen

Tabellen für Phase 3 (Schema-Analyse) werden hier NICHT angelegt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datenklassen für typsichere Übergabe
# ---------------------------------------------------------------------------

@dataclass
class ProcessedDocumentRecord:
    """Datensatz für einen Verarbeitungsversuch eines Dokuments.

    Wird von der Pipeline befüllt und an Database.insert_processed_document()
    übergeben.  Enthält alle Informationen aus PipelineResult, die für
    die Persistierung relevant sind.
    """

    # Pflichtfelder
    paperless_id: int
    model_used: str
    processing_mode: str = "immediate"

    # Claude-Ergebnis (None bei Fehler vor API-Aufruf)
    classification_json: str = ""
    confidence: str = ""           # "high", "medium", "low"
    reasoning: str | None = None

    # Status
    status: str = "classified"     # "classified", "review", "error"
    error_message: str | None = None

    # Token-Verbrauch
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    # Timing
    duration_seconds: float = 0.0

    # Phase 3/4 (vorab angelegt, aktuell immer None)
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    batch_id: str | None = None


@dataclass
class DailyCostSummary:
    """Aggregierte Tageskosten für Dashboard-Anzeige."""

    date: str                         # YYYY-MM-DD
    documents_processed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    sonnet_count: int = 0
    haiku_count: int = 0
    opus_count: int = 0
    batch_count: int = 0
    opus_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Schema-Definitionen
# ---------------------------------------------------------------------------

_SCHEMA_PROCESSED_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS processed_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paperless_id INTEGER NOT NULL,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    model_used TEXT NOT NULL,
    processing_mode TEXT NOT NULL DEFAULT 'immediate',

    -- Claude-Ergebnis
    classification_json TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    reasoning TEXT,

    -- Status
    status TEXT NOT NULL DEFAULT 'classified',
    error_message TEXT,

    -- Review (Phase 3 – vorab angelegt)
    reviewed_by TEXT,
    reviewed_at TIMESTAMP,

    -- Token-Verbrauch
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,

    -- Timing
    duration_seconds REAL DEFAULT 0.0,

    -- Batch (Phase 4 – vorab angelegt)
    batch_id TEXT
);
"""

_SCHEMA_DAILY_COSTS = """
CREATE TABLE IF NOT EXISTS daily_costs (
    date TEXT PRIMARY KEY,
    documents_processed INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0.0,
    sonnet_count INTEGER DEFAULT 0,
    haiku_count INTEGER DEFAULT 0,
    opus_count INTEGER DEFAULT 0,
    batch_count INTEGER DEFAULT 0,
    opus_cost_usd REAL DEFAULT 0.0,
    sonnet_cost_usd REAL DEFAULT 0.0,
    haiku_cost_usd REAL DEFAULT 0.0
);
"""

# Spalten die per ALTER TABLE nachträglich ergänzt werden (AP-09)
_DAILY_COSTS_MIGRATIONS: list[str] = [
    "ALTER TABLE daily_costs ADD COLUMN sonnet_cost_usd REAL DEFAULT 0.0",
    "ALTER TABLE daily_costs ADD COLUMN haiku_cost_usd REAL DEFAULT 0.0",
]

# Indizes für häufige Abfragen
_INDEXES = [
    # Lookups nach Paperless-Dokument-ID (Mehrfachverarbeitung möglich)
    "CREATE INDEX IF NOT EXISTS idx_pd_paperless_id "
    "ON processed_documents(paperless_id);",

    # Zeitbereichs-Abfragen (Dashboard, Kosten)
    "CREATE INDEX IF NOT EXISTS idx_pd_processed_at "
    "ON processed_documents(processed_at);",

    # Review-Queue: Dokumente mit Status "review" finden
    "CREATE INDEX IF NOT EXISTS idx_pd_status "
    "ON processed_documents(status);",
]

# Modell-Klassifikation für daily_costs-Zähler
_SONNET_MODELS = {"claude-sonnet-4-5-20250929"}
_HAIKU_MODELS = {"claude-haiku-4-5-20251001"}
_OPUS_MODELS = {"claude-opus-4-6", "claude-opus-4-5-20251101"}


# ---------------------------------------------------------------------------
# Database-Klasse
# ---------------------------------------------------------------------------

class Database:
    """Async SQLite-Datenbankzugriff mit Schema-Migration.

    Verwendung:
        db = Database(path)
        await db.initialize()
        ...
        await db.close()

    Oder als Context-Manager:
        async with Database(path) as db:
            ...
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path) if isinstance(db_path, str) else db_path
        self._connection: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Erstellt Verbindung, setzt PRAGMAs und führt Schema-Migration aus."""
        # Verzeichnis erstellen falls nötig
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = await aiosqlite.connect(str(self._db_path))

        # WAL-Modus: Bessere Performance bei gleichzeitigen Lese-/Schreibzugriffen
        await self._connection.execute("PRAGMA journal_mode=WAL")
        # Foreign Keys aktivieren (SQLite-Standard: aus)
        await self._connection.execute("PRAGMA foreign_keys=ON")
        # Row-Factory für dict-artigen Zugriff
        self._connection.row_factory = aiosqlite.Row

        await self._migrate()
        logger.info("Datenbank initialisiert: %s", self._db_path)

    async def close(self) -> None:
        """Schließt die Datenbankverbindung."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("Datenbankverbindung geschlossen")

    async def __aenter__(self) -> Database:
        await self.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        """Gibt die aktive Verbindung zurück.

        Raises:
            RuntimeError: Wenn die Datenbank nicht initialisiert ist.
        """
        if self._connection is None:
            raise RuntimeError(
                "Datenbank nicht initialisiert – "
                "await db.initialize() aufrufen"
            )
        return self._connection

    # --- Schema-Migration ---

    async def _migrate(self) -> None:
        """Erstellt Tabellen und Indizes falls sie nicht existieren.

        Verwendet CREATE TABLE/INDEX IF NOT EXISTS – idempotent und
        sicher bei mehrfachem Aufruf.  Für spätere Schema-Änderungen
        kann hier eine Versions-basierte Migration ergänzt werden.

        AP-09: ALTER TABLE für Spalten die nachträglich ergänzt wurden.
        SQLite hat kein ADD COLUMN IF NOT EXISTS, daher per PRAGMA
        prüfen ob die Spalte bereits existiert.
        """
        conn = self.connection

        await conn.execute(_SCHEMA_PROCESSED_DOCUMENTS)
        await conn.execute(_SCHEMA_DAILY_COSTS)

        for idx_sql in _INDEXES:
            await conn.execute(idx_sql)

        # AP-09: Nachträgliche Spalten in daily_costs (idempotent)
        existing_cols = set()
        cursor = await conn.execute("PRAGMA table_info(daily_costs)")
        for row in await cursor.fetchall():
            existing_cols.add(row[1])  # row[1] = column name

        for alter_sql in _DAILY_COSTS_MIGRATIONS:
            # Spaltennamen aus "ADD COLUMN <name> ..." extrahieren
            parts = alter_sql.split("ADD COLUMN ", 1)
            if len(parts) == 2:
                col_name = parts[1].split()[0]
                if col_name not in existing_cols:
                    await conn.execute(alter_sql)
                    logger.info("Migration: Spalte '%s' zu daily_costs hinzugefügt", col_name)

        await conn.commit()
        logger.debug("Schema-Migration abgeschlossen")

    # --- Verarbeitungshistorie ---

    async def insert_processed_document(
        self,
        record: ProcessedDocumentRecord,
    ) -> int:
        """Fügt einen Verarbeitungsdatensatz ein und aktualisiert daily_costs.

        Beide Operationen laufen in einer Transaktion.

        Args:
            record: Vollständiger Datensatz eines Verarbeitungsversuchs.

        Returns:
            Die generierte Zeilen-ID (ROWID).
        """
        conn = self.connection

        # INSERT in processed_documents
        cursor = await conn.execute(
            """
            INSERT INTO processed_documents (
                paperless_id, model_used, processing_mode,
                classification_json, confidence, reasoning,
                status, error_message,
                reviewed_by, reviewed_at,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                cost_usd, duration_seconds, batch_id
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?
            )
            """,
            (
                record.paperless_id,
                record.model_used,
                record.processing_mode,
                record.classification_json,
                record.confidence,
                record.reasoning,
                record.status,
                record.error_message,
                record.reviewed_by,
                record.reviewed_at,
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_creation_tokens,
                record.cost_usd,
                record.duration_seconds,
                record.batch_id,
            ),
        )
        row_id = cursor.lastrowid or 0

        # UPSERT in daily_costs
        today_str = date.today().isoformat()

        # Modell-Zähler bestimmen
        sonnet_delta = 1 if record.model_used in _SONNET_MODELS else 0
        haiku_delta = 1 if record.model_used in _HAIKU_MODELS else 0
        opus_delta = 1 if record.model_used in _OPUS_MODELS else 0
        batch_delta = 1 if record.processing_mode == "batch" else 0
        opus_cost_delta = record.cost_usd if record.model_used in _OPUS_MODELS else 0.0
        sonnet_cost_delta = record.cost_usd if record.model_used in _SONNET_MODELS else 0.0
        haiku_cost_delta = record.cost_usd if record.model_used in _HAIKU_MODELS else 0.0

        await conn.execute(
            """
            INSERT INTO daily_costs (
                date, documents_processed,
                total_input_tokens, total_output_tokens,
                total_cache_read_tokens, total_cache_creation_tokens,
                total_cost_usd,
                sonnet_count, haiku_count, opus_count, batch_count,
                opus_cost_usd, sonnet_cost_usd, haiku_cost_usd
            ) VALUES (
                ?, 1,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(date) DO UPDATE SET
                documents_processed = documents_processed + 1,
                total_input_tokens = total_input_tokens + excluded.total_input_tokens,
                total_output_tokens = total_output_tokens + excluded.total_output_tokens,
                total_cache_read_tokens = total_cache_read_tokens + excluded.total_cache_read_tokens,
                total_cache_creation_tokens = total_cache_creation_tokens + excluded.total_cache_creation_tokens,
                total_cost_usd = total_cost_usd + excluded.total_cost_usd,
                sonnet_count = sonnet_count + excluded.sonnet_count,
                haiku_count = haiku_count + excluded.haiku_count,
                opus_count = opus_count + excluded.opus_count,
                batch_count = batch_count + excluded.batch_count,
                opus_cost_usd = opus_cost_usd + excluded.opus_cost_usd,
                sonnet_cost_usd = sonnet_cost_usd + excluded.sonnet_cost_usd,
                haiku_cost_usd = haiku_cost_usd + excluded.haiku_cost_usd
            """,
            (
                today_str,
                record.input_tokens,
                record.output_tokens,
                record.cache_read_tokens,
                record.cache_creation_tokens,
                record.cost_usd,
                sonnet_delta,
                haiku_delta,
                opus_delta,
                batch_delta,
                opus_cost_delta,
                sonnet_cost_delta,
                haiku_cost_delta,
            ),
        )

        await conn.commit()

        logger.debug(
            "Verarbeitungsdatensatz gespeichert: paperless_id=%d, "
            "row_id=%d, cost=$%.6f",
            record.paperless_id, row_id, record.cost_usd,
        )
        return row_id

    # --- Kosten-Abfragen ---

    async def get_monthly_cost(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> float:
        """Gesamtkosten eines Monats aus daily_costs.

        Args:
            year: Jahr (default: aktuelles Jahr).
            month: Monat 1-12 (default: aktueller Monat).

        Returns:
            Gesamtkosten in USD.
        """
        now = date.today()
        y = year or now.year
        m = month or now.month

        # YYYY-MM-Präfix für LIKE-Abfrage auf das date-Feld
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0.0) FROM daily_costs "
            "WHERE date LIKE ?",
            (prefix,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_daily_cost(self, day: date | None = None) -> float:
        """Kosten eines einzelnen Tages.

        Args:
            day: Datum (default: heute).

        Returns:
            Gesamtkosten in USD.
        """
        target = day or date.today()
        conn = self.connection
        cursor = await conn.execute(
            "SELECT COALESCE(total_cost_usd, 0.0) FROM daily_costs "
            "WHERE date = ?",
            (target.isoformat(),),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_monthly_document_count(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> int:
        """Anzahl verarbeiteter Dokumente im Monat.

        Args:
            year: Jahr (default: aktuelles Jahr).
            month: Monat 1-12 (default: aktueller Monat).

        Returns:
            Anzahl Dokumente.
        """
        now = date.today()
        y = year or now.year
        m = month or now.month
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(documents_processed), 0) FROM daily_costs "
            "WHERE date LIKE ?",
            (prefix,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_model_breakdown(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, dict[str, float | int]]:
        """Aufschlüsselung nach Modell für einen Monat.

        Liest aus daily_costs (aggregiert) statt aus processed_documents
        (schneller, besonders bei vielen Dokumenten).

        AP-09: Jetzt mit per-Modell-Kosten (sonnet_cost_usd, haiku_cost_usd).

        Returns:
            Dict mit Modellnamen als Key, z.B.:
            {"sonnet": {"count": 42, "cost_usd": 1.23}, ...}
        """
        now = date.today()
        y = year or now.year
        m = month or now.month
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(SUM(sonnet_count), 0) as sonnet_count,
                COALESCE(SUM(haiku_count), 0) as haiku_count,
                COALESCE(SUM(opus_count), 0) as opus_count,
                COALESCE(SUM(batch_count), 0) as batch_count,
                COALESCE(SUM(opus_cost_usd), 0.0) as opus_cost,
                COALESCE(SUM(sonnet_cost_usd), 0.0) as sonnet_cost,
                COALESCE(SUM(haiku_cost_usd), 0.0) as haiku_cost,
                COALESCE(SUM(total_cost_usd), 0.0) as total_cost
            FROM daily_costs
            WHERE date LIKE ?
            """,
            (prefix,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}

        result: dict[str, dict[str, float | int]] = {}

        sonnet_count = int(row["sonnet_count"])
        haiku_count = int(row["haiku_count"])
        opus_count = int(row["opus_count"])
        batch_count = int(row["batch_count"])
        opus_cost = float(row["opus_cost"])
        sonnet_cost = float(row["sonnet_cost"])
        haiku_cost = float(row["haiku_cost"])

        if sonnet_count > 0:
            result["sonnet"] = {"count": sonnet_count, "cost_usd": sonnet_cost}
        if haiku_count > 0:
            result["haiku"] = {"count": haiku_count, "cost_usd": haiku_cost}
        if opus_count > 0:
            result["opus"] = {"count": opus_count, "cost_usd": opus_cost}
        if batch_count > 0:
            # Batch-Kosten: nicht separat getrackt, da Batch-Docs
            # bereits in sonnet/haiku/opus enthalten sind
            result["batch"] = {"count": batch_count}

        return result

    async def get_daily_cost_series(
        self,
        days: int = 30,
    ) -> list[DailyCostSummary]:
        """Tageskosten der letzten N Tage für Chart-Darstellung.

        Args:
            days: Anzahl Tage (default: 30).

        Returns:
            Liste von DailyCostSummary, chronologisch sortiert.
        """
        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT * FROM daily_costs
            ORDER BY date DESC
            LIMIT ?
            """,
            (days,),
        )
        rows = await cursor.fetchall()

        summaries = [
            DailyCostSummary(
                date=row["date"],
                documents_processed=row["documents_processed"],
                total_input_tokens=row["total_input_tokens"],
                total_output_tokens=row["total_output_tokens"],
                total_cache_read_tokens=row["total_cache_read_tokens"],
                total_cache_creation_tokens=row["total_cache_creation_tokens"],
                total_cost_usd=row["total_cost_usd"],
                sonnet_count=row["sonnet_count"],
                haiku_count=row["haiku_count"],
                opus_count=row["opus_count"],
                batch_count=row["batch_count"],
                opus_cost_usd=row["opus_cost_usd"],
            )
            for row in rows
        ]
        # Chronologisch sortieren (älteste zuerst)
        summaries.reverse()
        return summaries

    async def get_document_history(
        self,
        paperless_id: int,
    ) -> list[dict[str, Any]]:
        """Verarbeitungshistorie für ein bestimmtes Dokument.

        Args:
            paperless_id: Paperless Dokument-ID.

        Returns:
            Liste aller Verarbeitungsversuche, neueste zuerst.
        """
        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT * FROM processed_documents
            WHERE paperless_id = ?
            ORDER BY processed_at DESC
            """,
            (paperless_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_documents(
        self,
        limit: int = 50,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Letzte verarbeitete Dokumente für Dashboard.

        Args:
            limit: Maximale Anzahl Ergebnisse.
            status_filter: Optional nur bestimmten Status zeigen.

        Returns:
            Liste von Dokumentdatensätzen, neueste zuerst.
        """
        conn = self.connection

        if status_filter:
            cursor = await conn.execute(
                """
                SELECT * FROM processed_documents
                WHERE status = ?
                ORDER BY processed_at DESC
                LIMIT ?
                """,
                (status_filter, limit),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT * FROM processed_documents
                ORDER BY processed_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Wochen-/Tages-Abfragen (AP-07: Dashboard) ---

    async def get_weekly_cost(self) -> float:
        """Kosten der aktuellen Kalenderwoche (Montag bis heute).

        Returns:
            Gesamtkosten in USD.
        """
        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(total_cost_usd), 0.0)
            FROM daily_costs
            WHERE date >= date('now', 'weekday 1', '-7 days')
              AND date <= date('now')
            """,
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_weekly_document_count(self) -> int:
        """Anzahl verarbeiteter Dokumente in der aktuellen Kalenderwoche.

        Returns:
            Anzahl Dokumente.
        """
        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(documents_processed), 0)
            FROM daily_costs
            WHERE date >= date('now', 'weekday 1', '-7 days')
              AND date <= date('now')
            """,
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_today_document_count(self) -> int:
        """Anzahl verarbeiteter Dokumente heute.

        Returns:
            Anzahl Dokumente.
        """
        conn = self.connection
        cursor = await conn.execute(
            "SELECT COALESCE(documents_processed, 0) FROM daily_costs "
            "WHERE date = date('now')",
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # --- Review Queue (AP-08) ---

    async def get_review_count(self) -> int:
        """Anzahl der Dokumente mit status='review' in der DB.

        Wird für das Sidebar-Badge verwendet.

        Returns:
            Anzahl offener Reviews.
        """
        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT COUNT(*) FROM processed_documents
            WHERE status = 'review'
            """,
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_review_documents(self) -> list[dict[str, Any]]:
        """Alle Dokumente mit status='review', neueste zuerst.

        Liefert den jeweils letzten Verarbeitungsversuch pro paperless_id,
        da ein Dokument mehrfach verarbeitet worden sein kann (Retry).

        Returns:
            Liste von Datensätzen mit classification_json, confidence,
            reasoning, model_used, cost_usd, paperless_id etc.
        """
        conn = self.connection
        # Nur den letzten Verarbeitungsversuch pro Dokument nehmen
        # (höchste id = neuester Versuch)
        cursor = await conn.execute(
            """
            SELECT pd.* FROM processed_documents pd
            INNER JOIN (
                SELECT paperless_id, MAX(id) AS max_id
                FROM processed_documents
                WHERE status = 'review'
                GROUP BY paperless_id
            ) latest ON pd.id = latest.max_id
            ORDER BY pd.processed_at DESC
            """,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def update_review_status(
        self,
        record_id: int,
        new_status: str,
        reviewed_by: str = "user",
    ) -> None:
        """Setzt den Review-Status eines processed_document-Eintrags.

        Args:
            record_id: Primärschlüssel (id) in processed_documents.
            new_status: Neuer Status ('classified' oder 'manual').
            reviewed_by: Wer die Review durchgeführt hat.

        Raises:
            ValueError: Wenn new_status ungültig ist.
        """
        if new_status not in ("classified", "manual"):
            raise ValueError(
                f"Ungültiger Review-Status: '{new_status}' "
                "(erlaubt: 'classified', 'manual')"
            )

        conn = self.connection
        await conn.execute(
            """
            UPDATE processed_documents
            SET status = ?,
                reviewed_by = ?,
                reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_status, reviewed_by, record_id),
        )
        await conn.commit()
        logger.debug(
            "Review-Status aktualisiert: record_id=%d → %s (by %s)",
            record_id, new_status, reviewed_by,
        )

    async def get_avg_cost_per_document(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> float:
        """Durchschnittliche Kosten pro Dokument im Monat.

        Returns:
            Durchschnitt in USD, 0.0 wenn keine Dokumente.
        """
        now = date.today()
        y = year or now.year
        m = month or now.month
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(SUM(total_cost_usd), 0.0) as total,
                COALESCE(SUM(documents_processed), 0) as count
            FROM daily_costs
            WHERE date LIKE ?
            """,
            (prefix,),
        )
        row = await cursor.fetchone()
        if not row or int(row["count"]) == 0:
            return 0.0
        return float(row["total"]) / int(row["count"])

    async def get_avg_tokens_per_document(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, float]:
        """Durchschnittliche Token-Zahlen pro Dokument im Monat.

        Returns:
            Dict mit "input" und "output" als Durchschnittswerte.
        """
        now = date.today()
        y = year or now.year
        m = month or now.month
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(SUM(total_input_tokens), 0) as total_in,
                COALESCE(SUM(total_output_tokens), 0) as total_out,
                COALESCE(SUM(total_cache_read_tokens), 0) as total_cache_r,
                COALESCE(SUM(documents_processed), 0) as count
            FROM daily_costs
            WHERE date LIKE ?
            """,
            (prefix,),
        )
        row = await cursor.fetchone()
        if not row or int(row["count"]) == 0:
            return {"input": 0.0, "output": 0.0}

        count = int(row["count"])
        # Input-Tokens inkl. Cache-Read (= effektiv gelesene Tokens)
        total_in = int(row["total_in"]) + int(row["total_cache_r"])
        total_out = int(row["total_out"])
        return {
            "input": total_in / count,
            "output": total_out / count,
        }

    async def get_cache_savings(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> float:
        """Geschätzte Cache-Ersparnis im Monat in USD.

        Berechnung: Cache-Read-Tokens hätten ohne Cache den vollen
        Input-Preis gekostet.  Die Differenz zum Cache-Read-Preis
        ist die Ersparnis.  Da wir nicht wissen, welches Modell die
        Cache-Tokens jeweils erzeugt hat, verwenden wir den gewichteten
        Durchschnitt aus den Modell-Zählern.

        Returns:
            Geschätzte Ersparnis in USD.
        """
        now = date.today()
        y = year or now.year
        m = month or now.month
        prefix = f"{y:04d}-{m:02d}-%"

        conn = self.connection
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(SUM(total_cache_read_tokens), 0) as cache_tokens,
                COALESCE(SUM(sonnet_count), 0) as sonnet_n,
                COALESCE(SUM(haiku_count), 0) as haiku_n,
                COALESCE(SUM(opus_count), 0) as opus_n
            FROM daily_costs
            WHERE date LIKE ?
            """,
            (prefix,),
        )
        row = await cursor.fetchone()
        if not row or int(row["cache_tokens"]) == 0:
            return 0.0

        cache_tokens = int(row["cache_tokens"])
        sonnet_n = int(row["sonnet_n"])
        haiku_n = int(row["haiku_n"])
        opus_n = int(row["opus_n"])
        total_n = sonnet_n + haiku_n + opus_n

        if total_n == 0:
            return 0.0

        # Gewichteter Durchschnitt: Differenz (input_price - cache_read_price)
        # pro Modell, gewichtet nach Anteil der Dokumente
        # Sonnet: 3.0 - 0.30 = 2.70 $/MTok Ersparnis
        # Haiku:  1.0 - 0.10 = 0.90 $/MTok Ersparnis
        # Opus:   5.0 - 0.50 = 4.50 $/MTok Ersparnis
        weighted_savings_per_mtok = (
            sonnet_n * 2.70 + haiku_n * 0.90 + opus_n * 4.50
        ) / total_n

        return (cache_tokens / 1_000_000) * weighted_savings_per_mtok
