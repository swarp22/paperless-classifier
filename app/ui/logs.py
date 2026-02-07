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
            log_container_ref: dict[str, ui.column | None] = {"ref": None}

            def refresh_logs() -> None:
                """Lädt die Logs neu und rendert sie."""
                lines = _read_log_lines(log_path)
                entries = [
                    e for line in lines
                    if (e := _parse_log_line(line)) is not None
                ]

                # Alten Container entfernen
                if log_container_ref["ref"] is not None:
                    log_container_ref["ref"].clear()
                    # Content neu rendern innerhalb des bestehenden Containers
                    with log_container_ref["ref"]:
                        _render_log_content(entries, current_level["value"])
                else:
                    log_container_ref["ref"] = _render_log_content_wrapper(
                        entries, current_level["value"]
                    )

            def on_level_change(e: Any) -> None:
                """Handler für Level-Filter-Änderung."""
                current_level["value"] = e.value
                refresh_logs()

            # Header mit Controls
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-4 w-full"):
                    ui.label("Log-Datei:").classes("text-sm text-gray-500")
                    ui.label(str(log_path)).classes(
                        "text-sm font-mono text-gray-700"
                    )

                with ui.row().classes("items-center gap-4 mt-2"):
                    ui.select(
                        options=["DEBUG", "INFO", "WARNING", "ERROR"],
                        value="INFO",
                        label="Mindest-Level",
                        on_change=on_level_change,
                    ).classes("w-36")

                    ui.button(
                        "Aktualisieren",
                        on_click=refresh_logs,
                        icon="refresh",
                    ).props("flat")

            # Initiale Log-Anzeige
            lines = _read_log_lines(log_path)
            entries = [
                e for line in lines
                if (e := _parse_log_line(line)) is not None
            ]

            with ui.card().classes("w-full"):
                log_container_ref["ref"] = ui.column().classes("w-full gap-0")
                with log_container_ref["ref"]:
                    _render_log_content(entries, current_level["value"])


def _render_log_content(entries: list[LogEntry], min_level: str) -> None:
    """Rendert den eigentlichen Log-Inhalt (innerhalb eines Containers).

    Neueste Einträge werden oben angezeigt.  Zusammengehörige Zeilen
    (z.B. Tracebacks) bleiben als Block beisammen.
    """
    min_prio = _LEVEL_PRIORITY.get(min_level, 1)

    # Einträge in Blöcke gruppieren: Jeder Block beginnt mit einer
    # Timestamp-Zeile, gefolgt von 0..n Continuation-Zeilen (kein Timestamp).
    blocks: list[list[LogEntry]] = []
    for entry in entries:
        if entry.timestamp:
            blocks.append([entry])
        elif blocks:
            blocks[-1].append(entry)
        # else: Continuation-Zeile ohne vorherigen Block → ignorieren

    # Filter: Block gehört rein wenn die erste Zeile das Level erfüllt
    filtered_blocks = [
        block for block in blocks
        if _LEVEL_PRIORITY.get(block[0].level, 1) >= min_prio
    ]

    # Neueste zuerst
    filtered_blocks.reverse()

    if not filtered_blocks:
        ui.label("Keine Log-Einträge vorhanden.").classes(
            "text-gray-400 italic p-4"
        )
        return

    ui.label(
        f"{len(filtered_blocks)} Einträge (gefiltert von {len(blocks)}, "
        f"neueste zuerst)"
    ).classes("text-xs text-gray-400 mb-2")

    with ui.scroll_area().classes("w-full h-[600px] border rounded"):
        for block in filtered_blocks:
            for entry in block:
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
                        ui.label(entry.message).classes(
                            "text-xs font-mono text-gray-700 break-all"
                        )
                else:
                    ui.label(entry.message).classes(
                        "text-xs font-mono text-gray-500 pl-60 break-all"
                    )


def _render_log_content_wrapper(
    entries: list[LogEntry], min_level: str
) -> ui.column:
    """Erstellt einen Column-Container und rendert Logs hinein."""
    container = ui.column().classes("w-full gap-0")
    with container:
        _render_log_content(entries, min_level)
    return container
