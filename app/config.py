"""Konfigurationsmanagement mit Pydantic Settings.

Lädt Konfiguration aus Environment-Variablen und .env-Datei.
Validiert Pflichtfelder und setzt sinnvolle Defaults für optionale Felder.
"""

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProcessingMode(str, Enum):
    """Verarbeitungsmodus für neue Dokumente."""
    IMMEDIATE = "immediate"   # Sofort per API verarbeiten
    BATCH = "batch"           # Sammeln und per Batch API senden
    HYBRID = "hybrid"         # Eilige sofort, Rest als Batch


class SchemaMatrixSchedule(str, Enum):
    """Zeitplan für die Schema-Matrix-Analyse."""
    WEEKLY = "weekly"
    MANUAL = "manual"


class LogLevel(str, Enum):
    """Erlaubte Log-Level."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    """Zentrale Konfiguration des Paperless Claude Classifiers.

    Pflichtfelder (müssen in .env oder ENV gesetzt sein):
    - PAPERLESS_URL
    - PAPERLESS_API_TOKEN
    - ANTHROPIC_API_KEY
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # ENV-Variablen haben Vorrang vor .env-Datei
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    # --- Paperless-ngx Verbindung ---
    paperless_url: str = Field(
        ...,
        description="URL der Paperless-Instanz (z.B. http://192.168.178.73:8000)",
    )
    paperless_api_token: str = Field(
        ...,
        description="API-Token aus Paperless Web-UI → My Profile",
    )

    # --- Claude API ---
    # Optional bis AP-03 (Claude API Client) – Container startet auch ohne
    anthropic_api_key: Optional[str] = Field(
        default=None,
        description="API-Key aus der Anthropic Console (ab AP-03 erforderlich)",
    )

    # --- Modellwahl ---
    default_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Standard-Modell für Einzelklassifizierung",
    )
    batch_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Modell für Batch-Verarbeitung",
    )
    schema_matrix_model: str = Field(
        default="claude-opus-4-6",
        description="Modell für Schema-Matrix-Analyse (benötigt höchste Qualität)",
    )

    # --- Schema-Matrix ---
    schema_matrix_schedule: SchemaMatrixSchedule = Field(
        default=SchemaMatrixSchedule.WEEKLY,
        description="Zeitplan für automatische Schema-Analyse",
    )
    schema_matrix_threshold: int = Field(
        default=20,
        ge=1,
        description="Anzahl neuer Dokumente bis zur automatischen Schema-Analyse",
    )
    schema_matrix_min_interval_h: int = Field(
        default=24,
        ge=1,
        description="Mindestabstand in Stunden zwischen automatischen Schema-Analysen",
    )

    # --- Polling & Verarbeitung ---
    polling_interval_seconds: int = Field(
        default=300,
        ge=10,
        description="Intervall in Sekunden zwischen Polling-Durchläufen",
    )
    processing_mode: ProcessingMode = Field(
        default=ProcessingMode.IMMEDIATE,
        description="Verarbeitungsmodus: immediate, batch, hybrid",
    )

    # --- Kosten ---
    monthly_cost_limit_usd: float = Field(
        default=25.0,
        ge=0.0,
        description="Monatliches Kostenlimit in USD (0 = kein Limit)",
    )

    # --- Logging ---
    log_level: LogLevel = Field(
        default=LogLevel.INFO,
        description="Log-Level für die Anwendung",
    )

    # --- Pfade (intern, nicht konfigurierbar via ENV) ---
    data_dir: Path = Field(
        default=Path("/app/data"),
        description="Verzeichnis für SQLite-DB und Logs",
    )

    @field_validator("paperless_url")
    @classmethod
    def validate_paperless_url(cls, v: str) -> str:
        """Trailing Slash entfernen, damit URL-Joins konsistent funktionieren."""
        return v.rstrip("/")

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_api_key_format(cls, v: Optional[str]) -> Optional[str]:
        """Grundlegende Formatprüfung des API-Keys (falls gesetzt)."""
        if v is not None and not v.startswith("sk-ant-"):
            raise ValueError(
                "ANTHROPIC_API_KEY muss mit 'sk-ant-' beginnen. "
                "Bitte Key aus der Anthropic Console prüfen."
            )
        return v

    @property
    def db_path(self) -> Path:
        """Pfad zur SQLite-Datenbank."""
        return self.data_dir / "classifier.db"

    @property
    def log_dir(self) -> Path:
        """Pfad zum Log-Verzeichnis."""
        return self.data_dir / "logs"


# Singleton-Pattern: wird einmalig beim Import erstellt
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Gibt die Settings-Instanz zurück (Lazy Singleton).

    Wird beim ersten Aufruf erstellt und danach wiederverwendet.
    Wirft ValidationError, wenn Pflichtfelder fehlen.
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
