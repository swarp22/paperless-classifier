"""Spezifische Exceptions für den Paperless API Client.

Hierarchie:
    PaperlessError (Basis)
    ├── PaperlessConnectionError     – Netzwerkfehler, Timeout
    ├── PaperlessAuthError           – 401/403, ungültiger Token
    ├── PaperlessNotFoundError       – 404, Ressource existiert nicht
    ├── PaperlessValidationError     – 400, ungültige Daten gesendet
    ├── PaperlessServerError         – 5xx, serverseitiger Fehler
    └── PaperlessCacheError          – Fehler beim Cache-Lookup (z.B. Name nicht gefunden)
"""

from __future__ import annotations


class PaperlessError(Exception):
    """Basisklasse für alle Paperless API Fehler."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class PaperlessConnectionError(PaperlessError):
    """Netzwerkfehler: Paperless ist nicht erreichbar oder Timeout."""
    pass


class PaperlessAuthError(PaperlessError):
    """Authentifizierungsfehler: Token ungültig oder abgelaufen (401/403)."""
    pass


class PaperlessNotFoundError(PaperlessError):
    """Ressource nicht gefunden (404)."""

    def __init__(self, resource_type: str, resource_id: int | str) -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(
            f"{resource_type} mit ID {resource_id} nicht gefunden",
            status_code=404,
        )


class PaperlessValidationError(PaperlessError):
    """Ungültige Daten an die API gesendet (400)."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.details = details or {}
        super().__init__(message, status_code=400)


class PaperlessServerError(PaperlessError):
    """Serverseitiger Fehler (5xx) – kann transient sein."""
    pass


class PaperlessCacheError(PaperlessError):
    """Fehler bei Stammdaten-Lookup, z.B. Name nicht im Cache gefunden."""

    def __init__(self, entity_type: str, name: str) -> None:
        self.entity_type = entity_type
        self.name = name
        super().__init__(
            f"{entity_type} '{name}' nicht im Cache gefunden. "
            f"Cache-Refresh nötig oder Neuanlage erforderlich."
        )
