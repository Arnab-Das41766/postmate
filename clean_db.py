#!/usr/bin/env python3
"""
Clean Database: Removes user tables from the Postmate database.
Usage:
  python clean_db.py                     # Drops all user tables
  python clean_db.py jamshedpur_boundary  # Drops specific table
"""

import argparse
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import psycopg2
except ImportError:
    print("Error: psycopg2-binary is not installed.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Drop tables from the Postmate database.")
    parser.add_argument("tables", nargs="*", help="Specific table names to drop (leave empty to drop ALL user tables)")
    args = parser.parse_args()

    config_path = "config.json"
    if not os.path.exists(config_path):
        print("Error: config.json not found.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def_name = data.get("default_connection", "local")
    creds = data.get("connections", {}).get(def_name, {})

    host = creds.get("host", "localhost")
    port = creds.get("port", 5432)
    user = creds.get("user", "postgres")
    password = creds.get("password", "")
    database = creds.get("database", "postmate_demo")

    print(f"Connecting to '{database}' on {host}:{port} as user '{user}'...")
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password, database=database)
        conn.autocommit = True
        cur = conn.cursor()

        # Find tables to drop
        if args.tables:
            tables_to_drop = args.tables
        else:
            # Query all user tables in public schema, excluding system & PostGIS tables
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                  AND table_type = 'BASE TABLE'
                  AND table_name NOT IN ('spatial_ref_sys', 'geometry_columns', 'geography_columns');
            """)
            tables_to_drop = [r[0] for r in cur.fetchall()]

        if not tables_to_drop:
            print("\nℹ️ No user tables found in database. It is already empty!")
        else:
            print(f"\nDropping {len(tables_to_drop)} table(s)...")
            for table in tables_to_drop:
                cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE;')
                print(f"  ✓ Dropped table: {table}")

        cur.close()
        conn.close()

        # Clean map.html if it exists
        if os.path.exists("map.html"):
            try:
                os.remove("map.html")
                print("  ✓ Cleared map.html")
            except Exception:
                pass

        print("\n✅ Done! Database is clean and ready for your new datasets.\n")

    except Exception as e:
        print(f"\n❌ Error removing tables: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
