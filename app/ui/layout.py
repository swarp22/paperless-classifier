"""Gemeinsames Layout-Template für alle UI-Seiten.

Stellt eine konsistente Seitenstruktur mit Header und linker
Sidebar-Navigation bereit.  Jede Seite nutzt `with page_layout("Titel"):`,
um ihren Content im Hauptbereich zu platzieren.

Design-Referenz: Abschnitt 7.1 (Seitenstruktur)
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from nicegui import ui


# ---------------------------------------------------------------------------
# Farb- und Style-Konstanten
# ---------------------------------------------------------------------------

# Tailwind-Klassen für konsistente Darstellung
HEADER_BG = "bg-blue-800"
SIDEBAR_BG = "bg-gray-50"
SIDEBAR_WIDTH = "w-56"

# Navigationseinträge: (Icon, Label, Route)
NAV_ITEMS: list[tuple[str, str, str]] = [
    ("dashboard", "Dashboard", "/"),
    ("payments", "Kosten", "/costs"),
    ("settings", "Einstellungen", "/settings"),
    ("article", "Logs", "/logs"),
]


# ---------------------------------------------------------------------------
# Status-Farben und Icons für den Poller
# ---------------------------------------------------------------------------

POLLER_STATE_STYLES: dict[str, dict[str, str]] = {
    "running": {"color": "text-green-600", "icon": "circle", "label": "Aktiv"},
    "processing": {"color": "text-green-600", "icon": "sync", "label": "Verarbeitet"},
    "paused": {"color": "text-yellow-600", "icon": "pause_circle", "label": "Pausiert"},
    "stopped": {"color": "text-red-600", "icon": "stop_circle", "label": "Gestoppt"},
}


def _get_poller_state_display() -> dict[str, str]:
    """Ermittelt den aktuellen Poller-Status für den Header."""
    from app.state import get_poller

    poller = get_poller()
    if poller is None:
        return {"color": "text-gray-400", "icon": "help_outline", "label": "N/A"}

    state = poller.status.state.value
    return POLLER_STATE_STYLES.get(
        state,
        {"color": "text-gray-400", "icon": "help_outline", "label": state},
    )


# ---------------------------------------------------------------------------
# Layout-Builder
# ---------------------------------------------------------------------------

@contextmanager
def page_layout(title: str) -> Generator[None, None, None]:
    """Context-Manager für das Seitenlayout mit Header + Sidebar.

    Verwendung:
        @ui.page("/example")
        def example_page():
            with page_layout("Beispiel"):
                ui.label("Inhalt hier")

    Args:
        title: Seitentitel, wird im Browser-Tab und Header angezeigt.
    """
    ui.page_title(f"{title} – Paperless Classifier")

    # --- Header ---
    with ui.header().classes(f"{HEADER_BG} text-white items-center px-4 h-12"):
        # Linke Seite: App-Name
        ui.label("📄 Paperless Classifier").classes("text-lg font-semibold")

        # Rechte Seite: Poller-Status
        with ui.row().classes("ml-auto items-center gap-2"):
            state = _get_poller_state_display()
            ui.icon(state["icon"]).classes(f"{state['color']} text-xl")
            ui.label(state["label"]).classes(f"{state['color']} text-sm")

    # --- Sidebar + Content ---
    with ui.row().classes("w-full min-h-screen no-wrap"):
        # Sidebar
        with ui.column().classes(
            f"{SIDEBAR_BG} {SIDEBAR_WIDTH} min-h-screen pt-4 px-2 "
            "border-r border-gray-200 flex-shrink-0"
        ):
            for icon, label, route in NAV_ITEMS:
                _nav_link(icon, label, route)

        # Hauptbereich
        with ui.column().classes("flex-grow p-6 max-w-6xl"):
            yield


def _nav_link(icon: str, label: str, route: str) -> None:
    """Einzelner Navigations-Link in der Sidebar.

    Hebt die aktuelle Seite visuell hervor.
    """
    with ui.link(target=route).classes(
        "no-underline w-full"
    ):
        with ui.row().classes(
            "items-center gap-3 px-3 py-2 rounded-lg w-full "
            "hover:bg-blue-50 transition-colors cursor-pointer"
        ):
            ui.icon(icon).classes("text-gray-600 text-lg")
            ui.label(label).classes("text-gray-700 text-sm")
