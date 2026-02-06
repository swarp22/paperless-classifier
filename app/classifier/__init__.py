"""Classifier Core – Kernlogik der Dokumentklassifizierung.

Öffentliche API:
- ClassificationPipeline: Orchestrierung des gesamten Ablaufs
- PipelineResult: Ergebnis eines Pipeline-Durchlaufs
- PipelineConfig: Konfigurierbare Optionen
- resolve_classification: Name→ID Mapping
- evaluate_confidence: Confidence-Bewertung
- analyze_pdf / select_model: PDF-Analyse und Modellwahl
"""

from app.classifier.confidence import (
    ApplyAction,
    ConfidenceEvaluation,
    evaluate_confidence,
)
from app.classifier.model_router import (
    PdfAnalysis,
    RoutingDecision,
    analyze_pdf,
    select_model,
)
from app.classifier.pipeline import (
    ClassificationPipeline,
    PipelineConfig,
    PipelineResult,
)
from app.classifier.resolver import (
    ResolvedClassification,
    resolve_classification,
)

__all__ = [
    # Pipeline
    "ClassificationPipeline",
    "PipelineConfig",
    "PipelineResult",
    # Resolver
    "ResolvedClassification",
    "resolve_classification",
    # Confidence
    "ConfidenceEvaluation",
    "ApplyAction",
    "evaluate_confidence",
    # Model Router
    "PdfAnalysis",
    "RoutingDecision",
    "analyze_pdf",
    "select_model",
]
