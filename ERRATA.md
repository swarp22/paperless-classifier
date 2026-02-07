# Errata & Erkenntnisse – Paperless Claude Classifier

Laufende Sammlung von Korrekturen und Abweichungen gegenüber der Design-Dokumentation.

---

## E-001: Custom Field Select-Options API-Format (AP-00, 2025-02-06)

**Betrifft:** `paperless-claude-classifier-design_4.md`, `00-setup-custom-fields.md`

**Problem:** Die API-Beispiele nutzen einfache Strings für `select_options`:
```json
"select_options": ["Max", "Melanie", "Kilian"]
```

**Korrekt für Paperless-ngx v2.20.6:** Select-Options müssen als Objekte mit `label` übergeben werden. Die `id` wird serverseitig automatisch generiert:
```json
"select_options": [
  {"label": "Max"},
  {"label": "Melanie"},
  {"label": "Kilian"}
]
```

**Auswirkung:** POST auf `/api/custom_fields/` gibt 500er zurück, wenn das alte Format verwendet wird.

**Relevanz für Classifier-Code:** Beim Setzen von Custom Field Werten muss ggf. die interne `id` der Option verwendet werden, nicht der Label-String. Beim Lesen kommen Objekte mit `id` und `label` zurück.

---

## E-002: Tatsächliche Custom Field IDs (AP-00, 2025-02-06)

**Betrifft:** `00-setup-custom-fields.md`, Tabelle in Aufgabe 0.4

| Custom Field | ID | Typ | Optionen |
|---|---|---|---|
| Dokumenteverknüpfung | 1 | documentlink | — |
| Ordnerarchiv Paginierung | 2 | integer | — |
| Ordner Haus Ordnungszahl | 4 | integer | — |
| Ordner Haus Register | 5 | select | 1–11 (Haus-Kategorien) |
| zusammenhängender Vorgang | 6 | select | (dynamisch) |
| **Person** | **7** | select | Max, Melanie, Kilian |
| **ki_status** | **8** | select | classified, review, error, manual, skipped |

**Hinweis:** ID 3 fehlt – vermutlich ein früher gelöschtes Feld.

---

## E-003: Heredoc funktioniert nicht zuverlässig über Terminus/SSH (AP-00, 2025-02-06)

**Betrifft:** Alle Arbeitspakete mit mehrzeiligen Shell-Befehlen

