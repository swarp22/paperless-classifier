"""Log-Viewer – Letzte Log-Einträge aus der Datei-Log.

Zeigt:
- Letzte ~200 Zeilen aus classifier.log
- Filter nach Level (INFO/WARNING/ERROR)
- Manueller Refresh-Button

Die Logs werden aus der rotierenden Log-Datei gelesen, nicht aus
dem In-Memory-Logging-System.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.ui.layout import page_layout

logger = get_logger("app")

# Maximale Anzahl Zeilen, die geladen werden
MAX_LOG_LINES = 200


# ---------------------------------------------------------------------------
# Log-Parsing
# ---------------------------------------------------------------------------

@dataclass
class LogEntry:
    """Einzelner geparseter Log-Eintrag."""

    timestamp: str
    level: str
    component: str
    message: str
    raw: str


def _parse_log_line(line: str) -> LogEntry | None:
    """Parst eine Log-Zeile im Format: YYYY-MM-DD HH:MM:SS | LEVEL | component | message

    Gibt None zurück wenn die Zeile nicht geparst werden kann.
    """
    line = line.rstrip()
    if not line:
        return None

    # Format: "2026-02-07 14:23:01 | INFO     | paperless_classifier.app | Nachricht"
    parts = line.split(" | ", maxsplit=3)
    if len(parts) < 4:
        # Zeile ohne Standard-Format (z.B. Traceback-Fortsetzung)
        return LogEntry(
            timestamp="",
            level="",
            component="",
            message=line,
            raw=line,
        )

    return LogEntry(
        timestamp=parts[0].strip(),
        level=parts[1].strip(),
        component=parts[2].strip(),
        message=parts[3].strip(),
        raw=line,
    )


def _read_log_lines(log_path: Path, max_lines: int = MAX_LOG_LINES) -> list[str]:
    """Liest die letzten N Zeilen aus der Log-Datei.

    Nutzt einen einfachen Tail-Ansatz: Datei rückwärts lesen.
    Für die typische Log-Größe (<5 MB) ist das performant genug.
    """
    if not log_path.exists():
        return []

    try:
        # Gesamte Datei lesen und letzte N Zeilen nehmen
        # Bei 5 MB Limit (RotatingFileHandler) kein Speicherproblem
        text = log_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-max_lines:]
    except OSError as exc:
        logger.warning("Log-Datei konnte nicht gelesen werden: %s", exc)
        return []


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------

# Farben für Log-Level
_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "text-gray-400",
    "INFO": "text-blue-700",
    "WARNING": "text-yellow-700",
    "ERROR": "text-red-700",
}

# Filter-Optionen: Level → Mindest-Priorität
_LEVEL_PRIORITY: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
}


# ---------------------------------------------------------------------------
# Seiten-Definition
# ---------------------------------------------------------------------------

def register(app: Any = None) -> None:
    """Registriert die Log-Viewer-Seite."""

    @ui.page("/logs")
    async def logs_page() -> None:
        with page_layout("Logs"):
            from app.config import get_settings
            settings = get_settings()
            log_path = settings.log_dir / "classifier.log"

            # Zustandsvariablen
            current_level = {"value": "INFO"}
            current_search = {"value": ""}
            current_component = {"value": "Alle"}
            log_container_ref: dict[str, ui.column | None] = {"ref": None}
            auto_refresh_timer: dict[str, Any] = {"timer": None}

            def _get_available_components(entries: list[LogEntry]) -> list[str]:
                """Extrahiert alle eindeutigen Komponenten aus den Log-Einträgen."""
                components = sorted({
                    e.component for e in entries
                    if e.component
                })
                return ["Alle"] + components

            def _filter_entries(entries: list[LogEntry]) -> list[LogEntry]:
                """Filtert Einträge nach Level, Komponente und Suchbegriff."""
                min_prio = _LEVEL_PRIORITY.get(current_level["value"], 1)
                search_term = (current_search["value"] or "").lower().strip()
                comp_filter = current_component["value"]

                # In Blöcke gruppieren (Traceback-Zeilen gehören zum Vorgänger)
                blocks: list[list[LogEntry]] = []
                for entry in entries:
                    if entry.timestamp:
                        blocks.append([entry])
                    elif blocks:
                        blocks[-1].append(entry)

                filtered_blocks: list[list[LogEntry]] = []
                for block in blocks:
                    head = block[0]

                    # Level-Filter
                    if _LEVEL_PRIORITY.get(head.level, 1) < min_prio:
                        continue

                    # Component-Filter
                    if comp_filter != "Alle" and head.component != comp_filter:
                        continue

                    # Textsuche: über alle Zeilen des Blocks
                    if search_term:
                        block_text = " ".join(e.raw.lower() for e in block)
                        if search_term not in block_text:
                            continue

                    filtered_blocks.append(block)

                # Neueste zuerst
                filtered_blocks.reverse()

                # Flachliste zurückgeben
                return [entry for block in filtered_blocks for entry in block]

            def refresh_logs() -> None:
                """Lädt die Logs neu und rendert sie."""
                lines = _read_log_lines(log_path)
                all_entries = [
                    e for line in lines
                    if (e := _parse_log_line(line)) is not None
                ]

                # Komponenten-Dropdown aktualisieren (ohne Change-Event auszulösen)
                new_components = _get_available_components(all_entries)
                if hasattr(comp_select, 'options'):
                    comp_select.options = new_components
                    comp_select.update()

                filtered = _filter_entries(all_entries)

                # Container neu rendern
                if log_container_ref["ref"] is not None:
                    log_container_ref["ref"].clear()
                    with log_container_ref["ref"]:
                        _render_filtered_log(filtered, len(all_entries))

            def on_level_change(e: Any) -> None:
                current_level["value"] = e.value
                refresh_logs()

            def on_search_change(e: Any) -> None:
                current_search["value"] = e.value or ""
                refresh_logs()

            def on_component_change(e: Any) -> None:
                current_component["value"] = e.value
                refresh_logs()

            def on_auto_refresh_change(e: Any) -> None:
                """Aktiviert/deaktiviert den Auto-Refresh-Timer."""
                if e.value:
                    auto_refresh_timer["timer"] = ui.timer(5.0, refresh_logs)
                elif auto_refresh_timer["timer"] is not None:
                    auto_refresh_timer["timer"].cancel()
                    auto_refresh_timer["timer"] = None

            # Header mit Controls
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.label("Log-Datei:").classes("text-sm text-gray-500")
                    ui.label(str(log_path)).classes(
                        "text-sm font-mono text-gray-700"
                    )

                with ui.row().classes("items-center gap-4 mt-2 flex-wrap"):
                    ui.select(
                        options=["DEBUG", "INFO", "WARNING", "ERROR"],
                        value="INFO",
                        label="Mindest-Level",
                        on_change=on_level_change,
                    ).classes("w-36")

                    comp_select = ui.select(
                        options=["Alle"],
                        value="Alle",
                        label="Komponente",
                        on_change=on_component_change,
                    ).classes("w-52")

                    ui.input(
                        label="Suche",
                        placeholder="Text filtern...",
                        on_change=on_search_change,
                    ).props("clearable dense").classes("w-52")

                    ui.button(
                        "Aktualisieren",
                        on_click=refresh_logs,
                        icon="refresh",
                    ).props("flat")

                    ui.switch(
                        "Auto (5s)",
                        on_change=on_auto_refresh_change,
                    ).props("dense").classes("ml-2")

            # Initiale Log-Anzeige
            lines = _read_log_lines(log_path)
            all_entries = [
                e for line in lines
                if (e := _parse_log_line(line)) is not None
            ]

            # Komponenten-Dropdown initial befüllen
            comp_select.options = _get_available_components(all_entries)
            comp_select.update()

            filtered = _filter_entries(all_entries)

            with ui.card().classes("w-full"):
                log_container_ref["ref"] = ui.column().classes("w-full gap-0")
                with log_container_ref["ref"]:
                    _render_filtered_log(filtered, len(all_entries))


def _render_filtered_log(
    entries: list[LogEntry],
    total_count: int,
) -> None:
    """Rendert bereits gefilterte und sortierte Log-Einträge.

    Args:
        entries: Gefilterte Einträge (neueste zuerst, flache Liste).
        total_count: Gesamtanzahl ungefilterte Einträge (für Anzeige).
    """
    # Zähle Blöcke (= Einträge mit Timestamp)
    block_count = sum(1 for e in entries if e.timestamp)

    if not entries:
        ui.label("Keine Log-Einträge vorhanden.").classes(
            "text-gray-400 italic p-4"
        )
        return

    ui.label(
        f"{block_count} Einträge (gefiltert von {total_count}, "
        f"neueste zuerst)"
    ).classes("text-xs text-gray-400 mb-2")

    with ui.scroll_area().classes("w-full h-[600px] border rounded"):
        for entry in entries:
            color = _LEVEL_COLORS.get(entry.level, "text-gray-600")
            if entry.timestamp:
                with ui.row().classes(
                    "items-start gap-2 px-3 py-1 hover:bg-gray-50 "
                    "border-b border-gray-100 w-full"
                ):
                    ui.label(entry.timestamp).classes(
                        "text-xs text-gray-400 font-mono whitespace-nowrap "
                        "flex-shrink-0 w-40"
                    )
                    if entry.level:
                        ui.label(entry.level).classes(
                            f"text-xs font-mono font-bold {color} "
                            "flex-shrink-0 w-16"
                        )
                    if entry.component:
                        ui.label(entry.component).classes(
                            "text-xs font-mono text-gray-400 "
                            "flex-shrink-0 w-32 truncate"
                        )
                    ui.label(entry.message).classes(
                        "text-xs font-mono text-gray-700 break-all"
                    )
            else:
                ui.label(entry.message).classes(
                    "text-xs font-mono text-gray-500 pl-60 break-all"
                )
