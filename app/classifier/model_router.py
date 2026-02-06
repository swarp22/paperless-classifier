"""Model Router: Lokale PDF-Analyse und Modellwahl.

Analysiert ein PDF lokal (ohne LLM-Kosten) und entscheidet anhand der
Dokumenteigenschaften, welches Claude-Modell für die Klassifizierung
verwendet wird.

Entscheidungslogik (Design-Dokument Abschnitt 5.4):
- Sonnet 4.5: Scans, Bilder-PDFs, >5 Seiten, unbekannte Absender, Stempel
- Haiku 4.5:  Bekannte Korrespondenten, saubere digitale PDFs, ≤5 Seiten

Die Analyse nutzt PyMuPDF (fitz) und verursacht keine API-Kosten.
"""

from __future__ import annotations

from dataclasses import dataclass

import fitz  # PyMuPDF

from app.logging_config import get_logger

logger = get_logger("classifier.router")

# Modell-Strings (zentral definiert, damit Änderungen nur hier nötig sind)
MODEL_SONNET = "claude-sonnet-4-5-20250929"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Schwellwerte für die Analyse
TEXT_THRESHOLD = 50       # Zeichen auf Seite 1 – darunter gilt als Scan
PAGE_THRESHOLD = 5        # Ab dieser Seitenzahl wird Sonnet verwendet


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class PdfAnalysis:
    """Ergebnis der lokalen PDF-Analyse.

    Wird ohne LLM-Aufruf aus den PDF-Metadaten und der ersten Seite
    ermittelt.  Dient als Input für die Modellwahl und wird im
    PipelineResult für Logging/Dashboard gespeichert.
    """
    page_count: int
    is_image_pdf: bool       # Scan statt Digital-PDF (kaum Text, aber Bilder)
    has_text_layer: bool     # OCR-Text vorhanden?
    file_size_mb: float      # Dateigröße in MB
    first_page_text_len: int  # Zeichenanzahl auf Seite 1 (für Debugging)
    first_page_image_count: int  # Anzahl Bilder auf Seite 1


@dataclass
class RoutingDecision:
    """Ergebnis der Modellwahl.

    Enthält das gewählte Modell und eine menschenlesbare Begründung
    für Logging und Dashboard.
    """
    model: str
    reason: str


# ---------------------------------------------------------------------------
# PDF-Analyse
# ---------------------------------------------------------------------------

def analyze_pdf(pdf_bytes: bytes) -> PdfAnalysis:
    """Analysiert ein PDF lokal ohne LLM-Aufruf.

    Öffnet das PDF mit PyMuPDF und extrahiert:
    - Seitenanzahl
    - Textmenge auf Seite 1
    - Bilderanzahl auf Seite 1
    - Ob es ein Scan (Image-PDF) oder ein digitales PDF ist

    Args:
        pdf_bytes: Rohinhalt der PDF-Datei.

    Returns:
        PdfAnalysis mit allen ermittelten Eigenschaften.

    Raises:
        ValueError: Wenn das PDF nicht geöffnet werden kann.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"PDF konnte nicht geöffnet werden: {exc}") from exc

    try:
        page_count = len(doc)

        # Erste Seite analysieren
        if page_count > 0:
            first_page = doc[0]
            text = first_page.get_text().strip()
            images = first_page.get_images()
            first_page_text_len = len(text)
            first_page_image_count = len(images)
        else:
            first_page_text_len = 0
            first_page_image_count = 0

        file_size_mb = len(pdf_bytes) / (1024 * 1024)

        # Scan-Erkennung: Kaum Text auf Seite 1, aber Bilder vorhanden
        is_image_pdf = (
            first_page_text_len < TEXT_THRESHOLD
            and first_page_image_count > 0
        )
        has_text_layer = first_page_text_len >= TEXT_THRESHOLD

        analysis = PdfAnalysis(
            page_count=page_count,
            is_image_pdf=is_image_pdf,
            has_text_layer=has_text_layer,
            file_size_mb=round(file_size_mb, 2),
            first_page_text_len=first_page_text_len,
            first_page_image_count=first_page_image_count,
        )

        logger.debug(
            "PDF-Analyse: %d Seiten, %.2f MB, image_pdf=%s, text=%d Zeichen, %d Bilder",
            page_count, file_size_mb, is_image_pdf,
            first_page_text_len, first_page_image_count,
        )

        return analysis

    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Modellwahl
# ---------------------------------------------------------------------------

def select_model(
    pdf_analysis: PdfAnalysis,
    *,
    correspondent_known: bool = False,
    expects_stamp: bool = False,
    force_model: str | None = None,
) -> RoutingDecision:
    """Wählt das optimale Modell basierend auf Dokumenteigenschaften.

    Die Logik prüft Kriterien in absteigender Priorität.  Sobald ein
    Kriterium für Sonnet zutrifft, wird Sonnet gewählt.  Nur wenn
    alle Kriterien auf ein einfaches Dokument hindeuten, kommt Haiku
    zum Einsatz.

    Args:
        pdf_analysis: Ergebnis von analyze_pdf().
        correspondent_known: True wenn der Korrespondent bereits in
            Paperless zugewiesen ist.
        expects_stamp: True wenn ein Paginierstempel erwartet wird
            (typisch bei gescannten Dokumenten).
        force_model: Optionaler Override – überspringt die Routing-Logik.

    Returns:
        RoutingDecision mit gewähltem Modell und Begründung.
    """
    # Override: Nutzer oder PipelineConfig erzwingt ein bestimmtes Modell
    if force_model:
        return RoutingDecision(
            model=force_model,
            reason=f"Manuell erzwungen: {force_model}",
        )

    # Kriterium 1: Scan / Bild-PDF → Sonnet (bessere Vision-Qualität)
    if pdf_analysis.is_image_pdf:
        return RoutingDecision(
            model=MODEL_SONNET,
            reason="Bild-PDF / Scan erkannt (wenig Text, Bilder vorhanden)",
        )

    # Kriterium 2: Viele Seiten → Sonnet (komplexeres Dokument)
    if pdf_analysis.page_count > PAGE_THRESHOLD:
        return RoutingDecision(
            model=MODEL_SONNET,
            reason=f"Mehrseitiges Dokument ({pdf_analysis.page_count} Seiten > {PAGE_THRESHOLD})",
        )

    # Kriterium 3: Unbekannter Absender → Sonnet (braucht mehr Analyse)
    if not correspondent_known:
        return RoutingDecision(
            model=MODEL_SONNET,
            reason="Kein Korrespondent zugewiesen (unbekannter Absender)",
        )

    # Kriterium 4: Paginierstempel erwartet → Sonnet (Vision nötig)
    if expects_stamp:
        return RoutingDecision(
            model=MODEL_SONNET,
            reason="Paginierstempel erwartet (Stempel-Erkennung benötigt Vision)",
        )

    # Alle Kriterien sprechen für ein einfaches Dokument → Haiku
    return RoutingDecision(
        model=MODEL_HAIKU,
        reason="Bekannter Korrespondent, digitales PDF, ≤5 Seiten → Haiku ausreichend",
    )
