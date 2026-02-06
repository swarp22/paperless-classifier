"""Claude API Client für PDF-Klassifizierung.

Verwendet das Anthropic Python SDK (AsyncAnthropic) für asynchrone
API-Aufrufe.  Unterstützt PDF-Versand als Base64, Prompt Caching
und strukturiertes Response-Parsing.

Batch API ist als Schnittstelle vorbereitet (TODO Phase 4).
"""

import base64
import json
import logging
import re
from enum import Enum
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field

from app.claude.cost_tracker import CostTracker, TokenUsage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ClaudeError(Exception):
    """Basisklasse für alle Claude-Client-Fehler."""


class ClaudeConfigError(ClaudeError):
    """Fehlende oder ungültige Konfiguration (z.B. kein API-Key)."""


class ClaudeAPIError(ClaudeError):
    """Fehler bei der Kommunikation mit der Claude API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ClaudeResponseError(ClaudeError):
    """Antwort von Claude konnte nicht geparst oder validiert werden."""

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


class CostLimitReachedError(ClaudeError):
    """Monatliches Kostenlimit ist erreicht."""

    def __init__(self, limit_usd: float, current_usd: float) -> None:
        super().__init__(
            f"Monatliches Kostenlimit erreicht: ${current_usd:.2f} / ${limit_usd:.2f}"
        )
        self.limit_usd = limit_usd
        self.current_usd = current_usd


# ---------------------------------------------------------------------------
# Enums und Response-Modelle
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    """Konfidenz-Stufen für Klassifizierungsergebnisse."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SearchHints(BaseModel):
    """Suchhinweise für die Dokumentenverknüpfung (Kandidatensuche)."""
    correspondent_pattern: Optional[str] = None
    document_types: list[str] = Field(default_factory=list)
    date_range_days: int = Field(default=7, ge=1)


class LinkPosition(BaseModel):
    """Einzelne Position in einem Aggregator-Dokument (z.B. AXA Abrechnung)."""
    behandlungsdatum: Optional[str] = None
    leistungserbringer: Optional[str] = None
    rechnungsbetrag: Optional[float] = None
    search_hints: Optional[SearchHints] = None


class ExtractableData(BaseModel):
    """Extrahierbare Verknüpfungsdaten aus einem Source-Dokument."""
    behandlungsdatum: Optional[str] = None
    rechnungsbetrag: Optional[float] = None
    leistungserbringer: Optional[str] = None


class LinkExtraction(BaseModel):
    """Dokumentenverknüpfungs-Informationen."""
    is_linkable_document: bool = False
    document_role: Optional[str] = Field(
        default=None,
        description="'aggregator' oder 'source'",
    )
    positions: list[LinkPosition] = Field(default_factory=list)
    extractable_data: Optional[ExtractableData] = None


class NewStoragePath(BaseModel):
    """Vorschlag für einen neu anzulegenden Speicherpfad."""
    name: str
    path_template: str


class CreateNew(BaseModel):
    """Vorschläge für neu anzulegende Paperless-Entitäten."""
    correspondents: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)
    storage_paths: list[NewStoragePath] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """Strukturiertes Klassifizierungsergebnis von Claude.

    Bildet das vollständige JSON-Schema aus dem Design-Dokument
    Abschnitt 6.1 ab.  Alle Felder sind optional oder haben Defaults,
    damit auch unvollständige Claude-Antworten geparst werden können.
    """

    # Kernklassifizierung
    title: str = Field("", description="Dokumenttitel")
    document_type: Optional[str] = Field(None, description="Dokumenttyp-Name")
    correspondent: Optional[str] = Field(None, description="Korrespondent-Name")
    tags: list[str] = Field(default_factory=list, description="Tag-Namen")
    storage_path: Optional[str] = Field(None, description="Speicherpfad-Name")
    date: Optional[str] = Field(None, description="Dokumentdatum (YYYY-MM-DD)")

    # Scan-Erkennung
    is_scanned_document: bool = Field(False)

    # Paginierstempel
    pagination_stamp: Optional[int] = Field(None, description="Stempel-Nummer")
    pagination_stamp_confidence: Optional[ConfidenceLevel] = None

    # Haus-Ordner
    is_house_folder_candidate: bool = Field(False)
    house_register: Optional[str] = Field(None, description="Register-Name")

    # Personen-Zuordnung
    person: Optional[str] = Field(None, description="Max, Melanie oder Kilian")
    person_confidence: Optional[ConfidenceLevel] = None
    person_reasoning: Optional[str] = None

    # Steuer-Relevanz
    tax_relevant: bool = Field(False)
    tax_year: Optional[int] = None

    # Dokumentenverknüpfung
    link_extraction: Optional[LinkExtraction] = None

    # Gesamtbewertung
    confidence: ConfidenceLevel = Field(ConfidenceLevel.LOW)
    reasoning: str = Field("", description="Begründung der Klassifizierung")

    # Neuanlage-Vorschläge
    create_new: Optional[CreateNew] = None