**Problem:** `cat > datei << 'EOF'` und mehrzeilige curl-Befehle mit `\`-Zeilenumbrüchen werden in Terminus (SSH-Client) nicht korrekt übernommen.

**Workaround:** Dateien direkt mit `nano` bearbeiten statt heredoc zu nutzen. Mehrzeilige curl-Befehle in eine einzelne Zeile zusammenfassen.

**Relevanz:** Betrifft alle zukünftigen Anleitungen, die Shell-Snippets enthalten. Befehle immer als Einzeiler formulieren oder `nano`-Anweisungen geben.

---

## E-004: Projektpfad angepasst für CasaOS-Backup (AP-00, 2025-02-06)

**Betrifft:** Alle Arbeitspakete und Design-Dokumentation

**Dokumentiert:** `~/docker/paperless-classifier`

**Tatsächlich:** `/DATA/AppData/paperless-classifier`

**Grund:** Unter `/DATA/AppData/` liegende Verzeichnisse werden vom CasaOS-Backup-Konzept erfasst.

**Relevanz:** Alle Pfadangaben in Anleitungen und Code (Docker-Volumes, Mounts, etc.) müssen den tatsächlichen Pfad verwenden.

---

## E-005: libgl1-mesa-glx nicht mehr verfügbar in Debian Trixie (AP-01, 2025-02-06)

**Betrifft:** `Dockerfile`

**Problem:** `python:3.11-slim` basiert inzwischen auf Debian Trixie. Das Paket `libgl1-mesa-glx` wurde entfernt und ist nicht mehr installierbar. `docker compose build` bricht mit `E: Package 'libgl1-mesa-glx' has no installation candidate` ab.

**Lösung:** Paketname im Dockerfile ersetzen:
```
# Alt (funktioniert nicht mehr):
libgl1-mesa-glx
# Neu:
libgl1
```

**Relevanz:** Betrifft den Dockerfile-Build auf allen Plattformen (ARM64 und x86). Bei zukünftigen Änderungen am Dockerfile beachten.

---

## E-006: Model Router aus AP-03 nach AP-04 verschoben (AP-03, 2026-02-06)

**Betrifft:** `naechster-chat-kontext-ap03_1.md` (Kernaufgaben-Liste), Design-Dokument Abschnitt 5.4

**Problem:** Das Kontext-Dokument listet "Model Router: Dokumenteigenschaften → Modellwahl" als Kernaufgabe von AP-03 (Claude API Client). Der Model Router benötigt jedoch:

1. **PyMuPDF (`fitz`)** für lokale PDF-Analyse (`is_image_pdf`, `page_count`) – neue Dependency
2. **Zugriff auf Paperless-Metadaten** (`correspondent_known`, `expects_stamp`) – Abhängigkeit zum Paperless-Client
3. **Architektonisch:** Das Design-Dokument platziert den Router unter `classifier/model_router.py`, nicht unter `claude/`

Der Model Router gehört zur Classifier-Pipeline, nicht zum API-Client.

**Entscheidung:** Model Router wird in AP-04 (Classifier Core) implementiert, wo er architektonisch hingehört. Der `ClaudeClient` in AP-03 akzeptiert ein beliebiges Modell als Parameter – die Entscheidung *welches* Modell trifft der Aufrufer.

**Batch API:** Die Methodensignaturen `batch_classify()` und `get_batch_results()` sind im Client als Schnittstelle definiert, der Body ist als `TODO Phase 4` markiert (`NotImplementedError`). So ist die Schnittstelle dokumentiert, ohne dass Phase-4-Logik in Phase-1-Code landet.

**Relevanz:** AP-04 muss den Model Router (`classifier/model_router.py`) und die PyMuPDF-Dependency umsetzen. Die `requirements.txt` wird erst dann um `PyMuPDF` erweitert.

---

## E-007: Preistabelle aktualisiert – Opus massiv günstiger, Opus 4.6 neu (AP-03, 2026-02-06)

**Betrifft:** `cost_tracker.py`, Design-Dokument Abschnitte 2.3 und 5.5

**Problem:** Das Design-Dokument enthält veraltete Preise für Opus 4.5:

| Modell | Design-Dokument | Tatsächlich (06.02.2026) |
|---|---|---|
| Opus 4.5 Input | $15.00/MTok | **$5.00/MTok** |
| Opus 4.5 Output | $75.00/MTok | **$25.00/MTok** |
| Opus 4.5 Cache Read | $1.50/MTok | **$0.50/MTok** |
| Opus 4.5 Cache Write | $18.75/MTok | **$6.25/MTok (5m) / $10.00/MTok (1h)** |

Opus ist damit um Faktor 3 günstiger als angenommen. Die Kosten pro Schema-Analyse-Lauf sinken von ~$0.73 auf ~$0.24.

**Neue Modelle:**
- **Claude Opus 4.6** (`claude-opus-4-6`): Gleiche Preise wie Opus 4.5. Neues Flaggschiff-Modell, heute veröffentlicht.

**Strukturelle Änderung Cache Write:**
Anthropic hat zwei Cache-Write-Stufen eingeführt:
- **5m** (5 Minuten, `cache_control: {"type": "ephemeral"}`): Günstiger, unser Standard
- **1h** (1 Stunde): Teurer, aktuell nicht genutzt

`ModelPricing` hat jetzt `cache_write_5m_per_mtok` und `cache_write_1h_per_mtok` statt eines einzelnen `cache_write_per_mtok`. `calculate_cost()` akzeptiert `cache_ttl="5m"|"1h"`.

**Änderung in config.py (durchgeführt):** `schema_matrix_model` von `claude-opus-4-5-20251101` auf `claude-opus-4-6` geändert. Gleiches Preisniveau, neueres Modell. Beide bleiben in der Preistabelle hinterlegt.

---

## E-009: Race Condition bei Multi-PATCH – NEU-Tag wird nicht entfernt (AP-05, 2026-02-06)

**Betrifft:** `app/classifier/pipeline.py`, Methode `_apply_result()`

**Problem:** Im ersten Live-Test wurden 2 von 10 Dokumenten doppelt verarbeitet. Der NEU-Tag wurde trotz erfolgreicher Klassifizierung nicht entfernt. Beim nächsten Polling-Zyklus erkannte der Poller diese Dokumente erneut als "neu" und verarbeitete sie ein zweites Mal (zusätzliche API-Kosten, Titel-Überschreibung).

**Ursache:** `_apply_result()` führte 2–4 separate PATCH-Aufrufe gegen die Paperless-API aus:

1. PATCH: Titel, Korrespondent, Typ, Pfad, Tags (NEU entfernt)
2. PATCH: Custom Field `ki_status` setzen
3. PATCH: Custom Field `Person` setzen (falls aufgelöst)
4. PATCH: Custom Field `Paginierung` entfernen (falls digital)

Jeder `set_custom_field`-Aufruf lud das Dokument frisch und sendete einen separaten PATCH mit nur `custom_fields`. Race Condition: Wenn PATCH 2+ vor dem vollständigen Commit von PATCH 1 ausgeführt wurde, konnte Paperless-ngx den alten Tag-Zustand (mit NEU) zurückschreiben.

**Lösung:** Alle Änderungen (Metadaten + Tags + Custom Fields) in einem einzigen PATCH zusammengefasst. Custom Fields werden nicht mehr über die `set_custom_field()`-Hilfsmethode gesetzt, sondern direkt im Payload:

```python
# Vorher: 2-4 separate PATCH-Aufrufe
await self._paperless.update_document(doc_id, title=..., tags=..., ...)
await self._paperless.set_custom_field_by_label(doc_id, CF_KI_STATUS, ...)
await self._paperless.set_custom_field(doc_id, cf.field_id, cf.value)

