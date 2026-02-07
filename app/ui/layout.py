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
    ("rate_review", "Review Queue", "/review"),
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

        # Rechte Seite: Poller-Status (als Chip mit Rahmen für Sichtbarkeit)
        state = _get_poller_state_display()
        with ui.row().classes("ml-auto items-center"):
            with ui.row().classes(
                "items-center gap-1 px-3 py-1 rounded-full "
                "bg-white/10 border border-white/20"
            ):
                ui.icon(state["icon"]).classes(f"{state['color']} text-sm")
                ui.label(state["label"]).classes("text-xs text-white/90")

    # --- Sidebar + Content ---
    with ui.row().classes("w-full min-h-screen no-wrap"):
        # Sidebar
        with ui.column().classes(
            f"{SIDEBAR_BG} {SIDEBAR_WIDTH} min-h-screen pt-4 px-2 "
            "border-r border-gray-200 flex-shrink-0"
        ):
            review_badge = None
            for icon, label, route in NAV_ITEMS:
                badge = _nav_link(icon, label, route)
                if badge is not None:
                    review_badge = badge

            # Review-Zähler async nachladen
            _schedule_review_badge_update(review_badge)

        # Hauptbereich
        with ui.column().classes("flex-grow p-6 max-w-6xl"):
            yield


def _nav_link(icon: str, label: str, route: str) -> ui.element | None:
    """Einzelner Navigations-Link in der Sidebar.

    Hebt die aktuelle Seite visuell hervor.

    Returns:
        Das Badge-Element falls label == 'Review Queue', sonst None.
    """
    badge_element: ui.element | None = None

    with ui.link(target=route).classes(
        "no-underline w-full"
    ):
        with ui.row().classes(
            "items-center gap-3 px-3 py-2 rounded-lg w-full "
            "hover:bg-blue-50 transition-colors cursor-pointer"
        ):
            ui.icon(icon).classes("text-gray-600 text-lg")
            ui.label(label).classes("text-gray-700 text-sm")

            # Badge für Review Queue – wird async befüllt
            if label == "Review Queue":
                badge_element = ui.badge("", color="red").props("rounded")
                badge_element.classes("ml-auto text-xs")
                badge_element.set_visibility(False)

    return badge_element


def _schedule_review_badge_update(badge: ui.element | None) -> None:
    """Startet einen einmaligen async Timer zum Aktualisieren des Review-Badges.

    Wird aus page_layout heraus aufgerufen.  Lädt den Zähler aus SQLite
    und zeigt das Badge nur bei count > 0.
    """
    if badge is None:
        return

    async def _update() -> None:
        from app.state import get_database

        db = get_database()
        if db is None:
            return
        try:
            count = await db.get_review_count()
            badge.set_text(str(count))
            badge.set_visibility(count > 0)
        except Exception:
            # Kein Badge anzeigen wenn DB-Abfrage fehlschlägt
            pass

    # Einmaliger Timer nach 0.1s – async-safe in NiceGUI
    ui.timer(0.1, _update, once=True)
