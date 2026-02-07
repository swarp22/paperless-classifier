"""Review Queue – Dokumente mit mittlerer/niedriger Confidence prüfen.

Zeigt alle Dokumente mit ki_status='review' aus der SQLite-Datenbank,
angereichert mit aktuellen Werten aus Paperless. Drei Aktionen pro
Dokument:
- Übernehmen (✓): ki_status → 'classified'
- Korrigieren (✎): Formular → Werte ändern → PATCH → ki_status → 'manual'
- Ablehnen (✗): KI-Felder zurücksetzen → ki_status → 'manual'

Datenquellen:
- SQLite (processed_documents): Claude-Antwort, Confidence, Reasoning
- Paperless API: Aktuelle Feldwerte, Titel, Stammdaten für Dropdowns

Design-Referenz: Abschnitt 7.4 (Review Queue)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.paperless.exceptions import PaperlessNotFoundError
from app.ui.layout import page_layout

logger = get_logger("app")

# Custom Field IDs (ERRATA E-002)
CF_KI_STATUS = 8
CF_PERSON = 7


# ---------------------------------------------------------------------------
# Datenstrukturen
# ---------------------------------------------------------------------------

@dataclass
class ReviewItem:
    """Zusammengeführte Daten aus SQLite + Paperless für ein Review-Dokument."""

    # Aus SQLite (processed_documents)
    record_id: int                     # PK in processed_documents
    paperless_id: int
    confidence: str                    # "high", "medium", "low"
    reasoning: str
    model_used: str
    cost_usd: float
    classification_json: dict[str, Any] = field(default_factory=dict)

    # Aus Paperless (aktueller Zustand)
    title: str = ""
    current_correspondent: str = ""
    current_document_type: str = ""
    current_storage_path: str = ""
    current_tags: list[str] = field(default_factory=list)
    current_person: str = ""

    # KI-Vorschlag (aus classification_json extrahiert)
    suggested_correspondent: str = ""
    suggested_document_type: str = ""
    suggested_storage_path: str = ""
    suggested_tags: list[str] = field(default_factory=list)
    suggested_person: str = ""
    suggested_title: str = ""

    # Neuanlage-Vorschläge (aus classification_json.create_new)
    create_new_correspondents: list[str] = field(default_factory=list)
    create_new_document_types: list[str] = field(default_factory=list)
    create_new_tags: list[str] = field(default_factory=list)
    create_new_storage_paths: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Daten laden
# ---------------------------------------------------------------------------

async def _load_review_items() -> list[ReviewItem]:
    """Lädt Review-Dokumente aus SQLite und reichert sie mit Paperless-Daten an.

    Ablauf:
    1. Alle Dokumente mit status='review' aus SQLite holen
    2. Für jedes Dokument den aktuellen Zustand aus Paperless laden
    3. KI-Vorschläge aus classification_json extrahieren
    4. Beides in ReviewItem zusammenführen

    Returns:
        Liste von ReviewItems, bereit für die UI-Darstellung.
    """
    from app.state import get_database, get_paperless_client

    db = get_database()
    paperless = get_paperless_client()

    if db is None:
        logger.warning("Review Queue: Datenbank nicht verfügbar")
        return []

    try:
        review_rows = await db.get_review_documents()
    except Exception as exc:
        logger.error("Review Queue: DB-Abfrage fehlgeschlagen: %s", exc)
        return []

    if not review_rows:
        return []

    items: list[ReviewItem] = []

    for row in review_rows:
        paperless_id = row["paperless_id"]

        # classification_json parsen
        try:
            cl_json = json.loads(row.get("classification_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            cl_json = {}

        item = ReviewItem(
            record_id=row["id"],
            paperless_id=paperless_id,
            confidence=row.get("confidence", ""),
            reasoning=row.get("reasoning", "") or "",
            model_used=row.get("model_used", ""),
            cost_usd=row.get("cost_usd", 0.0),
            classification_json=cl_json,
            # KI-Vorschläge aus Claude-Antwort
            suggested_correspondent=cl_json.get("correspondent", ""),
            suggested_document_type=cl_json.get("document_type", ""),
            suggested_storage_path=cl_json.get("storage_path", ""),
            suggested_tags=[
                t for t in (cl_json.get("tags", []) or [])
                if t != "NEU"
            ],
            suggested_person=cl_json.get("person", ""),
            suggested_title=cl_json.get("title", ""),
        )

        # Neuanlage-Vorschläge gegen Cache filtern – bereits angelegte
        # Entitäten nicht erneut vorschlagen
        create_new = cl_json.get("create_new") or {}
        if paperless is not None and paperless.cache.is_loaded:
            cache = paperless.cache
            item.create_new_correspondents = [
                n for n in create_new.get("correspondents", [])
                if cache.get_correspondent_id(n) is None
            ]
            item.create_new_document_types = [
                n for n in create_new.get("document_types", [])
                if cache.get_document_type_id(n) is None
            ]
            item.create_new_tags = [
                n for n in create_new.get("tags", [])
                if n != "NEU" and cache.get_tag_id(n) is None
            ]
            item.create_new_storage_paths = [
                sp for sp in create_new.get("storage_paths", [])
                if sp.get("name") and cache.get_storage_path_id(sp["name"]) is None
            ]
        else:
            # Ohne Cache alle Vorschläge anzeigen (Fallback)
            item.create_new_correspondents = create_new.get("correspondents", [])
            item.create_new_document_types = create_new.get("document_types", [])
            item.create_new_tags = [
                n for n in create_new.get("tags", []) if n != "NEU"
            ]
            item.create_new_storage_paths = create_new.get("storage_paths", [])

        # Paperless-Daten anreichern (aktuelle Werte für Vergleich)
        if paperless is not None:
            try:
                doc = await paperless.get_document(paperless_id)
                cache = paperless.cache

                item.title = doc.title

                if doc.correspondent:
                    corr = cache.get_correspondent(doc.correspondent)
                    item.current_correspondent = corr.name if corr else ""
                if doc.document_type:
                    dt = cache.get_document_type(doc.document_type)
                    item.current_document_type = dt.name if dt else ""
                if doc.storage_path:
                    sp = cache.get_storage_path(doc.storage_path)
                    item.current_storage_path = sp.name if sp else ""

                item.current_tags = [
                    cache.get_tag(tid).name
                    for tid in doc.tags
                    if cache.get_tag(tid) is not None
                    and cache.get_tag(tid).name != "NEU"
                ]

                # Person aus Custom Field (Select → Label auflösen)
                person_value = doc.get_custom_field_value(CF_PERSON)
                if person_value:
                    label = cache.get_select_option_label(CF_PERSON, person_value)
                    item.current_person = label or ""

            except PaperlessNotFoundError:
                # E-024: Dokument in Paperless gelöscht → aus Review Queue
                # entfernen.  Ohne diese Bereinigung warnt das Log bei
                # jedem Seitenaufruf und das verwaiste Item blockiert die
                # Anzeige.
                logger.info(
                    "Review Queue: Dokument %d wurde aus Paperless gelöscht "
                    "→ Review-Eintrag %d wird als 'manual' geschlossen",
                    paperless_id, row["id"],
                )
                if db is not None:
                    try:
                        await db.update_review_status(
                            row["id"], "manual", reviewed_by="auto_cleanup",
                        )
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Konnte verwaisten Review-Eintrag %d nicht "
                            "bereinigen: %s", row["id"], cleanup_exc,
                        )
                continue  # Item nicht in die Liste aufnehmen

            except Exception as exc:
                logger.warning(
                    "Review Queue: Paperless-Daten für Dokument %d "
                    "konnten nicht geladen werden: %s",
                    paperless_id, exc,
                )
                item.title = f"Dokument #{paperless_id} (nicht erreichbar)"

        items.append(item)

    return items


def _get_stammdaten_options() -> dict[str, list[str]]:
    """Holt die Stammdaten-Listen aus dem Cache für Korrektur-Dropdowns.

    Returns:
        Dict mit Keys 'correspondents', 'document_types', 'tags',
        'storage_paths', 'persons'.
    """
    from app.state import get_paperless_client

    paperless = get_paperless_client()
    if paperless is None or not paperless.cache.is_loaded:
        return {
            "correspondents": [],
            "document_types": [],
            "tags": [],
            "storage_paths": [],
            "persons": [],
        }

    cache = paperless.cache
    return {
        "correspondents": sorted(cache.get_all_correspondent_names()),
        "document_types": sorted(cache.get_all_document_type_names()),
        "tags": sorted(
            name for name in cache.get_all_tag_names() if name != "NEU"
        ),
        "storage_paths": sorted(cache.get_all_storage_path_names()),
        "persons": cache.get_select_option_labels(CF_PERSON),
    }


# ---------------------------------------------------------------------------
# Aktionen
# ---------------------------------------------------------------------------

async def _action_accept(item: ReviewItem) -> str | None:
    """Übernehmen: ki_status → 'classified' in Paperless + SQLite.

    Returns:
        Fehlermeldung oder None bei Erfolg.
    """
    from app.state import get_database, get_paperless_client

    db = get_database()
    paperless = get_paperless_client()

    if db is None or paperless is None:
        return "Datenbank oder Paperless nicht verfügbar"

    try:
        # ki_status in Paperless auf 'classified' setzen
        await paperless.set_custom_field_by_label(
            item.paperless_id, CF_KI_STATUS, "classified",
        )
        # SQLite: Status aktualisieren
        await db.update_review_status(
            item.record_id, "classified", reviewed_by="user",
        )
        logger.info(
            "Review: Dokument %d übernommen (record_id=%d)",
            item.paperless_id, item.record_id,
        )
        return None
    except Exception as exc:
        logger.error(
            "Review: Fehler beim Übernehmen von Dokument %d: %s",
            item.paperless_id, exc,
        )
        return str(exc)


async def _action_reject(item: ReviewItem) -> str | None:
    """Ablehnen: KI-Felder zurücksetzen, ki_status → 'manual'.

    Bei MEDIUM-Confidence wurden die Felder vorläufig angewandt –
    hier setzen wir Korrespondent, Dokumenttyp, Speicherpfad und
    Person zurück auf None/leer.

    Bei LOW-Confidence wurden keine Felder gesetzt, also nur Status ändern.

    Returns:
        Fehlermeldung oder None bei Erfolg.
    """
    from app.state import get_database, get_paperless_client

    db = get_database()
    paperless = get_paperless_client()

    if db is None or paperless is None:
        return "Datenbank oder Paperless nicht verfügbar"

    try:
        doc = await paperless.get_document(item.paperless_id)
        cache = paperless.cache

        ki_status_option_id = cache.require_select_option_id(
            CF_KI_STATUS, "manual",
        )

        patch: dict[str, Any] = {}

        if item.confidence == "medium":
            # Bei MEDIUM wurden Felder vorläufig gesetzt → zurücksetzen
            patch["correspondent"] = None
            patch["document_type"] = None
            patch["storage_path"] = None

            # Person-Feld entfernen, ki_status aktualisieren
            remaining_cfs = [
                {"field": cf.field, "value": cf.value}
                for cf in doc.custom_fields
                if cf.field not in (CF_KI_STATUS, CF_PERSON)
            ]
            remaining_cfs.append(
                {"field": CF_KI_STATUS, "value": ki_status_option_id}
            )
            patch["custom_fields"] = remaining_cfs
        else:
            # Bei LOW wurden keine Felder gesetzt → nur ki_status ändern
            await paperless.set_custom_field_by_label(
                item.paperless_id, CF_KI_STATUS, "manual",
            )

        # Nur patchen wenn es etwas zu patchen gibt (MEDIUM-Fall)
        if patch:
            await paperless.update_document(item.paperless_id, **patch)

        # SQLite: Status aktualisieren
        await db.update_review_status(
            item.record_id, "manual", reviewed_by="user",
        )
        logger.info(
            "Review: Dokument %d abgelehnt, Felder zurückgesetzt (record_id=%d)",
            item.paperless_id, item.record_id,
        )
        return None
    except Exception as exc:
        logger.error(
            "Review: Fehler beim Ablehnen von Dokument %d: %s",
            item.paperless_id, exc,
        )
        return str(exc)


async def _action_correct(
    item: ReviewItem,
    corrections: dict[str, Any],
) -> str | None:
    """Korrigieren: Korrigierte Werte per PATCH an Paperless, ki_status → 'manual'.

    Args:
        item: Das Review-Dokument.
        corrections: Dict mit korrigierten Werten:
            correspondent, document_type, storage_path, tags, person

    Returns:
        Fehlermeldung oder None bei Erfolg.
    """
    from app.state import get_database, get_paperless_client

    db = get_database()
    paperless = get_paperless_client()

    if db is None or paperless is None:
        return "Datenbank oder Paperless nicht verfügbar"

    try:
        cache = paperless.cache
        doc = await paperless.get_document(item.paperless_id)
        patch: dict[str, Any] = {}

        # Korrespondent (Name → ID)
        corr_name = corrections.get("correspondent", "")
        if corr_name:
            corr_id = cache.get_correspondent_id(corr_name)
            if corr_id is not None:
                patch["correspondent"] = corr_id
        else:
            patch["correspondent"] = None

        # Dokumenttyp (Name → ID)
        dt_name = corrections.get("document_type", "")
        if dt_name:
            dt_id = cache.get_document_type_id(dt_name)
            if dt_id is not None:
                patch["document_type"] = dt_id
        else:
            patch["document_type"] = None

        # Speicherpfad (Name → ID)
        sp_name = corrections.get("storage_path", "")
        if sp_name:
            sp_id = cache.get_storage_path_id(sp_name)
            if sp_id is not None:
                patch["storage_path"] = sp_id
        else:
            patch["storage_path"] = None

        # Tags (Namen → IDs) – NEU niemals zurückschreiben
        tag_names = [
            name for name in corrections.get("tags", [])
            if name != "NEU"
        ]
        tag_ids = []
        for name in tag_names:
            tid = cache.get_tag_id(name)
            if tid is not None:
                tag_ids.append(tid)
        patch["tags"] = sorted(tag_ids)

        # Custom Fields: ki_status + Person
        ki_status_option_id = cache.require_select_option_id(
            CF_KI_STATUS, "manual",
        )
        cf_list = [
            {"field": cf.field, "value": cf.value}
            for cf in doc.custom_fields
            if cf.field not in (CF_KI_STATUS, CF_PERSON)
        ]
        cf_list.append({"field": CF_KI_STATUS, "value": ki_status_option_id})

        person_name = corrections.get("person", "")
        if person_name:
            person_option_id = cache.get_select_option_id(CF_PERSON, person_name)
            if person_option_id:
                cf_list.append({"field": CF_PERSON, "value": person_option_id})

        patch["custom_fields"] = cf_list

        # Einzelner PATCH (E-009: kein Multi-PATCH)
        await paperless.update_document(item.paperless_id, **patch)

        # SQLite aktualisieren
        await db.update_review_status(
            item.record_id, "manual", reviewed_by="user",
        )
        logger.info(
            "Review: Dokument %d korrigiert (record_id=%d), Korrekturen: %s",
            item.paperless_id, item.record_id,
            {k: v for k, v in corrections.items() if v},
        )
        return None
    except Exception as exc:
        logger.error(
            "Review: Fehler beim Korrigieren von Dokument %d: %s",
            item.paperless_id, exc,
        )
        return str(exc)


async def _action_create_entity(
    item: ReviewItem,
    entity_type: str,
    name: str,
    path_template: str = "",
) -> str | None:
    """Legt eine neue Entität in Paperless an und weist sie dem Dokument zu.

    Ablauf:
    1. Entität per POST in Paperless anlegen
    2. Dokument per PATCH die neue ID zuweisen
    3. Pipeline-Prompt-Cache invalidieren

    Args:
        item: Das Review-Dokument.
        entity_type: 'correspondent', 'document_type', 'tag', 'storage_path'
        name: Name der neuen Entität.
        path_template: Nur bei storage_path – das Pfad-Template.

    Returns:
        Fehlermeldung oder None bei Erfolg.
    """
    from app.state import get_paperless_client, get_pipeline

    paperless = get_paperless_client()
    if paperless is None:
        return "Paperless nicht verfügbar"

    try:
        patch: dict[str, Any] = {}

        if entity_type == "correspondent":
            created = await paperless.create_correspondent(name)
            patch["correspondent"] = created.id

        elif entity_type == "document_type":
            created = await paperless.create_document_type(name)
            patch["document_type"] = created.id

        elif entity_type == "tag":
            created = await paperless.create_tag(name)
            # Tags werden zu den bestehenden addiert, nicht ersetzt
            doc = await paperless.get_document(item.paperless_id)
            tag_ids = list(doc.tags) + [created.id]
            patch["tags"] = sorted(set(tag_ids))

        elif entity_type == "storage_path":
            # Template aus dem Namen ableiten – Claude kennt das Schema nicht.
            # Schema: Name "Topic / Objekt / Entität"
            # → Pfad "/Topic/Objekt/Entität/{{created_year}}/{{title}}_{{created}}"
            derived_template = (
                "/" + name.replace(" / ", "/")
                + "/{{created_year}}/{{title}}_{{created}}"
            )
            created = await paperless.create_storage_path(name, derived_template)
            patch["storage_path"] = created.id

        else:
            return f"Unbekannter Entitätstyp: {entity_type}"

        # Dokument zuweisen
        await paperless.update_document(item.paperless_id, **patch)

        # Prompt-Cache invalidieren → beim nächsten Aufruf werden neue
        # Stammdaten in den Prompt aufgenommen
        pipeline = get_pipeline()
        if pipeline is not None:
            pipeline.invalidate_prompt_cache()

        logger.info(
            "Review: %s '%s' angelegt und Dokument %d zugewiesen (ID %d)",
            entity_type, name, item.paperless_id, created.id,
        )
        return None

    except Exception as exc:
        logger.error(
            "Review: Fehler beim Anlegen von %s '%s': %s",
            entity_type, name, exc,
        )
        return str(exc)


# ---------------------------------------------------------------------------
# UI-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _confidence_chip(confidence: str) -> None:
    """Rendert einen farbigen Chip für das Confidence-Level."""
    colors = {
        "high": "bg-green-100 text-green-800",
        "medium": "bg-yellow-100 text-yellow-800",
        "low": "bg-red-100 text-red-800",
    }
    labels = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
    css = colors.get(confidence, "bg-gray-100 text-gray-800")
    label = labels.get(confidence, confidence.upper())
    ui.label(label).classes(f"{css} px-2 py-0.5 rounded-full text-xs font-semibold")


def _model_short_name(model_raw: str) -> str:
    """Kürzt den Modellnamen für die Anzeige."""
    if "sonnet" in model_raw:
        return "Sonnet 4.5"
    if "haiku" in model_raw:
        return "Haiku 4.5"
    if "opus-4-6" in model_raw:
        return "Opus 4.6"
    if "opus" in model_raw:
        return "Opus 4.5"
    return model_raw[:20]


def _field_comparison_row(
    label: str,
    suggested: str,
    current: str,
    is_medium: bool,
) -> None:
    """Rendert eine Zeile mit KI-Vorschlag und ggf. aktuellem Wert.

    Bei MEDIUM-Confidence werden die aktuellen (vorläufig gesetzten) Werte
    zum Vergleich gezeigt. Bei LOW nur der Vorschlag.
    """
    with ui.row().classes("items-start gap-2 w-full"):
        ui.label(f"{label}:").classes("text-gray-500 text-sm w-32 flex-shrink-0")
        ui.label(suggested or "–").classes("text-sm font-medium")

        # Bei MEDIUM: Wenn der aktuelle Wert abweicht, zeigen
        if is_medium and current and current != suggested:
            ui.label(f"(aktuell: {current})").classes(
                "text-xs text-gray-400 italic"
            )


# ---------------------------------------------------------------------------
# Review-Karte (eine pro Dokument)
# ---------------------------------------------------------------------------

def _render_review_card(
    item: ReviewItem,
    stammdaten: dict[str, list[str]],
    queue_container: ui.element,
) -> None:
    """Rendert eine Review-Karte mit Vorschlag und Aktions-Buttons.

    Die Karte enthält:
    - Header mit Titel + Confidence-Badge
    - KI-Vorschlag (Korrespondent, Typ, Pfad, Tags, Person)
    - Begründung
    - Aktions-Buttons
    - Expandierbares Korrektur-Formular

    Args:
        item: Zusammengeführte Daten für dieses Dokument.
        stammdaten: Dropdown-Optionen für das Korrektur-Formular.
        queue_container: Parent-Container für Refresh nach Aktion.
    """
    from app.config import get_settings

    is_medium = item.confidence == "medium"
    settings = get_settings()
    paperless_url = settings.paperless_url.rstrip("/")

    card = ui.card().classes("w-full")
    with card:
        # --- Header: Titel + Confidence ---
        with ui.row().classes("items-center gap-3 w-full"):
            with ui.link(
                target=f"{paperless_url}/documents/{item.paperless_id}/details",
                new_tab=True,
            ).classes("no-underline"):
                ui.label(item.title or f"Dokument #{item.paperless_id}").classes(
                    "text-base font-semibold text-blue-700 hover:underline"
                )

            _confidence_chip(item.confidence)

            # Modell + Kosten (rechts)
            with ui.row().classes("ml-auto items-center gap-2"):
                ui.label(_model_short_name(item.model_used)).classes(
                    "text-xs text-gray-400"
                )
                ui.label(f"${item.cost_usd:.4f}").classes(
                    "text-xs text-gray-400"
                )

        ui.separator().classes("my-2")

        # --- KI-Vorschlag ---
        ui.label("KI-Vorschlag").classes("text-xs text-gray-400 uppercase tracking-wide")

        with ui.column().classes("gap-1 mt-1"):
            _field_comparison_row(
                "Korrespondent",
                item.suggested_correspondent,
                item.current_correspondent,
                is_medium,
            )
            _field_comparison_row(
                "Dokumenttyp",
                item.suggested_document_type,
                item.current_document_type,
                is_medium,
            )
            _field_comparison_row(
                "Speicherpfad",
                item.suggested_storage_path,
                item.current_storage_path,
                is_medium,
            )
            # Tags
            with ui.row().classes("items-start gap-2 w-full"):
                ui.label("Tags:").classes("text-gray-500 text-sm w-32 flex-shrink-0")
                if item.suggested_tags:
                    with ui.row().classes("gap-1 flex-wrap"):
                        for tag in item.suggested_tags:
                            ui.badge(tag, color="blue").props("outline")
                else:
                    ui.label("–").classes("text-sm")

            _field_comparison_row(
                "Person",
                item.suggested_person,
                item.current_person,
                is_medium,
            )
            _field_comparison_row(
                "Titel",
                item.suggested_title,
                item.title,
                is_medium,
            )

        # --- Begründung ---
        if item.reasoning:
            with ui.column().classes("mt-2"):
                ui.label("Begründung").classes(
                    "text-xs text-gray-400 uppercase tracking-wide"
                )
                ui.label(item.reasoning).classes(
                    "text-sm text-gray-600 italic bg-gray-50 rounded p-2 mt-1"
                )

        # --- Neuanlage-Vorschläge ---
        _render_create_new_section(item, queue_container)

        ui.separator().classes("my-2")

        # --- Aktions-Buttons + Korrektur-Expansion ---
        _render_actions(item, stammdaten, card, queue_container)


def _render_create_new_section(
    item: ReviewItem,
    queue_container: ui.element,
) -> None:
    """Rendert Neuanlage-Vorschläge wenn Claude fehlende Entitäten erkannt hat.

    Zeigt pro Vorschlag eine Zeile mit Entitätstyp, Name und
    'Anlegen & Zuordnen'-Button.  Bei Speicherpfaden wird zusätzlich
    das Pfad-Template angezeigt.

    Nach erfolgreichem Anlegen wird das Dokument sofort zugewiesen,
    der Prompt-Cache invalidiert und die Queue neu geladen.
    """
    has_suggestions = (
        item.create_new_correspondents
        or item.create_new_document_types
        or item.create_new_tags
        or item.create_new_storage_paths
    )
    if not has_suggestions:
        return

    with ui.column().classes("mt-2"):
        ui.label("Neuanlage-Vorschläge").classes(
            "text-xs text-gray-400 uppercase tracking-wide"
        )
        ui.label(
            "Claude schlägt folgende neue Einträge vor, "
            "die in Paperless noch nicht existieren:"
        ).classes("text-xs text-gray-500 mt-1")

        with ui.column().classes("gap-2 mt-2"):
            # Korrespondenten
            for name in item.create_new_correspondents:
                _create_new_row(
                    item, queue_container,
                    icon="person_add",
                    entity_type="correspondent",
                    label="Korrespondent",
                    name=name,
                )

            # Dokumenttypen
            for name in item.create_new_document_types:
                _create_new_row(
                    item, queue_container,
                    icon="note_add",
                    entity_type="document_type",
                    label="Dokumenttyp",
                    name=name,
                )

            # Tags
            for name in item.create_new_tags:
                _create_new_row(
                    item, queue_container,
                    icon="label",
                    entity_type="tag",
                    label="Tag",
                    name=name,
                )

            # Speicherpfade (mit abgeleitetem Template)
            for sp in item.create_new_storage_paths:
                sp_name = sp.get("name", "")
                if sp_name:
                    # Template aus dem Namen ableiten (Claude kennt Schema nicht)
                    derived_template = (
                        "/" + sp_name.replace(" / ", "/")
                        + "/{{created_year}}/{{title}}_{{created}}"
                    )
                    _create_new_row(
                        item, queue_container,
                        icon="create_new_folder",
                        entity_type="storage_path",
                        label="Speicherpfad",
                        name=sp_name,
                        path_template=derived_template,
                    )


def _create_new_row(
    item: ReviewItem,
    queue_container: ui.element,
    *,
    icon: str,
    entity_type: str,
    label: str,
    name: str,
    path_template: str = "",
) -> None:
    """Rendert eine einzelne Neuanlage-Zeile mit Button.

    Args:
        item: Das Review-Dokument.
        queue_container: Für Queue-Refresh nach Aktion.
        icon: Material Icon Name.
        entity_type: API-Schlüssel ('correspondent', 'document_type', etc.)
        label: Anzeige-Label (z.B. 'Korrespondent').
        name: Name der neuen Entität.
        path_template: Nur bei storage_path.
    """
    with ui.card().classes(
        "w-full bg-amber-50 border border-amber-200"
    ).props("flat bordered"):
        with ui.row().classes("items-center gap-3 w-full py-1 px-2"):
            ui.icon(icon).classes("text-amber-700")
            with ui.column().classes("gap-0 flex-grow"):
                ui.label(f"{label}: {name}").classes(
                    "text-sm font-medium"
                )
                if path_template:
                    ui.label(f"Template: {path_template}").classes(
                        "text-xs text-gray-500 font-mono"
                    )

            async def _on_create(
                _item: ReviewItem = item,
                _type: str = entity_type,
                _name: str = name,
                _template: str = path_template,
            ) -> None:
                error = await _action_create_entity(
                    _item, _type, _name, _template,
                )
                if error:
                    ui.notify(f"Fehler: {error}", type="negative")
                else:
                    ui.notify(
                        f"{label} '{_name}' angelegt und zugewiesen",
                        type="positive",
                    )
                    await _refresh_queue(queue_container)

            ui.button(
                "Anlegen & Zuordnen",
                icon="add_circle",
                color="amber",
                on_click=_on_create,
            ).props("dense outline").classes("ml-auto")


def _render_actions(
    item: ReviewItem,
    stammdaten: dict[str, list[str]],
    card: ui.element,
    queue_container: ui.element,
) -> None:
    """Rendert die drei Aktions-Buttons und das Korrektur-Formular.

    Das Korrektur-Formular ist in einem Expansion-Panel versteckt
    und wird erst bei Klick auf 'Korrigieren' sichtbar.
    """

    # --- Expansion-Panel für Korrektur (initial geschlossen) ---
    expansion = ui.expansion(
        "Korrektur-Formular", icon="edit",
    ).classes("w-full mt-2")
    expansion.props("dense")
    expansion.set_visibility(False)

    # Formular-State als Dict (Referenz für Closure)
    # NiceGUI ui.select akzeptiert None als "nichts ausgewählt",
    # aber leere Strings ("") verursachen ValueError weil "" nicht
    # in der Options-Liste steht.  Daher: "" → None konvertieren.
    form_state: dict[str, Any] = {
        "correspondent": (item.suggested_correspondent or item.current_correspondent) or None,
        "document_type": (item.suggested_document_type or item.current_document_type) or None,
        "storage_path": (item.suggested_storage_path or item.current_storage_path) or None,
        "tags": list(item.suggested_tags or item.current_tags),
        "person": (item.suggested_person or item.current_person) or None,
    }

    with expansion:
        with ui.column().classes("gap-3 w-full py-2"):
            # Korrespondent
            ui.select(
                label="Korrespondent",
                options=stammdaten["correspondents"],
                value=form_state["correspondent"],
                with_input=True,
                clearable=True,
            ).classes("w-full").on_value_change(
                lambda e: form_state.__setitem__("correspondent", e.value)
            )

            # Dokumenttyp
            ui.select(
                label="Dokumenttyp",
                options=stammdaten["document_types"],
                value=form_state["document_type"],
                with_input=True,
                clearable=True,
            ).classes("w-full").on_value_change(
                lambda e: form_state.__setitem__("document_type", e.value)
            )

            # Speicherpfad
            ui.select(
                label="Speicherpfad",
                options=stammdaten["storage_paths"],
                value=form_state["storage_path"],
                with_input=True,
                clearable=True,
            ).classes("w-full").on_value_change(
                lambda e: form_state.__setitem__("storage_path", e.value)
            )

            # Tags (Multi-Select)
            ui.select(
                label="Tags",
                options=stammdaten["tags"],
                value=form_state["tags"],
                multiple=True,
                with_input=True,
                clearable=True,
            ).classes("w-full").on_value_change(
                lambda e: form_state.__setitem__("tags", e.value or [])
            )

            # Person
            ui.select(
                label="Person",
                options=stammdaten["persons"],
                value=form_state["person"],
                with_input=True,
                clearable=True,
            ).classes("w-full").on_value_change(
                lambda e: form_state.__setitem__("person", e.value)
            )

            # Speichern / Abbrechen
            with ui.row().classes("gap-2 mt-2"):
                async def _save_correction(
                    _item: ReviewItem = item,
                    _fs: dict = form_state,
                ) -> None:
                    error = await _action_correct(_item, _fs)
                    if error:
                        ui.notify(f"Fehler: {error}", type="negative")
                    else:
                        ui.notify(
                            f"Dokument #{_item.paperless_id} korrigiert",
                            type="positive",
                        )
                        await _refresh_queue(queue_container)

                ui.button(
                    "Speichern", icon="save", color="primary",
                    on_click=_save_correction,
                ).props("dense")

                def _close_expansion() -> None:
                    expansion.set_visibility(False)

                ui.button(
                    "Abbrechen", icon="close", color="grey",
                    on_click=_close_expansion,
                ).props("dense flat")

    # --- Haupt-Buttons ---
    with ui.row().classes("gap-2"):
        async def _on_accept(_item: ReviewItem = item) -> None:
            error = await _action_accept(_item)
            if error:
                ui.notify(f"Fehler: {error}", type="negative")
            else:
                ui.notify(
                    f"Dokument #{_item.paperless_id} übernommen",
                    type="positive",
                )
                await _refresh_queue(queue_container)

        ui.button(
            "Übernehmen", icon="check", color="green",
            on_click=_on_accept,
        ).props("dense")

        def _show_correction() -> None:
            expansion.set_visibility(True)
            expansion.props("model-value=true")

        ui.button(
            "Korrigieren", icon="edit", color="orange",
            on_click=_show_correction,
        ).props("dense")

        async def _on_reject(_item: ReviewItem = item) -> None:
            error = await _action_reject(_item)
            if error:
                ui.notify(f"Fehler: {error}", type="negative")
            else:
                ui.notify(
                    f"Dokument #{_item.paperless_id} abgelehnt, Felder zurückgesetzt",
                    type="warning",
                )
                await _refresh_queue(queue_container)

        ui.button(
            "Ablehnen", icon="close", color="red",
            on_click=_on_reject,
        ).props("dense outline")


# ---------------------------------------------------------------------------
# Queue-Container mit Refresh
# ---------------------------------------------------------------------------

async def _refresh_queue(container: ui.element) -> None:
    """Lädt die Review Queue neu und rendert sie in den Container."""
    container.clear()
    with container:
        await _render_queue_content(container)


async def _render_queue_content(queue_container: ui.element) -> None:
    """Rendert den Inhalt der Review Queue (Karten oder Leer-Hinweis).

    Args:
        queue_container: Das Element, in das gerendert wird.
            Wird an die Karten weitergegeben, damit Aktionen
            die Queue per Refresh neu laden können.
    """
    items = await _load_review_items()
    stammdaten = _get_stammdaten_options()

    # Header mit Zähler
    with ui.row().classes("items-center gap-3 w-full"):
        ui.icon("rate_review").classes("text-2xl text-blue-700")
        ui.label(f"Review Queue ({len(items)} Dokumente)").classes(
            "text-xl font-semibold"
        )

        async def _manual_refresh(
            _container: ui.element = queue_container,
        ) -> None:
            await _refresh_queue(_container)

        ui.button(
            icon="refresh", on_click=_manual_refresh,
        ).props("flat dense round").tooltip("Queue neu laden")

    if not items:
        with ui.card().classes("w-full"):
            with ui.column().classes("items-center py-8 gap-2"):
                ui.icon("check_circle").classes("text-5xl text-green-400")
                ui.label("Keine Dokumente zur Überprüfung").classes(
                    "text-gray-500 text-lg"
                )
                ui.label(
                    "Alle Dokumente wurden entweder automatisch klassifiziert "
                    "oder bereits überprüft."
                ).classes("text-gray-400 text-sm text-center max-w-md")
        return

    for item in items:
        _render_review_card(item, stammdaten, queue_container)


# ---------------------------------------------------------------------------
# Seiten-Definition
# ---------------------------------------------------------------------------

def register(app: Any = None) -> None:
    """Registriert die Review-Queue-Seite."""

    @ui.page("/review")
    async def review_page() -> None:
        with page_layout("Review Queue"):
            queue_container = ui.column().classes("w-full gap-4")
            with queue_container:
                await _render_queue_content(queue_container)
