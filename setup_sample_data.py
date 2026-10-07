#!/usr/bin/env python3
"""
Postmate Demo Setup & Sample Data Seeder
Creates a sample PostGIS database and tables for testing Postmate.
Tables:
  - neighborhoods (Polygons): Urban zones with population and boundaries.
  - parks (Polygons): Public parks with area and geometries.
  - metro_stations (Points): Transit stations with passenger volume and locations.
  - cafes (Points): Restaurants & cafes with cuisine, rating, and locations.
"""

import json
import os
import sys

try:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    print("Error: psycopg2-binary is not installed. Run: pip install psycopg2-binary")
    sys.exit(1)


def load_config_credentials():
    config_path = "config.json"
    if not os.path.exists(config_path):
        print("Creating default config.json...")
        config = {
            "default_connection": "local",
            "connections": {
                "local": {
                    "host": "localhost",
                    "port": 5432,
                    "database": "postmate_demo",
                    "user": "postgres",
                    "password": "your_password"
                }
            },
            "grok": {
                "model": "grok-2-latest",
                "temperature": 0.1
            }
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return config["connections"]["local"]

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    def_name = data.get("default_connection", "local")
    return data.get("connections", {}).get(def_name, {})


def main():
    print("==================================================")
    print("   Postmate PostGIS Sample Data Setup Tool       ")
    print("==================================================")

    creds = load_config_credentials()
    host = creds.get("host", "localhost")
    port = creds.get("port", 5432)
    user = creds.get("user", "postgres")
    password = creds.get("password", "")
    target_db = creds.get("database", "postmate_demo")

    if not password or password == "your_password":
        print(f"\n[!] Please enter your PostgreSQL '{user}' password to create/seed the database.")
        import getpass
        password = getpass.getpass(f"Password for user '{user}': ")
        creds["password"] = password
        # Update config.json
        if os.path.exists("config.json"):
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            def_name = cfg.get("default_connection", "local")
            if def_name in cfg.get("connections", {}):
                cfg["connections"][def_name]["password"] = password
                cfg["connections"][def_name]["database"] = target_db
                with open("config.json", "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                print(f"[OK] Updated 'config.json' with database: {target_db}")

    # 1. Connect to default 'postgres' database to create 'postmate_demo' if needed
    print(f"\nStep 1: Connecting to PostgreSQL server on {host}:{port}...")
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password, database="postgres")
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()

        # Check if database exists
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (target_db,))
        exists = cur.fetchone()
        if not exists:
            print(f"Creating database '{target_db}'...")
            cur.execute(f'CREATE DATABASE "{target_db}";')
            print(f"[OK] Database '{target_db}' created successfully.")
        else:
            print(f"[OK] Database '{target_db}' already exists.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"\n[ERROR] Could not connect to PostgreSQL server: {e}")
        print("Please verify your PostgreSQL password and that the service is running.")
        sys.exit(1)

    # 2. Connect to target database and enable PostGIS
    print(f"\nStep 2: Connecting to '{target_db}' and enabling PostGIS...")
    try:
        conn = psycopg2.connect(host=host, port=port, user=user, password=password, database=target_db)
        cur = conn.cursor()

        # Enable PostGIS extension
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        conn.commit()
        print("[OK] PostGIS extension verified/enabled.")

        # 3. Create Sample Tables
        print("\nStep 3: Creating sample spatial tables...")

        # Neighborhoods table
        cur.execute("""
            DROP TABLE IF EXISTS cafes CASCADE;
            DROP TABLE IF EXISTS metro_stations CASCADE;
            DROP TABLE IF EXISTS parks CASCADE;
            DROP TABLE IF EXISTS neighborhoods CASCADE;

            CREATE TABLE neighborhoods (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                borough VARCHAR(50),
                population INT,
                geom GEOMETRY(POLYGON, 4326)
            );

            CREATE TABLE parks (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                has_playground BOOLEAN DEFAULT TRUE,
                area_sqm DOUBLE PRECISION,
                geom GEOMETRY(MULTIPOLYGON, 4326)
            );

            CREATE TABLE metro_stations (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                lines VARCHAR(50),
                daily_ridership INT,
                geom GEOMETRY(POINT, 4326)
            );

            CREATE TABLE cafes (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                cuisine VARCHAR(50),
                rating NUMERIC(2, 1),
                price_tier VARCHAR(10),
                geom GEOMETRY(POINT, 4326)
            );
        """)
        conn.commit()
        print("[OK] Tables created: neighborhoods, parks, metro_stations, cafes.")

        # 4. Insert Sample Data (NYC Midtown / Manhattan Sample)
        print("\nStep 4: Inserting rich sample spatial data...")

        # Neighborhoods
        cur.execute("""
            INSERT INTO neighborhoods (name, borough, population, geom) VALUES
            ('Midtown Manhattan', 'Manhattan', 95000, 
             ST_GeomFromText('POLYGON((-73.995 40.745, -73.970 40.765, -73.960 40.755, -73.985 40.735, -73.995 40.745))', 4326)),
            ('Upper West Side', 'Manhattan', 210000, 
             ST_GeomFromText('POLYGON((-73.985 40.770, -73.960 40.800, -73.970 40.805, -73.995 40.775, -73.985 40.770))', 4326)),
            ('Greenwich Village', 'Manhattan', 68000, 
             ST_GeomFromText('POLYGON((-74.008 40.728, -73.990 40.738, -73.985 40.728, -74.005 40.718, -74.008 40.728))', 4326));
        """)

        # Parks
        cur.execute("""
            INSERT INTO parks (name, has_playground, area_sqm, geom) VALUES
            ('Central Park South', TRUE, 840000.0, 
             ST_Multi(ST_GeomFromText('POLYGON((-73.981 40.764, -73.958 40.785, -73.965 40.790, -73.988 40.768, -73.981 40.764))', 4326))),
            ('Bryant Park', FALSE, 39000.0, 
             ST_Multi(ST_GeomFromText('POLYGON((-73.984 40.753, -73.982 40.755, -73.984 40.756, -73.986 40.754, -73.984 40.753))', 4326))),
            ('Washington Square Park', TRUE, 39500.0, 
             ST_Multi(ST_GeomFromText('POLYGON((-73.998 40.730, -73.996 40.732, -73.998 40.733, -74.000 40.731, -73.998 40.730))', 4326)));
        """)

        # Metro Stations
        cur.execute("""
            INSERT INTO metro_stations (name, lines, daily_ridership, geom) VALUES
            ('Times Square - 42nd St', 'N,Q,R,W,1,2,3,7', 185000, ST_SetSRID(ST_MakePoint(-73.987, 40.755), 4326)),
            ('Grand Central - 42nd St', '4,5,6,7,S', 160000, ST_SetSRID(ST_MakePoint(-73.977, 40.752), 4326)),
            ('Herald Square - 34th St', 'B,D,F,M,N,Q,R,W', 120000, ST_SetSRID(ST_MakePoint(-73.988, 40.749), 4326)),
            ('Washington Sq - 8th St', 'N,R,W', 42000, ST_SetSRID(ST_MakePoint(-73.993, 40.730), 4326)),
            ('Columbus Circle - 59th St', 'A,B,C,D,1', 98000, ST_SetSRID(ST_MakePoint(-73.982, 40.768), 4326));
        """)

        # Cafes & Restaurants
        cur.execute("""
            INSERT INTO cafes (name, cuisine, rating, price_tier, geom) VALUES
            ('Blue Bottle Coffee', 'Coffee', 4.7, '$$', ST_SetSRID(ST_MakePoint(-73.983, 40.754), 4326)),
            ('Joe The Art of Coffee', 'Coffee', 4.5, '$', ST_SetSRID(ST_MakePoint(-73.986, 40.751), 4326)),
            ('La Colombe Torrefaction', 'Coffee', 4.8, '$$', ST_SetSRID(ST_MakePoint(-73.996, 40.729), 4326)),
            ('Lucali Pizza', 'Italian', 4.9, '$$$', ST_SetSRID(ST_MakePoint(-73.991, 40.742), 4326)),
            ('Ichiran Ramen', 'Japanese', 4.6, '$$', ST_SetSRID(ST_MakePoint(-73.989, 40.750), 4326)),
            ('Magnolia Bakery', 'Dessert', 4.4, '$$', ST_SetSRID(ST_MakePoint(-73.981, 40.759), 4326));
        """)

        conn.commit()
        print("[OK] Sample records inserted successfully.")

        # Spatial Indexing
        print("\nStep 5: Creating spatial GiST indexes...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_neighborhoods_geom ON neighborhoods USING GIST(geom);
            CREATE INDEX IF NOT EXISTS idx_parks_geom ON parks USING GIST(geom);
            CREATE INDEX IF NOT EXISTS idx_metro_geom ON metro_stations USING GIST(geom);
            CREATE INDEX IF NOT EXISTS idx_cafes_geom ON cafes USING GIST(geom);
        """)
        conn.commit()
        print("[OK] Spatial GiST indexes created.")

        cur.close()
        conn.close()

        print("\n==================================================")
        print("🎉 SUCCESS! Sample PostGIS database is ready.")
        print("Database Name: " + target_db)
        print("==================================================")
        print("\nYou can now start Postmate with:")
        print("  python postmate.py")
        print("\nTry these sample questions:")
        print("  1. 'List all cafes with rating above 4.6'")
        print("  2. 'Which cafes are within 500 meters of Bryant Park?'")
        print("  3. 'Show all metro stations and order by daily ridership descending'")
        print("  4. 'Show all parks'  (this generates map.html!)")
        print("  5. 'Drop the cafes table'  (tests VERY CRITICAL safety prompt!)")

    except Exception as e:
        print(f"\n[ERROR] Failed to seed database: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
