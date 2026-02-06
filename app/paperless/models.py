"""Pydantic-Modelle für Paperless-ngx API-Objekte.

Bilden die relevanten API-Responses als typisierte Python-Objekte ab.
Nicht alle Felder werden modelliert – nur die, die der Classifier braucht.
Unbekannte Felder werden über model_config ignoriert (Vorwärtskompatibilität).

Referenz: Paperless-ngx v2.20.6, API Version 7
Siehe auch: ERRATA E-001 (Select-Options Format), E-002 (Custom Field IDs)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# =============================================================================
# Generische API-Response (Pagination)
# =============================================================================

class PaginatedResponse(BaseModel):
    """Generische paginierte Antwort der Paperless API.

    Alle Listen-Endpoints liefern dieses Format:
    {count, next, previous, all, results}
    """
    model_config = ConfigDict(extra="ignore")

    count: int
    next: str | None = None
    previous: str | None = None
    all: list[int] = []
    results: list[dict[str, Any]] = []


# =============================================================================
# Stammdaten (Organisationsstruktur)
# =============================================================================

class Correspondent(BaseModel):
    """Korrespondent (Absender/Empfänger)."""
    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str = ""
    name: str
    match: str = ""
    matching_algorithm: int = 6
    is_insensitive: bool = True
    document_count: int = 0
    owner: int | None = None


class DocumentType(BaseModel):
    """Dokumenttyp (z.B. Rechnung, Gehaltsabrechnung)."""
    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str = ""
    name: str
    match: str = ""
    matching_algorithm: int = 6
    is_insensitive: bool = True
    document_count: int = 0
    owner: int | None = None


class Tag(BaseModel):
    """Tag (z.B. NEU, Archiv)."""
    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str = ""
    name: str
    color: str = "#a6cee3"
    is_inbox_tag: bool = False
    match: str = ""
    matching_algorithm: int = 6
    is_insensitive: bool = True
    document_count: int = 0
    owner: int | None = None


class StoragePath(BaseModel):
    """Speicherpfad mit Jinja2-Template-Syntax."""
    model_config = ConfigDict(extra="ignore")

    id: int
    slug: str = ""
    name: str
    path: str = ""
    match: str = ""
    matching_algorithm: int = 6
    is_insensitive: bool = True
    document_count: int = 0
    owner: int | None = None


# =============================================================================
# Custom Fields
# =============================================================================

class SelectOption(BaseModel):
    """Einzelne Option eines Select-Custom-Fields.

    ERRATA E-001: Select-Options sind Objekte mit {id, label},
    nicht einfache Strings. Die id wird serverseitig generiert.
    """
    model_config = ConfigDict(extra="ignore")

    id: str
    label: str


class CustomFieldDefinition(BaseModel):
    """Definition eines Custom Fields (nicht der Wert an einem Dokument).

    Typen: select, integer, string, date, boolean, url, float,
           monetary, documentlink
    """
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    data_type: str
    extra_data: dict[str, Any] = {}
    document_count: int = 0

    @property
    def select_options(self) -> list[SelectOption]:
        """Gibt die Select-Optionen zurück (nur für Select-Felder).

        Returns:
            Liste von SelectOption-Objekten, leere Liste wenn kein Select-Feld
            oder Optionen nicht parsbar.
        """
        if self.data_type != "select":
            return []
        raw_options = self.extra_data.get("select_options", [])
        result = []
        for opt in raw_options:
            if isinstance(opt, dict) and "id" in opt and "label" in opt:
                result.append(SelectOption(**opt))
        return result

    def get_option_id_by_label(self, label: str) -> str | None:
        """Findet die interne ID einer Select-Option anhand ihres Labels.

        ERRATA E-001: Beim Setzen von Select-Werten muss die interne ID
        verwendet werden, nicht der Label-String.

        Args:
            label: Der angezeigte Name der Option (z.B. "Max", "classified")

        Returns:
            Die interne ID (z.B. "1IOdA6xDPBZuJdvD") oder None
        """
        for opt in self.select_options:
            if opt.label == label:
                return opt.id
        return None

    def get_option_label_by_id(self, option_id: str) -> str | None:
        """Findet den Label-String einer Select-Option anhand ihrer ID.

        Args:
            option_id: Die interne ID der Option

        Returns:
            Der Label-String oder None
        """
        for opt in self.select_options:
            if opt.id == option_id:
                return opt.label
        return None


# =============================================================================
# Custom Field Wert (an einem Dokument)
# =============================================================================

class CustomFieldValue(BaseModel):
    """Custom-Field-Wert wie er an einem Dokument hängt.

    Format in der API-Response:
        {"field": 7, "value": "abc123"}       # Select: option-ID als String
        {"field": 2, "value": 523}            # Integer
        {"field": 1, "value": [234, 235]}     # Document Link: Liste von IDs
        {"field": 3, "value": "text"}         # String
        {"field": 9, "value": null}           # Kein Wert gesetzt
    """
    model_config = ConfigDict(extra="ignore")

    field: int
    value: Any = None


# =============================================================================
# Dokument
# =============================================================================

class Document(BaseModel):
    """Paperless-Dokument mit allen für den Classifier relevanten Feldern.

    Nicht alle API-Felder werden modelliert – nur was der Classifier
    zum Lesen, Klassifizieren und Updaten braucht.
    """
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""
    content: str = ""  # OCR-Text
    correspondent: int | None = None
    document_type: int | None = None
    storage_path: int | None = None
    tags: list[int] = []
    created: datetime | None = None
    created_date: str | None = None  # "YYYY-MM-DD"
    modified: datetime | None = None
    added: datetime | None = None
    original_file_name: str = ""
    archived_file_name: str = ""
    page_count: int = 0
    custom_fields: list[CustomFieldValue] = []
    # Hinweis: notes, owner, permissions etc. werden nicht modelliert

    def get_custom_field_value(self, field_id: int) -> Any | None:
        """Gibt den Wert eines Custom Fields zurück.

        Args:
            field_id: ID der Custom-Field-Definition

        Returns:
            Der Wert oder None wenn das Feld nicht am Dokument existiert.
        """
        for cf in self.custom_fields:
            if cf.field == field_id:
                return cf.value
        return None

    def has_custom_field(self, field_id: int) -> bool:
        """Prüft ob ein Custom Field am Dokument existiert (auch wenn Wert None)."""
        return any(cf.field == field_id for cf in self.custom_fields)

    def has_tag(self, tag_id: int) -> bool:
        """Prüft ob das Dokument einen bestimmten Tag hat."""
        return tag_id in self.tags
