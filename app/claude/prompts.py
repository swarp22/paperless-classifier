"""System-Prompt und Klassifizierungs-Prompt für die Claude API.

Baut den System-Prompt dynamisch aus Paperless-Stammdaten (Korrespondenten,
Dokumenttypen, Tags, Speicherpfade) und dem Klassifizierungs-Regelwerk auf.
Prompt Caching wird über cache_control gesteuert – der System-Prompt ändert
sich nur bei Stammdaten-Updates und kann daher gecacht werden.

AP-11: build_schema_rules_text() lädt Schema-Analyse-Ergebnisse aus SQLite
und formatiert sie als Textblock für den Klassifizierungs-Prompt.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Eingabe-Datenstruktur für den Prompt-Builder
# ---------------------------------------------------------------------------

@dataclass
class PromptData:
    """Stammdaten und Regeln für die System-Prompt-Generierung.

    Die Listen werden vom Aufrufer (Classifier-Pipeline) aus dem
    LookupCache befüllt.  Regeltexte haben sinnvolle Defaults aus
    dem Design-Dokument und können später über die Web-UI oder
    Schema-Analyse überschrieben werden.
    """

    # Stammdaten aus Paperless (Pflicht)
    correspondents: list[str] = field(default_factory=list)
    document_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    storage_paths: list[str] = field(default_factory=list)

    # Custom-Field-Optionen (mit Projekt-Defaults)
    person_options: list[str] = field(
        default_factory=lambda: ["Max", "Melanie", "Kilian"]
    )
    house_register_options: list[str] = field(default_factory=list)
    vorgang_options: list[str] = field(default_factory=list)

    # Regelwerk-Overrides (None = Default aus Konstanten verwenden)
    person_rules: str | None = None
    house_rules: str | None = None
    title_rules: str | None = None

    # Zusätzliche Regeln aus der Schema-Analyse (Phase 3)
    schema_analysis_rules: str | None = None


# ---------------------------------------------------------------------------
# Standard-Regelwerke (aus Design-Dokument v4, Abschnitt 6.1)
# ---------------------------------------------------------------------------

DEFAULT_PERSON_RULES = """\
Das Feld "Person" ist ein thematischer Filter: "Wen betrifft dieses Dokument
primär?" Alle Dokumente gehören der Familie – Person dient zum Filtern nach
Lebensbereichen, nicht zum Festlegen von Besitzverhältnissen.

Personen:
- Max: Dokumente die Max persönlich betreffen (Gehalt, eigene Arztrechnung,
  Steuerbescheid, Versicherung auf seinen Namen)
- Melanie: Dokumente die Melanie persönlich betreffen (ihre Arztrechnung
  nicht-schwangerschaftsbezogen, ihre Versicherung, ihre Korrespondenz)
- Kilian (Sohn, geb. voraussichtlich 2026): Dokumente die das Kind betreffen –
  Schwangerschaftsvorsorge, Geburtsvorbereitung, Kinderarzt, Kindermöbel,
  Elterngeld, Kindergeld, U-Untersuchungen. Das Kind "existiert" seit Zeugung.

Zuordnungsregeln (Priorität von oben nach unten):
1. Kontext vor Adressat: Hebammenkurs adressiert an Melanie → Kilian
   (betrifft das Kind)
2. Möbelkauf "Kinderbett"/"Wickelkommode" → Kilian
3. Arztrechnung "Schwangerschaftsvorsorge" → Kilian
4. Arztrechnung "Blutuntersuchung" ohne Schwangerschaftsbezug → Adressat
5. Gehaltsabrechnungen VBK/AVG → Max
6. Kinderarzt, U-Untersuchungen, Pädiatrie → Kilian
7. Gynäkologie, Frauenheilkunde (ohne Schwangerschaftsbezug) → Melanie
8. Mutterschaftsgeld, Mutterschutz → Melanie
9. Elterngeld, Kindergeld → Kilian
10. Fallback: Wenn nicht klar zuordenbar → Adressat des Dokuments
11. Gemeinsame Dokumente (Haus, Versorger) → null"""

DEFAULT_HOUSE_RULES = """\
Prüfe ob das Dokument in den Haus-Ordner gehört (physischer Aktenordner).

Kriterien für is_house_folder_candidate = true:
- Grundstück, Baufinanzierung, Grundbuch
- Versicherungen die das Haus betreffen (Wohngebäude, Hausrat)
- Ver- und Entsorgung (Strom, Gas, Wasser, Abfall)
- Handwerkerrechnungen, Renovierung, Wartung
- Grundsteuer, Nebenkosten