# Nachher: 1 einziger PATCH-Aufruf
patch["tags"] = sorted(current_tags)
patch["custom_fields"] = [{"field": fid, "value": val} for fid, val in cf_map.items()]
await self._paperless.update_document(doc_id, **patch)
```

**Nicht-deterministisch:** Nur 2 von 10 Dokumenten betroffen – typisch für Race Conditions. Hängt von Paperless-DB-Last und Timing ab.

---

## E-010: Rate-Limit-Handling – Dokument nicht als Error markieren (AP-05, 2026-02-06)

**Betrifft:** `app/classifier/pipeline.py`, `app/scheduler/poller.py`

**Problem:** Bei einem HTTP 429 (Rate Limit) von der Claude API wurde das betroffene Dokument als `ki_status=error` markiert und der NEU-Tag entfernt. Der Poller machte dann mit dem nächsten Dokument weiter – das ebenfalls ein Rate-Limit bekam. Ergebnis: Alle verbleibenden Dokumente im Zyklus wurden fälschlich als Error markiert. User musste bei jedem manuell den NEU-Tag wieder setzen.

**Ursache:** Die Pipeline fing alle `ClaudeError`-Exceptions gleich und rief `_set_error_status()` auf – egal ob permanenter Fehler (ungültige Antwort) oder temporärer Fehler (Rate-Limit). Der Poller hatte keine Möglichkeit, zwischen beiden zu unterscheiden.

**Lösung (zwei Teile):**

1. **Pipeline** (pipeline.py): Bei HTTP 429/529 wird die Exception **nicht** gefangen, sondern an den Poller weitergeworfen. `_set_error_status()` wird NICHT aufgerufen – NEU-Tag bleibt, ki_status bleibt null.

2. **Poller** (poller.py):
   - Fängt `ClaudeAPIError` mit `status_code in (429, 529)` explizit
   - Bricht den gesamten Zyklus ab (nicht nur das eine Dokument)
   - Verbleibende Dokumente werden beim nächsten Zyklus automatisch verarbeitet
   - `DOCUMENT_DELAY_SECONDS = 2.0` – Pause zwischen Dokumenten verhindert Bursts

**Unterschied zum bisherigen Verhalten:**

| Szenario | Vorher | Nachher |
|---|---|---|
| HTTP 429 bei Dokument X | ki_status=error, NEU entfernt, nächstes Dok. | Zyklus abgebrochen, Dok. unverändert |
| Nächster Polling-Zyklus | Dok. X wird ignoriert | Dok. X wird erneut versucht |
| User-Eingriff nötig? | Ja (NEU-Tag manuell setzen) | Nein |

---

## E-011: Haus-Register wird bei digitalen Dokumenten fälschlich gesetzt (AP-05, 2026-02-06)

**Betrifft:** `app/classifier/resolver.py`, `app/classifier/pipeline.py`

**Problem:** Claude setzt `is_house_folder_candidate: true` und `house_register` bei digitalen PDFs mit Speicherpfad "Haus Bietigheim / ...". Der Resolver prüfte nur `is_house_folder_candidate and house_register`, nicht aber `is_scanned_document`. Ergebnis: Alle Strom-, Gas-, Darlehensdokumente bekamen ein Haus-Register zugewiesen, obwohl sie nie physisch abgelegt werden.

**Design-Vorgabe (Abschnitt 13.6.1):** "Digital-native → Haus-Felder: ENTFERNEN". Haus-Ordner-Kandidat nur bei: gescanntes Dokument + kein Paginierstempel + Pfad beginnt mit "Haus Bietigheim".

**Lösung (zwei Teile):**

1. **Resolver** (resolver.py): Guard erweitert – Haus-Register wird nur aufgelöst wenn `is_scanned_document=true` UND `pagination_stamp=null`. Bei digitalen Dokumenten wird `is_house_folder_candidate` ignoriert.

2. **Pipeline** (`_apply_result`): Bestehende Haus-Felder (Register + Ordnungszahl) werden bei digitalen Dokumenten aus der `cf_map` entfernt – gleiche Logik wie für Paginierung.

---

## E-012: Steuer-Tag wird nicht automatisch angelegt (Eselsohr für Phase 3)

**Betrifft:** `app/classifier/resolver.py`, Zeile 333-338

**Problem:** Die Steuer-Tag-Ableitung (`"Steuer {year}"` aus `tax_relevant + tax_year`) sucht den Tag im Cache. Wenn er nicht existiert (z.B. "Steuer 2026" ab Januar 2026), wird nur geloggt – der Tag wird **nicht** in `create_new_tags` aufgenommen. Selbst mit `auto_create_tags=True` würde er daher nicht angelegt.

**Kein Fix nötig jetzt:** Auto-Create ist in Phase 3 vorgesehen ("Neuanlage von Tags/Korrespondenten/Typen/Pfaden" + Confidence-basierte Steuerung). Wenn das aktiviert wird, muss der Resolver den fehlenden Steuer-Tag in `resolved.create_new_tags` aufnehmen, damit `_handle_create_new()` ihn anlegen kann.

---

## E-013: Datenbank-Modul in `app/db/` statt `app/database.py` (AP-06, 2026-02-07)

**Betrifft:** Design-Dokument Abschnitt 3.1 (Verzeichnisstruktur)

**Design:** `app/database.py` als flache Datei.

**Tatsächlich:** `app/db/database.py` als Modul im Paket `app/db/`.

**Grund:** Das Paket `app/db/` existierte bereits als Platzhalter seit AP-01. Ein Paket ist besser erweiterbar (z.B. `app/db/migrations.py` oder `app/db/queries.py` in späteren Phasen) und konsistent mit der Paketstruktur der anderen Module (`app/claude/`, `app/classifier/`, etc.).

---

## E-014: CostTracker-Methoden sind async (AP-06, 2026-02-07)

**Betrifft:** `app/claude/cost_tracker.py`, `app/claude/client.py`, `app/scheduler/poller.py`

**Design:** CostTracker hat synchrone Methoden.

**Tatsächlich:** `get_monthly_cost()`, `get_daily_cost()`, `is_limit_reached()` und `get_model_breakdown()` sind jetzt `async` und lesen aus SQLite. `record()` bleibt synchron (nur In-Memory, wird vom ClaudeClient aufgerufen).

**Aufrufer-Änderungen:**
- `ClaudeClient._check_cost_limit()` → jetzt `async`, aufgerufen mit `await`
- `Poller._is_cost_limit_reached()` → jetzt `async`, aufgerufen mit `await`

**Fallback:** Ohne DB-Backend (Tests, Degraded-Modus) fallen die async-Methoden auf die In-Memory-Liste zurück.

**Grund:** SQLite-Zugriff über aiosqlite ist inherent async. Alle Aufrufer befinden sich bereits in async-Kontexten, daher ist die Migration mechanisch und risikoarm. Die Alternative (synchroner Wrapper mit `asyncio.run()`) wäre fehleranfällig in einer bereits laufenden Event-Loop.

---

## E-015: Schema-Abweichungen processed_documents (AP-06, 2026-02-07)

**Betrifft:** Design-Dokument Abschnitt 7 (Datenmodell), Tabelle `processed_documents`

**Änderungen gegenüber Design:**

1. **`paperless_id` ist NICHT UNIQUE:** Dokumente können mehrfach verarbeitet werden (Retry nach Error, manuelles Re-Tagging mit NEU-Tag). Jede Zeile ist ein Verarbeitungsversuch, nicht der Dokumentzustand. Design-Schema hatte `paperless_id INTEGER NOT NULL UNIQUE`.

2. **Spalte `duration_seconds REAL` hinzugefügt:** Verarbeitungsdauer pro Dokument, nützlich für Performance-Monitoring. War im Design nicht vorgesehen.

3. **Spalte `error_message TEXT` hinzugefügt:** Fehlermeldung bei Status "error". Ermöglicht Fehleranalyse ohne Log-Durchsicht.

4. **`daily_costs`: Cache-Token-Spalten ergänzt:** `total_cache_read_tokens` und `total_cache_creation_tokens` hinzugefügt. Das Design hatte nur `total_input_tokens` und `total_output_tokens`. Cache-Tokens sind für genaue Kostenanalyse nötig.

5. **DB-Persistierung im `finally`-Block:** Das Design sieht Schritt 10 als separaten Erfolgs-Schritt. Tatsächlich wird im `finally`-Block persistiert (aber nur wenn der API-Aufruf stattfand), damit auch Fehler-Fälle mit Kostendaten erfasst werden.

---

## E-016: AP-Nummerierung verschoben – Design +1 ab Phase 2 (AP-07, 2026-02-07)

**Betrifft:** Alle Arbeitspakete ab Phase 2, Design-Dokument Abschnitt 11

**Problem:** Das Design-Dokument und die ursprünglichen AP-Dateien nummerieren die Web-UI Basis als AP-06. Durch das Einschieben von "SQLite State-Management" als eigenständiges AP-06 verschiebt sich alles um +1:

| Aufgabe | Design-Nummer | Tatsächliche Nummer |
|---|---|---|
| SQLite State-Management | (Teil von AP-05/06) | **AP-06** |
| Web-UI Basis | AP-06 | **AP-07** |
| Review Queue | AP-07 | **AP-08** |
| Kosten-Dashboard & Logs | AP-08 | **AP-09** |
| ... | AP-N | **AP-(N+1)** |

**Auswirkung:** Die Datei `07-webui-review-queue.md` aus dem ursprünglichen Planungsstand beschreibt die Review Queue, nicht die Web-UI Basis. Bei neuen Chats gilt: AP-Nummern aus PROJECT_STATUS.md sind die Source of Truth, nicht die Dateinamen der ursprünglichen AP-Beschreibungen.

**Hinweis:** Die Phase-Zuordnung bleibt unverändert (Phase 2 = Web-UI Basis, Phase 3 = Review Queue etc.).

---

## E-017: Zirkulärer Import `__main__` vs `app.main` (AP-07, 2026-02-07)

**Betrifft:** `app/main.py`, alle UI-Module

**Problem:** `main.py` wird als `__main__` geladen (Einstiegspunkt). Wenn UI-Module per `from app.main import get_poller` importieren, lädt Python `app.main` als separates Modul und führt den gesamten Module-Level-Code erneut aus – inklusive `app.on_startup()`, was nach dem NiceGUI-Start nicht mehr erlaubt ist (`RuntimeError: Unable to register another startup handler`).

**Lösung:** State und Getter-Funktionen nach `app/state.py` ausgelagert (keine Seiteneffekte). Health-Check-Funktionen nach `app/health.py` ausgelagert. UI-Module importieren nur noch aus `app.state` und `app.health`, nie aus `app.main`.

**Regel:** Kein UI-Modul darf direkt aus `app.main` importieren. Neue Getter/Hilfsfunktionen gehören in `app/state.py` oder dedizierte Module.

---

## E-018: Null-Felder bei Confidence-Berechnung unsichtbar (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/resolver.py`, `app/classifier/confidence.py`

