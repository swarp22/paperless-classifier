"""Schema-Analyzer: Orchestriert die vollständige Schema-Analyse.

Ablauf eines Analyse-Laufs:
1. Collector sammelt Daten aus Paperless (lokal, kein LLM)
2. Opus-Prompt wird aus den Collector-Daten gebaut
3. Opus-API-Aufruf (ein einziger Request)
4. JSON-Antwort wird geparst und validiert
5. Ergebnisse werden über SchemaStorage in SQLite gespeichert
6. Audit-Log (AnalysisRunRecord) wird geschrieben

Fehler in jedem Schritt werden gefangen und als Audit-Log mit
status="error" festgehalten.  Der Poller darf nicht abstürzen.

AP-11: Schema-Analyse – Opus-Analyse & Prompt-Builder (Phase 3)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, Field

from app.claude.client import ClaudeClient, TextMessageResponse
from app.claude.cost_tracker import TokenUsage
from app.db.database import Database
from app.paperless.client import PaperlessClient
from app.schema_matrix.collector import CollectorResult, SchemaCollector
from app.schema_matrix.storage import (
    AnalysisRunRecord,
    MappingEntry,
    PathRule,
    SchemaStorage,
    TagRule,
    TitlePattern,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic-Modelle für die Opus-Antwort (Entscheidung 5)
# ---------------------------------------------------------------------------

class OpusTitleSchema(BaseModel):
    """Ein Titel-Schema aus der Opus-Antwort."""

    document_type: str
    correspondent: str
    title_template: str = ""
    rule_description: str = ""
    confidence: str = "medium"
    document_count: int = 0
    outlier_count: int = 0
    outlier_titles: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class OpusPathRule(BaseModel):
    """Eine Pfad-Regel aus der Opus-Antwort."""

    topic: str
    rule_description: str = ""
    path_template: str = ""
    examples: list[str] = Field(default_factory=list)
    topic_document_count: int = 0
    normalization_suggestions: list[Any] = Field(default_factory=list)
    confidence: str = "medium"


class OpusMappingEntry(BaseModel):
    """Eine Zuordnung aus der Opus-Antwort."""

    correspondent: str
    document_type: str | None = None
    storage_path_name: str | None = None
    mapping_type: str = "exact"
    condition_description: str | None = None
    document_count: int = 0
    confidence: str = "medium"


class OpusSuggestion(BaseModel):
    """Ein Verbesserungsvorschlag von Opus."""

    category: str = "general"
    description: str = ""
    priority: str = "medium"


class OpusTagRuleItem(BaseModel):
    """Eine Tag-Zuordnungsregel aus der Opus-Antwort (AP-11b)."""

    correspondent: str | None = None      # None/leer = gilt für alle
    document_type: str
    positive_tags: list[str] = Field(default_factory=list)
    negative_tags: list[str] = Field(default_factory=list)
    reasoning: str = ""
    confidence: float = 0.5


class OpusAnalysisResponse(BaseModel):
    """Vollständige validierte Opus-Antwort."""

    title_schemas: list[OpusTitleSchema] = Field(default_factory=list)
    path_rules: list[OpusPathRule] = Field(default_factory=list)
    mapping_matrix: list[OpusMappingEntry] = Field(default_factory=list)
    tag_rules: list[OpusTagRuleItem] = Field(default_factory=list)
    suggestions: list[OpusSuggestion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Opus-Prompt-Vorlage (aus Design-Dokument Abschnitt 8)
# ---------------------------------------------------------------------------

_SCHEMA_ANALYSIS_SYSTEM_PROMPT = """\
Du analysierst die vollständige Organisationsstruktur eines Paperless-ngx \
Dokumentenarchivs. Deine Aufgabe hat vier Teile.

Antworte ausschließlich mit validem JSON. Kein Markdown, kein erklärender Text.
Verwende exakt die Schlüssel "title_schemas", "path_rules", \
"mapping_matrix", "tag_rules", "suggestions"."""

_SCHEMA_ANALYSIS_USER_TEMPLATE = """\
## Teil 1: Titel-Schemata

