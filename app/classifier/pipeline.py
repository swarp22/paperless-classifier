"""Klassifizierungs-Pipeline: Orchestrierung des gesamten Ablaufs.

Implementiert den 10-Schritte-Flow aus dem Design-Dokument (Abschnitt 6):

 1. PDF von Paperless herunterladen
 2. Lokale PDF-Analyse (Seitenanzahl, Scan-Erkennung) → Modellwahl
 3. System-Prompt aus Stammdaten generieren
 4. PDF + Prompt an Claude senden
 5. JSON-Antwort parsen (im ClaudeClient)
 6. Namen → Paperless-IDs auflösen (Resolver)
 7. Neue Einträge anlegen falls nötig
 8. Confidence bewerten
 9. Ergebnis auf Dokument anwenden (je nach Confidence)
10. Protokollierung (vorbereitet, SQLite kommt in AP-06)

Die Pipeline ist zustandslos – alle Abhängigkeiten werden injiziert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.classifier.confidence import (
    ApplyAction,
    ConfidenceEvaluation,
    evaluate_confidence,
)
from app.classifier.model_router import (
    PdfAnalysis,
    RoutingDecision,
    analyze_pdf,
    select_model,
)
from app.classifier.resolver import (
    CF_HAUS_ORDNUNGSZAHL,
    CF_HAUS_REGISTER,
    CF_KI_STATUS,
    CF_PAGINIERUNG,
    CF_PERSON,
    TAG_NEU_ID,
    ResolvedClassification,
    resolve_classification,
)
from app.claude.client import (
    ClassificationResponse,
    ClaudeAPIError,
    ClaudeClient,
    ClaudeError,
    ConfidenceLevel,
)
from app.claude.prompts import PromptData, build_system_prompt
from app.logging_config import get_logger
from app.paperless.client import PaperlessClient
from app.paperless.exceptions import PaperlessError

logger = get_logger("classifier")


# ---------------------------------------------------------------------------
# Pipeline-Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Gesamtergebnis eines Pipeline-Durchlaufs für ein Dokument.

    Enthält alle Zwischenergebnisse für Logging, Dashboard und Review-Queue.
    """

    # Identifikation
    document_id: int
    success: bool = False

    # Zwischenergebnisse
    pdf_analysis: PdfAnalysis | None = None
    routing_decision: RoutingDecision | None = None
    classification: ClassificationResponse | None = None
    resolved: ResolvedClassification | None = None
    confidence: ConfidenceEvaluation | None = None

    # Neuanlage-Tracking
    created_correspondents: list[dict[str, Any]] = field(default_factory=list)
    created_document_types: list[dict[str, Any]] = field(default_factory=list)
    created_tags: list[dict[str, Any]] = field(default_factory=list)
    created_storage_paths: list[dict[str, Any]] = field(default_factory=list)

    # Timing
    duration_seconds: float = 0.0

    # Fehler
    error: str | None = None
    error_type: str | None = None

    @property
    def model_used(self) -> str:
        """Verwendetes Modell (oder leer bei Fehler)."""
        if self.classification:
            return self.classification.model
        return ""

    @property
    def cost_usd(self) -> float:
        """API-Kosten in USD (oder 0 bei Fehler)."""
        if self.classification:
            return self.classification.usage.cost_usd
        return 0.0


# ---------------------------------------------------------------------------
# Pipeline-Konfiguration
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """Konfigurierbare Optionen für die Pipeline.

    Wird aus Settings / Web-UI befüllt.
    """

    # Neuanlage erlauben? (Default: aus, muss in Einstellungen aktiviert werden)
    auto_create_correspondents: bool = False
    auto_create_tags: bool = False
    auto_create_document_types: bool = False
    auto_create_storage_paths: bool = False

    # Modell-Override (None = automatisch via Model Router)
    force_model: str | None = None

    # Default-Template für neue Speicherpfade
    storage_path_template: str = "{{created_year}}/{{title}}_{{created}}"


# ---------------------------------------------------------------------------
# Klassifizierungs-Pipeline
# ---------------------------------------------------------------------------