**Problem:** Wenn Claude für Hauptfelder (Korrespondent, Dokumenttyp, Speicherpfad) `null` zurückgibt, überspringt der Resolver den gesamten Auflösungsblock. Damit zählt `total_fields` nur Felder, für die Claude einen Namen hatte. Ergebnis: 1 Feld benannt + aufgelöst → 1/1 = 100% Mapping-Ratio, obwohl 2 von 3 Kernfeldern unbestimmt sind. Gesamtscore 1.00 → HIGH.

**Testfall:** Nebenkostenabrechnung von unbekanntem Absender. Claude: `correspondent=null, storage_path=null, document_type="Verbrauchsabrechnung"`. Alte Logik: 1/1 = 100%, Score 1.00 → HIGH (auto_apply). Paperless-eigener Matcher hatte vorher falsche Werte gesetzt → falsche Klassifizierung mit höchster Confidence.

**Lösung:**
- `resolver.py`: Neues Feld `null_field_count` in `ResolvedClassification`. Zählt Hauptfelder, für die Claude null zurückgab.
- `confidence.py`: Effektive Mapping-Ratio = `resolved / (named + null_fields)`. Im Testfall: 1/3 = 33%.
- `confidence.py`: HIGH-Schwelle von `>=` auf `>` geändert, damit Grenzfälle (Score = 0.80) in der Review Queue landen.

