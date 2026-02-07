"""Schema-Analyse-Paket (AP-10, Phase 3).

Drei Module:
- collector: Sammelt und gruppiert Paperless-Daten (kein LLM)
- storage: CRUD für Schema-Analyse-Tabellen in SQLite
- trigger: Entscheidet ob eine Analyse ausgelöst werden soll

Das Opus-LLM-Modul (Analyse + Prompt-Builder) folgt in AP-11.
Die Web-UI folgt in AP-12.
"""

from app.schema_matrix.collector import CollectorResult, SchemaCollector
from app.schema_matrix.storage import (
    AnalysisRunRecord,
    MappingEntry,
    PathRule,
    SchemaStorage,
    TitlePattern,
)
from app.schema_matrix.trigger import SchemaTrigger

__all__ = [
    "AnalysisRunRecord",
    "CollectorResult",
    "MappingEntry",
    "PathRule",
    "SchemaCollector",
    "SchemaStorage",
    "SchemaTrigger",
    "TitlePattern",
]