class ClassificationResponse(BaseModel):
    """Vollständige Antwort einer Klassifizierung inkl. Metadaten.

    Kapselt das Klassifizierungsergebnis zusammen mit Token-Verbrauch
    und Debugging-Informationen.
    """

    result: ClassificationResult
    usage: TokenUsage
    raw_response: str = Field("", description="Rohe JSON-Antwort für Debugging")
    model: str = Field("", description="Tatsächlich verwendetes Modell")
    stop_reason: str = Field("", description="Grund für Antwort-Ende")


# ---------------------------------------------------------------------------
# Claude Client
# ---------------------------------------------------------------------------

# Maximale PDF-Größe: 32 MB (Anthropic API Limit)
MAX_PDF_SIZE_BYTES = 32 * 1024 * 1024

# Standard max_tokens für Klassifizierung (Design-Dokument Abschnitt 2.2)
DEFAULT_MAX_TOKENS = 2048


class ClaudeClient:
    """Asynchroner Client für die Claude API.

    Verwendet AsyncAnthropic für nicht-blockierende API-Aufrufe.
    Unterstützt Prompt Caching, Token-Tracking und Kostenkontrolle.

    Verwendung:
        async with ClaudeClient(api_key="sk-ant-...") as client:
            response = await client.classify_document(pdf_bytes, system_prompt)
    """

    def __init__(
        self,
        api_key: str,
        default_model: str = "claude-sonnet-4-5-20250929",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = 2,
        cost_tracker: CostTracker | None = None,
        monthly_cost_limit_usd: float = 0.0,
    ) -> None:
        """Initialisiert den Claude Client.

        Args:
            api_key: Anthropic API Key (muss mit 'sk-ant-' beginnen).
            default_model: Standard-Modell für classify_document().
            max_tokens: Maximale Anzahl Output-Tokens.
            max_retries: Anzahl automatischer Retries bei 429/5xx.
            cost_tracker: Optionaler CostTracker für Verbrauchsaufzeichnung.
            monthly_cost_limit_usd: Monatslimit in USD (0 = kein Limit).

        Raises:
            ClaudeConfigError: Wenn der API-Key fehlt oder ungültig ist.
        """
        if not api_key:
            raise ClaudeConfigError(
                "ANTHROPIC_API_KEY ist nicht konfiguriert. "
                "Bitte in .env oder als Umgebungsvariable setzen."
            )
        if not api_key.startswith("sk-ant-"):
            raise ClaudeConfigError(
                "ANTHROPIC_API_KEY hat ein ungültiges Format "
                "(erwartet: Prefix 'sk-ant-')."
            )

        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            max_retries=max_retries,
        )
        self._default_model = default_model
        self._max_tokens = max_tokens
        self._cost_tracker = cost_tracker
        self._monthly_cost_limit_usd = monthly_cost_limit_usd

        logger.info(
            "ClaudeClient initialisiert: model=%s, max_tokens=%d, "
            "retries=%d, limit=$%.2f",
            default_model,
            max_tokens,
            max_retries,
            monthly_cost_limit_usd,
        )

    async def close(self) -> None:
        """Schließt den HTTP-Client und gibt Ressourcen frei."""
        await self._client.close()
        logger.debug("ClaudeClient geschlossen")

    async def __aenter__(self) -> "ClaudeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # --- Hauptmethode: Einzelklassifizierung ---

    async def classify_document(
        self,
        pdf_bytes: bytes,
        system_prompt: str,
        model: str | None = None,
        document_id: int | None = None,
        enable_cache: bool = True,
    ) -> ClassificationResponse:
        """Sendet ein PDF an Claude und erhält ein Klassifizierungsergebnis.

        Args:
            pdf_bytes: Rohbytes des PDFs.
            system_prompt: Vollständiger System-Prompt (aus prompts.build_system_prompt).
            model: Modell-Override (None = default_model).
            document_id: Paperless Dokument-ID für Tracking.
            enable_cache: Prompt Caching für System-Prompt aktivieren.

        Returns:
            ClassificationResponse mit Ergebnis, Token-Verbrauch und Metadaten.

        Raises:
            ClaudeConfigError: API-Key fehlt.
            CostLimitReachedError: Monatslimit erreicht.
            ClaudeAPIError: API-Kommunikationsfehler.
            ClaudeResponseError: Antwort konnte nicht geparst werden.
            ValueError: PDF zu groß oder leer.
        """
        # Vorprüfungen
        self._validate_pdf(pdf_bytes)
        self._check_cost_limit()

        used_model = model or self._default_model

        # PDF als Base64 kodieren
        pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

        # System-Prompt mit optionalem Cache-Control
        system_block: dict[str, Any] = {
            "type": "text",
            "text": system_prompt,
        }
        if enable_cache:
            system_block["cache_control"] = {"type": "ephemeral"}

        logger.info(
            "Klassifizierung starten: model=%s, pdf_size=%d bytes, "
            "doc_id=%s, cache=%s",
            used_model,
            len(pdf_bytes),
            document_id,
            enable_cache,
        )

        # API-Aufruf
        try:
            message = await self._client.messages.create(
                model=used_model,
                max_tokens=self._max_tokens,
                system=[system_block],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_base64,
                                },
                            },
                            {
                                "type": "text",
                                "text": "Analysiere und klassifiziere dieses Dokument.",
                            },
                        ],
                    }
                ],
            )
        except anthropic.APIConnectionError as exc:
            raise ClaudeAPIError(
                f"Verbindung zur Claude API fehlgeschlagen: {exc}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeAPIError(
                "Claude API Rate-Limit erreicht (429). "
                "Alle Retries erschöpft.",
                status_code=429,
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeAPIError(
                f"Claude API Fehler (HTTP {exc.status_code}): {exc.message}",
                status_code=exc.status_code,
            ) from exc

        # Token-Verbrauch extrahieren und aufzeichnen
        usage = self._extract_usage(message, used_model, document_id)
        if self._cost_tracker:
            self._cost_tracker.record(usage)

        # Antwort parsen
        raw_text = self._extract_text(message)
        result = self._parse_response(raw_text)

        logger.info(
            "Klassifizierung abgeschlossen: doc_id=%s, confidence=%s, "
            "title='%s', cost=$%.6f",
            document_id,
            result.confidence.value,
            result.title[:50],
            usage.cost_usd,
        )

        return ClassificationResponse(
            result=result,
            usage=usage,
            raw_response=raw_text,
            model=message.model,
            stop_reason=message.stop_reason or "",
        )

    # --- Batch API (TODO Phase 4) ---

    async def batch_classify(
        self,
        documents: list[dict[str, Any]],
        system_prompt: str,
        model: str | None = None,
    ) -> str:
        """Erstellt einen Batch-Job für mehrere Dokumente.

        TODO Phase 4: Batch API Integration.
        Erwartet Liste von {"id": int, "pdf_bytes": bytes}.
        Gibt die Batch-ID zurück, Ergebnisse werden asynchron abgeholt.

        Args:
            documents: Liste mit Dokument-Dicts (id + pdf_bytes).
            system_prompt: System-Prompt (für alle Dokumente identisch).
            model: Modell-Override.

        Returns:
            Batch-Job-ID als String.

        Raises:
            NotImplementedError: Batch API ist noch nicht implementiert.
        """
        raise NotImplementedError(
            "Batch API wird in Phase 4 implementiert. "
            "Verwende classify_document() für Einzelverarbeitung."
        )

    async def get_batch_results(self, batch_id: str) -> list[ClassificationResponse]:
        """Holt die Ergebnisse eines abgeschlossenen Batch-Jobs.

        TODO Phase 4: Batch API Integration.

        Args:
            batch_id: ID des Batch-Jobs.

        Returns:
            Liste von ClassificationResponse-Objekten.

        Raises:
            NotImplementedError: Batch API ist noch nicht implementiert.
        """
        raise NotImplementedError(
            "Batch API wird in Phase 4 implementiert."
        )

    # --- Hilfsmethoden (intern) ---

    @staticmethod
    def _validate_pdf(pdf_bytes: bytes) -> None:
        """Validiert PDF-Rohdaten vor dem API-Aufruf.

        Raises:
            ValueError: Wenn das PDF leer oder zu groß ist.
        """
        if not pdf_bytes:
            raise ValueError("PDF-Daten sind leer")
        if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
            size_mb = len(pdf_bytes) / (1024 * 1024)
            raise ValueError(
                f"PDF ist zu groß: {size_mb:.1f} MB "
                f"(Maximum: {MAX_PDF_SIZE_BYTES / (1024 * 1024):.0f} MB)"
            )
        # Minimale Magic-Byte-Prüfung: PDF beginnt mit %PDF
        if not pdf_bytes[:5].startswith(b"%PDF"):
            logger.warning(
                "PDF-Daten beginnen nicht mit %%PDF Magic Bytes – "
                "möglicherweise kein gültiges PDF"
            )

    def _check_cost_limit(self) -> None:
        """Prüft ob das monatliche Kostenlimit erreicht ist.

        Raises:
            CostLimitReachedError: Wenn das Limit erreicht ist.
        """
        if not self._cost_tracker or self._monthly_cost_limit_usd <= 0:
            return
        if self._cost_tracker.is_limit_reached(self._monthly_cost_limit_usd):
            current = self._cost_tracker.get_monthly_cost()
            raise CostLimitReachedError(
                limit_usd=self._monthly_cost_limit_usd,
                current_usd=current,
            )

    @staticmethod
    def _extract_usage(
        message: Any,
        model: str,
        document_id: int | None,
    ) -> TokenUsage:
        """Extrahiert Token-Verbrauch aus der API-Antwort.

        Verwendet getattr() für Cache-Token-Felder, da diese je nach
        SDK-Version optional sein können.
        """
        return TokenUsage(
            model=model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=getattr(
                message.usage, "cache_read_input_tokens", 0
            ) or 0,
            cache_creation_tokens=getattr(
                message.usage, "cache_creation_input_tokens", 0
            ) or 0,
            document_id=document_id,
        )

    @staticmethod
    def _extract_text(message: Any) -> str:
        """Extrahiert den Textinhalt aus der API-Antwort.

        Sucht den ersten TextBlock in message.content.

        Raises:
            ClaudeResponseError: Wenn kein Textinhalt vorhanden ist.
        """
        for block in message.content:
            if hasattr(block, "text") and block.text:
                return block.text

        raise ClaudeResponseError(
            "Claude-Antwort enthält keinen Textinhalt",
            raw_response=str(message.content),
        )

    @staticmethod
    def _parse_response(raw_text: str) -> ClassificationResult:
        """Parst die JSON-Antwort von Claude in ein ClassificationResult.

        Behandelt gängige Abweichungen:
        - JSON in Markdown-Codeblöcken (```json ... ```)
        - Führender/nachfolgender Whitespace
        - Unbekannte Felder (werden ignoriert)

        Args:
            raw_text: Roher Antworttext von Claude.

        Returns:
            Validiertes ClassificationResult.

        Raises:
            ClaudeResponseError: Wenn JSON ungültig oder nicht parsbar ist.
        """
        cleaned = raw_text.strip()

        # Markdown-Codeblock entfernen falls vorhanden
        # Matcht ```json ... ``` oder ``` ... ```
        codeblock_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            cleaned,
            re.DOTALL,
        )
        if codeblock_match:
            cleaned = codeblock_match.group(1).strip()

        # JSON parsen
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ClaudeResponseError(
                f"Ungültiges JSON in Claude-Antwort: {exc}",
                raw_response=raw_text,
            ) from exc

        if not isinstance(data, dict):
            raise ClaudeResponseError(
                f"JSON ist kein Objekt sondern {type(data).__name__}",
                raw_response=raw_text,
            )

        # Pydantic-Validierung (unbekannte Felder werden ignoriert)
        try:
            return ClassificationResult.model_validate(data)
        except Exception as exc:
            raise ClaudeResponseError(
                f"JSON-Validierung fehlgeschlagen: {exc}",
                raw_response=raw_text,
            ) from exc