**Nachbesserung E-018b (gleiche Session):** Die Mapping-Penalty allein reicht nicht – bei hoher Claude-Confidence und perfekten Fuzzy/Special-Scores kann der Gesamtscore trotz 2 Null-Feldern über 0.80 liegen (z.B. 0.88). Daher zusätzlich: harte Regel, dass Null-Felder HIGH verhindern. Wenn `null_field_count > 0` und Level = HIGH → automatisch auf MEDIUM herabstufen. Prinzip: Unvollständige Klassifizierung = nie auto_apply.

**Ergebnis Testfall nach E-018b:** Score 0.85, aber 2 Null-Felder → MEDIUM → Review Queue.

---

## E-019: Null-Felder überschreiben Paperless-Matcher nicht (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/pipeline.py`, `_apply_result()`

**Problem:** Bei `should_apply_fields=True` (HIGH/MEDIUM) wurden nur Felder mit aufgelöster ID an Paperless geschrieben (`if resolved.correspondent_id is not None`). Wenn Claude null zurückgab, blieb der Patch leer → Paperless' eigener Auto-Matcher-Wert blieb stehen. Im Testfall: "VBK Verkehrsbetriebe" und "Ärzte / Goldstadt Privatklinik" (beide falsch vom Paperless-Matcher) wurden nie korrigiert.

