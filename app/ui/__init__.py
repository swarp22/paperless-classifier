"""Web-UI Paket – NiceGUI-Seiten für den Paperless Claude Classifier.

Stellt eine zentrale `register_pages()`-Funktion bereit, die alle
UI-Seiten beim NiceGUI-Server registriert.  Wird von `main.py`
beim Start aufgerufen.
"""

from __future__ import annotations


def register_pages() -> None:
    """Registriert alle UI-Seiten beim NiceGUI-Server.

    Muss aufgerufen werden BEVOR `ui.run()` startet, damit die
    Routen beim Server-Start bekannt sind.
    """
    from app.ui import costs, dashboard, logs, review, settings

    dashboard.register()
    review.register()
    costs.register()
    settings.register()
    logs.register()
