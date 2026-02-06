"""Token-Zählung und Kostenberechnung pro API-Request.

Berechnet Kosten basierend auf Modell und Token-Typ (Input, Output,
Cache Read, Cache Write). Akkumuliert Verbrauchsdaten für Daily/Monthly Tracking.

Preistabelle: Stand Anthropic Pricing Page, 06.02.2026.
Cache Write hat zwei Stufen: 5min (ephemeral) und 1h.
Bei Preisänderungen von Anthropic hier aktualisieren.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preistabelle (USD pro Million Tokens)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelPricing:
    """Preisinformationen für ein Claude-Modell (USD pro Million Tokens).

    Alle Preise beziehen sich auf die Anthropic Messages API.
    Cache Write hat zwei Stufen:
    - 5min (cache_control: {"type": "ephemeral"}) – unser Standard
    - 1h   (cache_control: {"type": "ephemeral", "ttl": 3600})
    Batch-Preise liegen bei 50 % der regulären Preise.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float       # "Cache Hits & Refreshes"
    cache_write_5m_per_mtok: float   # 5-Minuten-Cache (Standard)
    cache_write_1h_per_mtok: float   # 1-Stunden-Cache
    batch_input_per_mtok: float
    batch_output_per_mtok: float


# Modell-String → Preise.  Schlüssel müssen exakt den config.py-Werten entsprechen.
# Stand: Anthropic Pricing Page, 06.02.2026.
PRICING_TABLE: dict[str, ModelPricing] = {
    # --- Opus (Schema-Analyse) ---
    "claude-opus-4-6": ModelPricing(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.50,
        cache_write_5m_per_mtok=6.25,
        cache_write_1h_per_mtok=10.0,
        batch_input_per_mtok=2.50,
        batch_output_per_mtok=12.50,
    ),
    "claude-opus-4-5-20251101": ModelPricing(
        input_per_mtok=5.0,
        output_per_mtok=25.0,
        cache_read_per_mtok=0.50,
        cache_write_5m_per_mtok=6.25,
        cache_write_1h_per_mtok=10.0,
        batch_input_per_mtok=2.50,
        batch_output_per_mtok=12.50,
    ),
    # --- Sonnet (Standard-Klassifizierung) ---
    "claude-sonnet-4-5-20250929": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.30,
        cache_write_5m_per_mtok=3.75,
        cache_write_1h_per_mtok=6.0,
        batch_input_per_mtok=1.50,
        batch_output_per_mtok=7.50,
    ),
    # --- Haiku (einfache Dokumente) ---
    "claude-haiku-4-5-20251001": ModelPricing(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_read_per_mtok=0.10,
        cache_write_5m_per_mtok=1.25,
        cache_write_1h_per_mtok=2.0,
        batch_input_per_mtok=0.50,
        batch_output_per_mtok=2.50,
    ),
}

# Fallback-Modell für Kostenberechnung bei unbekannten Modell-Strings
_FALLBACK_MODEL = "claude-sonnet-4-5-20250929"


# ---------------------------------------------------------------------------
# Kostenberechnung
# ---------------------------------------------------------------------------

def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    is_batch: bool = False,
    cache_ttl: str = "5m",
) -> float:
    """Berechnet die Kosten eines einzelnen API-Aufrufs in USD.

    Args:
        model: Modell-String (z.B. "claude-sonnet-4-5-20250929").
        input_tokens: Anzahl Input-Tokens (ohne Cache-Tokens).
        output_tokens: Anzahl Output-Tokens.
        cache_read_tokens: Aus dem Cache gelesene Tokens.
        cache_creation_tokens: In den Cache geschriebene Tokens.
        is_batch: True wenn der Request über die Batch API lief.
        cache_ttl: Cache-Stufe: "5m" (Standard, ephemeral) oder "1h".

    Returns:
        Kosten in USD als float.
    """
    pricing = PRICING_TABLE.get(model)
    if pricing is None:
        logger.warning(
            "Unbekanntes Modell '%s' – verwende '%s'-Preise als Fallback",
            model,
            _FALLBACK_MODEL,
        )
        pricing = PRICING_TABLE[_FALLBACK_MODEL]

    if is_batch:
        input_cost = (input_tokens / 1_000_000) * pricing.batch_input_per_mtok
        output_cost = (output_tokens / 1_000_000) * pricing.batch_output_per_mtok
    else:
        input_cost = (input_tokens / 1_000_000) * pricing.input_per_mtok
        output_cost = (output_tokens / 1_000_000) * pricing.output_per_mtok

    cache_read_cost = (cache_read_tokens / 1_000_000) * pricing.cache_read_per_mtok

    # Cache Write: 5-Minuten-Stufe (ephemeral) oder 1-Stunden-Stufe
    cache_write_price = (
        pricing.cache_write_1h_per_mtok if cache_ttl == "1h"
        else pricing.cache_write_5m_per_mtok
    )
    cache_write_cost = (cache_creation_tokens / 1_000_000) * cache_write_price

    return input_cost + output_cost + cache_read_cost + cache_write_cost


# ---------------------------------------------------------------------------
# TokenUsage – Verbrauchsdaten eines einzelnen API-Aufrufs
# ---------------------------------------------------------------------------

