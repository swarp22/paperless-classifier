"""Collector für die Schema-Analyse.

Sammelt alle Dokumente aus Paperless und bereitet sie lokal vor
(kein LLM-Aufruf).  Das Ergebnis ist eine strukturierte Datengrundlage,
die in AP-11 an Opus geschickt wird.

Drei Vorverarbeitungs-Ebenen:

1. **Titel-Gruppierung**: Titel gruppiert nach (Dokumenttyp, Korrespondent)
   → Input für Muster-Erkennung

2. **Pfad-Analyse**: Speicherpfade in Ebenen zerlegt (Topic/Objekt/Entität)
   → Input für Organisationsprinzip-Erkennung

3. **Zuordnungstabelle**: Welcher (Korrespondent + Typ) → welcher Pfad?
   → Input für Mapping-Analyse

Zusätzlich: Erkennung von Änderungen seit dem letzten Analyse-Lauf
(neue Korrespondenten, neue Pfade, korrigierte Titel).

AP-10: Collector & Datenmodell (Phase 3)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.paperless.cache import LookupCache
from app.paperless.client import PaperlessClient
from app.paperless.models import Document

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datenklassen für Collector-Output
# ---------------------------------------------------------------------------

@dataclass
class TitleGroup:
    """Eine Gruppe von Dokumenttiteln für eine (Typ, Korrespondent)-Kombination."""

    document_type: str
    correspondent: str
    titles: list[str] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    # Tag-Muster (AP-11b): Welche Tags kommen wie oft vor?
    tag_distribution: dict[str, int] = field(default_factory=dict)
    # Tags die bei >50% der Dokumente in dieser Gruppe vorkommen
    common_tags: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.titles)


@dataclass
class PathLevel:
    """Ein Speicherpfad, zerlegt in seine Hierarchie-Ebenen."""

    full_name: str                  # Originaler Pfad-Name (z.B. "Ärzte / Dr. Hansen")
    path_id: int                    # Paperless-ID
    levels: list[str]               # Zerlegte Ebenen ["Ärzte", "Dr. Hansen"]
    document_count: int = 0
    document_ids: list[int] = field(default_factory=list)

    @property
    def topic(self) -> str:
        """Oberkategorie (erste Ebene)."""
        return self.levels[0] if self.levels else self.full_name

    @property
    def depth(self) -> int:
        """Anzahl der Hierarchie-Ebenen."""
        return len(self.levels)


@dataclass
class MappingRecord:
    """Ein Dokument-Datensatz für die Zuordnungsanalyse."""

    document_id: int
    correspondent: str
    document_type: str
    storage_path: str
    storage_path_id: int
    title: str


@dataclass
class ChangesSinceLastRun:
    """Änderungen seit dem letzten Analyse-Lauf."""

    new_correspondents: list[str] = field(default_factory=list)
    new_document_types: list[str] = field(default_factory=list)
    new_storage_paths: list[str] = field(default_factory=list)
    # Dokumente deren Titel seit dem letzten Lauf geändert wurden
    title_corrections: list[dict[str, Any]] = field(default_factory=list)
    new_documents_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.new_correspondents
            or self.new_document_types
            or self.new_storage_paths
            or self.title_corrections
            or self.new_documents_count > 0
        )


@dataclass
class CollectorResult:
    """Vollständiges Ergebnis des Collectors – Input für die Opus-Analyse.

    Enthält alle drei Vorverarbeitungs-Ebenen plus Metadaten.
    """

    # Metadaten
    total_documents: int = 0
    total_correspondents: int = 0
    total_document_types: int = 0
    total_storage_paths: int = 0

    # Ebene 1: Titel-Gruppierung
    title_groups: list[TitleGroup] = field(default_factory=list)

    # Ebene 2: Pfad-Analyse
    path_levels: list[PathLevel] = field(default_factory=list)
    # Topics mit ihren Unter-Pfaden
    topics: dict[str, list[PathLevel]] = field(default_factory=dict)

    # Ebene 3: Zuordnungstabelle
    mapping_records: list[MappingRecord] = field(default_factory=list)
    # Aggregiert: (Korrespondent, Typ) → set(Pfade)
    mapping_summary: dict[tuple[str, str], dict[str, int]] = field(
        default_factory=dict,
    )

    # Änderungen
    changes: ChangesSinceLastRun = field(default_factory=ChangesSinceLastRun)


# ---------------------------------------------------------------------------
# Collector-Klasse
# ---------------------------------------------------------------------------

class SchemaCollector:
    """Sammelt und gruppiert Paperless-Daten für die Schema-Analyse.

    Nutzt den bestehenden PaperlessClient und dessen Cache.
    Kein LLM-Aufruf – reine lokale Datenverarbeitung.

    Verwendung:
        collector = SchemaCollector(paperless_client)
        result = await collector.collect()
        # result enthält die vorverarbeiteten Daten für Opus
    """

    def __init__(self, paperless: PaperlessClient) -> None:
        self._paperless = paperless

    @property
    def _cache(self) -> LookupCache:
        return self._paperless.cache

    async def collect(
        self,
        *,
        last_run_at: str | None = None,
        previous_correspondents: set[str] | None = None,
        previous_document_types: set[str] | None = None,
        previous_storage_paths: set[str] | None = None,
    ) -> CollectorResult:
        """Hauptmethode: Sammelt und verarbeitet alle Daten.

        Args:
            last_run_at: ISO-Timestamp des letzten Analyse-Laufs (für Änderungserkennung).
            previous_correspondents: Korrespondenten-Namen des letzten Laufs.
            previous_document_types: Dokumenttyp-Namen des letzten Laufs.
            previous_storage_paths: Speicherpfad-Namen des letzten Laufs.

        Returns:
            CollectorResult mit allen Vorverarbeitungs-Ebenen.
        """
        logger.info("Schema-Collector: Starte Datensammlung")

        # Stammdaten-Cache aktualisieren (falls seit Startup was geändert wurde)
        await self._paperless.refresh_cache()

        # Alle Dokumente laden (ungefiltert, ohne NEU-Filter)
        all_documents = await self._paperless.get_documents(
            ordering="created",
            page_size=100,
        )
        logger.info(
            "Schema-Collector: %d Dokumente geladen",
            len(all_documents),
        )

        result = CollectorResult(
            total_documents=len(all_documents),
            total_correspondents=len(self._cache.correspondents),
            total_document_types=len(self._cache.document_types),
            total_storage_paths=len(self._cache.storage_paths),
        )

        # Ebene 1: Titel gruppieren
        result.title_groups = self._group_titles(all_documents)
        logger.info(
            "Schema-Collector: %d Titel-Gruppen erstellt",
            len(result.title_groups),
        )

        # Ebene 2: Pfade analysieren
        result.path_levels = self._analyze_paths()
        result.topics = self._group_by_topic(
            result.path_levels, all_documents,
        )
        logger.info(
            "Schema-Collector: %d Pfade in %d Topics analysiert",
            len(result.path_levels), len(result.topics),
        )

        # Ebene 3: Zuordnungstabelle aufbauen
        result.mapping_records = self._build_mapping_records(all_documents)
        result.mapping_summary = self._summarize_mappings(
            result.mapping_records,
        )
        logger.info(
            "Schema-Collector: %d Zuordnungen, %d eindeutige Kombinationen",
            len(result.mapping_records), len(result.mapping_summary),
        )

        # Änderungen erkennen
        result.changes = self._detect_changes(
            all_documents,
            last_run_at=last_run_at,
            previous_correspondents=previous_correspondents or set(),
            previous_document_types=previous_document_types or set(),
            previous_storage_paths=previous_storage_paths or set(),
        )
        if result.changes.has_changes:
            logger.info(
                "Schema-Collector: Änderungen erkannt – "
                "%d neue Korrespondenten, %d neue Typen, "
                "%d neue Pfade, %d neue Dokumente",
                len(result.changes.new_correspondents),
                len(result.changes.new_document_types),
                len(result.changes.new_storage_paths),
                result.changes.new_documents_count,
            )

        return result

    # =========================================================================
    # Ebene 1: Titel-Gruppierung
    # =========================================================================

    def _group_titles(self, documents: list[Document]) -> list[TitleGroup]:
        """Gruppiert Dokumenttitel nach (Dokumenttyp, Korrespondent).

        Dokumente ohne Typ oder ohne Korrespondent werden übersprungen,
        da für sie kein Schema erstellt werden kann.

        AP-11b: Erfasst zusätzlich die Tag-Distribution pro Gruppe
        (welche Tags kommen wie oft vor).
        """
        groups: dict[tuple[str, str], TitleGroup] = {}

        for doc in documents:
            # Typ und Korrespondent aus Cache auflösen (ID → Name)
            type_name = self._resolve_type_name(doc.document_type)
            corr_name = self._resolve_correspondent_name(doc.correspondent)

            if not type_name or not corr_name:
                continue

            key = (type_name, corr_name)
            if key not in groups:
                groups[key] = TitleGroup(
                    document_type=type_name,
                    correspondent=corr_name,
                )
            groups[key].titles.append(doc.title)
            groups[key].document_ids.append(doc.id)

            # Tag-Distribution: Alle Tags des Dokuments zählen (AP-11b)
            for tag_id in doc.tags:
                tag_name = self._resolve_tag_name(tag_id)
                if tag_name and tag_name != "NEU":
                    groups[key].tag_distribution[tag_name] = (
                        groups[key].tag_distribution.get(tag_name, 0) + 1
                    )

        # Common Tags berechnen: Tags die bei >50% der Gruppengröße vorkommen
        for group in groups.values():
            threshold = group.count / 2
            group.common_tags = sorted(
                tag_name
                for tag_name, count in group.tag_distribution.items()
                if count > threshold
            )

        # Sortiert zurückgeben: größte Gruppen zuerst (für Opus-Priorisierung)
        result = sorted(groups.values(), key=lambda g: g.count, reverse=True)
        return result

    # =========================================================================
    # Ebene 2: Pfad-Analyse
    # =========================================================================

    def _analyze_paths(self) -> list[PathLevel]:
        """Zerlegt alle Speicherpfade in ihre Hierarchie-Ebenen.

        Paperless speichert den Pfad-Namen als "Topic / Objekt / Entität"
        (mit " / " als Separator).  Wir zerlegen das in eine Liste.

        Heterogene Tiefen (2–4 Ebenen) werden korrekt verarbeitet.
        """
        path_levels: list[PathLevel] = []

        for sp in self._cache.storage_paths.values():
            # Pfad-Name zerlegen: "Ärzte / Dr. Hansen" → ["Ärzte", "Dr. Hansen"]
            levels = [level.strip() for level in sp.name.split(" / ")]
            # Leere Ebenen entfernen (z.B. bei führendem/trailendem " / ")
            levels = [lv for lv in levels if lv]

            if not levels:
                logger.warning(
                    "Speicherpfad ohne auswertbare Ebenen: id=%d, name='%s'",
                    sp.id, sp.name,
                )
                continue

            path_levels.append(PathLevel(
                full_name=sp.name,
                path_id=sp.id,
                levels=levels,
                document_count=sp.document_count,
            ))

        return sorted(path_levels, key=lambda p: p.full_name)

    def _group_by_topic(
        self,
        path_levels: list[PathLevel],
        documents: list[Document],
    ) -> dict[str, list[PathLevel]]:
        """Gruppiert Pfade nach ihrem Topic (erste Ebene).

        Ergänzt die document_ids pro Pfad basierend auf den
        tatsächlichen Dokumenten.
        """
        # Dokument-IDs pro Pfad-ID sammeln
        docs_by_path: dict[int, list[int]] = defaultdict(list)
        for doc in documents:
            if doc.storage_path is not None:
                docs_by_path[doc.storage_path].append(doc.id)

        # Pfade mit Dokument-IDs anreichern
        for pl in path_levels:
            pl.document_ids = docs_by_path.get(pl.path_id, [])
            # document_count aus Cache könnte veraltet sein – überschreiben
            if pl.document_ids:
                pl.document_count = len(pl.document_ids)

        # Nach Topic gruppieren
        topics: dict[str, list[PathLevel]] = defaultdict(list)
        for pl in path_levels:
            topics[pl.topic].append(pl)

        return dict(sorted(topics.items()))

    # =========================================================================
    # Ebene 3: Zuordnungstabelle
    # =========================================================================

    def _build_mapping_records(
        self,
        documents: list[Document],
    ) -> list[MappingRecord]:
        """Erstellt Zuordnungs-Datensätze für alle Dokumente.

        Nur Dokumente mit Korrespondent, Typ UND Pfad werden berücksichtigt.
        """
        records: list[MappingRecord] = []

        for doc in documents:
            corr_name = self._resolve_correspondent_name(doc.correspondent)
            type_name = self._resolve_type_name(doc.document_type)
            path_name = self._resolve_path_name(doc.storage_path)

            # Alle drei müssen vorhanden sein
            if not corr_name or not type_name or not path_name:
                continue

            records.append(MappingRecord(
                document_id=doc.id,
                correspondent=corr_name,
                document_type=type_name,
                storage_path=path_name,
                storage_path_id=doc.storage_path or 0,
                title=doc.title,
            ))

        return records

    def _summarize_mappings(
        self,
        records: list[MappingRecord],
    ) -> dict[tuple[str, str], dict[str, int]]:
        """Aggregiert Zuordnungen: (Korrespondent, Typ) → {Pfad: Anzahl}.

        Ergebnis zeigt ob eine Kombination eindeutig (1 Pfad) oder
        mehrdeutig (>1 Pfade) ist.
        """
        summary: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )

        for record in records:
            key = (record.correspondent, record.document_type)
            summary[key][record.storage_path] += 1

        # defaultdict in normale dict konvertieren
        return {k: dict(v) for k, v in summary.items()}

    # =========================================================================
    # Änderungserkennung
    # =========================================================================

    def _detect_changes(
        self,
        documents: list[Document],
        *,
        last_run_at: str | None,
        previous_correspondents: set[str],
        previous_document_types: set[str],
        previous_storage_paths: set[str],
    ) -> ChangesSinceLastRun:
        """Erkennt Änderungen seit dem letzten Schema-Analyse-Lauf.

        Vergleicht aktuelle Stammdaten mit den bei-letzten-Lauf-bekannten.
        Wenn noch nie gelaufen (previous-Sets leer), ist alles "neu".
        """
        changes = ChangesSinceLastRun()

        current_correspondents = {
            c.name for c in self._cache.correspondents.values()
        }
        current_types = {
            dt.name for dt in self._cache.document_types.values()
        }
        current_paths = {
            sp.name for sp in self._cache.storage_paths.values()
        }

        # Neue Entitäten erkennen (nur wenn wir Vergleichsdaten haben)
        if previous_correspondents:
            changes.new_correspondents = sorted(
                current_correspondents - previous_correspondents,
            )
        if previous_document_types:
            changes.new_document_types = sorted(
                current_types - previous_document_types,
            )
        if previous_storage_paths:
            changes.new_storage_paths = sorted(
                current_paths - previous_storage_paths,
            )

        # Neue Dokumente seit dem letzten Lauf zählen
        if last_run_at:
            changes.new_documents_count = sum(
                1 for doc in documents
                if doc.added and doc.added.isoformat() > last_run_at
            )

        return changes

    # =========================================================================
    # Serialisierung (für Opus-Prompt in AP-11)
    # =========================================================================

    def serialize_for_prompt(self, result: CollectorResult) -> dict[str, Any]:
        """Konvertiert das CollectorResult in ein JSON-kompatibles Dict.

        Dieses Dict wird in AP-11 als Input in den Opus-Prompt eingebettet.
        Optimiert auf kompakte Darstellung bei maximaler Informationsdichte.
        """
        # Ebene 1: Titel-Gruppen
        title_groups_data = []
        for group in result.title_groups:
            group_data: dict[str, Any] = {
                "document_type": group.document_type,
                "correspondent": group.correspondent,
                "count": group.count,
                "titles": group.titles,
            }
            # Tag-Muster nur einfügen wenn vorhanden (AP-11b)
            if group.tag_distribution:
                group_data["common_tags"] = group.common_tags
                group_data["tag_distribution"] = group.tag_distribution
            title_groups_data.append(group_data)

        # Ebene 2: Pfad-Hierarchie
        paths_data = []
        for topic_name, topic_paths in result.topics.items():
            topic_data = {
                "topic": topic_name,
                "total_documents": sum(p.document_count for p in topic_paths),
                "paths": [
                    {
                        "name": p.full_name,
                        "levels": p.levels,
                        "depth": p.depth,
                        "documents": p.document_count,
                    }
                    for p in topic_paths
                ],
            }
            paths_data.append(topic_data)

        # Ebene 3: Zuordnungstabelle (aggregiert)
        mappings_data = []
        for (corr, dtype), path_counts in sorted(
            result.mapping_summary.items(),
        ):
            mapping_type = "exact" if len(path_counts) == 1 else "conditional"
            mappings_data.append({
                "correspondent": corr,
                "document_type": dtype,
                "mapping_type": mapping_type,
                "paths": path_counts,  # {pfad_name: count}
            })

        # Änderungen
        changes_data = None
        if result.changes.has_changes:
            changes_data = {
                "new_correspondents": result.changes.new_correspondents,
                "new_document_types": result.changes.new_document_types,
                "new_storage_paths": result.changes.new_storage_paths,
                "new_documents_count": result.changes.new_documents_count,
            }

        return {
            "metadata": {
                "total_documents": result.total_documents,
                "total_correspondents": result.total_correspondents,
                "total_document_types": result.total_document_types,
                "total_storage_paths": result.total_storage_paths,
            },
            "title_groups": title_groups_data,
            "path_hierarchy": paths_data,
            "mapping_table": mappings_data,
            "changes_since_last_run": changes_data,
        }

    # =========================================================================
    # Hilfsmethoden: ID → Name via Cache
    # =========================================================================

    def _resolve_correspondent_name(self, corr_id: int | None) -> str | None:
        """Löst eine Korrespondenten-ID in den Namen auf."""
        if corr_id is None:
            return None
        obj = self._cache.get_correspondent(corr_id)
        return obj.name if obj else None

    def _resolve_type_name(self, type_id: int | None) -> str | None:
        """Löst eine Dokumenttyp-ID in den Namen auf."""
        if type_id is None:
            return None
        obj = self._cache.get_document_type(type_id)
        return obj.name if obj else None

    def _resolve_path_name(self, path_id: int | None) -> str | None:
        """Löst eine Speicherpfad-ID in den Namen auf."""
        if path_id is None:
            return None
        obj = self._cache.get_storage_path(path_id)
        return obj.name if obj else None

    def _resolve_tag_name(self, tag_id: int) -> str | None:
        """Löst eine Tag-ID in den Namen auf."""
        obj = self._cache.get_tag(tag_id)
        return obj.name if obj else None