**Lösung:** Korrespondent, Dokumenttyp und Speicherpfad werden bei HIGH/MEDIUM IMMER im Patch gesetzt, auch wenn null. Paperless akzeptiert `null` als "Feld leeren".

```python
# Vorher (Bug):
if resolved.correspondent_id is not None:
    patch["correspondent"] = resolved.correspondent_id

# Nachher (Fix):
patch["correspondent"] = resolved.correspondent_id  # int | None
```

**Auswirkung:** Dokumente, bei denen Claude ein Feld nicht bestimmen kann, werden explizit ohne diesen Wert gespeichert, statt falsche Paperless-Matcher-Werte durchzulassen.

---

## E-020: Modellwahl vertraut Paperless-Auto-Matcher (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/pipeline.py`, `_apply_result()` → `select_model()`

**Problem:** Die Modellwahl prüft `doc.correspondent is not None` um zu entscheiden ob Haiku (einfach) oder Sonnet (komplex) verwendet wird. Bei NEU-getaggten Dokumenten hat Paperless' eigener Matching-Algorithmus aber oft schon einen Korrespondenten gesetzt – auch wenn dieser falsch ist. Dadurch wird Haiku für unbekannte Dokumente gewählt, die eigentlich Sonnet bräuchten.

**Testfall:** Nebenkostenabrechnung von unbekanntem Absender. Paperless-Matcher setzte "VBK Verkehrsbetriebe" → `correspondent_known=True` → Haiku gewählt.

**Lösung:** Ein Korrespondent gilt nur als "bekannt", wenn zusätzlich ki_status gesetzt ist (= der Classifier hat das Dokument bereits verarbeitet). NEU-Dokumente haben keinen ki_status → immer Sonnet.

```python
ki_status_value = doc.get_custom_field_value(CF_KI_STATUS)
correspondent_known = (
    doc.correspondent is not None
    and ki_status_value is not None
)
```

---

## E-021: NEU-Tag wird vom Resolver wieder hinzugefügt (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/resolver.py`, `app/classifier/pipeline.py`

**Problem:** Claude sieht den Tag "NEU" im System-Prompt als verfügbaren Tag und gibt ihn in seiner Klassifizierung zurück (`tags: ["NEU"]`). Der Resolver löst "NEU" korrekt zu TAG_NEU_ID (12) auf und fügt ihn in `resolved.tag_ids` ein.

In `_apply_result` wird der NEU-Tag in Zeile 517 korrekt entfernt:
```python
current_tags.discard(TAG_NEU_ID)  # entfernt 12
```

...aber in Zeile 552 sofort wieder hinzugefügt:
```python
current_tags.update(resolved.tag_ids)  # fügt 12 zurück!
```

Resultat: NEU-Tag bleibt stehen → Poller verarbeitet das Dokument im nächsten Zyklus erneut → Endlosschleife.

**Lösung:** Doppelte Absicherung:
1. `resolver.py`: NEU-Tag wird im Tag-Resolver ausgefiltert (`if resolution.resolved_id == TAG_NEU_ID: continue`). So taucht er gar nicht in `resolved.tag_ids` auf, und die Feld-Zählung (total_fields/resolved_fields) wird nicht durch einen Workflow-Tag aufgebläht.
2. `pipeline.py`: Zusätzlicher Filter in `_apply_result` als Defense-in-Depth (`new_tags = [t for t in resolved.tag_ids if t != TAG_NEU_ID]`).

**Auswirkung:** Vor dem Fix zählte der Resolver "3/3 Felder aufgelöst" (inkl. NEU-Tag), was die Mapping-Ratio nach oben verzerrte. Nach dem Fix: "2/2 Felder aufgelöst" (Dokumenttyp + Steuer-Tag), korrekte 50% effektive Mapping-Ratio.

