#!/usr/bin/env python3
"""Manueller Trigger fuer Schema-Analyse (AP-11b Test).

Aufruf im Container:
  python3 /app/trigger_schema.py

Erstellt PaperlessClient, ClaudeClient, Database aus den
Environment-Variablen und fuehrt einen Schema-Analyse-Lauf durch.
"""
import asyncio
import logging
import sys
import os

# Logging auf INFO damit man den Fortschritt sieht
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trigger_schema")


async def main() -> int:
    from app.config import Settings
    from app.db.database import Database
    from app.paperless.client import PaperlessClient
    from app.claude.client import ClaudeClient
    from app.schema_matrix.analyzer import SchemaAnalyzer

    settings = Settings()
    logger.info("Settings geladen: model=%s", settings.schema_matrix_model)

    # Database
    db = Database(str(settings.db_path))
    await db.initialize()
    logger.info("Datenbank initialisiert")

    # PaperlessClient
    paperless = PaperlessClient(
        base_url=str(settings.paperless_url),
        token=settings.paperless_token,
    )
    await paperless.initialize()
    logger.info("PaperlessClient verbunden")

    # ClaudeClient
    claude = ClaudeClient(api_key=settings.anthropic_api_key)
    logger.info("ClaudeClient erstellt")

    # Schema-Analyse
    analyzer = SchemaAnalyzer(
        paperless=paperless,
        claude=claude,
        database=db,
        model=settings.schema_matrix_model,
    )

    logger.info("Starte Schema-Analyse (manuell)...")
    run_record = await analyzer.run(trigger_type="manual")

    # Ergebnis ausgeben
    print("\n" + "=" * 60)
    print(f"Status:            {run_record.status}")
    print(f"Dokumente:         {run_record.total_documents}")
    print(f"Titel-Schemata:    {run_record.title_schemas_created} neu, "
          f"{run_record.title_schemas_updated} aktualisiert")
    print(f"Pfad-Regeln:       {run_record.path_rules_created} neu, "
          f"{run_record.path_rules_updated} aktualisiert")
    print(f"Mappings:          {run_record.mappings_created} neu, "
          f"{run_record.mappings_updated} aktualisiert")
    print(f"Tag-Regeln:        {run_record.tag_rules_created} neu, "
          f"{run_record.tag_rules_updated} aktualisiert")
    print(f"Manuell beibeh.:   {run_record.manual_entries_preserved}")
    print(f"Kosten:            ${run_record.cost_usd:.4f}")
    if run_record.error_message:
        print(f"Fehler:            {run_record.error_message}")
    print("=" * 60)

    # Tag-Regeln anzeigen
    from app.schema_matrix.storage import SchemaStorage
    storage = SchemaStorage(db)
    tag_rules = await storage.get_all_tag_rules()
    if tag_rules:
        print(f"\nTag-Regeln in DB ({len(tag_rules)}):")
        for tr in tag_rules:
            corr = tr.correspondent or "(alle)"
            print(f"  {corr} + {tr.document_type}:")
            if tr.positive_tags:
                print(f"    + {', '.join(tr.positive_tags)}")
            if tr.negative_tags:
                print(f"    - {', '.join(tr.negative_tags)}")
            if tr.reasoning:
                print(f"    Grund: {tr.reasoning}")
    else:
        print("\nKeine Tag-Regeln generiert.")

    await paperless.close()
    await db.close()

    return 0 if run_record.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
