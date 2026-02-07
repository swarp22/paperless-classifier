"""Kosten-Übersicht – Token-Verbrauch und Kosten pro Tag/Monat.

Zeigt:
- Zusammenfassung: Heute, Diese Woche, Dieser Monat, Limit (Progressbar)
- Tageskosten-Chart (letzte 30 Tage) via ECharts
- Modell-Aufschlüsselung (Sonnet/Haiku/Opus) mit Kosten
- Durchschnittliche Kosten und Tokens pro Dokument
- Geschätzte Prompt-Cache-Ersparnis
- Auto-Refresh alle 30 Sekunden

Design-Referenz: Abschnitt 7.6 (Kosten-Dashboard)
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from app.logging_config import get_logger
from app.ui.layout import page_layout

logger = get_logger("app")

# Auto-Refresh-Intervall in Sekunden
_REFRESH_INTERVAL_S = 30.0


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
        "week_cost": 0.0,
        "week_docs": 0,
        "month_cost": 0.0,
        "cost_limit": settings.monthly_cost_limit_usd,
        "month_docs": 0,
        "avg_per_doc": 0.0,
        "avg_tokens": {"input": 0.0, "output": 0.0},
        "cache_savings": 0.0,
        "breakdown": {},
        "daily_series": [],
    }

    db = get_database()
    if db is None:
        return data

    try:
        data["today_cost"] = await db.get_daily_cost()
        data["week_cost"] = await db.get_weekly_cost()
        data["week_docs"] = await db.get_weekly_document_count()
        data["month_cost"] = await db.get_monthly_cost()
        data["month_docs"] = await db.get_monthly_document_count()
        data["avg_per_doc"] = await db.get_avg_cost_per_document()
        data["avg_tokens"] = await db.get_avg_tokens_per_document()
        data["cache_savings"] = await db.get_cache_savings()
        data["breakdown"] = await db.get_model_breakdown()
        data["daily_series"] = await db.get_daily_cost_series(days=30)
    except Exception as exc:
        logger.warning("Kostendaten konnten nicht geladen werden: %s", exc)

    return data


# ---------------------------------------------------------------------------
# Formatierungs-Helfer
# ---------------------------------------------------------------------------

def _format_usd(value: float) -> str:
    """Formatiert einen USD-Betrag – im Sub-Cent-Bereich genauer."""
    if value >= 0.01:
        return f"${value:.2f}"
    elif value > 0:
        return f"${value:.4f}".rstrip("0")
    return "$0.00"


def _format_tokens(value: float) -> str:
    """Formatiert Token-Zahlen mit Tausender-Punkt."""
    if value >= 1000:
        return f"{value:,.0f}".replace(",", ".")
    return f"{value:.0f}"


# ---------------------------------------------------------------------------
# UI-Komponenten
# ---------------------------------------------------------------------------

def _render_cost_summary(data: dict[str, Any]) -> None:
    """Zusammenfassung: Heute, Woche, Monat, Limit, Ø pro Dokument."""
    limit = data["cost_limit"]
    month_cost = data["month_cost"]
    pct = (month_cost / limit * 100) if limit > 0 else 0.0

    with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
        # Heute
        with ui.card().classes("flex-1 min-w-40 h-full"):
            ui.label("Heute").classes("text-sm text-gray-500")
            ui.label(_format_usd(data["today_cost"])).classes(
                "text-2xl font-bold text-gray-800"
            )

        # Diese Woche
        with ui.card().classes("flex-1 min-w-40 h-full"):
            ui.label("Diese Woche").classes("text-sm text-gray-500")
            ui.label(_format_usd(data["week_cost"])).classes(
                "text-2xl font-bold text-gray-800"
            )
            ui.label(f"{data['week_docs']} Dokumente").classes(
                "text-sm text-gray-500"
            )

        # Dieser Monat
        with ui.card().classes("flex-1 min-w-40 h-full"):
            ui.label("Dieser Monat").classes("text-sm text-gray-500")
            ui.label(_format_usd(month_cost)).classes(
                "text-2xl font-bold text-gray-800"
            )
            ui.label(f"{data['month_docs']} Dokumente").classes(
                "text-sm text-gray-500"
            )

        # Monatslimit
        with ui.card().classes("flex-1 min-w-40 h-full"):
            ui.label("Monatslimit").classes("text-sm text-gray-500")
            pct_display = f"{pct:.1f}%" if pct >= 0.1 or pct == 0 else "< 0.1%"
            ui.label(pct_display).classes("text-2xl font-bold text-gray-800")
            ui.label(f"{_format_usd(month_cost)} / {_format_usd(limit)}").classes(
                "text-sm text-gray-500"
            )
            bar_color = "green" if pct < 70 else ("orange" if pct < 90 else "red")
            ui.linear_progress(
                value=min(pct / 100, 1.0),
                color=bar_color,
                size="8px",
                show_value=False,
            ).classes("mt-2")

        # Ø pro Dokument
        with ui.card().classes("flex-1 min-w-40 h-full"):
            ui.label("Ø pro Dokument").classes("text-sm text-gray-500")
            ui.label(_format_usd(data["avg_per_doc"])).classes(
                "text-2xl font-bold text-gray-800"
            )
            avg_tok = data["avg_tokens"]
            if avg_tok["input"] > 0:
                ui.label(
                    f"{_format_tokens(avg_tok['input'])} in / "
                    f"{_format_tokens(avg_tok['output'])} out"
                ).classes("text-sm text-gray-500")


def _render_model_breakdown(
    breakdown: dict[str, dict[str, Any]],
    cache_savings: float,
) -> None:
    """Modell-Aufschlüsselung als Karte mit Kosten und Cache-Ersparnis."""
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

        # Gesamtkosten für Prozentberechnung
        total_cost = sum(
            m.get("cost_usd", 0.0) for m in breakdown.values()
        )

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
                    pct_str = ""
                    if total_cost > 0:
                        pct_str = f" ({cost / total_cost * 100:.0f}%)"
                    ui.label(f"{_format_usd(cost)}{pct_str}").classes(
                        "text-sm text-gray-500"
                    )

        # Cache-Ersparnis
        if cache_savings > 0:
            ui.separator().classes("my-2")
            with ui.row().classes("items-center gap-3 py-1"):
                ui.icon("savings").classes("text-green-600 text-lg")
                ui.label(
                    f"Prompt-Cache-Ersparnis: ~{_format_usd(cache_savings)} (geschätzt)"
                ).classes("text-sm text-green-700")


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
            # Container für den gesamten dynamischen Inhalt (Auto-Refresh)
            content = ui.column().classes("w-full gap-4")

            async def render_content() -> None:
                """Lädt Daten und rendert alle Kosten-Komponenten."""
                content.clear()
                data = await _load_cost_data()
                with content:
                    _render_cost_summary(data)
                    _render_daily_chart(data["daily_series"])
                    _render_model_breakdown(data["breakdown"], data["cache_savings"])

            # Initialer Render
            await render_content()

            # Auto-Refresh Timer (alle 30s)
            ui.timer(_REFRESH_INTERVAL_S, render_content)
