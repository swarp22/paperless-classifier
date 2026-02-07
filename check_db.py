import asyncio
import aiosqlite

async def check():
    async with aiosqlite.connect("/app/data/classifier.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM processed_documents ORDER BY id DESC LIMIT 5") as cur:
            rows = await cur.fetchall()
            for r in rows:
                print(f"ID {r['paperless_id']}: status={r['status']}, "
                      f"model={r['model_used']}, cost=${r['cost_usd']:.4f}, "
                      f"confidence={r['confidence']}")
            if not rows:
                print("Keine Eintraege.")
        async with db.execute("SELECT * FROM daily_costs") as cur:
            rows = await cur.fetchall()
            for r in rows:
                print(f"Tag {r['date']}: {r['documents_processed']} Docs, "
                      f"${r['total_cost_usd']:.4f}")

asyncio.run(check())