Wenn is_house_folder_candidate = true, wähle das passende Register
aus der Liste der verfügbaren Register."""

DEFAULT_TITLE_RULES = """\
Titel-Konventionen:
- Kein Korrespondent im Titel (ist bereits als Metadatum gesetzt)
- Kein Dokumenttyp im Titel (ist bereits als Metadatum gesetzt)
- Datumsangaben im Titel nur wenn inhaltlich relevant (z.B. Abrechnungszeitraum)
- Kurz und prägnant, maximal 60 Zeichen
- Deutsche Sprache"""


# ---------------------------------------------------------------------------
# Antwortformat-Spezifikation (JSON-Schema für Claude)
# ---------------------------------------------------------------------------

RESPONSE_FORMAT_SPEC = """\
Antworte ausschließlich mit validem JSON. Kein Markdown, kein erklÃ¤render Text.
Verwende exakt dieses Schema:

{
  "title": "string – Dokumenttitel nach Titel-Konventionen",
  "document_type": "string | null – Exakter Name aus der Dokumenttyp-Liste, oder null",
  "correspondent": "string | null – Exakter Name aus der Korrespondenten-Liste, oder null",
  "tags": ["string"] – "Liste von Tag-Namen aus der Tag-Liste",
  "storage_path": "string | null – Exakter Name aus der Speicherpfad-Liste, oder null",
  "date": "string | null – Dokumentdatum im Format YYYY-MM-DD",

  "is_scanned_document": "boolean – true wenn Scan/Foto statt Digital-PDF",

  "pagination_stamp": "integer | null – Paginierstempel-Nummer falls sichtbar",
  "pagination_stamp_confidence": "string | null – 'high', 'medium' oder 'low'",

  "is_house_folder_candidate": "boolean – true wenn Haus-Ordner-relevant",
  "house_register": "string | null – Register-Name aus der Liste, wenn zutreffend",

  "person": "string | null – Person aus der Personen-Liste oder null",
  "person_confidence": "string | null – 'high', 'medium' oder 'low'",
  "person_reasoning": "string | null – Kurze Begründung der Personenzuordnung",

  "tax_relevant": "boolean – true wenn steuerlich relevant",
  "tax_year": "integer | null – Steuerjahr falls relevant (z.B. 2026)",

  "link_extraction": {
    "is_linkable_document": "boolean – true wenn verknüpfbar",
    "document_role": "string | null – 'aggregator' oder 'source'",
    "positions": [
      {
        "behandlungsdatum": "string | null – YYYY-MM-DD",
        "leistungserbringer": "string | null",
        "rechnungsbetrag": "number | null",
        "search_hints": {
          "correspondent_pattern": "string | null",
          "document_types": ["string"],
          "date_range_days": "integer (default 7)"
        }
      }
    ],
    "extractable_data": {
      "behandlungsdatum": "string | null",
      "rechnungsbetrag": "number | null",
      "leistungserbringer": "string | null"
    }
  },

  "confidence": "string – 'high', 'medium' oder 'low' (Gesamtbewertung)",
  "reasoning": "string – Kurze Begründung der Klassifizierung",

  "create_new": {
    "correspondents": ["string"] – "Neue Korrespondenten die angelegt werden sollten",
    "tags": ["string"] – "Neue Tags die angelegt werden sollten",
    "document_types": ["string"] – "Neue Dokumenttypen die angelegt werden sollten",
    "storage_paths": [
      {"name": "string", "path_template": "string"}
    ]
  }
}

