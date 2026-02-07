"""Dashboard – Startseite des Paperless Claude Classifiers.

Zeigt auf einen Blick:
- Poller-Status (aktiv/pausiert/gestoppt, letzte/nächste Ausführung)
- Zähler: Heute / Diese Woche / Dieser Monat
- Aktuelle Kosten mit Limit-Prozent
- Letzte verarbeitete Dokumente als Tabelle

Design-Referenz: Abschnitt 7.2 (Dashboard)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.ui.layout import POLLER_STATE_STYLES, page_layout

logger = get_logger("app")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _format_timestamp(ts: datetime | None) -> str:
    """Formatiert einen Timestamp für die Anzeige (lokal, deutsch)."""
    if ts is None:
        return "–"
    # In lokale Zeit konvertieren (Container-TZ)
    local = ts.astimezone()
    return local.strftime("%d.%m.%Y %H:%M:%S")


def _format_cost(usd: float) -> str:
    """Formatiert einen USD-Betrag."""
    return f"${usd:.2f}"


def _confidence_color(confidence: str) -> str:
    """Tailwind-Textfarbe für Confidence-Level."""
    return {
        "high": "text-green-700",
        "medium": "text-yellow-700",
        "low": "text-red-700",
    }.get(confidence, "text-gray-500")


def _status_badge(status: str) -> tuple[str, str]:
    """(Farbe, Label) für ki_status-Werte."""
    return {
        "classified": ("bg-green-100 text-green-800", "Klassifiziert"),
        "review": ("bg-yellow-100 text-yellow-800", "Review"),
        "error": ("bg-red-100 text-red-800", "Fehler"),
        "manual": ("bg-blue-100 text-blue-800", "Manuell"),
        "skipped": ("bg-gray-100 text-gray-800", "Übersprungen"),
    }.get(status, ("bg-gray-100 text-gray-800", status))


# ---------------------------------------------------------------------------
# Daten laden
# ---------------------------------------------------------------------------

async def _load_dashboard_data() -> dict[str, Any]:
    """Lädt alle Dashboard-Daten aus DB und Poller-Status.

    Gibt ein Dict mit allen benötigten Werten zurück.
    Fehlerresistent: Bei DB-Problemen werden Fallback-Werte genutzt.
    """
    from app.state import get_cost_tracker, get_database, get_poller

    data: dict[str, Any] = {
        "poller": None,
        "today_docs": 0,
        "week_docs": 0,
        "month_docs": 0,
        "today_cost": 0.0,
        "week_cost": 0.0,
        "month_cost": 0.0,
        "cost_limit": 25.0,
        "recent_docs": [],
    }

    # Poller-Status
    poller = get_poller()
    if poller is not None:
        data["poller"] = poller.status

    # Settings für Kostenlimit
    try:
        from app.config import get_settings
        data["cost_limit"] = get_settings().monthly_cost_limit_usd
    except Exception:
        pass

    # DB-Abfragen
    db = get_database()
    if db is None:
        return data

    try:
        data["today_docs"] = await db.get_today_document_count()
        data["today_cost"] = await db.get_daily_cost()
        data["week_docs"] = await db.get_weekly_document_count()
        data["week_cost"] = await db.get_weekly_cost()
        data["month_docs"] = await db.get_monthly_document_count()
        data["month_cost"] = await db.get_monthly_cost()
        data["recent_docs"] = await db.get_recent_documents(limit=20)
    except Exception as exc:
        logger.warning("Dashboard-Daten konnten nicht geladen werden: %s", exc)

    return data


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------

def _render_poller_status(poller_status: Any | None) -> None:
    """Rendert die Poller-Status-Karte."""
    with ui.card().classes("w-full"):
        ui.label("Poller").classes("text-sm text-gray-500 font-medium")

        if poller_status is None:
            ui.label("Nicht initialisiert").classes("text-gray-400 italic")
            return

        state = poller_status.state.value
        style = POLLER_STATE_STYLES.get(
            state,
            {"color": "text-gray-400", "icon": "help_outline", "label": state},
        )

        with ui.row().classes("items-center gap-2 mt-1"):
            ui.icon(style["icon"]).classes(f"{style['color']} text-2xl")
            ui.label(style["label"]).classes(f"{style['color']} text-lg font-semibold")

        # Details
        with ui.column().classes("gap-1 mt-2 text-sm text-gray-600"):
            ui.label(
                f"Letzte Ausführung: {_format_timestamp(poller_status.last_run_at)}"
            )
            ui.label(
                f"Nächster Lauf: {_format_timestamp(poller_status.next_run_at)}"
            )

            with ui.row().classes("gap-4"):
                ui.label(f"Verarbeitet: {poller_status.documents_processed}")
                ui.label(f"Fehler: {poller_status.documents_errored}")

            if poller_status.cost_limit_paused:
                ui.label("⚠ Kostenlimit erreicht – pausiert").classes(
                    "text-yellow-700 font-medium"
                )

            if poller_status.last_error:
                ui.label(f"Letzter Fehler: {poller_status.last_error}").classes(
                    "text-red-600 text-xs"
                )


def _render_counter_cards(data: dict[str, Any]) -> None:
    """Rendert die Zähler-Karten (Heute/Woche/Monat) und Kosten."""
    with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
        # Dokument-Zähler
        for label, doc_key, cost_key in [
            ("Heute", "today_docs", "today_cost"),
            ("Diese Woche", "week_docs", "week_cost"),
            ("Dieser Monat", "month_docs", "month_cost"),
        ]:
            with ui.card().classes("flex-1 min-w-48 h-full"):
                ui.label(label).classes("text-sm text-gray-500")
                ui.label(str(data[doc_key])).classes("text-3xl font-bold text-gray-800")
                ui.label(f"Dokumente · {_format_cost(data[cost_key])}").classes(
                    "text-sm text-gray-500"
                )

        # Kostenlimit-Karte
        limit = data["cost_limit"]
        month_cost = data["month_cost"]
        pct = (month_cost / limit * 100) if limit > 0 else 0.0

        with ui.card().classes("flex-1 min-w-48 h-full"):
            ui.label("Monatslimit").classes("text-sm text-gray-500")
            pct_display = f"{pct:.1f}%" if pct >= 0.1 or pct == 0 else "< 0.1%"
            ui.label(pct_display).classes("text-3xl font-bold text-gray-800")
            ui.label(
                f"{_format_cost(month_cost)} / {_format_cost(limit)}"
            ).classes("text-sm text-gray-500")

            # Fortschrittsbalken ohne eingebettetes Zahlenlabel
            bar_color = "green" if pct < 70 else ("orange" if pct < 90 else "red")
            ui.linear_progress(
                value=min(pct / 100, 1.0),
                color=bar_color,
                size="8px",
                show_value=False,
            ).classes("mt-2")


def _render_recent_documents(docs: list[dict[str, Any]]) -> None:
    """Rendert die Tabelle der letzten verarbeiteten Dokumente.

    Paperless-ID ist ein klickbarer Link zur Paperless-Detailseite.
    """
    from app.config import get_settings

    paperless_url = get_settings().paperless_url

    with ui.card().classes("w-full"):
        ui.label("Letzte Verarbeitungen").classes(
            "text-sm text-gray-500 font-medium mb-2"
        )

        if not docs:
            ui.label("Noch keine Dokumente verarbeitet.").classes(
                "text-gray-400 italic"
            )
            return

        columns = [
            {"name": "id", "label": "Paperless-ID", "field": "paperless_id",
             "align": "left", "sortable": True},
            {"name": "status", "label": "Status", "field": "status",
             "align": "left", "sortable": True},
            {"name": "confidence", "label": "Confidence", "field": "confidence",
             "align": "left", "sortable": True},
            {"name": "model", "label": "Modell", "field": "model_used",
             "align": "left"},
            {"name": "cost", "label": "Kosten", "field": "cost_usd",
             "align": "right"},
            {"name": "duration", "label": "Dauer", "field": "duration_seconds",
             "align": "right"},
            {"name": "time", "label": "Zeitpunkt", "field": "processed_at",
             "align": "left", "sortable": True},
        ]

        rows = []
        for doc in docs:
            # Modellname kürzen: "claude-sonnet-4-5-20250929" → "Sonnet 4.5"
            model_raw = doc.get("model_used", "")
            if "sonnet" in model_raw:
                model_short = "Sonnet 4.5"
            elif "haiku" in model_raw:
                model_short = "Haiku 4.5"
            elif "opus-4-6" in model_raw:
                model_short = "Opus 4.6"
            elif "opus" in model_raw:
                model_short = "Opus 4.5"
            else:
                model_short = model_raw[:20]

            # Timestamp kürzen
            ts_raw = doc.get("processed_at", "")
            ts_display = ts_raw[:19].replace("T", " ") if ts_raw else "–"

            # Dauer formatieren
            dur = doc.get("duration_seconds", 0) or 0
            dur_display = f"{dur:.1f}s" if dur else "–"

            rows.append({
                "paperless_id": doc.get("paperless_id", "–"),
                "status": doc.get("status", "–"),
                "confidence": doc.get("confidence", "–"),
                "model_used": model_short,
                "cost_usd": _format_cost(doc.get("cost_usd", 0)),
                "duration_seconds": dur_display,
                "processed_at": ts_display,
            })

        table = ui.table(
            columns=columns,
            rows=rows,
            row_key="paperless_id",
            pagination={"rowsPerPage": 10},
        ).classes("w-full")
        table.props("dense flat bordered")

        # Paperless-ID als klickbaren Link rendern
        table.add_slot(
            "body-cell-id",
            f'''
            <q-td :props="props">
                <a :href="'{paperless_url}/documents/' + props.row.paperless_id + '/details'"
                   target="_blank"
                   class="text-blue-600 hover:underline font-medium">
                    {{{{ props.row.paperless_id }}}}
                </a>
            </q-td>
            ''',
        )


# ---------------------------------------------------------------------------
# Seiten-Definition
# ---------------------------------------------------------------------------

def register(app: Any = None) -> None:
    """Registriert die Dashboard-Seite."""

    @ui.page("/")
    async def dashboard_page() -> None:
        with page_layout("Dashboard"):
            # Container für dynamischen Inhalt (Auto-Refresh)
            content = ui.column().classes("w-full gap-4")

            async def render_content() -> None:
                """Lädt Daten und rendert alle Dashboard-Komponenten."""
                content.clear()
                data = await _load_dashboard_data()
                with content:
                    _render_poller_status(data["poller"])
                    _render_counter_cards(data)
                    _render_recent_documents(data["recent_docs"])

            # Initialer Render
            await render_content()

            # Auto-Refresh Timer (alle 30s)
            ui.timer(30.0, render_content)
