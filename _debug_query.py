"""Check event_id values in both tables."""
from app.data.triathlon_db import get_triathlon_engine
from sqlalchemy import text

engine = get_triathlon_engine()
with engine.connect() as conn:
    # Sample event_ids from program_entries
    r1 = conn.execute(text(
        "SELECT DISTINCT event_id FROM program_entries ORDER BY event_id LIMIT 10"
    )).fetchall()
    print("program_entries event_ids (sample):")
    for row in r1:
        print(f"  {row[0]}")

    # Sample event_ids from events
    r2 = conn.execute(text(
        "SELECT DISTINCT event_id FROM events ORDER BY event_id LIMIT 10"
    )).fetchall()
    print("\nevents event_ids (sample):")
    for row in r2:
        print(f"  {row[0]}")

    # Check overlap
    r3 = conn.execute(text(
        "SELECT COUNT(DISTINCT pe.event_id) FROM program_entries pe "
        "WHERE pe.event_id IN (SELECT DISTINCT event_id FROM events)"
    )).scalar()
    print(f"\nOverlapping event_ids: {r3}")

    # Total distinct in each
    r4 = conn.execute(text("SELECT COUNT(DISTINCT event_id) FROM program_entries")).scalar()
    r5 = conn.execute(text("SELECT COUNT(DISTINCT event_id) FROM events")).scalar()
    print(f"Distinct event_ids in program_entries: {r4}")
    print(f"Distinct event_ids in events: {r5}")

    # Check prog_ids in program_entries
    r6 = conn.execute(text(
        "SELECT DISTINCT event_id, prog_id FROM program_entries ORDER BY event_id, prog_id"
    )).fetchall()
    print(f"\nAll (event_id, prog_id) combos in program_entries:")
    for row in r6:
        print(f"  event_id={row[0]}, prog_id={row[1]}")

    # Check if those event_ids exist in events at all
    event_ids = [row[0] for row in r6]
    for eid in set(event_ids):
        r7 = conn.execute(text(
            "SELECT event_id, event_name, event_date FROM events WHERE event_id = :eid LIMIT 1"
        ), {"eid": eid}).fetchone()
        if r7:
            print(f"  -> Found in events: {r7[1]} ({r7[2]})")
        else:
            print(f"  -> event_id={eid} NOT FOUND in events table")
