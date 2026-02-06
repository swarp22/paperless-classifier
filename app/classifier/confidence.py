"""Confidence-Bewertung für Klassifizierungsergebnisse.

Kombiniert mehrere Signale zu einer Gesamtbewertung:
- Claude's eigene Confidence (Selbsteinschätzung)
- Mapping-Erfolgsquote (wie viele Namen wurden aufgelöst)
- Fuzzy-Match-Anteil (unsichere Zuordnungen)
- Person-Confidence (Personen-Zuordnung ist fehleranfällig)
- Paginierstempel-Confidence (bei Scans wichtig)

Ergebnis ist ein ConfidenceLevel (HIGH/MEDIUM/LOW) und eine
Empfehlung was damit geschehen soll (auto_apply, needs_review, etc.).

Design-Dokument Abschnitte 6 und 13.8:
- HIGH:   Alle Felder direkt anwenden, ki_status = "classified"
- MEDIUM: Alle Felder anwenden (vorläufig), ki_status = "review"
- LOW:    Felder NICHT anwenden, ki_status = "review" (priorisiert)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.claude.client import ConfidenceLevel
from app.classifier.resolver import ResolvedClassification
from app.logging_config import get_logger

logger = get_logger("classifier")


# ---------------------------------------------------------------------------
# Konfiguration: Gewichtung der einzelnen Signale
# ---------------------------------------------------------------------------

# Gewichte für die Gesamtbewertung (müssen sich zu 1.0 addieren)
WEIGHT_CLAUDE_CONFIDENCE = 0.40    # Claude's eigene Einschätzung
WEIGHT_MAPPING_RATIO = 0.30        # Anteil aufgelöster Felder
WEIGHT_FUZZY_PENALTY = 0.15        # Abzug für Fuzzy-Matches
WEIGHT_SPECIAL_FIELDS = 0.15       # Person + Paginierung Confidence

# Schwellwerte für die Gesamtbewertung (Score 0.0–1.0)
THRESHOLD_HIGH = 0.80
THRESHOLD_MEDIUM = 0.50

# Claude-Confidence → numerischer Score
CONFIDENCE_SCORES: dict[ConfidenceLevel, float] = {
    ConfidenceLevel.HIGH: 1.0,
    ConfidenceLevel.MEDIUM: 0.6,
    ConfidenceLevel.LOW: 0.2,
}


# ---------------------------------------------------------------------------
# Ergebnis-Datenstrukturen
# ---------------------------------------------------------------------------

class ApplyAction(str, Enum):
    """Was mit dem Klassifizierungsergebnis geschehen soll."""
    AUTO_APPLY = "auto_apply"        # Direkt anwenden, ki_status = "classified"
    APPLY_FOR_REVIEW = "apply_review"  # Anwenden aber zur Review markieren
    REVIEW_ONLY = "review_only"      # Nicht anwenden, nur in Review Queue


@dataclass(frozen=True)
class ConfidenceEvaluation:
    """Ergebnis der Confidence-Bewertung mit Erklärung.

    Enthält den finalen ConfidenceLevel, die empfohlene Aktion
    und eine nachvollziehbare Begründung mit Einzelwerten.
    """

    level: ConfidenceLevel
    action: ApplyAction
    score: float                # Gesamtscore (0.0–1.0)

    # Einzelwerte für Nachvollziehbarkeit
    claude_confidence_score: float
    mapping_ratio_score: float
    fuzzy_penalty_score: float
    special_fields_score: float

    # Textuelle Zusammenfassung
    reasons: list[str]

    @property
    def ki_status(self) -> str:
        """ki_status-Wert für Paperless Custom Field."""
        if self.action == ApplyAction.AUTO_APPLY:
            return "classified"
        return "review"

    @property
    def should_apply_fields(self) -> bool:
        """True wenn Felder auf das Dokument angewendet werden sollen."""
        return self.action in (ApplyAction.AUTO_APPLY, ApplyAction.APPLY_FOR_REVIEW)


# ---------------------------------------------------------------------------
# Bewertungslogik
# ---------------------------------------------------------------------------

def evaluate_confidence(resolved: ResolvedClassification) -> ConfidenceEvaluation:
    """Bewertet die Gesamtconfidence eines aufgelösten Klassifizierungsergebnisses.

    Kombiniert vier Signale zu einem Gesamtscore:

    1. Claude-Confidence (40%): Wie sicher war Claude selbst?
    2. Mapping-Quote (30%): Wie viele Namen wurden erfolgreich aufgelöst?
    3. Fuzzy-Penalty (15%): Wurden unsichere Fuzzy-Matches verwendet?
    4. Spezialfelder (15%): Person- und Paginierung-Confidence.

    Args:
        resolved: Aufgelöstes Klassifizierungsergebnis aus dem Resolver.

    Returns:
        ConfidenceEvaluation mit Level, Aktion und Begründung.
    """
    raw = resolved.raw_result
    reasons: list[str] = []

    # --- Signal 1: Claude's eigene Confidence ---
    claude_level = raw.confidence if raw else ConfidenceLevel.LOW
    claude_score = CONFIDENCE_SCORES.get(claude_level, 0.2)
    reasons.append(f"Claude-Confidence: {claude_level.value} ({claude_score:.1f})")

    # --- Signal 2: Mapping-Erfolgsquote ---
    mapping_score = resolved.resolution_ratio
    if mapping_score < 1.0:
        reasons.append(
            f"Mapping: {resolved.resolved_fields}/{resolved.total_fields} "
            f"aufgelöst ({mapping_score:.0%})"
        )
        if resolved.unresolved_names:
            reasons.append(
                f"  Nicht aufgelöst: {', '.join(resolved.unresolved_names[:3])}"
            )
    else:
        reasons.append("Mapping: alle Felder aufgelöst")

    # --- Signal 3: Fuzzy-Match-Penalty ---
    # Fuzzy-Matches sind OK, aber unsicherer als exakte Treffer
    fuzzy_score = 1.0  # Kein Abzug = perfekt
    if resolved.has_fuzzy_matches:
        # Zähle Fuzzy-Matches und mittlere den Score
        fuzzy_resolutions = [
            r for r in [
                resolved.correspondent_resolution,
                resolved.document_type_resolution,
                resolved.storage_path_resolution,
            ] + resolved.tag_resolutions
            if r is not None and r.match_type == "fuzzy"
        ]
        if fuzzy_resolutions:
            avg_fuzzy = sum(r.fuzzy_score for r in fuzzy_resolutions) / len(fuzzy_resolutions)
            fuzzy_score = avg_fuzzy  # Durchschnittlicher Fuzzy-Score als Penalty
            fuzzy_names = [
                f"'{r.original_name}'→'{r.fuzzy_matched_name}' ({r.fuzzy_score:.2f})"
                for r in fuzzy_resolutions
            ]
            reasons.append(f"Fuzzy-Matches: {', '.join(fuzzy_names)}")

    # --- Signal 4: Spezialfelder (Person + Paginierung) ---
    special_score = _evaluate_special_fields(raw, reasons) if raw else 0.5

    # --- Gesamtscore berechnen ---
    total_score = (
        WEIGHT_CLAUDE_CONFIDENCE * claude_score
        + WEIGHT_MAPPING_RATIO * mapping_score
        + WEIGHT_FUZZY_PENALTY * fuzzy_score
        + WEIGHT_SPECIAL_FIELDS * special_score
    )

    # --- Level und Aktion ableiten ---
    if total_score >= THRESHOLD_HIGH:
        level = ConfidenceLevel.HIGH
        action = ApplyAction.AUTO_APPLY
    elif total_score >= THRESHOLD_MEDIUM:
        level = ConfidenceLevel.MEDIUM
        action = ApplyAction.APPLY_FOR_REVIEW
    else:
        level = ConfidenceLevel.LOW
        action = ApplyAction.REVIEW_ONLY

    reasons.insert(0, f"Gesamtscore: {total_score:.2f} → {level.value} → {action.value}")

    evaluation = ConfidenceEvaluation(
        level=level,
        action=action,
        score=total_score,
        claude_confidence_score=claude_score,
        mapping_ratio_score=mapping_score,
        fuzzy_penalty_score=fuzzy_score,
        special_fields_score=special_score,
        reasons=reasons,
    )

    logger.info(
        "Confidence: %.2f → %s (%s) | Claude=%s, Mapping=%.0f%%, "
        "Fuzzy=%.2f, Special=%.2f",
        total_score, level.value, action.value,
        claude_level.value, mapping_score * 100,
        fuzzy_score, special_score,
    )

    return evaluation


def _evaluate_special_fields(
    raw: "ClassificationResult",
    reasons: list[str],
) -> float:
    """Bewertet Person- und Paginierung-Confidence.

    Beide Felder sind optional – wenn sie nicht gesetzt sind,
    gibt es keinen Abzug (neutraler Score 0.7).

    Returns:
        Score zwischen 0.0 und 1.0.
    """
    scores: list[float] = []

    # Person-Confidence
    if raw.person:
        if raw.person_confidence:
            p_score = CONFIDENCE_SCORES.get(raw.person_confidence, 0.2)
            scores.append(p_score)
            if p_score < 0.6:
                reasons.append(
                    f"Person '{raw.person}': {raw.person_confidence.value} "
                    f"({raw.person_reasoning or 'keine Begründung'})"
                )
        else:
            # Person gesetzt aber keine Confidence angegeben → mittel
            scores.append(0.6)

    # Paginierstempel-Confidence
    if raw.pagination_stamp is not None:
        if raw.pagination_stamp_confidence:
            s_score = CONFIDENCE_SCORES.get(raw.pagination_stamp_confidence, 0.2)
            scores.append(s_score)
            if s_score < 0.6:
                reasons.append(
                    f"Paginierstempel {raw.pagination_stamp}: "
                    f"{raw.pagination_stamp_confidence.value}"
                )
        else:
            scores.append(0.6)

    if not scores:
        # Keine Spezialfelder gesetzt → neutraler Wert
        return 0.7

    return sum(scores) / len(scores)
