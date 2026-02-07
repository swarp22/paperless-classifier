import sqlite3, json
conn = sqlite3.connect('/app/data/classifier.db')
row = conn.execute('SELECT classification_json, confidence, reasoning FROM processed_documents ORDER BY id DESC LIMIT 1').fetchone()
if row:
    print('=== CLASSIFICATION JSON ===')
    try:
        print(json.dumps(json.loads(row[0]), indent=2, ensure_ascii=False))
    except Exception:
        print(row[0])
    print('=== CONFIDENCE ===')
    print(row[1])
    print('=== REASONING ===')
    print(row[2])
conn.close()