class TokenUsage(BaseModel):
    """Token-Verbrauch und Kosten eines einzelnen API-Aufrufs.

    Kosten werden automatisch bei Erstellung berechnet, sofern Token-Zahlen
    vorhanden sind und cost_usd nicht explizit gesetzt wurde.
    """

    model: str = Field(..., description="Verwendetes Modell (z.B. claude-sonnet-4-5-20250929)")
    input_tokens: int = Field(0, ge=0, description="Input-Tokens (ohne Cache)")
    output_tokens: int = Field(0, ge=0, description="Output-Tokens")
    cache_read_tokens: int = Field(0, ge=0, description="Aus Cache gelesene Input-Tokens")
    cache_creation_tokens: int = Field(0, ge=0, description="In Cache geschriebene Input-Tokens")
    is_batch: bool = Field(False, description="Request lief über die Batch API")
    cost_usd: float = Field(0.0, ge=0.0, description="Berechnete Kosten in USD")
    timestamp: datetime = Field(default_factory=datetime.now, description="Zeitpunkt des Aufrufs")
    document_id: Optional[int] = Field(None, description="Zugehörige Paperless Dokument-ID")

    @model_validator(mode="after")
    def _auto_calculate_cost(self) -> "TokenUsage":
        """Berechnet Kosten automatisch, falls Tokens vorhanden aber cost_usd = 0."""
        has_tokens = self.input_tokens > 0 or self.output_tokens > 0
        if self.cost_usd == 0.0 and has_tokens:
            self.cost_usd = calculate_cost(
                model=self.model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_read_tokens=self.cache_read_tokens,
                cache_creation_tokens=self.cache_creation_tokens,
                is_batch=self.is_batch,
            )
        return self

    @property
    def total_tokens(self) -> int:
        """Gesamtzahl aller Tokens (Input + Output + Cache)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


# ---------------------------------------------------------------------------
# CostTracker – Akkumulation über mehrere Aufrufe
# ---------------------------------------------------------------------------

class CostTracker:
    """Akkumuliert Token-Verbrauch und Kosten über die Laufzeit.

    In-Memory-Tracking.  Persistierung in SQLite erfolgt separat
    über das State-Management (späteres AP).
    """

    def __init__(self) -> None:
        self._usages: list[TokenUsage] = []

    def record(self, usage: TokenUsage) -> None:
        """Zeichnet einen API-Aufruf auf und loggt den Verbrauch."""
        self._usages.append(usage)
        logger.info(
            "API-Verbrauch aufgezeichnet: model=%s, in=%d, out=%d, "
            "cache_r=%d, cache_w=%d, cost=$%.6f, doc_id=%s",
            usage.model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_creation_tokens,
            usage.cost_usd,
            usage.document_id,
        )

    # --- Abfragen ---

    @property
    def total_cost_usd(self) -> float:
        """Gesamtkosten aller aufgezeichneten Aufrufe."""
        return sum(u.cost_usd for u in self._usages)

    @property
    def total_requests(self) -> int:
        """Anzahl aufgezeichneter API-Aufrufe."""
        return len(self._usages)

    def get_daily_cost(self, day: Optional[date] = None) -> float:
        """Kosten für einen bestimmten Tag (default: heute)."""
        target = day or date.today()
        return sum(u.cost_usd for u in self._usages if u.timestamp.date() == target)

    def get_monthly_cost(
        self,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> float:
        """Kosten für einen bestimmten Monat (default: aktueller Monat)."""
        now = date.today()
        y = year or now.year
        m = month or now.month
        return sum(
            u.cost_usd
            for u in self._usages
            if u.timestamp.year == y and u.timestamp.month == m
        )

    def is_limit_reached(self, limit_usd: float) -> bool:
        """Prüft ob das monatliche Kostenlimit erreicht oder überschritten ist.

        Args:
            limit_usd: Monatslimit in USD.  0 = kein Limit.

        Returns:
            True wenn das Limit erreicht ist.
        """
        if limit_usd <= 0:
            return False
        return self.get_monthly_cost() >= limit_usd

    def get_model_breakdown(self) -> dict[str, dict[str, float | int]]:
        """Aufschlüsselung nach Modell für den aktuellen Monat.

        Returns:
            Dict mit Modell-String als Key und
            {"cost_usd": float, "requests": int, "total_tokens": int} als Value.
        """
        now = date.today()
        breakdown: dict[str, dict[str, float | int]] = {}

        for u in self._usages:
            if u.timestamp.year != now.year or u.timestamp.month != now.month:
                continue
            if u.model not in breakdown:
                breakdown[u.model] = {"cost_usd": 0.0, "requests": 0, "total_tokens": 0}
            breakdown[u.model]["cost_usd"] += u.cost_usd
            breakdown[u.model]["requests"] += 1
            breakdown[u.model]["total_tokens"] += u.total_tokens

        return breakdown

    @property
    def usages(self) -> list[TokenUsage]:
        """Alle aufgezeichneten Nutzungsdaten (Kopie der internen Liste)."""
        return list(self._usages)

    def clear(self) -> None:
        """Löscht alle aufgezeichneten Daten (z.B. nach Persistierung)."""
        count = len(self._usages)
        self._usages.clear()
        logger.debug("CostTracker geleert: %d Einträge entfernt", count)
