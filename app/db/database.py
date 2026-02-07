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
    opus_cost_usd REAL DEFAULT 0.0
);
"""

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
        """
        conn = self.connection

        await conn.execute(_SCHEMA_PROCESSED_DOCUMENTS)
        await conn.execute(_SCHEMA_DAILY_COSTS)

        for idx_sql in _INDEXES:
            await conn.execute(idx_sql)

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

        await conn.execute(
            """
            INSERT INTO daily_costs (
                date, documents_processed,
                total_input_tokens, total_output_tokens,
                total_cache_read_tokens, total_cache_creation_tokens,
                total_cost_usd,
                sonnet_count, haiku_count, opus_count, batch_count,
                opus_cost_usd
            ) VALUES (
                ?, 1,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?, ?,
                ?
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
                opus_cost_usd = opus_cost_usd + excluded.opus_cost_usd
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
        total_cost = float(row["total_cost"])
        opus_cost = float(row["opus_cost"])

        # Sonnet- und Haiku-Kosten werden approximiert:
        # total_cost - opus_cost = sonnet + haiku
        # Genauere Aufschlüsselung kommt über processed_documents
        non_opus_cost = total_cost - opus_cost

        if sonnet_count > 0:
            result["sonnet"] = {"count": sonnet_count}
        if haiku_count > 0:
            result["haiku"] = {"count": haiku_count}
        if opus_count > 0:
            result["opus"] = {"count": opus_count, "cost_usd": opus_cost}
        if batch_count > 0:
            result["batch"] = {"count": batch_count}

        # Gesamtkosten-Aufschlüsselung aus processed_documents
        # (genauer, aber langsamer – nur auf Anfrage in Phase 2)

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
