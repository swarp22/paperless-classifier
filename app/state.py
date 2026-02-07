"""Globaler Laufzeit-Zustand des Classifiers.

Dieses Modul enthält ausschließlich die Referenzen auf Laufzeit-Objekte
und Getter-Funktionen.  Es hat KEINE Seiteneffekte beim Import –
kein Logging, kein NiceGUI, keine Registrierungen.

Hintergrund (E-017): `app.main` wird als `__main__` geladen.
Ein späterer `from app.main import ...` würde das Modul erneut
ausführen und dabei `app.on_startup()` doppelt registrieren.
Durch Auslagerung der Getter hierher wird das Problem vermieden.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Laufzeit-Objekte (werden von main.async_startup() gesetzt)
# ---------------------------------------------------------------------------

database: Any = None            # Database | None
paperless_client: Any = None    # PaperlessClient | None
claude_client: Any = None       # ClaudeClient | None
cost_tracker: Any = None        # CostTracker | None
pipeline: Any = None            # ClassificationPipeline | None
poller: Any = None              # Poller | None


# ---------------------------------------------------------------------------
# Getter-Funktionen (für UI-Module und Health-Check)
# ---------------------------------------------------------------------------

def get_database() -> Any:
    """Gibt die Database-Instanz zurück."""
    return database


def get_poller() -> Any:
    """Gibt die Poller-Instanz zurück."""
    return poller


def get_pipeline() -> Any:
    """Gibt die Pipeline-Instanz zurück."""
    return pipeline


def get_cost_tracker() -> Any:
    """Gibt den CostTracker zurück."""
    return cost_tracker