Analysiere die gruppierten Dokumenttitel und erkenne Benennungsmuster.

{title_groups_json}

Für jede Gruppe mit ≥3 Dokumenten:
1. Erkenne das Muster (Datumsformate, Nummerierung, Präfixe, etc.)
2. Formuliere eine eindeutige Regel in natürlicher Sprache
3. Gib das Template mit Platzhaltern an (z.B. "{{YYYY}}-{{MM}}", \
"Abrechnung {{Freitext}}")
4. Bewerte die Confidence (high wenn >80% der Titel dem Muster folgen)
5. Markiere Ausreißer

Für Gruppen mit <3 Dokumenten:
→ Schlage ein Schema vor basierend auf ähnlichen Gruppen

## Teil 2: Pfad-Regeln

Analysiere die Speicherpfad-Hierarchie und erkenne das Organisationsprinzip.

{path_hierarchy_json}

Das Archiv folgt einem Topic/Objekt/Entität-Schema:
- Topic: Übergeordnetes Thema (z.B. "Fahrzeuge", "Haus Bietigheim", "Ärzte")
- Objekt: Konkretes Ding/Person (z.B. "Mustang", "Dr. Hansen")
- Entität: Spezifischer Aspekt (z.B. "Versicherung", "Autohaus")

Deine Aufgabe:
1. Erkenne und formalisiere das Ordnungsprinzip pro Topic
2. Beschreibe die Regel, nach der neue Pfade angelegt werden sollten
3. Identifiziere Inkonsistenzen
4. Schlage Normalisierungen vor (optional, als Vorschlag, nicht als Pflicht)

## Teil 3: Zuordnungsmatrix

Analysiere, welcher Korrespondent + Dokumenttyp zu welchem Speicherpfad führt.

{mapping_table_json}

Für jede Kombination:
1. Wenn eindeutig (1:1): Markiere als "exact"
2. Wenn mehrdeutig (1:N): Beschreibe das Unterscheidungskriterium als \
"conditional" mit condition_description
3. Wenn ein Korrespondent keinem Pfad zugeordnet ist: Vorschlag machen

{changes_section}

## Teil 4: Tag-Zuordnungsregeln

Die Titel-Gruppen enthalten jetzt auch Tag-Informationen (common_tags, \
tag_distribution). Analysiere die Tag-Vergabe pro Dokumenttyp-Korrespondent-\
Kombination:

1. Welche Tags werden konsistent vergeben (bei >80% der Dokumente)?
2. Welche Tags fehlen BEWUSST – d.h. ein Tag liegt inhaltlich nahe, wird \
aber bei dieser Kombination nie oder fast nie vergeben? Beispiel: \
"Steuer {{Jahr}}" bei Gehaltsabrechnungen, wenn eine elektronische \
Lohnsteuerbescheinigung das Einzeltagging überflüssig macht.
3. Formuliere positive Regeln ("Typ X bekommt immer Tag Y") und negative \
Regeln ("Typ X bekommt NICHT Tag Y, weil ...").
4. Wenn ein Korrespondent für die Regel irrelevant ist (Regel gilt \
dokumenttyp-weit), setze correspondent auf null.

Erstelle nur Regeln mit hoher Aussagekraft (≥5 Dokumente in der Gruppe). \
Keine Regeln für Gruppen mit <5 Dokumenten.

Antwortformat: JSON mit den Schlüsseln "title_schemas", "path_rules", \
"mapping_matrix", "tag_rules", "suggestions".

Jedes Element in "title_schemas" hat: document_type, correspondent, \
title_template, rule_description, confidence, document_count, \
outlier_count, outlier_titles, examples.

Jedes Element in "path_rules" hat: topic, rule_description, path_template, \
examples, topic_document_count, normalization_suggestions, confidence.

Jedes Element in "mapping_matrix" hat: correspondent, document_type, \
storage_path_name, mapping_type, condition_description, document_count, \
confidence.

