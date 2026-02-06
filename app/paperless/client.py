"""Asynchroner API-Client für Paperless-ngx.

Kapselt alle HTTP-Kommunikation mit der Paperless REST API und bietet
typisierte Methoden für Dokument- und Stammdaten-Operationen.

Features:
- httpx AsyncClient mit Connection-Pooling
- Retry-Logik für transiente Fehler (5xx, Timeouts)
- Automatische Pagination für Listen-Endpoints
- Integrierter Stammdaten-Cache (LookupCache)
- Spezifische Exceptions für verschiedene Fehlerfälle

Referenz: Paperless-ngx v2.20.6, API Version 7
Siehe: Design-Dokument Abschnitt 4, ERRATA E-001/E-002
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.logging_config import get_logger
from app.paperless.cache import LookupCache
from app.paperless.exceptions import (
    PaperlessAuthError,
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
    StoragePath,
    Tag,
)

logger = get_logger("paperless")

# Maximale Seiten beim automatischen Paging (Schutz vor Endlosschleifen)
MAX_PAGES = 50

# Timeout-Konfiguration
DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,    # Verbindungsaufbau
    read=30.0,       # Antwort lesen (PDFs können groß sein)
    write=10.0,      # Request senden
    pool=10.0,       # Auf freie Connection warten
)

# Timeout speziell für PDF-Downloads (große Dateien)
DOWNLOAD_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=120.0,      # 2 Minuten für große PDFs
    write=10.0,
    pool=10.0,
)


class PaperlessClient:
    """Asynchroner Client für die Paperless-ngx REST API.

    Verwendung:
        async with PaperlessClient(url, token) as client:
            await client.load_cache()
            docs = await client.get_documents(tags=[12])
            pdf = await client.get_document_content(docs[0].id)

    Der Client verwaltet einen internen httpx.AsyncClient mit Connection-Pooling
    und einen LookupCache für Stammdaten.
    """

    def __init__(self, base_url: str, token: str) -> None:
        """Initialisiert den Client.

        Args:
            base_url: Paperless-URL ohne Trailing-Slash (z.B. "http://192.168.178.73:8000")
            token: API-Token aus Paperless
        """
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._http: httpx.AsyncClient | None = None
        self.cache = LookupCache()

    async def __aenter__(self) -> PaperlessClient:
        """Erstellt den httpx AsyncClient beim Betreten des Context-Managers."""
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Token {self._token}",
                "Accept": "application/json; version=7",
            },
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Schließt den httpx AsyncClient."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        """Gibt den httpx Client zurück, wirft wenn nicht initialisiert."""
        if self._http is None:
            raise PaperlessError(
                "PaperlessClient nicht initialisiert – bitte als async context manager verwenden: "
                "'async with PaperlessClient(url, token) as client:'"
            )
        return self._http

    # =========================================================================
    # HTTP-Basismethoden mit Fehlerbehandlung
    # =========================================================================

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Wirft spezifische Exceptions basierend auf HTTP-Status.

        Args:
            response: httpx Response-Objekt

        Raises:
            PaperlessAuthError: Bei 401/403
            PaperlessNotFoundError: Bei 404
            PaperlessValidationError: Bei 400
            PaperlessServerError: Bei 5xx
            PaperlessError: Bei sonstigen Fehlern
        """
        if response.is_success:
            return

        status = response.status_code
        # Versuche Fehlermeldung aus der Response zu extrahieren
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]

        if status in (401, 403):
            raise PaperlessAuthError(
                f"Authentifizierung fehlgeschlagen (HTTP {status}): {detail}",
                status_code=status,
            )
        if status == 404:
            raise PaperlessError(
                f"Ressource nicht gefunden (HTTP 404): {response.url}",
                status_code=404,
            )
        if status == 400:
            raise PaperlessValidationError(
                f"Ungültige Anfrage (HTTP 400): {detail}",
                details=detail if isinstance(detail, dict) else {},
            )
        if status >= 500:
            raise PaperlessServerError(
                f"Serverfehler (HTTP {status}): {detail}",
                status_code=status,
            )
        raise PaperlessError(
            f"Unerwarteter HTTP-Status {status}: {detail}",
            status_code=status,
        )

    @retry(
        retry=retry_if_exception_type((PaperlessServerError, PaperlessConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        """Führt einen HTTP-Request aus mit Retry bei transienten Fehlern.

        Args:
            method: HTTP-Methode (GET, POST, PATCH, DELETE)
            path: API-Pfad (z.B. "/api/documents/")
            params: Query-Parameter
            json_data: JSON-Body für POST/PATCH
            timeout: Optionaler Timeout-Override

        Returns:
            httpx Response

        Raises:
            PaperlessConnectionError: Bei Netzwerkfehlern
            PaperlessAuthError: Bei 401/403
            PaperlessServerError: Bei 5xx (nach Retries)
        """
        try:
            response = await self.http.request(
                method,
                path,
                params=params,
                json=json_data,
                timeout=timeout,
            )
        except httpx.TimeoutException as e:
            raise PaperlessConnectionError(
                f"Timeout bei {method} {path}: {e}"
            ) from e
        except httpx.RequestError as e:
            raise PaperlessConnectionError(
                f"Verbindungsfehler bei {method} {path}: {e}"
            ) from e

        self._raise_for_status(response)
        return response

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET-Request mit JSON-Response."""
        response = await self._request("GET", path, params=params)
        return response.json()

    async def _get_paginated_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Holt alle Seiten eines paginierten Endpoints.

        Folgt automatisch dem 'next'-Link bis alle Ergebnisse geladen sind.
        Schutz gegen Endlosschleifen durch MAX_PAGES.

        Args:
            path: API-Pfad (z.B. "/api/tags/")
            params: Initiale Query-Parameter

        Returns:
            Alle results über alle Seiten zusammengeführt
        """
        all_results: list[dict[str, Any]] = []
        current_params = dict(params or {})
        pages_fetched = 0

        while pages_fetched < MAX_PAGES:
            data = await self._get_json(path, params=current_params)
            page = PaginatedResponse.model_validate(data)
            all_results.extend(page.results)
            pages_fetched += 1

            if page.next is None:
                break

            # 'next' ist eine vollständige URL – wir extrahieren die Query-Parameter
            next_url = httpx.URL(page.next)
            current_params = dict(next_url.params)
            # Pfad könnte sich unterscheiden (absolute URL), wir bleiben beim Original-Pfad
        else:
            logger.warning(
                "Pagination-Limit (%d Seiten) erreicht für %s – Ergebnisse unvollständig",
                MAX_PAGES,
                path,
            )

        return all_results

    async def _post_json(
        self,
        path: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """POST-Request mit JSON-Body und JSON-Response."""
        response = await self._request("POST", path, json_data=data)
        return response.json()

    async def _patch_json(
        self,
        path: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """PATCH-Request mit JSON-Body und JSON-Response."""
        response = await self._request("PATCH", path, json_data=data)
        return response.json()

    # =========================================================================
    # Dokument-Operationen
    # =========================================================================

    async def get_documents(
        self,
        *,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        document_type: int | None = None,
        storage_path: int | None = None,
        custom_field_query: str | None = None,
        ordering: str = "-added",
        query: str | None = None,
        page_size: int = 100,
    ) -> list[Document]:
        """Dokumente abrufen mit optionalen Filtern.

        Args:
            tags: Liste von Tag-IDs (alle müssen vorhanden sein: AND-Verknüpfung)
            correspondent: Korrespondenten-ID
            document_type: Dokumenttyp-ID
            storage_path: Speicherpfad-ID
            custom_field_query: Custom-Field-Filter im Paperless-Format
                z.B. '["ki_status","isnull",true]'
            ordering: Sortierung (z.B. "-added" für neueste zuerst)
            query: Volltextsuche
            page_size: Ergebnisse pro Seite (max. 100)

        Returns:
            Liste von Document-Objekten
        """
        params: dict[str, Any] = {
            "ordering": ordering,
            "page_size": min(page_size, 100),
        }

        if tags:
            # tags__id__all: Dokument muss ALLE angegebenen Tags haben
            params["tags__id__all"] = ",".join(str(t) for t in tags)
        if correspondent is not None:
            params["correspondent__id"] = correspondent
        if document_type is not None:
            params["document_type__id"] = document_type
        if storage_path is not None:
            params["storage_path__id"] = storage_path
        if custom_field_query is not None:
            params["custom_field_query"] = custom_field_query
        if query is not None:
            params["query"] = query

        raw_results = await self._get_paginated_all("/api/documents/", params=params)
        return [Document.model_validate(r) for r in raw_results]

    async def get_document(self, doc_id: int) -> Document:
        """Einzelnes Dokument mit allen Metadaten abrufen.

        Args:
            doc_id: Paperless Dokument-ID

        Returns:
            Document-Objekt

        Raises:
            PaperlessNotFoundError: Wenn Dokument nicht existiert
        """
        try:
            data = await self._get_json(f"/api/documents/{doc_id}/")
        except PaperlessError as e:
            if e.status_code == 404:
                raise PaperlessNotFoundError("Dokument", doc_id) from e
            raise
        return Document.model_validate(data)

    async def get_document_content(self, doc_id: int, *, original: bool = False) -> bytes:
        """Original-PDF eines Dokuments herunterladen.

        Args:
            doc_id: Paperless Dokument-ID
            original: True für das unverarbeitete Original (nicht PDF/A-konvertiert)

        Returns:
            PDF als Bytes

        Raises:
            PaperlessNotFoundError: Wenn Dokument nicht existiert
        """
        path = f"/api/documents/{doc_id}/download/"
        params = {"original": "true"} if original else None

        try:
            response = await self._request(
                "GET",
                path,
                params=params,
                timeout=DOWNLOAD_TIMEOUT,
            )
        except PaperlessError as e:
            if e.status_code == 404:
                raise PaperlessNotFoundError("Dokument", doc_id) from e
            raise

        return response.content

    async def get_document_thumbnail(self, doc_id: int) -> bytes:
        """Thumbnail eines Dokuments herunterladen (für Web-UI).

        Args:
            doc_id: Paperless Dokument-ID

        Returns:
            Thumbnail als Bytes (WebP oder PNG)
        """
        response = await self._request("GET", f"/api/documents/{doc_id}/thumbnail/")
        return response.content

    async def update_document(self, doc_id: int, **fields: Any) -> Document:
        """Dokument-Metadaten aktualisieren.

        Nur die übergebenen Felder werden geändert (PATCH-Semantik).

        Args:
            doc_id: Paperless Dokument-ID
            **fields: Zu aktualisierende Felder, z.B.:
                title="Neuer Titel"
                correspondent=5
                document_type=18
                storage_path=2
                tags=[1, 3, 12]
                custom_fields=[{"field": 8, "value": "abc123"}]

        Returns:
            Aktualisiertes Document-Objekt

        Raises:
            PaperlessNotFoundError: Wenn Dokument nicht existiert
            PaperlessValidationError: Wenn Felder ungültig sind
        """
        try:
            data = await self._patch_json(f"/api/documents/{doc_id}/", fields)
        except PaperlessError as e:
            if e.status_code == 404:
                raise PaperlessNotFoundError("Dokument", doc_id) from e
            raise
        return Document.model_validate(data)

    # =========================================================================
    # Custom Field Operationen
    # =========================================================================

    async def set_custom_field(
        self,
        doc_id: int,
        field_id: int,
        value: Any,
    ) -> Document:
        """Setzt den Wert eines Custom Fields an einem Dokument.

        Vorhandene Custom Fields bleiben erhalten – nur das angegebene Feld
        wird geändert oder hinzugefügt.

        ERRATA E-001: Für Select-Felder muss 'value' die interne Option-ID
        sein (z.B. "1IOdA6xDPBZuJdvD"), nicht der Label-String.
        Nutze cache.get_select_option_id() zum Auflösen.

        Args:
            doc_id: Paperless Dokument-ID
            field_id: ID der Custom-Field-Definition
            value: Neuer Wert (Typ abhängig vom Feld-Typ)

        Returns:
            Aktualisiertes Document-Objekt
        """
        # Aktuelle Custom Fields laden, um andere Felder nicht zu verlieren
        doc = await self.get_document(doc_id)
        existing_fields = [
            {"field": cf.field, "value": cf.value}
            for cf in doc.custom_fields
            if cf.field != field_id  # Bestehendes Feld ersetzen
        ]
        existing_fields.append({"field": field_id, "value": value})

        return await self.update_document(doc_id, custom_fields=existing_fields)

    async def set_custom_field_by_label(
        self,
        doc_id: int,
        field_id: int,
        label: str,
    ) -> Document:
        """Setzt ein Select-Custom-Field anhand des Labels (nicht der internen ID).

        Komfort-Methode, die automatisch Label→Option-ID über den Cache auflöst.

        Args:
            doc_id: Paperless Dokument-ID
            field_id: ID der Custom-Field-Definition (z.B. 7 für Person)
            label: Label-String der Option (z.B. "Max")

        Returns:
            Aktualisiertes Document-Objekt

        Raises:
            PaperlessCacheError: Wenn Label nicht gefunden wird
        """
        option_id = self.cache.require_select_option_id(field_id, label)
        return await self.set_custom_field(doc_id, field_id, option_id)

    async def remove_custom_field(self, doc_id: int, field_id: int) -> Document:
        """Entfernt ein Custom Field von einem Dokument komplett.

        Das Feld erscheint danach nicht mehr am Dokument – es ist nicht
        dasselbe wie "auf null setzen". In Paperless gibt es für Select-Felder
        kein "null", nur "Feld nicht vorhanden".

        Args:
            doc_id: Paperless Dokument-ID
            field_id: ID der Custom-Field-Definition

        Returns:
            Aktualisiertes Document-Objekt
        """
        doc = await self.get_document(doc_id)
        remaining_fields = [
            {"field": cf.field, "value": cf.value}
            for cf in doc.custom_fields
            if cf.field != field_id
        ]
        return await self.update_document(doc_id, custom_fields=remaining_fields)

    # =========================================================================
    # Tag-Operationen
    # =========================================================================

    async def add_tag(self, doc_id: int, tag_id: int) -> Document:
        """Fügt einen Tag zu einem Dokument hinzu (falls nicht bereits vorhanden).

        Args:
            doc_id: Paperless Dokument-ID
            tag_id: Tag-ID

        Returns:
            Aktualisiertes Document-Objekt
        """
        doc = await self.get_document(doc_id)
        if tag_id in doc.tags:
            logger.debug("Tag %d bereits an Dokument %d vorhanden, überspringe", tag_id, doc_id)
            return doc
        new_tags = doc.tags + [tag_id]
        return await self.update_document(doc_id, tags=new_tags)

    async def remove_tag(self, doc_id: int, tag_id: int) -> Document:
        """Entfernt einen Tag von einem Dokument.

        Args:
            doc_id: Paperless Dokument-ID
            tag_id: Tag-ID

        Returns:
            Aktualisiertes Document-Objekt (unverändert wenn Tag nicht vorhanden)
        """
        doc = await self.get_document(doc_id)
        if tag_id not in doc.tags:
            logger.debug("Tag %d nicht an Dokument %d vorhanden, überspringe", tag_id, doc_id)
            return doc
        new_tags = [t for t in doc.tags if t != tag_id]
        return await self.update_document(doc_id, tags=new_tags)

    # =========================================================================
    # Stammdaten: Lesen
    # =========================================================================

    async def get_correspondents(self) -> list[Correspondent]:
        """Alle Korrespondenten abrufen (paginiert)."""
        raw = await self._get_paginated_all("/api/correspondents/")
        return [Correspondent.model_validate(r) for r in raw]

    async def get_document_types(self) -> list[DocumentType]:
        """Alle Dokumenttypen abrufen (paginiert)."""
        raw = await self._get_paginated_all("/api/document_types/")
        return [DocumentType.model_validate(r) for r in raw]

    async def get_tags(self) -> list[Tag]:
        """Alle Tags abrufen (paginiert)."""
        raw = await self._get_paginated_all("/api/tags/")
        return [Tag.model_validate(r) for r in raw]

    async def get_storage_paths(self) -> list[StoragePath]:
        """Alle Speicherpfade abrufen (paginiert)."""
        raw = await self._get_paginated_all("/api/storage_paths/")
        return [StoragePath.model_validate(r) for r in raw]

    async def get_custom_fields(self) -> list[CustomFieldDefinition]:
        """Alle Custom-Field-Definitionen abrufen (paginiert)."""
        raw = await self._get_paginated_all("/api/custom_fields/")
        return [CustomFieldDefinition.model_validate(r) for r in raw]

    # =========================================================================
    # Stammdaten: Erstellen
    # =========================================================================

    async def create_correspondent(self, name: str, **kwargs: Any) -> Correspondent:
        """Neuen Korrespondenten anlegen.

        Args:
            name: Name des Korrespondenten
            **kwargs: Optionale Felder (match, matching_algorithm, etc.)

        Returns:
            Angelegter Correspondent mit ID
        """
        payload = {"name": name, **kwargs}
        data = await self._post_json("/api/correspondents/", payload)
        result = Correspondent.model_validate(data)
        self.cache.add_correspondent(result)
        logger.info("Korrespondent angelegt: '%s' (ID %d)", result.name, result.id)
        return result

    async def create_document_type(self, name: str, **kwargs: Any) -> DocumentType:
        """Neuen Dokumenttyp anlegen.

        Args:
            name: Name des Dokumenttyps
            **kwargs: Optionale Felder

        Returns:
            Angelegter DocumentType mit ID
        """
        payload = {"name": name, **kwargs}
        data = await self._post_json("/api/document_types/", payload)
        result = DocumentType.model_validate(data)
        self.cache.add_document_type(result)
        logger.info("Dokumenttyp angelegt: '%s' (ID %d)", result.name, result.id)
        return result

    async def create_tag(self, name: str, **kwargs: Any) -> Tag:
        """Neuen Tag anlegen.

        Args:
            name: Name des Tags
            **kwargs: Optionale Felder (color, is_inbox_tag, etc.)

        Returns:
            Angelegter Tag mit ID
        """
        payload = {"name": name, **kwargs}
        data = await self._post_json("/api/tags/", payload)
        result = Tag.model_validate(data)
        self.cache.add_tag(result)
        logger.info("Tag angelegt: '%s' (ID %d)", result.name, result.id)
        return result

    async def create_storage_path(
        self,
        name: str,
        path: str,
        **kwargs: Any,
    ) -> StoragePath:
        """Neuen Speicherpfad anlegen.

        Args:
            name: Anzeigename (z.B. "Haus Bietigheim / Ver- und Entsorgung / Strom")
            path: Template-Pfad (z.B. "/Haus Bietigheim/Strom/{{created_year}}/{{title}}_{{created}}")
            **kwargs: Optionale Felder (matching_algorithm, etc.)

        Returns:
            Angelegter StoragePath mit ID
        """
        payload = {"name": name, "path": path, **kwargs}
        data = await self._post_json("/api/storage_paths/", payload)
        result = StoragePath.model_validate(data)
        self.cache.add_storage_path(result)
        logger.info("Speicherpfad angelegt: '%s' (ID %d)", result.name, result.id)
        return result

    # =========================================================================
    # Cache-Management
    # =========================================================================

    async def load_cache(self) -> dict[str, int]:
        """Lädt alle Stammdaten in den Cache.

        Sollte beim Startup einmalig aufgerufen werden.
        Überschreibt bestehende Cache-Einträge komplett.

        Returns:
            Dict mit Anzahl geladener Einträge pro Kategorie
        """
        logger.info("Lade Stammdaten-Cache...")

        correspondents = await self.get_correspondents()
        document_types = await self.get_document_types()
        tags = await self.get_tags()
        storage_paths = await self.get_storage_paths()
        custom_fields = await self.get_custom_fields()

        self.cache.set_correspondents(correspondents)
        self.cache.set_document_types(document_types)
        self.cache.set_tags(tags)
        self.cache.set_storage_paths(storage_paths)
        self.cache.set_custom_fields(custom_fields)

        stats = self.cache.stats()
        logger.info(
            "Cache geladen: %d Korrespondenten, %d Dokumenttypen, %d Tags, "
            "%d Speicherpfade, %d Custom Fields",
            stats["correspondents"],
            stats["document_types"],
            stats["tags"],
            stats["storage_paths"],
            stats["custom_fields"],
        )
        return stats

    async def refresh_cache(self) -> dict[str, int]:
        """Cache komplett neu laden (Alias für load_cache).

        Für die Web-UI als manueller Refresh-Button.
        """
        self.cache.clear()
        return await self.load_cache()
