"""CRUD-Operationen für die Schema-Analyse-Tabellen.

Bietet typisierte Datenklassen und async Methoden zum Lesen, Schreiben
und Aktualisieren der Schema-Analyse-Ergebnisse in SQLite.

Zentrale Upsert-Logik: Einträge mit is_manual=TRUE werden NICHT
automatisch überschrieben – nur explizite manuelle Änderungen via
Web-UI dürfen manuelle Einträge ändern.

AP-10: Collector & Datenmodell (Phase 3)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiosqlite

from app.db.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class TitlePattern:
    """Ein erkanntes Titel-Schema für eine (Dokumenttyp, Korrespondent)-Kombination."""

    document_type: str
    correspondent: str
    title_template: str
    rule_description: str | None = None
    confidence: str = "medium"
    document_count: int = 0
    outlier_count: int = 0
    outlier_titles: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    is_manual: bool = False

    # Nur bei Lesen aus DB gefüllt
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class PathRule:
    """Eine erkannte Pfad-Organisationsregel pro Topic."""

    topic: str
    rule_description: str
    path_template: str
    examples: list[str] = field(default_factory=list)
    topic_document_count: int = 0
    normalization_suggestions: list[dict[str, str]] = field(default_factory=list)
    confidence: str = "medium"
    is_manual: bool = False

    # Nur bei Lesen aus DB gefüllt
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class MappingEntry:
    """Eine Zuordnung (Korrespondent + Dokumenttyp) → Speicherpfad."""

    correspondent: str
    storage_path_name: str
    document_type: str | None = None      # None = Wildcard (alle Typen)
    storage_path_id: int | None = None
    mapping_type: str = "exact"           # 'exact' oder 'conditional'
    condition_description: str | None = None
    document_count: int = 0
    confidence: str = "medium"
    is_manual: bool = False

    # Nur bei Lesen aus DB gefüllt
    id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class AnalysisRunRecord:
    """Datensatz für einen Schema-Analyse-Lauf (Audit-Log)."""

    trigger_type: str                     # 'schedule', 'threshold', 'manual'
    total_documents: int = 0
    docs_since_last_run: int = 0
    title_schemas_created: int = 0
    title_schemas_updated: int = 0
    title_schemas_unchanged: int = 0
    path_rules_created: int = 0
    path_rules_updated: int = 0
    mappings_created: int = 0
    mappings_updated: int = 0
    manual_entries_preserved: int = 0
    suggestions_count: int = 0
    suggestions_json: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model_used: str | None = None
    status: str = "completed"
    error_message: str | None = None
    raw_response: str | None = None

    # Nur bei Lesen aus DB gefüllt
    id: int | None = None
    run_at: str | None = None


# ---------------------------------------------------------------------------
# Storage-Klasse
# ---------------------------------------------------------------------------

class SchemaStorage:
    """Async CRUD für die 4 Schema-Analyse-Tabellen.

    Verwendet die bestehende Database-Instanz (aiosqlite).
    Alle Methoden sind idempotent – Upsert statt Insert+Update.

    Verwendung:
        storage = SchemaStorage(database)
        await storage.upsert_title_pattern(pattern)
        patterns = await storage.get_all_title_patterns()
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def _conn(self) -> aiosqlite.Connection:
        """Kurzschreibweise für die DB-Connection."""
        return self._db.connection

    # =========================================================================
    # Titel-Schemata (Ebene 1)
    # =========================================================================

    async def upsert_title_pattern(
        self,
        pattern: TitlePattern,
        *,
        force: bool = False,
    ) -> tuple[str, int]:
        """Titel-Schema einfügen oder aktualisieren.

        Respektiert is_manual: Manuelle Einträge werden nur überschrieben
        wenn force=True (für explizite manuelle Änderungen via UI).

        Args:
            pattern: Das Titel-Schema.
            force: Wenn True, überschreibt auch manuelle Einträge.

        Returns:
            Tuple (action, row_id):
            - action: 'created', 'updated', 'preserved' (manuell, nicht überschrieben)
            - row_id: ID des Eintrags.
        """
        conn = self._conn

        # Prüfen ob manueller Eintrag existiert
        if not force:
            cursor = await conn.execute(
                """
                SELECT id, is_manual FROM schema_title_patterns
                WHERE document_type = ? AND correspondent = ?
                """,
                (pattern.document_type, pattern.correspondent),
            )
            existing = await cursor.fetchone()
            if existing and existing["is_manual"]:
                logger.debug(
                    "Manuelles Titel-Schema beibehalten: %s + %s",
                    pattern.document_type, pattern.correspondent,
                )
                return ("preserved", int(existing["id"]))

        cursor = await conn.execute(
            """
            INSERT INTO schema_title_patterns (
                document_type, correspondent, title_template,
                rule_description, confidence, document_count,
                outlier_count, outlier_titles, examples, is_manual,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(document_type, correspondent) DO UPDATE SET
                title_template = excluded.title_template,
                rule_description = excluded.rule_description,
                confidence = excluded.confidence,
                document_count = excluded.document_count,
                outlier_count = excluded.outlier_count,
                outlier_titles = excluded.outlier_titles,
                examples = excluded.examples,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                pattern.document_type,
                pattern.correspondent,
                pattern.title_template,
                pattern.rule_description,
                pattern.confidence,
                pattern.document_count,
                pattern.outlier_count,
                json.dumps(pattern.outlier_titles, ensure_ascii=False),
                json.dumps(pattern.examples, ensure_ascii=False),
                pattern.is_manual,
            ),
        )
        await conn.commit()

        row_id = cursor.lastrowid or 0
        # Herausfinden ob created oder updated
        if cursor.rowcount == 1:
            action = "created"
        else:
            # SQLite ON CONFLICT DO UPDATE ändert die Zeile
            action = "updated"

        # Bei Upsert die tatsächliche ID holen (lastrowid ist bei UPDATE 0)
        if row_id == 0:
            id_cursor = await conn.execute(
                """
                SELECT id FROM schema_title_patterns
                WHERE document_type = ? AND correspondent = ?
                """,
                (pattern.document_type, pattern.correspondent),
            )
            id_row = await id_cursor.fetchone()
            row_id = int(id_row["id"]) if id_row else 0

        logger.debug(
            "Titel-Schema %s: %s + %s → %s (id=%d)",
            action, pattern.document_type, pattern.correspondent,
            pattern.title_template, row_id,
        )
        return (action, row_id)

    async def get_all_title_patterns(self) -> list[TitlePattern]:
        """Alle Titel-Schemata laden, sortiert nach Dokumenttyp + Korrespondent."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM schema_title_patterns
            ORDER BY document_type, correspondent
            """,
        )
        rows = await cursor.fetchall()
        return [self._row_to_title_pattern(row) for row in rows]

    async def get_title_pattern(
        self,
        document_type: str,
        correspondent: str,
    ) -> TitlePattern | None:
        """Einzelnes Titel-Schema laden."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM schema_title_patterns
            WHERE document_type = ? AND correspondent = ?
            """,
            (document_type, correspondent),
        )
        row = await cursor.fetchone()
        return self._row_to_title_pattern(row) if row else None

    async def delete_title_pattern(self, pattern_id: int) -> bool:
        """Titel-Schema löschen. Gibt True zurück wenn gelöscht."""
        cursor = await self._conn.execute(
            "DELETE FROM schema_title_patterns WHERE id = ?",
            (pattern_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_title_pattern(row: aiosqlite.Row) -> TitlePattern:
        """Konvertiert eine DB-Zeile in ein TitlePattern-Objekt."""
        return TitlePattern(
            id=row["id"],
            document_type=row["document_type"],
            correspondent=row["correspondent"],
            title_template=row["title_template"],
            rule_description=row["rule_description"],
            confidence=row["confidence"],
            document_count=row["document_count"],
            outlier_count=row["outlier_count"],
            outlier_titles=_parse_json_list(row["outlier_titles"]),
            examples=_parse_json_list(row["examples"]),
            is_manual=bool(row["is_manual"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # =========================================================================
    # Pfad-Regeln (Ebene 2)
    # =========================================================================

    async def upsert_path_rule(
        self,
        rule: PathRule,
        *,
        force: bool = False,
    ) -> tuple[str, int]:
        """Pfad-Regel einfügen oder aktualisieren.

        Gleiche is_manual-Logik wie bei Titel-Schemata.
        """
        conn = self._conn

        if not force:
            cursor = await conn.execute(
                "SELECT id, is_manual FROM schema_path_rules WHERE topic = ?",
                (rule.topic,),
            )
            existing = await cursor.fetchone()
            if existing and existing["is_manual"]:
                logger.debug("Manuelle Pfad-Regel beibehalten: %s", rule.topic)
                return ("preserved", int(existing["id"]))

        cursor = await conn.execute(
            """
            INSERT INTO schema_path_rules (
                topic, rule_description, path_template,
                examples, topic_document_count,
                normalization_suggestions, confidence, is_manual,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(topic) DO UPDATE SET
                rule_description = excluded.rule_description,
                path_template = excluded.path_template,
                examples = excluded.examples,
                topic_document_count = excluded.topic_document_count,
                normalization_suggestions = excluded.normalization_suggestions,
                confidence = excluded.confidence,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                rule.topic,
                rule.rule_description,
                rule.path_template,
                json.dumps(rule.examples, ensure_ascii=False),
                rule.topic_document_count,
                json.dumps(rule.normalization_suggestions, ensure_ascii=False),
                rule.confidence,
                rule.is_manual,
            ),
        )
        await conn.commit()

        row_id = cursor.lastrowid or 0
        action = "created" if cursor.rowcount == 1 else "updated"

        if row_id == 0:
            id_cursor = await conn.execute(
                "SELECT id FROM schema_path_rules WHERE topic = ?",
                (rule.topic,),
            )
            id_row = await id_cursor.fetchone()
            row_id = int(id_row["id"]) if id_row else 0

        logger.debug("Pfad-Regel %s: %s (id=%d)", action, rule.topic, row_id)
        return (action, row_id)

    async def get_all_path_rules(self) -> list[PathRule]:
        """Alle Pfad-Regeln laden, sortiert nach Topic."""
        cursor = await self._conn.execute(
            "SELECT * FROM schema_path_rules ORDER BY topic",
        )
        rows = await cursor.fetchall()
        return [self._row_to_path_rule(row) for row in rows]

    async def get_path_rule(self, topic: str) -> PathRule | None:
        """Einzelne Pfad-Regel laden."""
        cursor = await self._conn.execute(
            "SELECT * FROM schema_path_rules WHERE topic = ?",
            (topic,),
        )
        row = await cursor.fetchone()
        return self._row_to_path_rule(row) if row else None

    async def delete_path_rule(self, rule_id: int) -> bool:
        """Pfad-Regel löschen."""
        cursor = await self._conn.execute(
            "DELETE FROM schema_path_rules WHERE id = ?",
            (rule_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_path_rule(row: aiosqlite.Row) -> PathRule:
        """Konvertiert eine DB-Zeile in ein PathRule-Objekt."""
        return PathRule(
            id=row["id"],
            topic=row["topic"],
            rule_description=row["rule_description"],
            path_template=row["path_template"],
            examples=_parse_json_list(row["examples"]),
            topic_document_count=row["topic_document_count"] or 0,
            normalization_suggestions=_parse_json_list(
                row["normalization_suggestions"],
            ),
            confidence=row["confidence"],
            is_manual=bool(row["is_manual"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # =========================================================================
    # Zuordnungsmatrix (Ebene 3)
    # =========================================================================

    async def upsert_mapping(
        self,
        mapping: MappingEntry,
        *,
        force: bool = False,
    ) -> tuple[str, int]:
        """Zuordnung einfügen oder aktualisieren.

        Gleiche is_manual-Logik wie bei Titel-Schemata.
        """
        conn = self._conn

        # document_type kann None sein (Wildcard) – in SQL wird NULL
        # nicht per = verglichen, daher IS-Operator
        if not force:
            cursor = await conn.execute(
                """
                SELECT id, is_manual FROM schema_mapping_matrix
                WHERE correspondent = ?
                  AND document_type IS ?
                  AND storage_path_name = ?
                """,
                (mapping.correspondent, mapping.document_type,
                 mapping.storage_path_name),
            )
            existing = await cursor.fetchone()
            if existing and existing["is_manual"]:
                logger.debug(
                    "Manuelles Mapping beibehalten: %s + %s → %s",
                    mapping.correspondent,
                    mapping.document_type or "*",
                    mapping.storage_path_name,
                )
                return ("preserved", int(existing["id"]))

        cursor = await conn.execute(
            """
            INSERT INTO schema_mapping_matrix (
                correspondent, document_type, storage_path_name,
                storage_path_id, mapping_type, condition_description,
                document_count, confidence, is_manual, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(correspondent, document_type, storage_path_name)
            DO UPDATE SET
                storage_path_id = excluded.storage_path_id,
                mapping_type = excluded.mapping_type,
                condition_description = excluded.condition_description,
                document_count = excluded.document_count,
                confidence = excluded.confidence,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                mapping.correspondent,
                mapping.document_type,
                mapping.storage_path_name,
                mapping.storage_path_id,
                mapping.mapping_type,
                mapping.condition_description,
                mapping.document_count,
                mapping.confidence,
                mapping.is_manual,
            ),
        )
        await conn.commit()

        row_id = cursor.lastrowid or 0
        action = "created" if cursor.rowcount == 1 else "updated"

        if row_id == 0:
            id_cursor = await conn.execute(
                """
                SELECT id FROM schema_mapping_matrix
                WHERE correspondent = ?
                  AND document_type IS ?
                  AND storage_path_name = ?
                """,
                (mapping.correspondent, mapping.document_type,
                 mapping.storage_path_name),
            )
            id_row = await id_cursor.fetchone()
            row_id = int(id_row["id"]) if id_row else 0

        logger.debug(
            "Mapping %s: %s + %s → %s (id=%d)",
            action, mapping.correspondent,
            mapping.document_type or "*",
            mapping.storage_path_name, row_id,
        )
        return (action, row_id)

    async def get_all_mappings(self) -> list[MappingEntry]:
        """Alle Zuordnungen laden, sortiert nach Korrespondent."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM schema_mapping_matrix
            ORDER BY correspondent, document_type
            """,
        )
        rows = await cursor.fetchall()
        return [self._row_to_mapping(row) for row in rows]

    async def get_mappings_for_correspondent(
        self,
        correspondent: str,
    ) -> list[MappingEntry]:
        """Alle Zuordnungen für einen Korrespondenten."""
        cursor = await self._conn.execute(
            """
            SELECT * FROM schema_mapping_matrix
            WHERE correspondent = ?
            ORDER BY document_type
            """,
            (correspondent,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_mapping(row) for row in rows]

    async def delete_mapping(self, mapping_id: int) -> bool:
        """Zuordnung löschen."""
        cursor = await self._conn.execute(
            "DELETE FROM schema_mapping_matrix WHERE id = ?",
            (mapping_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_mapping(row: aiosqlite.Row) -> MappingEntry:
        """Konvertiert eine DB-Zeile in ein MappingEntry-Objekt."""
        return MappingEntry(
            id=row["id"],
            correspondent=row["correspondent"],
            document_type=row["document_type"],
            storage_path_name=row["storage_path_name"],
            storage_path_id=row["storage_path_id"],
            mapping_type=row["mapping_type"],
            condition_description=row["condition_description"],
            document_count=row["document_count"],
            confidence=row["confidence"],
            is_manual=bool(row["is_manual"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # =========================================================================
    # Analyse-Läufe (Audit-Log)
    # =========================================================================

    async def insert_analysis_run(self, run: AnalysisRunRecord) -> int:
        """Speichert einen Schema-Analyse-Lauf.

        Returns:
            Die generierte Zeilen-ID.
        """
        conn = self._conn
        cursor = await conn.execute(
            """
            INSERT INTO schema_analysis_runs (
                trigger_type, total_documents, docs_since_last_run,
                title_schemas_created, title_schemas_updated,
                title_schemas_unchanged,
                path_rules_created, path_rules_updated,
                mappings_created, mappings_updated,
                manual_entries_preserved, suggestions_count,
                suggestions_json,
                input_tokens, output_tokens, cost_usd, model_used,
                status, error_message, raw_response
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                run.trigger_type,
                run.total_documents,
                run.docs_since_last_run,
                run.title_schemas_created,
                run.title_schemas_updated,
                run.title_schemas_unchanged,
                run.path_rules_created,
                run.path_rules_updated,
                run.mappings_created,
                run.mappings_updated,
                run.manual_entries_preserved,
                run.suggestions_count,
                run.suggestions_json,
                run.input_tokens,
                run.output_tokens,
                run.cost_usd,
                run.model_used,
                run.status,
                run.error_message,
                run.raw_response,
            ),
        )
        await conn.commit()
        row_id = cursor.lastrowid or 0
        logger.info(
            "Schema-Analyse-Lauf gespeichert: trigger=%s, status=%s, id=%d",
            run.trigger_type, run.status, row_id,
        )
        return row_id

    async def get_analysis_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Letzte Schema-Analyse-Läufe für Audit-Anzeige.

        Args:
            limit: Maximale Anzahl Ergebnisse.

        Returns:
            Liste von Lauf-Datensätzen, neueste zuerst.
        """
        cursor = await self._conn.execute(
            """
            SELECT * FROM schema_analysis_runs
            ORDER BY run_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Statistiken (für UI und Trigger)
    # =========================================================================

    async def get_schema_stats(self) -> dict[str, int]:
        """Schnelle Zählung aller Schema-Einträge.

        Returns:
            Dict mit Anzahl pro Kategorie und Anzahl manueller Einträge.
        """
        conn = self._conn

        stats: dict[str, int] = {}

        cursor = await conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN is_manual THEN 1 ELSE 0 END) as manual "
            "FROM schema_title_patterns",
        )
        row = await cursor.fetchone()
        stats["title_patterns_total"] = int(row["total"]) if row else 0
        stats["title_patterns_manual"] = int(row["manual"] or 0) if row else 0

        cursor = await conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN is_manual THEN 1 ELSE 0 END) as manual "
            "FROM schema_path_rules",
        )
        row = await cursor.fetchone()
        stats["path_rules_total"] = int(row["total"]) if row else 0
        stats["path_rules_manual"] = int(row["manual"] or 0) if row else 0

        cursor = await conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN is_manual THEN 1 ELSE 0 END) as manual "
            "FROM schema_mapping_matrix",
        )
        row = await cursor.fetchone()
        stats["mappings_total"] = int(row["total"]) if row else 0
        stats["mappings_manual"] = int(row["manual"] or 0) if row else 0

        cursor = await conn.execute(
            "SELECT COUNT(*) FROM schema_analysis_runs",
        )
        row = await cursor.fetchone()
        stats["analysis_runs"] = int(row[0]) if row else 0

        return stats

    async def set_manual_flag(
        self,
        table: str,
        entry_id: int,
        is_manual: bool,
    ) -> bool:
        """Setzt/entfernt das is_manual-Flag für einen Eintrag.

        Args:
            table: Tabellenname ('title_patterns', 'path_rules', 'mappings').
            entry_id: Primärschlüssel.
            is_manual: Neuer Wert.

        Returns:
            True wenn erfolgreich, False wenn Eintrag nicht gefunden.

        Raises:
            ValueError: Wenn der Tabellenname ungültig ist.
        """
        table_map = {
            "title_patterns": "schema_title_patterns",
            "path_rules": "schema_path_rules",
            "mappings": "schema_mapping_matrix",
        }
        real_table = table_map.get(table)
        if real_table is None:
            raise ValueError(
                f"Ungültiger Tabellenname: '{table}' "
                f"(erlaubt: {list(table_map.keys())})"
            )

        conn = self._conn
        cursor = await conn.execute(
            f"UPDATE {real_table} SET is_manual = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (is_manual, entry_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _parse_json_list(raw: str | None) -> list[Any]:
    """Parst einen JSON-String als Liste.  Gibt leere Liste bei Fehler."""
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
