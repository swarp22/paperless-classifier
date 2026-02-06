"""Webhook-Endpunkt für Paperless-ngx (Phase 2).

Paperless-ngx unterstützt seit v2.14 Webhook Workflow Actions.
Damit kann Paperless bei neuen Dokumenten einen HTTP-Request an
diesen Endpunkt senden – als Alternative oder Ergänzung zum Polling.

Phase 2: Implementierung mit Paperless Webhook-Konfiguration,
Signaturprüfung und direktem Pipeline-Aufruf.
"""

from __future__ import annotations

from nicegui import app

from app.logging_config import get_logger

logger = get_logger("scheduler.webhook")


@app.post("/api/webhook")
async def handle_webhook(document_id: int) -> dict[str, str]:
    """Empfängt Webhook-Benachrichtigungen von Paperless-ngx.

    Paperless sendet die Document-ID wenn ein neues Dokument
    hinzugefügt oder ein Workflow-Trigger ausgelöst wird.

    Args:
        document_id: Paperless Dokument-ID aus dem Webhook-Payload.

    Returns:
        Status-Meldung als JSON.

    Raises:
        NotImplementedError: Phase 2 – noch nicht implementiert.
    """
    logger.info("Webhook empfangen für Dokument %d (nicht implementiert)", document_id)
    raise NotImplementedError(
        "Webhook-Verarbeitung ist für Phase 2 vorgesehen. "
        "Aktuell wird Polling verwendet (siehe Poller)."
    )