---

## E-022: DB speichert Claude-Confidence statt System-Confidence (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/pipeline.py`, `_persist_result()`

**Problem:** Zeile 690 speichert `raw_result.confidence.value` – das ist Claudes eigene Selbsteinschätzung (z.B. "high"), nicht die vom Confidence-Evaluator berechnete System-Confidence (z.B. "medium" nach Null-Feld-Herabstufung E-018b).

Downstream-Auswirkungen:
- Review Queue Badge zeigt "HIGH" statt "MEDIUM"
- `is_medium`-Flag wird falsch berechnet → aktuelle Paperless-Werte werden bei MEDIUM-Dokumenten nicht zum Vergleich angezeigt
- Kosten-Dashboard und Statistiken basieren auf falschen Confidence-Werten

**Lösung:** Evaluierte Confidence (`result.confidence.level.value`) hat Vorrang. Fallback auf Claude-Confidence nur wenn kein Evaluierungsergebnis vorliegt.

---

## E-023: Review Queue ValueError bei leeren Select-Feldern (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py`, `_render_actions()`

**Problem:** Wenn `suggested_correspondent` und `current_correspondent` beide leer sind (z.B. bei Null-Feldern), ergibt `"" or ""` → `""`. NiceGUI's `ui.select(value="")` wirft `ValueError: Invalid value: ` weil ein leerer String nicht in der Options-Liste ist. `None` wäre der korrekte Wert für "nichts ausgewählt".

**Symptom:** Die gesamte Review-Queue-Seite crasht beim Laden – kein einziges Dokument sichtbar.

**Lösung:**
1. `form_state`-Initialisierung: `(... or ...) or None` – leere Strings werden zu None
2. `on_value_change`-Handler: `e.value` statt `e.value or ""` – None bleibt None

---

## E-024: Verwaiste Review-Einträge bei gelöschten Dokumenten (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py`, `_load_review_items()`

**Problem:** Wenn ein Dokument in Paperless gelöscht wird, bleibt der zugehörige Eintrag in der SQLite-Datenbank mit status="review" bestehen. Bei jedem Laden der Review Queue versucht die UI, das Dokument per API zu laden → `PaperlessNotFoundError` → Warning im Log. Das passiert endlos bei jedem Seitenaufruf.

**Lösung:** `PaperlessNotFoundError` wird gezielt gefangen (statt generisches `Exception`). Bei 404 wird der DB-Eintrag automatisch auf status="manual", reviewed_by="auto_cleanup" gesetzt und das Item wird nicht in die Anzeige-Liste aufgenommen. Einmaliger Info-Log statt dauerhafter Warning-Spam.

---

## E-025: NEU-Tag in Review Queue nicht gefiltert (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py`

**Problem:** E-021 filtert den NEU-Tag korrekt im Resolver und in der Pipeline, aber die Review Queue hat drei eigene Quellen für Tags:
1. `suggested_tags` aus `classification_json` (Claudes Roh-Antwort enthält "NEU")
2. `current_tags` aus Paperless (kann "NEU" enthalten wenn Pipeline es nicht entfernt hat)
3. Tag-Dropdown im Korrektur-Formular (bietet "NEU" als auswählbare Option an)

Wenn ein Nutzer unachtsam "Korrigieren" klickt, wird NEU zurückgeschrieben → Dokument wird beim nächsten Polling-Zyklus erneut verarbeitet → verliert korrekt zugewiesene Felder.

**Lösung:** Vier Filter:
1. `suggested_tags`: `[t for t in tags if t != "NEU"]` beim Laden aus DB
2. `current_tags`: Gleiches Filter beim Laden aus Paperless
3. `_get_stammdaten_options()`: "NEU" aus Tag-Dropdown entfernen
4. `_action_correct()`: "NEU" aus Tag-Liste filtern vor PATCH (Defense-in-Depth)

---

## E-026: NEU-Tag im System-Prompt sichtbar (AP-08, 2026-02-07)

**Betrifft:** `app/classifier/pipeline.py`, `_get_system_prompt()`

**Problem:** Der Tag "NEU" ist ein reiner Workflow-Trigger (inbox_tag), hat aber keine semantische Bedeutung für die Klassifizierung. Claude sieht "NEU" in der Tag-Liste des System-Prompts und schlägt ihn als Tag in seiner Antwort vor. Das verursacht downstream Probleme (E-021, E-025).

**Lösung:** `tags`-Liste im PromptData wird beim Aufbau gefiltert: `[t for t in tags if t != "NEU"]`. Claude sieht den Tag nicht mehr und schlägt ihn nicht mehr vor. Zusammen mit E-021 (Resolver-Filter) und E-025 (UI-Filter) ist NEU jetzt an der Quelle, im Resolver und in der UI gefiltert.

