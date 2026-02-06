"""Logging-Konfiguration für den Paperless Claude Classifier.

Setzt strukturiertes Logging auf mit:
- Console-Handler (stdout) für `docker logs`
- RotatingFileHandler für persistente Logs
- Logger-Hierarchie: paperless_classifier.{component}
  → app, classifier, paperless, claude, costs, schema_matrix

Alle Logger schreiben in dieselbe Datei mit Komponenten-Feld,
sodass per grep/Filter nach Komponente gesucht werden kann.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


# Basis-Logger-Name – alle Sublogger erben davon
ROOT_LOGGER_NAME = "paperless_classifier"

# Verfügbare Komponenten-Logger
COMPONENTS = ("app", "classifier", "paperless", "claude", "costs", "schema_matrix")

# Log-Format: Zeitstempel | Level | Komponente | Nachricht
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Rotation: 5 MB pro Datei, maximal 3 Dateien behalten (≈15 MB gesamt)
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(log_level: str = "INFO", log_dir: Path | None = None) -> None:
    """Konfiguriert das Logging-System.

    Args:
        log_level: Log-Level als String (DEBUG, INFO, WARNING, ERROR)
        log_dir: Verzeichnis für Log-Dateien. None = nur stdout.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Root-Logger für die Anwendung
    root_logger = logging.getLogger(ROOT_LOGGER_NAME)
    root_logger.setLevel(level)

    # Vorhandene Handler entfernen (bei erneutem Aufruf, z.B. in Tests)
    root_logger.handlers.clear()

    # Console-Handler: stdout für Docker-Logs
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Datei-Handler: nur wenn log_dir angegeben und beschreibbar
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "classifier.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError as e:
            root_logger.warning("Log-Verzeichnis nicht beschreibbar: %s – nur stdout aktiv", e)

    # Externe Libraries leiser stellen
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("nicegui").setLevel(logging.WARNING)


def get_logger(component: str) -> logging.Logger:
    """Gibt einen Logger für die angegebene Komponente zurück.

    Args:
        component: Name der Komponente (app, classifier, paperless, claude, costs, schema_matrix)

    Returns:
        Logger-Instanz mit Name 'paperless_classifier.{component}'
    """
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{component}")