Jedes Element in "tag_rules" hat: correspondent (string oder null), \
document_type, positive_tags (Liste), negative_tags (Liste), \
reasoning (Begründung), confidence (0.0-1.0).

Jedes Element in "suggestions" hat: category (title/path/mapping/tags/general), \
description, priority (high/medium/low)."""


# ---------------------------------------------------------------------------
# Analyzer-Klasse
# ---------------------------------------------------------------------------

class SchemaAnalyzer:
    """Orchestriert einen vollständigen Schema-Analyse-Lauf.

    Wird vom Poller bei Trigger-Auslösung aufgerufen.
    Kann auch manuell via Web-UI gestartet werden (AP-12).

    Verwendung:
        analyzer = SchemaAnalyzer(paperless, claude, database, settings)
        run_record = await analyzer.run(trigger_type="schedule")
    """

    def __init__(
        self,
        paperless: PaperlessClient,
        claude: ClaudeClient,
        database: Database,
        model: str = "claude-opus-4-6",
        max_output_tokens: int = 16384,
    ) -> None:
        """Initialisiert den Analyzer.

        Args:
            paperless: Initialisierter PaperlessClient (mit Cache).
            claude: Initialisierter ClaudeClient.
            database: Initialisierte Database-Instanz.
            model: Opus-Modell für die Analyse.
            max_output_tokens: Max. Output-Tokens für Opus (Schema-Analyse
                liefert umfangreiche JSON-Antworten).
        """
        self._paperless = paperless
        self._claude = claude
        self._db = database
        self._storage = SchemaStorage(database)
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def run(self, trigger_type: str = "schedule") -> AnalysisRunRecord:
        """Führt einen vollständigen Schema-Analyse-Lauf durch.

        Args:
            trigger_type: Auslöser ('schedule', 'threshold', 'manual').

        Returns:
            AnalysisRunRecord mit Ergebnis und Statistiken.
            Bei Fehler: status="error" mit error_message.
        """
        start_time = time.monotonic()
        logger.info(
            "Schema-Analyse gestartet: trigger=%s, model=%s",
            trigger_type, self._model,
        )

        # Leeren Audit-Record vorbereiten (wird bei Fehler mit Teilinfos gespeichert)
        run_record = AnalysisRunRecord(
            trigger_type=trigger_type,
            model_used=self._model,
        )

        try:
            # --- Schritt 1: Daten sammeln ---
            collector_result = await self._collect_data(run_record)

            # --- Schritt 2: Opus-Prompt bauen ---
            user_prompt = self._build_opus_prompt(collector_result)

            # --- Schritt 3: Opus API aufrufen ---
            response = await self._call_opus(user_prompt, run_record)

            # --- Schritt 4: JSON-Antwort parsen ---
            parsed = self._parse_response(response.text)

            # --- Schritt 5: In SQLite speichern ---
            await self._store_results(parsed, run_record)

            # --- Schritt 6: Audit-Log finalisieren ---
            run_record.status = "completed"
            run_record.raw_response = response.text
            run_record.suggestions_json = json.dumps(
                [s.model_dump() for s in parsed.suggestions],
                ensure_ascii=False,
            )
            run_record.suggestions_count = len(parsed.suggestions)

            duration = time.monotonic() - start_time
            logger.info(
                "Schema-Analyse abgeschlossen: %.1fs, $%.4f, "
                "%d Titel-Schemata, %d Pfad-Regeln, %d Mappings, "
                "%d Tag-Regeln, %d Vorschläge",
                duration, run_record.cost_usd,
                run_record.title_schemas_created + run_record.title_schemas_updated,
                run_record.path_rules_created + run_record.path_rules_updated,
                run_record.mappings_created + run_record.mappings_updated,
                run_record.tag_rules_created + run_record.tag_rules_updated,
                run_record.suggestions_count,
            )

        except Exception as exc:
            run_record.status = "error"
            run_record.error_message = f"{type(exc).__name__}: {exc}"
            duration = time.monotonic() - start_time
            logger.error(
                "Schema-Analyse fehlgeschlagen nach %.1fs: %s",
                duration, exc,
                exc_info=True,
            )

        # Audit-Log immer schreiben (auch bei Fehler)
        try:
            await self._storage.insert_analysis_run(run_record)
        except Exception as exc:
            logger.error(
                "Audit-Log konnte nicht geschrieben werden: %s", exc,
            )

        return run_record

    # =========================================================================
    # Schritt 1: Daten sammeln
    # =========================================================================

    async def _collect_data(
        self,
        run_record: AnalysisRunRecord,
    ) -> CollectorResult:
        """Sammelt Daten über den SchemaCollector.

        Leitet vorherige Entitäten aus den bestehenden Schema-Tabellen ab
        (Entscheidung 4, Option c): Die Schema-Tabellen enthalten nach
        einem erfolgreichen Lauf genau die Entitäten, die Opus gesehen hat.
        """
        # Vorherige Entitäten aus Schema-Tabellen ableiten
        previous_correspondents: set[str] = set()
        previous_document_types: set[str] = set()
        previous_storage_paths: set[str] = set()

        try:
            existing_patterns = await self._storage.get_all_title_patterns()
            for p in existing_patterns:
                previous_correspondents.add(p.correspondent)
                previous_document_types.add(p.document_type)

            existing_mappings = await self._storage.get_all_mappings()
            for m in existing_mappings:
                previous_correspondents.add(m.correspondent)
                previous_storage_paths.add(m.storage_path_name)
                if m.document_type:
                    previous_document_types.add(m.document_type)

            existing_rules = await self._storage.get_all_path_rules()
            for r in existing_rules:
                # PathRules haben Topics, nicht direkt Pfadnamen –
                # für die Änderungserkennung reicht das indirekt
                pass
        except Exception as exc:
            logger.warning(
                "Vorherige Entitäten konnten nicht geladen werden: %s – "
                "Änderungserkennung wird übersprungen",
                exc,
            )

        # Letzten erfolgreichen Lauf für Zeitstempel holen
        last_run = await self._db.get_last_schema_analysis_run()
        last_run_at = last_run.get("run_at") if last_run else None

        collector = SchemaCollector(self._paperless)
        result = await collector.collect(
            last_run_at=last_run_at,
            previous_correspondents=previous_correspondents or None,
            previous_document_types=previous_document_types or None,
            previous_storage_paths=previous_storage_paths or None,
        )

        run_record.total_documents = result.total_documents
        run_record.docs_since_last_run = result.changes.new_documents_count

        logger.info(
            "Collector abgeschlossen: %d Dokumente, %d Titel-Gruppen, "
            "%d Pfade, %d Zuordnungen",
            result.total_documents,
            len(result.title_groups),
            len(result.path_levels),
            len(result.mapping_records),
        )

        return result

    # =========================================================================
    # Schritt 2: Opus-Prompt bauen
    # =========================================================================

    def _build_opus_prompt(self, result: CollectorResult) -> str:
        """Baut den User-Prompt für Opus aus den Collector-Daten.

        Verwendet die serialize_for_prompt()-Methode des Collectors und
        füllt die Platzhalter im User-Template.
        """
        collector = SchemaCollector(self._paperless)
        serialized = collector.serialize_for_prompt(result)

        # Änderungs-Sektion (nur wenn es Änderungen gibt)
        changes_section = ""
        if serialized.get("changes_since_last_run"):
            changes_section = (
                "## Änderungen seit dem letzten Lauf\n\n"
                + json.dumps(
                    serialized["changes_since_last_run"],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n\nBewerte, ob die Änderungen zu bestehenden Regeln "
                "passen oder ob Regeln angepasst werden müssen."
            )

        user_prompt = _SCHEMA_ANALYSIS_USER_TEMPLATE.format(
            title_groups_json=json.dumps(
                serialized["title_groups"],
                ensure_ascii=False,
                indent=2,
            ),
            path_hierarchy_json=json.dumps(
                serialized["path_hierarchy"],
                ensure_ascii=False,
                indent=2,
            ),
            mapping_table_json=json.dumps(
                serialized["mapping_table"],
                ensure_ascii=False,
                indent=2,
            ),
            changes_section=changes_section,
        )

        logger.debug(
            "Opus-Prompt gebaut: %d Zeichen User-Message", len(user_prompt),
        )
        return user_prompt

    # =========================================================================
    # Schritt 3: Opus API aufrufen
    # =========================================================================

    async def _call_opus(
        self,
        user_prompt: str,
        run_record: AnalysisRunRecord,
    ) -> TextMessageResponse:
        """Sendet den Analyse-Prompt an Opus.

        effort="low" verhindert, dass Opus Adaptive Thinking benutzt.
        Für strukturierte JSON-Extraktion ist Deep Reasoning kontraproduktiv –
        es verbraucht Token-Budget ohne Mehrwert und kann zu leeren Antworten
        führen (E-034).
        """
        response = await self._claude.send_message(
            system_prompt=_SCHEMA_ANALYSIS_SYSTEM_PROMPT,
            user_message=user_prompt,
            model=self._model,
            max_tokens=self._max_output_tokens,
            enable_cache=False,  # Schema-Prompt ändert sich bei jedem Lauf
            tracking_label="schema_analysis",
            effort="low",
        )

        # Token/Kosten im Audit-Record festhalten
        run_record.input_tokens = response.usage.input_tokens
        run_record.output_tokens = response.usage.output_tokens
        run_record.cost_usd = response.usage.cost_usd

        logger.info(
            "Opus-Antwort erhalten: %d input, %d output, $%.4f",
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.usage.cost_usd,
        )

        return response

    # =========================================================================
    # Schritt 4: JSON-Antwort parsen
    # =========================================================================

    def _parse_response(self, raw_text: str) -> OpusAnalysisResponse:
        """Parst die Opus-Antwort in validierte Pydantic-Modelle.

        Behandelt:
        - JSON in Markdown-Codeblöcken (```json ... ```)
        - Unbekannte Felder (werden ignoriert)
        - Fehlende optionale Felder (Defaults greifen)

        Raises:
            ValueError: Wenn die Antwort kein valides JSON enthält.
        """
        # Diagnose: Rohtext loggen (AP-11 Debugging)
        logger.info(
            "Opus-Rohtext: %d Zeichen, erste 200: %s",
            len(raw_text),
            repr(raw_text[:200]) if raw_text else "(leer)",
        )

        cleaned = raw_text.strip()

        # Markdown-Codeblock entfernen
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
            raise ValueError(
                f"Opus-Antwort enthält kein valides JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"Opus-Antwort ist kein JSON-Objekt sondern {type(data).__name__}"
            )

        # Pydantic-Validierung (fehlertolerant dank Defaults)
        try:
            parsed = OpusAnalysisResponse.model_validate(data)
        except Exception as exc:
            raise ValueError(
                f"Opus-Antwort konnte nicht validiert werden: {exc}"
            ) from exc

        logger.info(
            "Opus-Antwort geparst: %d Titel-Schemata, %d Pfad-Regeln, "
            "%d Mappings, %d Tag-Regeln, %d Vorschläge",
            len(parsed.title_schemas),
            len(parsed.path_rules),
            len(parsed.mapping_matrix),
            len(parsed.tag_rules),
            len(parsed.suggestions),
        )

        return parsed

    # =========================================================================
    # Schritt 5: Ergebnisse speichern
    # =========================================================================

    async def _store_results(
        self,
        parsed: OpusAnalysisResponse,
        run_record: AnalysisRunRecord,
    ) -> None:
        """Speichert die geparsten Ergebnisse über SchemaStorage.

        Respektiert is_manual-Schutz: Manuelle Einträge werden nicht
        überschrieben (force=False ist der Default).
        """
        # --- Titel-Schemata (Ebene 1) ---
        for schema in parsed.title_schemas:
            pattern = TitlePattern(
                document_type=schema.document_type,
                correspondent=schema.correspondent,
                title_template=schema.title_template,
                rule_description=schema.rule_description,
                confidence=schema.confidence,
                document_count=schema.document_count,
                outlier_count=schema.outlier_count,
                outlier_titles=schema.outlier_titles,
                examples=schema.examples,
            )
            action, _ = await self._storage.upsert_title_pattern(pattern)

            if action == "created":
                run_record.title_schemas_created += 1
            elif action == "updated":
                run_record.title_schemas_updated += 1
            elif action == "preserved":
                run_record.manual_entries_preserved += 1
                run_record.title_schemas_unchanged += 1

        # --- Pfad-Regeln (Ebene 2) ---
        for rule_data in parsed.path_rules:
            rule = PathRule(
                topic=rule_data.topic,
                rule_description=rule_data.rule_description,
                path_template=rule_data.path_template,
                examples=rule_data.examples,
                topic_document_count=rule_data.topic_document_count,
                normalization_suggestions=rule_data.normalization_suggestions,
                confidence=rule_data.confidence,
            )
            action, _ = await self._storage.upsert_path_rule(rule)

            if action == "created":
                run_record.path_rules_created += 1
            elif action == "updated":
                run_record.path_rules_updated += 1
            elif action == "preserved":
                run_record.manual_entries_preserved += 1

        # --- Zuordnungsmatrix (Ebene 3) ---
        for mapping_data in parsed.mapping_matrix:
            # Mappings ohne Speicherpfad sind nicht speicherbar → überspringen
            if not mapping_data.storage_path_name:
                logger.debug(
                    "Mapping übersprungen (kein Speicherpfad): %s + %s",
                    mapping_data.correspondent, mapping_data.document_type,
                )
                continue
            mapping = MappingEntry(
                correspondent=mapping_data.correspondent,
                document_type=mapping_data.document_type,
                storage_path_name=mapping_data.storage_path_name,
                mapping_type=mapping_data.mapping_type,
                condition_description=mapping_data.condition_description,
                document_count=mapping_data.document_count,
                confidence=mapping_data.confidence,
            )
            action, _ = await self._storage.upsert_mapping(mapping)

            if action == "created":
                run_record.mappings_created += 1
            elif action == "updated":
                run_record.mappings_updated += 1
            elif action == "preserved":
                run_record.manual_entries_preserved += 1

        # --- Tag-Regeln (AP-11b) ---
        for tag_data in parsed.tag_rules:
            # Korrespondent normalisieren: None/leer → '' (DB-Sentinel)
            correspondent = (tag_data.correspondent or "").strip()

            # Regeln ohne Dokumenttyp sind nicht sinnvoll → überspringen
            if not tag_data.document_type:
                logger.debug(
                    "Tag-Regel übersprungen (kein Dokumenttyp): %s",
                    tag_data,
                )
                continue

            # Regeln ohne positive UND ohne negative Tags sind leer → überspringen
            if not tag_data.positive_tags and not tag_data.negative_tags:
                continue

            tag_rule = TagRule(
                document_type=tag_data.document_type,
                correspondent=correspondent,
                positive_tags=tag_data.positive_tags,
                negative_tags=tag_data.negative_tags,
                reasoning=tag_data.reasoning,
                confidence=tag_data.confidence,
            )
            action, _ = await self._storage.upsert_tag_rule(tag_rule)

            if action == "created":
                run_record.tag_rules_created += 1
            elif action == "updated":
                run_record.tag_rules_updated += 1
            elif action == "preserved":
                run_record.manual_entries_preserved += 1

        logger.info(
            "Ergebnisse gespeichert: "
            "Titel %d neu/%d aktualisiert, "
            "Pfade %d neu/%d aktualisiert, "
            "Mappings %d neu/%d aktualisiert, "
            "Tag-Regeln %d neu/%d aktualisiert, "
            "%d manuelle beibehalten",
            run_record.title_schemas_created,
            run_record.title_schemas_updated,
            run_record.path_rules_created,
            run_record.path_rules_updated,
            run_record.mappings_created,
            run_record.mappings_updated,
            run_record.tag_rules_created,
            run_record.tag_rules_updated,
            run_record.manual_entries_preserved,
        )
