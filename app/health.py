"""Health-Check-Funktionen für Subsystem-Prüfungen.

Seiteneffekt-frei: Wird sowohl vom Health-Check-Endpoint in main.py
als auch von der Einstellungsseite (settings.py) importiert.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


async def check_paperless_reachable(settings: Settings) -> dict[str, Any]:
    """Prüft ob die Paperless-ngx API erreichbar ist.

    Kein harter Fehler – der Classifier kann auch bei Paperless-Downtime
    laufen (wartet dann auf nächsten Polling-Zyklus).
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(
                f"{settings.paperless_url}/api/",
                headers={"Authorization": f"Token {settings.paperless_api_token}"},
            )
            if response.status_code == 200:
                return {"status": "ok", "url": settings.paperless_url}
            return {
                "status": "error",
                "url": settings.paperless_url,
                "http_status": response.status_code,
            }
    except httpx.RequestError as e:
        return {"status": "unreachable", "url": settings.paperless_url, "error": str(e)}


def check_api_key_present(settings: Settings) -> dict[str, Any]:
    """Prüft ob der Anthropic API-Key konfiguriert ist.

    Validiert nur das Vorhandensein, nicht die Gültigkeit
    (das würde einen API-Call kosten).
    """
    if settings.anthropic_api_key and settings.anthropic_api_key.startswith("sk-ant-"):
        return {"status": "ok", "key_prefix": settings.anthropic_api_key[:12] + "..."}
    return {"status": "not_configured"}


def check_sqlite_writable(settings: Settings) -> dict[str, Any]:
    """Prüft ob das Datenverzeichnis beschreibbar ist."""
    try:
        data_dir = settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return {"status": "ok", "path": str(data_dir)}
    except OSError as e:
        return {"status": "error", "path": str(settings.data_dir), "error": str(e)}
