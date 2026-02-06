"""Paperless-ngx API-Client Paket.

Öffentliche API:
    PaperlessClient  – Async HTTP Client für alle Paperless-Operationen
    LookupCache      – Stammdaten-Cache mit Name→ID Lookup
    Document, Tag, Correspondent, ... – Pydantic-Modelle

Exceptions:
    PaperlessError, PaperlessConnectionError, PaperlessAuthError, ...

Typische Verwendung:
    from app.paperless import PaperlessClient

    async with PaperlessClient(url, token) as client:
        await client.load_cache()
        docs = await client.get_documents(tags=[12])
"""

from app.paperless.cache import LookupCache
from app.paperless.client import PaperlessClient
from app.paperless.exceptions import (
    PaperlessAuthError,
    PaperlessCacheError,
    PaperlessConnectionError,
    PaperlessError,
    PaperlessNotFoundError,
    PaperlessServerError,
    PaperlessValidationError,
)
from app.paperless.models import (
    Correspondent,
    CustomFieldDefinition,
    CustomFieldValue,
    Document,
    DocumentType,
    PaginatedResponse,
    SelectOption,
    StoragePath,
    Tag,
)

__all__ = [
    # Client + Cache
    "PaperlessClient",
    "LookupCache",
    # Modelle
    "Correspondent",
    "CustomFieldDefinition",
    "CustomFieldValue",
    "Document",
    "DocumentType",
    "PaginatedResponse",
    "SelectOption",
    "StoragePath",
    "Tag",
    # Exceptions
    "PaperlessAuthError",
    "PaperlessCacheError",
    "PaperlessConnectionError",
    "PaperlessError",
    "PaperlessNotFoundError",
    "PaperlessServerError",
    "PaperlessValidationError",
]