---

## Neuanlage-Vorschläge in Review Queue (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py`

**Feature:** Claudes `create_new`-Vorschläge (Korrespondenten, Dokumenttypen, Tags, Speicherpfade) werden in der Review Card als separate Sektion angezeigt. Pro Vorschlag ein "Anlegen & Zuordnen"-Button der:
1. Die Entität per POST in Paperless anlegt
2. Das Dokument per PATCH sofort zuweist
3. Den Pipeline-Prompt-Cache invalidiert
4. Die Queue neu lädt

Betroffene Funktionen:
- `ReviewItem`: Neue Felder `create_new_*`
- `_load_review_items()`: Parsing von `classification_json.create_new`
- `_action_create_entity()`: Neuer Handler für Anlage + Zuweisung
- `_render_create_new_section()`: UI-Sektion mit gelben Karten
- `_create_new_row()`: Einzelne Zeile mit Button

---

## 🔖 Eselsohr: Personen-Zuordnung unvollständig (Phase 3)

**Betrifft:** System-Prompt / Regelwerk / Schema-Analyse

**Beobachtung:** Claude ordnet Dokumente ohne explizite Namensnennung im Text keiner Person zu, obwohl der Adressat eindeutig erkennbar ist (z.B. Nebenkostenabrechnung adressiert an "Max Mustermann"). Das Feld Person darf nicht leer bleiben – jedes Dokument gehört einer Person.

**Ursache:** Kein Fallback-Regelwerk vorhanden. Claude hat keine Zuordnungsregeln wie "Mietwohnung Kaiserstraße 142 → Max" oder "Adressat im Dokument → Person-Feld". Die Personen-Zuordnung stützt sich aktuell nur auf Claudes eigenständige Erkennung ohne gelernten Kontext.

**Lösung (Phase 3):** Schema-Analyse soll Zuordnungsregeln für Personen lernen, analog zu den Speicherpfad-Regeln. Mögliche Quellen: Adressat im Dokument, Korrespondent-Person-Mapping aus historischen Daten, Mietobjekt/Eigentum-Zuordnungen.

---

## E-027: Speicherpfad-Template von Claude unbrauchbar (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py` (`_action_create_entity`), `app/classifier/pipeline.py` (`_handle_create_new`)

**Problem:** Claude kennt das Template-Schema nicht und liefert in `create_new.storage_paths[].path_template` fehlerhafte Pfade (z.B. `Mietverträge / Kaiserstraße 142 Karlsruhe` statt `/Mietverträge/Kaiserstraße 142 Karlsruhe/{{created_year}}/{{title}}_{{created}}`).

**Schema:** Name `"Topic / Objekt / Entität"` → Pfad `"/Topic/Objekt/Entität/{{created_year}}/{{title}}_{{created}}"`. Transformation: ` / ` → `/`, führendes `/`, Suffix `{{created_year}}/{{title}}_{{created}}`.

**Lösung:** Template wird in Review-UI und Pipeline automatisch aus dem Namen abgeleitet. Claudes `path_template` wird ignoriert.

---

## E-028: Neuanlage-Vorschläge verschwinden nicht nach Anlage (AP-08, 2026-02-07)

**Betrifft:** `app/ui/review.py`, `_load_review_items()`

**Problem:** Nach "Anlegen & Zuordnen" lädt `_refresh_queue` die Vorschläge erneut aus `classification_json` (unveränderlich in SQLite). Die bereits angelegte Entität wird erneut als Vorschlag angezeigt → Doppelklick → HTTP 400 "unique constraint".

**Lösung:** Beim Laden der create_new-Vorschläge wird jeder Name gegen den Paperless-Cache geprüft. Existiert die Entität bereits (`cache.get_*_id(name) is not None`), wird der Vorschlag ausgefiltert.

---

## E-029: Aktiv-Indikator und Review-Badge fehlpositioniert (AP-08, 2026-02-07)

**Betrifft:** `app/ui/layout.py`

**Problem:** Poller-Status "Aktiv" mit grünem Punkt war kaum sichtbar ganz rechts oben im Browser-Chrome. Review-Badge mit `floating`-Prop wurde absolut positioniert und erschien außerhalb des Sidebar-Containers.

**Lösung:** Poller-Status als halbtransparenter Chip (`bg-white/10 border-white/20`) im Header. Review-Badge ohne `floating`, stattdessen `ml-auto` für Inline-Positionierung in der Sidebar-Zeile.
