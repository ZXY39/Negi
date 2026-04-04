import sqlite3
import json
import os

db_path = r"D:\NewProjects\G3KU\.g3ku\main-runtime\governance.sqlite3"
if not os.path.exists(db_path):
    print(f"DB not found: {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# List all tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print("=== TABLES ===")
for t in tables:
    print(t[0])

# Find tables related to nodes
for t in tables:
    if 'node' in t[0].lower():
        cursor.execute(f"SELECT sql FROM sqlite_master WHERE name='{t[0]}'")
        schema = cursor.fetchone()
        print(f"\n=== SCHEMA: {t[0]} ===")
        if schema:
            print(schema[0])

# Search for the task_id in all tables
task_id = "84bf2ed0b685"
node_ids = ["bc7fdc2c6558", "05177c7fa8bb"]

for t in tables:
    tname = t[0]
    try:
        cursor.execute(f"PRAGMA table_info({tname})")
        cols = cursor.fetchall()
        col_names = [c[1] for c in cols]
        print(f"\n=== TABLE: {tname} ({len(col_names)} cols) ===")
        print(f"Columns: {col_names}")
        
        # Try to find rows containing the task_id
        for col in col_names:
            try:
                cursor.execute(f"SELECT * FROM {tname} WHERE {col} LIKE '%84bf2ed0b685%' LIMIT 2")
                rows = cursor.fetchall()
                if rows:
                    print(f"  Found in column '{col}':")
                    for row in rows:
                        for i, val in enumerate(row):
                            if val and isinstance(val, str) and len(val) > 200:
                                print(f"    {col_names[i]}: {val[:500]}... (truncated)")
                            else:
                                print(f"    {col_names[i]}: {val}")
            except Exception as e:
                pass
    except Exception as e:
        print(f"  Error: {e}")

conn.close()
