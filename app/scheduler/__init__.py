"""Scheduler – Automatische Dokumenterkennung und -verarbeitung.

Öffentliche API:
- Poller: Polling-basierte Dokumenterkennung (Tag "NEU")
- PollerState: Zustandsenum (stopped/running/paused/processing)
- PollerStatus: Aktueller Status für Dashboard und API
"""

from app.scheduler.poller import Poller, PollerState, PollerStatus

__all__ = [
    "Poller",
    "PollerState",
    "PollerStatus",
]
