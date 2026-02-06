"""Resolver: Wandelt Claude-Antwort-Namen in Paperless-ngx IDs um.

Zuständig für:
- Exaktes Matching (case-insensitive) über den LookupCache
- Fuzzy-Matching via difflib für leichte Abweichungen
- Select-Option-Auflösung für Custom Fields (ERRATA E-001)
- Tracking nicht aufgelöster Namen (für Neuanlage oder Review)
- Steuer-Tag-Ableitung aus tax_relevant + tax_year

Keine externen Dependencies außer der Standardbibliothek (difflib).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.claude.client import ClassificationResult, ConfidenceLevel
from app.logging_config import get_logger
from app.paperless.cache import LookupCache

logger = get_logger("classifier")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Minimale Ähnlichkeit für Fuzzy-Match (0.0–1.0)
# 0.85 fängt typische Abweichungen ab: "Dr Hansen" vs "Dr. Hansen",
# "Goldstadt-Privatklinik" vs "Goldstadt Privatklinik"
FUZZY_THRESHOLD = 0.85

# Custom Field IDs (ERRATA E-002)
CF_PERSON = 7
CF_KI_STATUS = 8
CF_PAGINIERUNG = 2
CF_HAUS_REGISTER = 5
CF_HAUS_ORDNUNGSZAHL = 4

# Tag "NEU" (Inbox-Tag, wird nach Verarbeitung entfernt)
TAG_NEU_ID = 12


# ---------------------------------------------------------------------------
# Ergebnis-Datenstrukturen
# ---------------------------------------------------------------------------

@dataclass
class FieldResolution:
    """Ergebnis der Auflösung eines einzelnen Feldes."""

    original_name: str           # Name aus Claude-Antwort
    resolved_id: int | None      # Aufgelöste Paperless-ID (None = nicht gefunden)
    match_type: str              # "exact", "fuzzy", "not_found", "create_new"
    fuzzy_score: float = 0.0     # Ähnlichkeits-Score bei Fuzzy-Match
    fuzzy_matched_name: str = "" # Tatsächlich gematchter Name bei Fuzzy


@dataclass
class CustomFieldResolution:
    """Ergebnis der Auflösung eines Custom-Field-Werts."""

    field_id: int
    value: int | str | None      # Aufgelöster Wert (Option-ID für Select, int/str sonst)
    original_label: str          # Original-Label aus Claude-Antwort
    resolved: bool = True        # True wenn erfolgreich aufgelöst


@dataclass
class ResolvedClassification:
    """Vollständig aufgelöstes Klassifizierungsergebnis mit Paperless-IDs.

    Enthält sowohl die aufgelösten IDs als auch Tracking-Info darüber,
    welche Felder nicht gefunden wurden (für Confidence-Bewertung und
    Neuanlage-Handling).
    """

    # Aufgelöste Kern-IDs (None = nicht aufgelöst)
    correspondent_id: int | None = None
    document_type_id: int | None = None
    storage_path_id: int | None = None
    tag_ids: list[int] = field(default_factory=list)

    # Direkt übernommene Felder (kein Mapping nötig)
    title: str = ""
    date: str | None = None

    # Custom Fields (aufgelöst)
    custom_fields: list[CustomFieldResolution] = field(default_factory=list)

    # Detaillierte Auflösungsergebnisse (für Confidence-Bewertung)
    correspondent_resolution: FieldResolution | None = None
    document_type_resolution: FieldResolution | None = None
    storage_path_resolution: FieldResolution | None = None
    tag_resolutions: list[FieldResolution] = field(default_factory=list)

    # Nicht aufgelöste Namen (für Neuanlage-Prüfung)
    unresolved_names: list[str] = field(default_factory=list)

    # Neuanlage-Vorschläge (direkt von Claude)
    create_new_correspondents: list[str] = field(default_factory=list)
    create_new_tags: list[str] = field(default_factory=list)
    create_new_document_types: list[str] = field(default_factory=list)
    create_new_storage_paths: list[dict[str, str]] = field(default_factory=list)

    # Original-Ergebnis für Referenz
    raw_result: ClassificationResult | None = None

    @property
    def total_fields(self) -> int:
        """Gesamtzahl der Felder die aufgelöst werden mussten."""
        count = 0
        if self.correspondent_resolution:
            count += 1
        if self.document_type_resolution:
            count += 1
        if self.storage_path_resolution:
            count += 1
        count += len(self.tag_resolutions)
        return count

    @property
    def resolved_fields(self) -> int:
        """Anzahl erfolgreich aufgelöster Felder."""
        count = 0
        if self.correspondent_resolution and self.correspondent_resolution.resolved_id is not None:
            count += 1
        if self.document_type_resolution and self.document_type_resolution.resolved_id is not None:
            count += 1
        if self.storage_path_resolution and self.storage_path_resolution.resolved_id is not None:
            count += 1
        count += sum(
            1 for r in self.tag_resolutions if r.resolved_id is not None
        )
        return count

    @property
    def resolution_ratio(self) -> float:
        """Anteil erfolgreich aufgelöster Felder (0.0–1.0)."""
        if self.total_fields == 0:
            return 1.0  # Keine Felder zu mappen = alles OK
        return self.resolved_fields / self.total_fields

    @property
    def has_fuzzy_matches(self) -> bool:
        """True wenn mindestens ein Feld per Fuzzy-Matching aufgelöst wurde."""
        resolutions = [
            r for r in [
                self.correspondent_resolution,
                self.document_type_resolution,
                self.storage_path_resolution,
            ]
            if r is not None
        ] + self.tag_resolutions
        return any(r.match_type == "fuzzy" for r in resolutions)


# ---------------------------------------------------------------------------
# Fuzzy-Matching Hilfsfunktionen
# ---------------------------------------------------------------------------

def _fuzzy_match(
    name: str,
    candidates: dict[str, int],
    threshold: float = FUZZY_THRESHOLD,
) -> FieldResolution:
    """Sucht den besten Fuzzy-Match für einen Namen in einer ID-Map.

    Verwendet SequenceMatcher aus der Standardbibliothek – keine
    externen Dependencies nötig.  Vergleicht case-insensitive.

    Args:
        name: Gesuchter Name aus Claude-Antwort.
        candidates: Dict von {lowercase_name: id} aus dem Cache.
        threshold: Minimale Ähnlichkeit für einen Match.

    Returns:
        FieldResolution mit dem besten Treffer oder "not_found".
    """
    name_lower = name.lower()

    # Erst exakten Match versuchen
    if name_lower in candidates:
        return FieldResolution(
            original_name=name,
            resolved_id=candidates[name_lower],
            match_type="exact",
            fuzzy_score=1.0,
        )

    # Fuzzy-Suche über alle Kandidaten
    best_score = 0.0
    best_name = ""
    best_id: int | None = None

    for candidate_name, candidate_id in candidates.items():
        score = SequenceMatcher(None, name_lower, candidate_name).ratio()
        if score > best_score:
            best_score = score
            best_name = candidate_name
            best_id = candidate_id

    if best_score >= threshold and best_id is not None:
        logger.info(
            "Fuzzy-Match: '%s' → '%s' (Score: %.2f)",
            name, best_name, best_score,
        )
        return FieldResolution(
            original_name=name,
            resolved_id=best_id,
            match_type="fuzzy",
            fuzzy_score=best_score,
            fuzzy_matched_name=best_name,
        )

    # Kein Match gefunden
    logger.warning(
        "Nicht aufgelöst: '%s' (bester Kandidat: '%s' mit Score %.2f < %.2f)",
        name, best_name, best_score, threshold,
    )
    return FieldResolution(
        original_name=name,
        resolved_id=None,
        match_type="not_found",
        fuzzy_score=best_score,
        fuzzy_matched_name=best_name,
    )


# ---------------------------------------------------------------------------
# Haupt-Resolver
# ---------------------------------------------------------------------------

def resolve_classification(
    result: ClassificationResult,
    cache: LookupCache,
) -> ResolvedClassification:
    """Löst alle Namen einer ClassificationResult in Paperless-IDs auf.

    Geht Feld für Feld durch das Claude-Ergebnis und versucht:
    1. Exaktes Matching (case-insensitive) über den Cache
    2. Fuzzy-Matching bei Nicht-Treffer
    3. Tracking als "nicht aufgelöst" für Neuanlage-Handling

    Zusätzlich werden Custom Fields (Person, ki_status, Paginierung,
    Haus-Register) aufgelöst.

    Args:
        result: Rohes Klassifizierungsergebnis von Claude.
        cache: Befüllter LookupCache mit allen Paperless-Stammdaten.

    Returns:
        ResolvedClassification mit IDs und Auflösungs-Details.
    """
    resolved = ResolvedClassification(
        title=result.title,
        date=result.date,
        raw_result=result,
    )

    # --- Korrespondent ---
    if result.correspondent:
        corr_map = {
            name.lower(): id
            for name, id in (
                (c.name, c.id) for c in cache.correspondents.values()
            )
        }
        resolution = _fuzzy_match(result.correspondent, corr_map)
        resolved.correspondent_resolution = resolution
        resolved.correspondent_id = resolution.resolved_id
        if resolution.match_type == "not_found":
            resolved.unresolved_names.append(f"Korrespondent: {result.correspondent}")

    # --- Dokumenttyp ---
    if result.document_type:
        dt_map = {
            name.lower(): id
            for name, id in (
                (dt.name, dt.id) for dt in cache.document_types.values()
            )
        }
        resolution = _fuzzy_match(result.document_type, dt_map)
        resolved.document_type_resolution = resolution
        resolved.document_type_id = resolution.resolved_id
        if resolution.match_type == "not_found":
            resolved.unresolved_names.append(f"Dokumenttyp: {result.document_type}")

    # --- Speicherpfad ---
    if result.storage_path:
        sp_map = {
            name.lower(): id
            for name, id in (
                (sp.name, sp.id) for sp in cache.storage_paths.values()
            )
        }
        resolution = _fuzzy_match(result.storage_path, sp_map)
        resolved.storage_path_resolution = resolution
        resolved.storage_path_id = resolution.resolved_id
        if resolution.match_type == "not_found":
            resolved.unresolved_names.append(f"Speicherpfad: {result.storage_path}")

    # --- Tags ---
    tag_map = {
        name.lower(): id
        for name, id in (
            (t.name, t.id) for t in cache.tags.values()
        )
    }
    for tag_name in result.tags:
        resolution = _fuzzy_match(tag_name, tag_map)
        resolved.tag_resolutions.append(resolution)
        if resolution.resolved_id is not None:
            resolved.tag_ids.append(resolution.resolved_id)
        else:
            resolved.unresolved_names.append(f"Tag: {tag_name}")

    # --- Steuer-Tags ableiten ---
    # Wenn Claude tax_relevant=true und ein tax_year nennt, den passenden
    # Steuer-Tag hinzufügen (falls er existiert und nicht schon drin ist)
    if result.tax_relevant and result.tax_year:
        tax_tag_name = f"Steuer {result.tax_year}"
        tax_tag_id = cache.get_tag_id(tax_tag_name)
        if tax_tag_id and tax_tag_id not in resolved.tag_ids:
            resolved.tag_ids.append(tax_tag_id)
            resolved.tag_resolutions.append(FieldResolution(
                original_name=tax_tag_name,
                resolved_id=tax_tag_id,
                match_type="exact",
                fuzzy_score=1.0,
            ))
            logger.info("Steuer-Tag abgeleitet: '%s' (ID %d)", tax_tag_name, tax_tag_id)
        elif not tax_tag_id:
            logger.info(
                "Steuer-Tag '%s' existiert nicht in Paperless – "
                "könnte in create_new aufgenommen werden",
                tax_tag_name,
            )

    # --- Custom Fields ---
    resolved.custom_fields = _resolve_custom_fields(result, cache)

    # --- Neuanlage-Vorschläge übernehmen ---
    if result.create_new:
        resolved.create_new_correspondents = result.create_new.correspondents
        resolved.create_new_tags = result.create_new.tags
        resolved.create_new_document_types = result.create_new.document_types
        resolved.create_new_storage_paths = [
            {"name": sp.name, "path_template": sp.path_template}
            for sp in result.create_new.storage_paths
        ]

    logger.info(
        "Auflösung abgeschlossen: %d/%d Felder aufgelöst (%.0f%%), "
        "%d nicht aufgelöst, %d Fuzzy-Matches",
        resolved.resolved_fields,
        resolved.total_fields,
        resolved.resolution_ratio * 100,
        len(resolved.unresolved_names),
        sum(
            1 for r in [
                resolved.correspondent_resolution,
                resolved.document_type_resolution,
                resolved.storage_path_resolution,
            ] + resolved.tag_resolutions
            if r is not None and r.match_type == "fuzzy"
        ),
    )

    return resolved


def _resolve_custom_fields(
    result: ClassificationResult,
    cache: LookupCache,
) -> list[CustomFieldResolution]:
    """Löst Custom-Field-Werte aus dem Claude-Ergebnis auf.

    Behandelt:
    - Person (Select, CF 7): Label → Option-ID (ERRATA E-001)
    - Paginierung (Integer, CF 2): Direkter Wert
    - Haus-Register (Select, CF 5): Label → Option-ID
    """
    custom_fields: list[CustomFieldResolution] = []

    # Person (Select-Feld)
    if result.person:
        option_id = cache.get_select_option_id(CF_PERSON, result.person)
        if option_id:
            custom_fields.append(CustomFieldResolution(
                field_id=CF_PERSON,
                value=option_id,
                original_label=result.person,
                resolved=True,
            ))
        else:
            logger.warning(
                "Person '%s' nicht in Select-Optionen gefunden (CF %d)",
                result.person, CF_PERSON,
            )
            custom_fields.append(CustomFieldResolution(
                field_id=CF_PERSON,
                value=None,
                original_label=result.person,
                resolved=False,
            ))

    # Paginierstempel (Integer-Feld, kein Mapping nötig)
    if result.pagination_stamp is not None:
        custom_fields.append(CustomFieldResolution(
            field_id=CF_PAGINIERUNG,
            value=result.pagination_stamp,
            original_label=str(result.pagination_stamp),
            resolved=True,
        ))

    # Haus-Register (Select-Feld)
    if result.is_house_folder_candidate and result.house_register:
        option_id = cache.get_select_option_id(CF_HAUS_REGISTER, result.house_register)
        if option_id:
            custom_fields.append(CustomFieldResolution(
                field_id=CF_HAUS_REGISTER,
                value=option_id,
                original_label=result.house_register,
                resolved=True,
            ))
        else:
            logger.warning(
                "Haus-Register '%s' nicht in Select-Optionen gefunden (CF %d)",
                result.house_register, CF_HAUS_REGISTER,
            )
            custom_fields.append(CustomFieldResolution(
                field_id=CF_HAUS_REGISTER,
                value=None,
                original_label=result.house_register,
                resolved=False,
            ))

    return custom_fields
