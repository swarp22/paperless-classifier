"""In-Memory Cache für Paperless-ngx Stammdaten.

Hält Korrespondenten, Dokumenttypen, Tags, Speicherpfade und Custom Fields
im Speicher und bietet schnelles Name→ID Mapping.

Cache-Strategie:
- Eager Load bei Startup (load_all)
- Automatische Invalidierung bei Neuanlage über den Client
- Manueller Refresh über refresh() für die Web-UI
- Kein TTL nötig – Stammdaten ändern sich nur durch den Classifier selbst

ERRATA E-001: Select-Options sind Objekte mit {id, label}.
Der Cache bietet Hilfsmethoden zum Auflösen von Label→Option-ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.logging_config import get_logger
from app.paperless.exceptions import PaperlessCacheError
from app.paperless.models import (
    Correspondent,
    CustomFieldDefinition,
    DocumentType,
    StoragePath,
    Tag,
)

logger = get_logger("paperless")


@dataclass
class LookupCache:
    """Bidirektionaler Cache: ID→Objekt und Name→ID für alle Stammdaten.

    Verwendung:
        cache = LookupCache()
        # ... wird vom Client via load_all() befüllt ...
        tag_id = cache.get_tag_id("NEU")
        correspondent = cache.get_correspondent(1)
        option_id = cache.get_select_option_id(7, "Max")
    """

    # ID → Objekt Mappings
    correspondents: dict[int, Correspondent] = field(default_factory=dict)
    document_types: dict[int, DocumentType] = field(default_factory=dict)
    tags: dict[int, Tag] = field(default_factory=dict)
    storage_paths: dict[int, StoragePath] = field(default_factory=dict)
    custom_fields: dict[int, CustomFieldDefinition] = field(default_factory=dict)

    # Name → ID Mappings (lowercase für case-insensitive Lookup)
    _correspondent_names: dict[str, int] = field(default_factory=dict)
    _document_type_names: dict[str, int] = field(default_factory=dict)
    _tag_names: dict[str, int] = field(default_factory=dict)
    _storage_path_names: dict[str, int] = field(default_factory=dict)
    _custom_field_names: dict[str, int] = field(default_factory=dict)

    @property
    def is_loaded(self) -> bool:
        """True wenn mindestens eine Kategorie geladen wurde."""
        return bool(
            self.correspondents
            or self.document_types
            or self.tags
            or self.storage_paths
            or self.custom_fields
        )

    # =========================================================================
    # Befüllung (wird vom Client aufgerufen)
    # =========================================================================

    def set_correspondents(self, items: list[Correspondent]) -> None:
        """Cache mit Korrespondenten befüllen."""
        self.correspondents = {item.id: item for item in items}
        self._correspondent_names = {item.name.lower(): item.id for item in items}
        logger.debug("Cache: %d Korrespondenten geladen", len(items))

    def set_document_types(self, items: list[DocumentType]) -> None:
        """Cache mit Dokumenttypen befüllen."""
        self.document_types = {item.id: item for item in items}
        self._document_type_names = {item.name.lower(): item.id for item in items}
        logger.debug("Cache: %d Dokumenttypen geladen", len(items))

    def set_tags(self, items: list[Tag]) -> None:
        """Cache mit Tags befüllen."""
        self.tags = {item.id: item for item in items}
        self._tag_names = {item.name.lower(): item.id for item in items}
        logger.debug("Cache: %d Tags geladen", len(items))

    def set_storage_paths(self, items: list[StoragePath]) -> None:
        """Cache mit Speicherpfaden befüllen."""
        self.storage_paths = {item.id: item for item in items}
        self._storage_path_names = {item.name.lower(): item.id for item in items}
        logger.debug("Cache: %d Speicherpfade geladen", len(items))

    def set_custom_fields(self, items: list[CustomFieldDefinition]) -> None:
        """Cache mit Custom-Field-Definitionen befüllen."""
        self.custom_fields = {item.id: item for item in items}
        self._custom_field_names = {item.name.lower(): item.id for item in items}
        logger.debug("Cache: %d Custom Fields geladen", len(items))

    def clear(self) -> None:
        """Gesamten Cache leeren."""
        self.correspondents.clear()
        self.document_types.clear()
        self.tags.clear()
        self.storage_paths.clear()
        self.custom_fields.clear()
        self._correspondent_names.clear()
        self._document_type_names.clear()
        self._tag_names.clear()
        self._storage_path_names.clear()
        self._custom_field_names.clear()
        logger.debug("Cache geleert")

    # =========================================================================
    # Einzelne Einträge hinzufügen (nach Neuanlage via API)
    # =========================================================================

    def add_correspondent(self, item: Correspondent) -> None:
        """Einzelnen Korrespondenten zum Cache hinzufügen."""
        self.correspondents[item.id] = item
        self._correspondent_names[item.name.lower()] = item.id

    def add_document_type(self, item: DocumentType) -> None:
        """Einzelnen Dokumenttyp zum Cache hinzufügen."""
        self.document_types[item.id] = item
        self._document_type_names[item.name.lower()] = item.id

    def add_tag(self, item: Tag) -> None:
        """Einzelnen Tag zum Cache hinzufügen."""
        self.tags[item.id] = item
        self._tag_names[item.name.lower()] = item.id

    def add_storage_path(self, item: StoragePath) -> None:
        """Einzelnen Speicherpfad zum Cache hinzufügen."""
        self.storage_paths[item.id] = item
        self._storage_path_names[item.name.lower()] = item.id

    # =========================================================================
    # Lookup: ID → Objekt
    # =========================================================================

    def get_correspondent(self, id: int) -> Correspondent | None:
        """Korrespondent anhand seiner ID."""
        return self.correspondents.get(id)

    def get_document_type(self, id: int) -> DocumentType | None:
        """Dokumenttyp anhand seiner ID."""
        return self.document_types.get(id)

    def get_tag(self, id: int) -> Tag | None:
        """Tag anhand seiner ID."""
        return self.tags.get(id)

    def get_storage_path(self, id: int) -> StoragePath | None:
        """Speicherpfad anhand seiner ID."""
        return self.storage_paths.get(id)

    def get_custom_field(self, id: int) -> CustomFieldDefinition | None:
        """Custom-Field-Definition anhand ihrer ID."""
        return self.custom_fields.get(id)

    # =========================================================================
    # Lookup: Name → ID (case-insensitive)
    # =========================================================================

    def get_correspondent_id(self, name: str) -> int | None:
        """Korrespondenten-ID anhand des Namens (case-insensitive)."""
        return self._correspondent_names.get(name.lower())

    def get_document_type_id(self, name: str) -> int | None:
        """Dokumenttyp-ID anhand des Namens (case-insensitive)."""
        return self._document_type_names.get(name.lower())

    def get_tag_id(self, name: str) -> int | None:
        """Tag-ID anhand des Namens (case-insensitive)."""
        return self._tag_names.get(name.lower())

    def get_storage_path_id(self, name: str) -> int | None:
        """Speicherpfad-ID anhand des Namens (case-insensitive)."""
        return self._storage_path_names.get(name.lower())

    def get_custom_field_id(self, name: str) -> int | None:
        """Custom-Field-ID anhand des Namens (case-insensitive)."""
        return self._custom_field_names.get(name.lower())

    # =========================================================================
    # Lookup: Name → ID mit Exception (wenn Pflicht)
    # =========================================================================

    def require_correspondent_id(self, name: str) -> int:
        """Wie get_correspondent_id, wirft PaperlessCacheError wenn nicht gefunden."""
        result = self.get_correspondent_id(name)
        if result is None:
            raise PaperlessCacheError("Korrespondent", name)
        return result

    def require_document_type_id(self, name: str) -> int:
        """Wie get_document_type_id, wirft PaperlessCacheError wenn nicht gefunden."""
        result = self.get_document_type_id(name)
        if result is None:
            raise PaperlessCacheError("Dokumenttyp", name)
        return result

    def require_tag_id(self, name: str) -> int:
        """Wie get_tag_id, wirft PaperlessCacheError wenn nicht gefunden."""
        result = self.get_tag_id(name)
        if result is None:
            raise PaperlessCacheError("Tag", name)
        return result

    def require_storage_path_id(self, name: str) -> int:
        """Wie get_storage_path_id, wirft PaperlessCacheError wenn nicht gefunden."""
        result = self.get_storage_path_id(name)
        if result is None:
            raise PaperlessCacheError("Speicherpfad", name)
        return result

    # =========================================================================
    # Custom Field Select-Options Lookup
    # =========================================================================

    def get_select_option_id(self, field_id: int, label: str) -> str | None:
        """Interne Option-ID für ein Select-Feld anhand des Labels.

        ERRATA E-001: Beim Setzen von Select-Werten wird die interne ID
        benötigt, nicht der Label-String.

        Args:
            field_id: ID der Custom-Field-Definition (z.B. 7 für Person)
            label: Angezeigter Name der Option (z.B. "Max")

        Returns:
            Interne Option-ID (z.B. "1IOdA6xDPBZuJdvD") oder None
        """
        cf = self.custom_fields.get(field_id)
        if cf is None:
            return None
        return cf.get_option_id_by_label(label)

    def get_select_option_label(self, field_id: int, option_id: str) -> str | None:
        """Label einer Select-Option anhand der internen ID.

        Args:
            field_id: ID der Custom-Field-Definition
            option_id: Interne Option-ID

        Returns:
            Label-String oder None
        """
        cf = self.custom_fields.get(field_id)
        if cf is None:
            return None
        return cf.get_option_label_by_id(option_id)

    def require_select_option_id(self, field_id: int, label: str) -> str:
        """Wie get_select_option_id, wirft PaperlessCacheError wenn nicht gefunden."""
        result = self.get_select_option_id(field_id, label)
        if result is None:
            cf = self.custom_fields.get(field_id)
            field_name = cf.name if cf else f"ID {field_id}"
            raise PaperlessCacheError(f"Select-Option in '{field_name}'", label)
        return result

    # =========================================================================
    # Hilfsmethoden für die Klassifizierungs-Pipeline
    # =========================================================================

    def get_all_correspondent_names(self) -> list[str]:
        """Alle Korrespondenten-Namen (für den System-Prompt)."""
        return [c.name for c in self.correspondents.values()]

    def get_all_document_type_names(self) -> list[str]:
        """Alle Dokumenttyp-Namen (für den System-Prompt)."""
        return [dt.name for dt in self.document_types.values()]

    def get_all_tag_names(self) -> list[str]:
        """Alle Tag-Namen (für den System-Prompt)."""
        return [t.name for t in self.tags.values()]

    def get_all_storage_path_names(self) -> list[str]:
        """Alle Speicherpfad-Namen (für den System-Prompt)."""
        return [sp.name for sp in self.storage_paths.values()]

    def get_select_option_labels(self, field_id: int) -> list[str]:
        """Alle Labels eines Select-Feldes (für den System-Prompt)."""
        cf = self.custom_fields.get(field_id)
        if cf is None:
            return []
        return [opt.label for opt in cf.select_options]

    # =========================================================================
    # Debug / Statistik
    # =========================================================================

    def stats(self) -> dict[str, int]:
        """Gibt die Anzahl gecachter Einträge pro Kategorie zurück."""
        return {
            "correspondents": len(self.correspondents),
            "document_types": len(self.document_types),
            "tags": len(self.tags),
            "storage_paths": len(self.storage_paths),
            "custom_fields": len(self.custom_fields),
        }