Wichtige Regeln:
- Verwende NUR Namen die exakt in den Stammdaten-Listen vorkommen
- Wenn ein passender Eintrag fehlt: in create_new aufnehmen UND im Hauptfeld verwenden
- Leere Arrays statt null bei Listen-Feldern
- confidence bezieht sich auf die Gesamtklassifizierung
- link_extraction.positions nur bei Aggregator-Dokumenten (z.B. AXA Abrechnung)
- link_extraction.extractable_data nur bei Source-Dokumenten (z.B. Arztrechnung)"""


# ---------------------------------------------------------------------------
# Prompt-Builder
# ---------------------------------------------------------------------------

def _format_list(items: list[str], prefix: str = "- ") -> str:
    """Formatiert eine Liste als Aufzählung mit Präfix."""
    if not items:
        return "(keine vorhanden)"
    return "\n".join(f"{prefix}{item}" for item in sorted(items))


def build_system_prompt(data: PromptData) -> str:
    """Baut den vollständigen System-Prompt aus Stammdaten und Regelwerk.

    Der generierte Prompt ist für Prompt Caching optimiert: Er ändert sich
    nur wenn sich Stammdaten oder Regeln ändern.  Der Aufrufer setzt
    cache_control={"type": "ephemeral"} beim API-Aufruf.

    Args:
        data: Stammdaten und optionale Regel-Overrides.

    Returns:
        Vollständiger System-Prompt als String.
    """
    # Regelwerk: Override oder Default
    person_rules = data.person_rules or DEFAULT_PERSON_RULES
    house_rules = data.house_rules or DEFAULT_HOUSE_RULES
    title_rules = data.title_rules or DEFAULT_TITLE_RULES

    # Personen-Liste für den Prompt
    person_line = ", ".join(data.person_options) if data.person_options else "(keine definiert)"

    sections = [
        # --- Rolle ---
        "Du bist ein Dokumenten-Klassifizierungs-System für ein privates Paperless-ngx Archiv.",
        "Analysiere das bereitgestellte PDF visuell und inhaltlich.",
        "Antworte ausschließlich mit validem JSON.\n",

        # --- Stammdaten ---
        "## Verfügbare Korrespondenten\n",
        _format_list(data.correspondents),
        "\n\n## Verfügbare Dokumenttypen\n",
        _format_list(data.document_types),
        "\n\n## Verfügbare Tags\n",
        _format_list(data.tags),
        "\n\n## Verfügbare Speicherpfade\n",
        _format_list(data.storage_paths),

        # --- Person ---
        f"\n\n## Personen-Zuordnung\n\nMögliche Werte: {person_line}\n",
        person_rules,

        # --- Haus-Ordner ---
        "\n\n## Haus-Ordner\n",
    ]

    # Register-Optionen nur einfügen wenn vorhanden
    if data.house_register_options:
        sections.append("Verfügbare Register:\n")
        sections.append(_format_list(data.house_register_options))
        sections.append("\n")
    sections.append(house_rules)

    # --- Titel ---
    sections.extend([
        "\n\n## Titel-Konventionen\n",
        title_rules,
    ])

    # --- Vorgänge (wenn vorhanden) ---
    if data.vorgang_options:
        sections.extend([
            "\n\n## Zusammenhängende Vorgänge\n",
            "Bekannte Vorgänge:\n",
            _format_list(data.vorgang_options),
            "\nWenn das Dokument zu einem bekannten Vorgang gehört, "
            "erwähne dies im reasoning-Feld.",
        ])

    # --- Schema-Analyse-Regeln (Phase 3, optional) ---
    if data.schema_analysis_rules:
        sections.extend([
            "\n\n## Erkannte Muster (Schema-Analyse)\n",
            data.schema_analysis_rules,
        ])

    # --- Antwortformat ---
    sections.extend([
        "\n\n## Antwortformat\n",
        RESPONSE_FORMAT_SPEC,
    ])

    prompt = "\n".join(sections)

    logger.debug(
        "System-Prompt generiert: %d Zeichen, %d Korrespondenten, "
        "%d Typen, %d Tags, %d Pfade",
        len(prompt),
        len(data.correspondents),
        len(data.document_types),
        len(data.tags),
        len(data.storage_paths),
    )

    return prompt


def build_user_prompt(extra_context: str | None = None) -> str:
    """Erstellt den User-Prompt für die Klassifizierung.

    Der User-Prompt ist bewusst kurz gehalten – die gesamte Logik
    steckt im System-Prompt.  Optional kann zusätzlicher Kontext
    mitgegeben werden (z.B. OCR-Text als Fallback bei Scans).

    Args:
        extra_context: Optionaler Zusatztext (z.B. OCR-Extrakt).

    Returns:
        User-Prompt als String.
    """
    prompt = "Analysiere und klassifiziere dieses Dokument."

    if extra_context:
        prompt += (
            "\n\nZusätzlicher Kontext (z.B. OCR-Text aus Paperless):\n"
            f"{extra_context}"
        )

    return prompt


# ---------------------------------------------------------------------------
# Schema-Regeln aus SQLite laden (AP-11, Entscheidung 6)
# ---------------------------------------------------------------------------

async def build_schema_rules_text(database: "Database") -> str | None:
    """Lädt Schema-Analyse-Ergebnisse aus SQLite und formatiert sie als Textblock.

    Wird von der Pipeline aufgerufen, um schema_analysis_rules in PromptData
    zu befüllen.  Der formatierte Text wird dann im System-Prompt unter
    "Erkannte Muster (Schema-Analyse)" eingefügt.

    AP-11b: Zusätzlich werden Tag-Zuordnungsregeln als vierte Sektion
    eingebunden.

    Args:
        database: Initialisierte Database-Instanz.

    Returns:
        Formatierter Regeltext oder None wenn keine Schema-Daten vorhanden.
    """
    from app.schema_matrix.storage import SchemaStorage

    storage = SchemaStorage(database)

    # Daten laden
    title_patterns = await storage.get_all_title_patterns()
    path_rules = await storage.get_all_path_rules()
    mappings = await storage.get_all_mappings()
    tag_rules = await storage.get_all_tag_rules()

    # Keine Daten → kein Regelblock
    if not title_patterns and not path_rules and not mappings and not tag_rules:
        return None

    sections: list[str] = []

    # --- Sektion 1: Titel-Schemata ---
    if title_patterns:
        lines = ["### Titel-Schemata (verwende diese Templates für bekannte Kombinationen)\n"]
        for p in title_patterns:
            conf_marker = f" (Confidence: {p.confidence})" if p.confidence else ""
            template = p.title_template or "(kein Template)"
            lines.append(
                f"- {p.correspondent} + {p.document_type} → "
                f"\"{template}\"{conf_marker}"
            )
            if p.rule_description:
                lines.append(f"  Regel: {p.rule_description}")
        sections.append("\n".join(lines))

    # --- Sektion 2: Pfad-Zuordnungen ---
    if path_rules:
        lines = ["### Pfad-Regeln (verwende dieses Ordnungsprinzip für Speicherpfade)\n"]
        for r in path_rules:
            conf_marker = f" (Confidence: {r.confidence})" if r.confidence else ""
            template = r.path_template or "(kein Template)"
            lines.append(f"- Topic \"{r.topic}\": {template}{conf_marker}")
            if r.rule_description:
                lines.append(f"  Regel: {r.rule_description}")
            if r.examples:
                examples_str = ", ".join(r.examples[:3])
                lines.append(f"  Beispiele: {examples_str}")
        sections.append("\n".join(lines))

    # --- Sektion 3: Zuordnungsmatrix ---
    if mappings:
        # Gruppieren nach Korrespondent für Lesbarkeit
        by_correspondent: dict[str, list] = {}
        for m in mappings:
            by_correspondent.setdefault(m.correspondent, []).append(m)

        lines = ["### Zuordnungsmatrix (verwende diese Zuordnungen als Orientierung)\n"]
        for correspondent, entries in sorted(by_correspondent.items()):
            if len(entries) == 1:
                e = entries[0]
                doc_type_note = f" + {e.document_type}" if e.document_type else ""
                lines.append(
                    f"- {correspondent}{doc_type_note} → "
                    f"\"{e.storage_path_name}\" ({e.mapping_type})"
                )
            else:
                lines.append(f"- {correspondent}:")
                for e in entries:
                    doc_type_note = f" [{e.document_type}]" if e.document_type else ""
                    condition = ""
                    if e.condition_description:
                        condition = f" – {e.condition_description}"
                    lines.append(
                        f"  → \"{e.storage_path_name}\"{doc_type_note}"
                        f" ({e.mapping_type}){condition}"
                    )
        sections.append("\n".join(lines))

    # --- Sektion 4: Tag-Zuordnungsregeln (AP-11b) ---
    if tag_rules:
        lines = ["### Tag-Zuordnungsregeln (gelernt aus dem Dokumentenbestand)\n"]
        for tr in tag_rules:
            # Kopfzeile: Korrespondent + Dokumenttyp
            corr_label = tr.correspondent if tr.correspondent else "(alle Korrespondenten)"
            lines.append(f"Für {corr_label} + {tr.document_type}:")

            if tr.positive_tags:
                tags_str = ", ".join(f"\"{t}\"" for t in tr.positive_tags)
                lines.append(f"  ✔ Vergib: {tags_str}")
            if tr.negative_tags:
                tags_str = ", ".join(f"\"{t}\"" for t in tr.negative_tags)
                lines.append(f"  ✗ Vergib NICHT: {tags_str}")
            if tr.reasoning:
                lines.append(f"  Begründung: {tr.reasoning}")
            lines.append("")  # Leerzeile zwischen Regeln
        sections.append("\n".join(lines).rstrip())

    result = "\n\n".join(sections)

    logger.debug(
        "Schema-Regeln formatiert: %d Zeichen, %d Titel-Schemata, "
        "%d Pfad-Regeln, %d Mappings, %d Tag-Regeln",
        len(result), len(title_patterns), len(path_rules),
        len(mappings), len(tag_rules),
    )

    return result