class ClassificationPipeline:
    """Orchestriert den gesamten Klassifizierungsablauf.

    Verwendet Dependency Injection: Paperless-Client, Claude-Client
    und Konfiguration werden von außen übergeben.

    Verwendung:
        pipeline = ClassificationPipeline(paperless, claude, config)
        result = await pipeline.classify_document(doc_id)
    """

    def __init__(
        self,
        paperless: PaperlessClient,
        claude: ClaudeClient,
        config: PipelineConfig | None = None,
    ) -> None:
        self._paperless = paperless
        self._claude = claude
        self._config = config or PipelineConfig()

        # System-Prompt wird beim ersten Aufruf gebaut und gecacht
        self._system_prompt: str | None = None
        self._prompt_data: PromptData | None = None

    # --- Hauptmethode ---

    async def classify_document(
        self,
        document_id: int,
        force_model: str | None = None,
    ) -> PipelineResult:
        """Führt den vollständigen Klassifizierungsablauf für ein Dokument durch.

        Args:
            document_id: Paperless Dokument-ID.
            force_model: Optionaler Modell-Override (überschreibt Config und Router).

        Returns:
            PipelineResult mit allen Zwischenergebnissen.
        """
        result = PipelineResult(document_id=document_id)
        start_time = time.monotonic()

        try:
            # Schritt 1: PDF von Paperless herunterladen
            logger.info("Pipeline Start: Dokument %d", document_id)
            pdf_bytes = await self._download_pdf(document_id)

            # Schritt 2: Lokale PDF-Analyse + Modellwahl
            pdf_analysis = analyze_pdf(pdf_bytes)
            result.pdf_analysis = pdf_analysis

            # Korrespondent bereits bekannt? (für Model Router)
            doc = await self._paperless.get_document(document_id)
            correspondent_known = doc.correspondent is not None

            # Paginierstempel erwartet? (Heuristik: gescanntes Dokument)
            expects_stamp = pdf_analysis.is_image_pdf

            routing = select_model(
                pdf_analysis,
                correspondent_known=correspondent_known,
                expects_stamp=expects_stamp,
                force_model=force_model or self._config.force_model,
            )
            result.routing_decision = routing
            logger.info(
                "Modellwahl: %s (%s)", routing.model, routing.reason,
            )

            # Schritt 3: System-Prompt generieren (gecacht)
            system_prompt = await self._get_system_prompt()

            # Schritt 4+5: An Claude senden + Antwort parsen
            classification = await self._claude.classify_document(
                pdf_bytes=pdf_bytes,
                system_prompt=system_prompt,
                model=routing.model,
                document_id=document_id,
            )
            result.classification = classification
            logger.info(
                "Claude-Antwort: title='%s', confidence=%s, cost=$%.6f",
                classification.result.title[:50],
                classification.result.confidence.value,
                classification.usage.cost_usd,
            )

            # Schritt 6: Namen → IDs auflösen
            resolved = resolve_classification(
                classification.result,
                self._paperless.cache,
            )
            result.resolved = resolved

            # Schritt 7: Neue Einträge anlegen (falls konfiguriert)
            await self._handle_create_new(resolved, result)

            # Schritt 8: Confidence bewerten
            confidence = evaluate_confidence(resolved)
            result.confidence = confidence

            # Schritt 9: Ergebnis auf Dokument anwenden
            await self._apply_result(document_id, resolved, confidence, doc)

            # Schritt 10: Protokollierung (SQLite kommt in AP-06)
            result.success = True
            logger.info(
                "Pipeline abgeschlossen: Dokument %d → %s (%s), %.1fs",
                document_id,
                confidence.level.value,
                confidence.action.value,
                time.monotonic() - start_time,
            )

        except PaperlessError as exc:
            result.error = str(exc)
            result.error_type = type(exc).__name__
            logger.error(
                "Paperless-Fehler bei Dokument %d: %s", document_id, exc,
            )
            # Fehler-Status setzen (Tag "NEU" bleibt erhalten für Retry)
            await self._set_error_status(document_id)

        except ClaudeError as exc:
            # Rate-Limit (429) oder Server-Überlast (529): Dokument NICHT als
            # Error markieren – NEU-Tag bleibt, ki_status bleibt null.
            # Exception wird an den Poller weitergereicht, der den Zyklus abbricht.
            if isinstance(exc, ClaudeAPIError) and exc.status_code in (429, 529):
                logger.warning(
                    "Rate-Limit/Überlast bei Dokument %d (HTTP %d) – "
                    "Dokument bleibt unverändert für nächsten Zyklus",
                    document_id, exc.status_code,
                )
                result.error = str(exc)
                result.error_type = "RateLimitError"
                raise

            result.error = str(exc)
            result.error_type = type(exc).__name__
            logger.error(
                "Claude-Fehler bei Dokument %d: %s", document_id, exc,
            )
            await self._set_error_status(document_id)

        except Exception as exc:
            result.error = str(exc)
            result.error_type = type(exc).__name__
            logger.exception(
                "Unerwarteter Fehler bei Dokument %d: %s", document_id, exc,
            )
            await self._set_error_status(document_id)

        finally:
            result.duration_seconds = time.monotonic() - start_time

        return result

    # --- System-Prompt (gecacht) ---

    async def _get_system_prompt(self) -> str:
        """Gibt den System-Prompt zurück, baut ihn bei Bedarf neu.

        Der Prompt wird gecacht weil sich Stammdaten selten ändern.
        Prompt Caching auf API-Seite profitiert davon, dass der
        System-Prompt zwischen Aufrufen identisch ist.
        """
        if self._system_prompt is not None:
            return self._system_prompt

        cache = self._paperless.cache
        if not cache.is_loaded:
            logger.info("Cache nicht geladen – lade Stammdaten...")
            await self._paperless.load_cache()

        # PromptData aus Cache befüllen
        data = PromptData(
            correspondents=cache.get_all_correspondent_names(),
            document_types=cache.get_all_document_type_names(),
            tags=cache.get_all_tag_names(),
            storage_paths=cache.get_all_storage_path_names(),
            person_options=cache.get_select_option_labels(CF_PERSON),
            house_register_options=cache.get_select_option_labels(CF_HAUS_REGISTER),
        )
        self._prompt_data = data
        self._system_prompt = build_system_prompt(data)

        logger.info(
            "System-Prompt gebaut: %d Zeichen", len(self._system_prompt),
        )
        return self._system_prompt

    def invalidate_prompt_cache(self) -> None:
        """Erzwingt Neugenerierung des System-Prompts beim nächsten Aufruf.

        Aufrufen nach:
        - Stammdaten-Änderung (neuer Korrespondent, Tag, etc.)
        - Regelwerk-Update über die Web-UI
        - Cache-Refresh
        """
        self._system_prompt = None
        self._prompt_data = None
        logger.debug("System-Prompt-Cache invalidiert")

    # --- Hilfsmethoden ---

    async def _download_pdf(self, document_id: int) -> bytes:
        """Lädt das Original-PDF von Paperless herunter.

        Verwendet das Original (nicht die archivierte Version), damit
        Claude den physischen Zustand sieht (Stempel, Scans, etc.).
        """
        pdf_bytes = await self._paperless.get_document_content(
            document_id, original=True,
        )
        logger.debug(
            "PDF heruntergeladen: Dokument %d, %d bytes",
            document_id, len(pdf_bytes),
        )
        return pdf_bytes

    async def _handle_create_new(
        self,
        resolved: ResolvedClassification,
        result: PipelineResult,
    ) -> None:
        """Legt neue Paperless-Einträge an wenn konfiguriert.

        Prüft erst ob die Neuanlage-Option in der Config aktiviert ist.
        Nach Anlage wird die ID im Resolved-Objekt aktualisiert und
        der Prompt-Cache invalidiert.
        """
        cache_dirty = False

        # Neue Korrespondenten
        if self._config.auto_create_correspondents:
            for name in resolved.create_new_correspondents:
                # Duplikatprüfung: Vielleicht wurde der Name inzwischen doch
                # per Fuzzy-Match aufgelöst
                if self._paperless.cache.get_correspondent_id(name) is not None:
                    logger.debug("Korrespondent '%s' existiert bereits – überspringe", name)
                    continue
                try:
                    created = await self._paperless.create_correspondent(name)
                    result.created_correspondents.append(
                        {"name": name, "id": created.id}
                    )
                    # ID im Resolved-Objekt nachträglich setzen
                    if (resolved.correspondent_resolution
                            and resolved.correspondent_resolution.original_name == name):
                        resolved.correspondent_id = created.id
                    cache_dirty = True
                    logger.info("Korrespondent angelegt: '%s' (ID %d)", name, created.id)
                except PaperlessError as exc:
                    logger.warning(
                        "Korrespondent '%s' konnte nicht angelegt werden: %s",
                        name, exc,
                    )

        # Neue Dokumenttypen
        if self._config.auto_create_document_types:
            for name in resolved.create_new_document_types:
                if self._paperless.cache.get_document_type_id(name) is not None:
                    continue
                try:
                    created = await self._paperless.create_document_type(name)
                    result.created_document_types.append(
                        {"name": name, "id": created.id}
                    )
                    if (resolved.document_type_resolution
                            and resolved.document_type_resolution.original_name == name):
                        resolved.document_type_id = created.id
                    cache_dirty = True
                    logger.info("Dokumenttyp angelegt: '%s' (ID %d)", name, created.id)
                except PaperlessError as exc:
                    logger.warning(
                        "Dokumenttyp '%s' konnte nicht angelegt werden: %s",
                        name, exc,
                    )

        # Neue Tags
        if self._config.auto_create_tags:
            for name in resolved.create_new_tags:
                if self._paperless.cache.get_tag_id(name) is not None:
                    continue
                try:
                    created = await self._paperless.create_tag(name)
                    result.created_tags.append(
                        {"name": name, "id": created.id}
                    )
                    resolved.tag_ids.append(created.id)
                    cache_dirty = True
                    logger.info("Tag angelegt: '%s' (ID %d)", name, created.id)
                except PaperlessError as exc:
                    logger.warning(
                        "Tag '%s' konnte nicht angelegt werden: %s",
                        name, exc,
                    )

        # Neue Speicherpfade
        if self._config.auto_create_storage_paths:
            for sp_data in resolved.create_new_storage_paths:
                sp_name = sp_data["name"]
                if self._paperless.cache.get_storage_path_id(sp_name) is not None:
                    continue
                sp_path = sp_data.get(
                    "path_template",
                    self._config.storage_path_template,
                )
                try:
                    created = await self._paperless.create_storage_path(
                        name=sp_name, path=sp_path,
                    )
                    result.created_storage_paths.append(
                        {"name": sp_name, "id": created.id}
                    )
                    if (resolved.storage_path_resolution
                            and resolved.storage_path_resolution.original_name == sp_name):
                        resolved.storage_path_id = created.id
                    cache_dirty = True
                    logger.info(
                        "Speicherpfad angelegt: '%s' (ID %d)", sp_name, created.id,
                    )
                except PaperlessError as exc:
                    logger.warning(
                        "Speicherpfad '%s' konnte nicht angelegt werden: %s",
                        sp_name, exc,
                    )

        if cache_dirty:
            self.invalidate_prompt_cache()

    async def _apply_result(
        self,
        document_id: int,
        resolved: ResolvedClassification,
        confidence: ConfidenceEvaluation,
        doc: Any,
    ) -> None:
        """Wendet das Klassifizierungsergebnis auf das Paperless-Dokument an.

        Alle Änderungen (Metadaten, Tags, Custom Fields) werden in einem
        einzigen PATCH-Aufruf gesendet.  Das verhindert Race Conditions,
        bei denen nachfolgende PATCHes den Tag-Zustand aus einem vorherigen
        PATCH überschreiben, bevor Paperless ihn vollständig committet hat.

        Je nach Confidence-Level werden Felder gesetzt oder nicht:
        - HIGH:   Alles anwenden, ki_status = "classified"
        - MEDIUM: Alles anwenden, ki_status = "review"
        - LOW:    Nur ki_status = "review", Felder nicht anwenden

        In allen Fällen wird Tag "NEU" entfernt.
        """
        cache = self._paperless.cache
        patch: dict[str, Any] = {}

        # --- Tags: In jedem Fall NEU entfernen ---
        current_tags = set(doc.tags)
        current_tags.discard(TAG_NEU_ID)

        # --- Custom Fields: Bestehende übernehmen, dann gezielt ändern ---
        # Wir bauen die komplette custom_fields-Liste selbst, statt
        # mehrere Einzel-PATCHes zu machen.  So sendet Paperless nur
        # einen einzigen DB-Write.
        cf_map: dict[int, Any] = {
            cf.field: cf.value for cf in doc.custom_fields
        }

        # ki_status immer setzen (Label → Option-ID über Cache)
        ki_status_option_id = cache.require_select_option_id(
            CF_KI_STATUS, confidence.ki_status,
        )
        cf_map[CF_KI_STATUS] = ki_status_option_id

        if confidence.should_apply_fields:
            # Titel
            if resolved.title:
                patch["title"] = resolved.title

            # Korrespondent
            if resolved.correspondent_id is not None:
                patch["correspondent"] = resolved.correspondent_id

            # Dokumenttyp
            if resolved.document_type_id is not None:
                patch["document_type"] = resolved.document_type_id

            # Speicherpfad
            if resolved.storage_path_id is not None:
                patch["storage_path"] = resolved.storage_path_id

            # Datum
            if resolved.date:
                patch["created_date"] = resolved.date

            # Neue Tags hinzufügen
            current_tags.update(resolved.tag_ids)

            # Aufgelöste Custom Fields setzen (z.B. Person)
            for cf in resolved.custom_fields:
                if cf.resolved and cf.value is not None:
                    cf_map[cf.field_id] = cf.value
                    logger.debug(
                        "Custom Field %d gesetzt: %s", cf.field_id, cf.value,
                    )

            # Paginierung: Bei digitalem PDF das Feld entfernen
            raw = resolved.raw_result
            if raw and not raw.is_scanned_document and raw.pagination_stamp is None:
                existing_val = doc.get_custom_field_value(CF_PAGINIERUNG)
                if existing_val is not None:
                    cf_map.pop(CF_PAGINIERUNG, None)
                    logger.debug(
                        "Paginierung entfernt (digitales Dokument): Dokument %d",
                        document_id,
                    )

            # Haus-Felder: Bei digitalem PDF ebenfalls entfernen.
            # Digitale Dokumente werden nicht physisch im Haus-Ordner abgelegt
            # (Design-Dok 13.6.1).
            if raw and not raw.is_scanned_document:
                for cf_id in (CF_HAUS_REGISTER, CF_HAUS_ORDNUNGSZAHL):
                    existing_val = doc.get_custom_field_value(cf_id)
                    if existing_val is not None:
                        cf_map.pop(cf_id, None)
                        logger.debug(
                            "Haus-Feld %d entfernt (digitales Dokument): Dokument %d",
                            cf_id, document_id,
                        )

            logger.info(
                "Felder anwenden: Dokument %d, confidence=%s",
                document_id, confidence.level.value,
            )
        else:
            logger.info(
                "Felder NICHT anwenden (low confidence): Dokument %d",
                document_id,
            )

        # --- Alles in einem einzigen PATCH senden ---
        patch["tags"] = sorted(current_tags)
        patch["custom_fields"] = [
            {"field": fid, "value": val} for fid, val in cf_map.items()
        ]

        await self._paperless.update_document(document_id, **patch)

    async def _set_error_status(self, document_id: int) -> None:
        """Setzt ki_status = "error" und entfernt Tag "NEU" bei Verarbeitungsfehlern.

        Tag "NEU" wird entfernt, damit der Poller das Dokument nicht endlos
        erneut versucht.  Retry ist jederzeit möglich, indem der Nutzer den
        NEU-Tag manuell wieder zuweist.
        """
        try:
            await self._paperless.set_custom_field_by_label(
                document_id, CF_KI_STATUS, "error",
            )
            logger.info("ki_status='error' gesetzt: Dokument %d", document_id)
        except Exception as exc:
            logger.error(
                "Konnte ki_status='error' nicht setzen für Dokument %d: %s",
                document_id, exc,
            )

        # Tag "NEU" entfernen – separat gefangen, damit ki_status auch bei
        # Tag-Fehler gesetzt bleibt
        try:
            doc = await self._paperless.get_document(document_id)
            current_tags = set(doc.tags)
            if TAG_NEU_ID in current_tags:
                current_tags.discard(TAG_NEU_ID)
                await self._paperless.update_document(
                    document_id, tags=sorted(current_tags),
                )
                logger.info("Tag 'NEU' entfernt (Error): Dokument %d", document_id)
        except Exception as exc:
            logger.error(
                "Konnte Tag 'NEU' nicht entfernen für Dokument %d: %s",
                document_id, exc,
            )
