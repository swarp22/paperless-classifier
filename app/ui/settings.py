"""Einstellungen – Verbindungsstatus, Konfiguration und Poller-Steuerung.

Zeigt:
- Verbindungsstatus: Paperless erreichbar? API-Key vorhanden? DB OK?
- Aktuelle Konfiguration (read-only)
- Poller-Steuerung: Start/Stop/Pause-Buttons

Design-Referenz: Abschnitt 7.3 (Einstellungen)
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.ui.layout import page_layout

logger = get_logger("app")


# ---------------------------------------------------------------------------
# Verbindungsprüfung
# ---------------------------------------------------------------------------

async def _check_connections() -> dict[str, dict[str, Any]]:
    """Prüft den Status aller externen Verbindungen.

    Nutzt die bestehenden Health-Check-Funktionen aus main.py.
    """
    from app.config import get_settings
    from app.health import (
        check_api_key_present,
        check_paperless_reachable,
        check_sqlite_writable,
    )
    from app.state import get_database

    settings = get_settings()

    return {
        "paperless": await check_paperless_reachable(settings),
        "api_key": check_api_key_present(settings),
        "database": check_sqlite_writable(settings),
        "db_initialized": {"status": "ok" if get_database() is not None else "error"},
    }


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------

def _status_icon(status: str) -> tuple[str, str]:
    """Icon und Farbe für einen Verbindungsstatus."""
    return {
        "ok": ("check_circle", "text-green-600"),
        "error": ("error", "text-red-600"),
        "not_configured": ("warning", "text-yellow-600"),
        "unreachable": ("cloud_off", "text-red-600"),
    }.get(status, ("help_outline", "text-gray-400"))


def _render_connections(checks: dict[str, dict[str, Any]]) -> None:
    """Rendert die Verbindungsstatus-Karten."""
    with ui.card().classes("w-full"):
        ui.label("Verbindungsstatus").classes(
            "text-sm text-gray-500 font-medium mb-3"
        )

        items = [
            ("Paperless-ngx", checks["paperless"]),
            ("Anthropic API-Key", checks["api_key"]),
            ("SQLite (Dateisystem)", checks["database"]),
            ("SQLite (Verbindung)", checks["db_initialized"]),
        ]

        for label, check in items:
            status = check.get("status", "unknown")
            icon_name, icon_color = _status_icon(status)

            with ui.row().classes("items-center gap-3 py-2"):
                ui.icon(icon_name).classes(f"{icon_color} text-xl")
                with ui.column().classes("gap-0"):
                    ui.label(label).classes("text-sm font-medium text-gray-700")
                    # Details je nach Check
                    detail = ""
                    if "url" in check:
                        detail = check["url"]
                    elif "key_prefix" in check:
                        detail = check["key_prefix"]
                    elif "path" in check:
                        detail = check["path"]
                    elif "error" in check:
                        detail = check["error"]

                    if detail:
                        ui.label(detail).classes("text-xs text-gray-400")


def _render_config() -> None:
    """Zeigt die aktuelle Konfiguration (read-only)."""
    from app.config import get_settings

    settings = get_settings()

    with ui.card().classes("w-full"):
        ui.label("Aktuelle Konfiguration").classes(
            "text-sm text-gray-500 font-medium mb-3"
        )

        config_items = [
            ("Paperless-URL", settings.paperless_url),
            ("Standard-Modell", settings.default_model),
            ("Batch-Modell", settings.batch_model),
            ("Schema-Matrix-Modell", settings.schema_matrix_model),
            ("Verarbeitungsmodus", settings.processing_mode.value),
            ("Polling-Intervall", f"{settings.polling_interval_seconds}s"),
            ("Monatslimit", f"${settings.monthly_cost_limit_usd:.2f}"),
            ("Log-Level", settings.log_level.value),
            ("Datenverzeichnis", str(settings.data_dir)),
            ("DB-Pfad", str(settings.db_path)),
        ]

        for label, value in config_items:
            with ui.row().classes("items-center gap-4 py-1"):
                ui.label(label).classes("text-sm text-gray-600 w-44")
                ui.label(value).classes(
                    "text-sm font-mono text-gray-800 bg-gray-50 px-2 py-1 rounded"
                )


def _render_poller_control() -> None:
    """Poller-Steuerung mit Start/Stop/Pause-Buttons."""
    from app.state import get_poller
    from app.scheduler.poller import PollerState

    with ui.card().classes("w-full"):
        ui.label("Poller-Steuerung").classes(
            "text-sm text-gray-500 font-medium mb-3"
        )

        poller = get_poller()
        if poller is None:
            ui.label("Poller nicht initialisiert.").classes(
                "text-gray-400 italic"
            )
            ui.label(
                "Mögliche Ursachen: Paperless nicht erreichbar, "
                "API-Key fehlt, Pipeline-Fehler."
            ).classes("text-xs text-gray-400")
            return

        # Status-Anzeige (wird bei Button-Klick aktualisiert)
        status_label = ui.label()
        _update_poller_label(status_label, poller)

        with ui.row().classes("gap-3 mt-2"):
            async def on_start() -> None:
                try:
                    if poller.status.state == PollerState.PAUSED:
                        poller.resume()
                    elif poller.status.state == PollerState.STOPPED:
                        poller.start()
                    _update_poller_label(status_label, poller)
                    ui.notify("Poller gestartet", type="positive")
                except Exception as exc:
                    ui.notify(f"Fehler: {exc}", type="negative")

            async def on_pause() -> None:
                try:
                    poller.pause()
                    _update_poller_label(status_label, poller)
                    ui.notify("Poller pausiert", type="info")
                except Exception as exc:
                    ui.notify(f"Fehler: {exc}", type="negative")

            async def on_stop() -> None:
                try:
                    await poller.stop()
                    _update_poller_label(status_label, poller)
                    ui.notify("Poller gestoppt", type="warning")
                except Exception as exc:
                    ui.notify(f"Fehler: {exc}", type="negative")

            ui.button("Start / Fortsetzen", on_click=on_start, icon="play_arrow").props(
                "color=positive flat"
            )
            ui.button("Pausieren", on_click=on_pause, icon="pause").props(
                "color=warning flat"
            )
            ui.button("Stoppen", on_click=on_stop, icon="stop").props(
                "color=negative flat"
            )


def _update_poller_label(label: ui.label, poller: Any) -> None:
    """Aktualisiert das Status-Label des Pollers."""
    state = poller.status.state.value
    label.text = f"Status: {state}"
    label.classes(replace="text-sm font-medium")


# ---------------------------------------------------------------------------
# Seiten-Definition
# ---------------------------------------------------------------------------

def register(app: Any = None) -> None:
    """Registriert die Einstellungs-Seite."""

    @ui.page("/settings")
    async def settings_page() -> None:
        with page_layout("Einstellungen"):
            checks = await _check_connections()
            _render_connections(checks)
            _render_config()
            _render_poller_control()
