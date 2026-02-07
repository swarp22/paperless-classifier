"""Kosten-Übersicht – Token-Verbrauch und Kosten pro Tag/Monat.

Zeigt:
- Monatskosten, Tageskosten, Limit-Anzeige
- Modell-Aufschlüsselung (Sonnet/Haiku/Opus)
- Tageskosten-Chart (letzte 30 Tage) via ECharts
- Durchschnittliche Kosten pro Dokument

Design-Referenz: Abschnitt 7.6 (Kosten-Dashboard)
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.ui.layout import page_layout

logger = get_logger("app")


# ---------------------------------------------------------------------------
# Daten laden
# ---------------------------------------------------------------------------

async def _load_cost_data() -> dict[str, Any]:
    """Lädt alle Kostendaten aus der Datenbank."""
    from app.config import get_settings
    from app.state import get_database

    settings = get_settings()
    data: dict[str, Any] = {
        "today_cost": 0.0,
        "month_cost": 0.0,
        "cost_limit": settings.monthly_cost_limit_usd,
        "month_docs": 0,
        "avg_per_doc": 0.0,
        "breakdown": {},
        "daily_series": [],
    }

    db = get_database()
    if db is None:
        return data

    try:
        data["today_cost"] = await db.get_daily_cost()
        data["month_cost"] = await db.get_monthly_cost()
        data["month_docs"] = await db.get_monthly_document_count()
        data["avg_per_doc"] = await db.get_avg_cost_per_document()
        data["breakdown"] = await db.get_model_breakdown()
        data["daily_series"] = await db.get_daily_cost_series(days=30)
    except Exception as exc:
        logger.warning("Kostendaten konnten nicht geladen werden: %s", exc)

    return data


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------

def _render_cost_summary(data: dict[str, Any]) -> None:
    """Zusammenfassung: Heute, Monat, Limit, Ø pro Dokument."""
    limit = data["cost_limit"]
    month_cost = data["month_cost"]
    pct = (month_cost / limit * 100) if limit > 0 else 0.0

    # Gleiche Kartenhöhe über items-stretch + h-full
    with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
        with ui.card().classes("flex-1 min-w-44 h-full"):
            ui.label("Heute").classes("text-sm text-gray-500")
            ui.label(f"${data['today_cost']:.2f}").classes(
                "text-2xl font-bold text-gray-800"
            )

        with ui.card().classes("flex-1 min-w-44 h-full"):
            ui.label("Dieser Monat").classes("text-sm text-gray-500")
            ui.label(f"${month_cost:.2f}").classes(
                "text-2xl font-bold text-gray-800"
            )
            ui.label(f"{data['month_docs']} Dokumente").classes(
                "text-sm text-gray-500"
            )

        with ui.card().classes("flex-1 min-w-44 h-full"):
            ui.label("Monatslimit").classes("text-sm text-gray-500")
            pct_display = f"{pct:.1f}%" if pct >= 0.1 or pct == 0 else "< 0.1%"
            ui.label(pct_display).classes("text-2xl font-bold text-gray-800")
            ui.label(f"${month_cost:.2f} / ${limit:.2f}").classes(
                "text-sm text-gray-500"
            )
            # Fortschrittsbalken ohne eingebettetes Zahlenlabel
            bar_color = "green" if pct < 70 else ("orange" if pct < 90 else "red")
            ui.linear_progress(
                value=min(pct / 100, 1.0),
                color=bar_color,
                size="8px",
                show_value=False,
            ).classes("mt-2")

        with ui.card().classes("flex-1 min-w-44 h-full"):
            ui.label("Ø pro Dokument").classes("text-sm text-gray-500")
            # Intelligente Formatierung: Cent-Bereich → 2 Nachkommastellen,
            # Sub-Cent → max 4 Stellen, aber ohne trailing zeros
            avg = data["avg_per_doc"]
            if avg >= 0.01:
                avg_str = f"${avg:.2f}"
            elif avg > 0:
                avg_str = f"${avg:.4f}".rstrip("0")
            else:
                avg_str = "$0.00"
            ui.label(avg_str).classes("text-2xl font-bold text-gray-800")


def _render_model_breakdown(breakdown: dict[str, dict[str, Any]]) -> None:
    """Modell-Aufschlüsselung als Karte."""
    with ui.card().classes("w-full"):
        ui.label("Modell-Aufschlüsselung (Monat)").classes(
            "text-sm text-gray-500 font-medium mb-3"
        )

        if not breakdown:
            ui.label("Keine Daten vorhanden.").classes("text-gray-400 italic")
            return

        # Modell-Infos mit Farben
        model_meta: dict[str, dict[str, str]] = {
            "sonnet": {"label": "Sonnet 4.5", "color": "bg-blue-100 text-blue-800"},
            "haiku": {"label": "Haiku 4.5", "color": "bg-teal-100 text-teal-800"},
            "opus": {"label": "Opus 4.5/4.6", "color": "bg-purple-100 text-purple-800"},
            "batch": {"label": "Batch", "color": "bg-gray-100 text-gray-800"},
        }

        for model_key, model_data in breakdown.items():
            meta = model_meta.get(
                model_key,
                {"label": model_key, "color": "bg-gray-100 text-gray-800"},
            )
            count = model_data.get("count", 0)
            cost = model_data.get("cost_usd")

            with ui.row().classes("items-center gap-3 py-1"):
                ui.badge(meta["label"]).classes(f"{meta['color']} text-xs px-2 py-1")
                ui.label(f"{count} Dokumente").classes("text-sm text-gray-700")
                if cost is not None:
                    ui.label(f"${cost:.2f}").classes("text-sm text-gray-500")


def _render_daily_chart(daily_series: list[Any]) -> None:
    """Tageskosten-Chart (letzte 30 Tage) mit ECharts."""
    with ui.card().classes("w-full"):
        ui.label("Kosten letzte 30 Tage").classes(
            "text-sm text-gray-500 font-medium mb-2"
        )

        if not daily_series:
            ui.label("Keine Daten vorhanden.").classes("text-gray-400 italic")
            return

        # Datum kürzen: "2026-02-07" → "07.02."
        dates = [
            f"{s.date[8:10]}.{s.date[5:7]}." if len(s.date) >= 10 else s.date
            for s in daily_series
        ]
        costs = [round(s.total_cost_usd, 4) for s in daily_series]
        doc_counts = [s.documents_processed for s in daily_series]

        ui.echart({
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "cross"},
            },
            "legend": {
                "data": ["Kosten ($)", "Dokumente"],
                "bottom": 0,
            },
            "grid": {
                "left": "8%",
                "right": "8%",
                "top": "10%",
                "bottom": "22%",
            },
            "xAxis": {
                "type": "category",
                "data": dates,
                "axisLabel": {
                    "rotate": 45,
                    "fontSize": 10,
                    "interval": 0,
                },
            },
            "yAxis": [
                {
                    "type": "value",
                    "name": "Kosten ($)",
                    "axisLabel": {"formatter": "${value}"},
                    "min": 0,
                },
                {
                    "type": "value",
                    "name": "Dokumente",
                    "min": 0,
                    "minInterval": 1,
                },
            ],
            "series": [
                {
                    "name": "Kosten ($)",
                    "type": "bar",
                    "data": costs,
                    "itemStyle": {"color": "#3b82f6"},
                    "yAxisIndex": 0,
                },
                {
                    "name": "Dokumente",
                    "type": "line",
                    "data": doc_counts,
                    "itemStyle": {"color": "#10b981"},
                    "yAxisIndex": 1,
                    "smooth": True,
                },
            ],
        }).classes("w-full h-80")


# ---------------------------------------------------------------------------
# Seiten-Definition
# ---------------------------------------------------------------------------

def register(app: Any = None) -> None:
    """Registriert die Kosten-Seite."""

    @ui.page("/costs")
    async def costs_page() -> None:
        with page_layout("Kosten"):
            data = await _load_cost_data()

            _render_cost_summary(data)
            _render_daily_chart(data["daily_series"])
            _render_model_breakdown(data["breakdown"])
