"""System-Prompt und Klassifizierungs-Prompt für die Claude API.

Baut den System-Prompt dynamisch aus Paperless-Stammdaten (Korrespondenten,
Dokumenttypen, Tags, Speicherpfade) und dem Klassifizierungs-Regelwerk auf.
Prompt Caching wird über cache_control gesteuert – der System-Prompt ändert
sich nur bei Stammdaten-Updates und kann daher gecacht werden.
"""

import logging
from dataclasses import dataclass, field

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
Ordne jedes Dokument einer Person zu, wenn möglich.

Erkennungsreihenfolge:
1. Direkte Namensnennung (Anrede, Adressat, Patient)
   - "Herr Obenaus", "Maximilian" → Max
   - "Frau Obenaus", "Melanie" → Melanie
   - "Kilian" → Kilian

2. Kontextbasierte Ableitung:
   - Gehaltsabrechnungen VBK/AVG → Max
   - Kinderarzt, U-Untersuchungen, Pädiatrie → Kilian
   - Schwangerschaft, Geburt, Baby, Säugling → Kilian
   - Gynäkologie, Frauenheilkunde → Melanie
   - Mutterschaftsgeld, Mutterschutz → Melanie
   - Elterngeld, Kindergeld → Kilian

3. Fallback:
   - Gemeinsame Dokumente (Haus, Versorger) → null
   - Keine Zuordnung möglich → null"""

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
