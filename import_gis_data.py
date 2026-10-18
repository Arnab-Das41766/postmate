#!/usr/bin/env python3
"""
Postmate Spatial Data Importer (Single file or Batch Folder)
Effortlessly imports GIS data (Shapefile, GeoJSON, GeoPackage, CSV) into PostgreSQL/PostGIS.
Usage:
  python import_gis_data.py                           # Imports all files in ./input_data/
  python import_gis_data.py "path/to/folder"          # Imports all files in specified folder
  python import_gis_data.py "path/to/file.shp"        # Imports a single file
"""

import argparse
import json
import os
import sys
from typing import List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import geopandas as gpd
    import pandas as pd
    import geoalchemy2
    from sqlalchemy import create_engine, text
except ImportError as e:
    print(f"Error: Missing dependency ({e}). Run: pip install geopandas sqlalchemy geoalchemy2 psycopg2-binary")
    sys.exit(1)


SUPPORTED_EXTENSIONS = {".shp", ".geojson", ".json", ".gpkg", ".csv"}
DEFAULT_INPUT_DIR = "input_data"


def get_db_engine():
    config_path = "config.json"
    if not os.path.exists(config_path):
        print("Error: config.json not found.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    def_name = data.get("default_connection", "local")
    creds = data.get("connections", {}).get(def_name, {})

    user = creds.get("user", "postgres")
    password = creds.get("password", "")
    host = creds.get("host", "localhost")
    port = creds.get("port", 5432)
    database = creds.get("database", "postmate_demo")

    conn_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
    return create_engine(conn_url), database


def import_single_file(file_path: str, engine, database: str, custom_table: str = None, if_exists: str = "replace") -> bool:
    ext = os.path.splitext(file_path)[1].lower()
    base_name = os.path.splitext(os.path.basename(file_path))[0].lower()
    table_name = custom_table or "".join(c if c.isalnum() or c == "_" else "_" for c in base_name).strip("_")

    print(f"\n📂 Processing: {file_path}")
    try:
        # Check if file is a multi-layer GeoPackage
        layers_to_import = [(None, table_name)]
        if ext == ".gpkg":
            try:
                import pyogrio
                gpkg_layers = pyogrio.list_layers(file_path)
                layer_names = [l[0] for l in gpkg_layers]
            except Exception:
                import sqlite3
                conn = sqlite3.connect(file_path)
                layer_names = [r[0] for r in conn.execute("SELECT table_name FROM gpkg_contents").fetchall()]
                conn.close()

            if len(layer_names) > 1:
                prefix = custom_table or "osm"
                layers_to_import = [(lyr, f"{prefix}_{lyr}") for lyr in layer_names]
                print(f"   Detected multi-layer GeoPackage with {len(layer_names)} layers: {', '.join(layer_names)}")

        overall_success = True
        for lyr_name, tbl_name in layers_to_import:
            if lyr_name:
                print(f"\n   Layer: '{lyr_name}' -> PostGIS table: '{tbl_name}'")
                gdf = gpd.read_file(file_path, layer=lyr_name)
            elif ext == ".csv":
                df = pd.read_csv(file_path)
                cols = {c.lower(): c for c in df.columns}
                lon_col = cols.get("lon") or cols.get("longitude") or cols.get("x")
                lat_col = cols.get("lat") or cols.get("latitude") or cols.get("y")

                if not lon_col or not lat_col:
                    print(f"   ⚠️ Skipped CSV: Missing latitude/longitude columns in {file_path}")
                    return False

                gdf = gpd.GeoDataFrame(
                    df,
                    geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
                    crs="EPSG:4326"
                )
            else:
                gdf = gpd.read_file(file_path)

            if len(gdf) == 0:
                print(f"   ℹ️ Layer '{lyr_name or tbl_name}' has 0 rows, skipping.")
                continue

            # Ensure CRS is EPSG:4326
            if gdf.crs is None:
                print("   ⚠️ No CRS defined in file. Defaulting to EPSG:4326 (WGS84).")
                gdf.set_crs(epsg=4326, inplace=True)
            elif gdf.crs.to_epsg() != 4326:
                print(f"   Reprojecting from {gdf.crs} to EPSG:4326...")
                gdf = gdf.to_crs(epsg=4326)

            num_rows = len(gdf)
            geom_col = gdf.geometry.name or "geometry"
            geom_type = gdf.geometry.geom_type.iloc[0] if num_rows > 0 else "UNKNOWN"
            print(f"   Loaded {num_rows} records (Type: {geom_type}, CRS: EPSG:4326, Geom Column: '{geom_col}')")

            # Validate and auto-repair self-intersecting geometries
            try:
                invalid_mask = ~gdf[geom_col].is_valid
                if invalid_mask.any():
                    invalid_count = int(invalid_mask.sum())
                    print(f"   ⚠️ Found {invalid_count} invalid geometry(ies). Auto-repairing with make_valid()...")
                    gdf[geom_col] = gdf[geom_col].make_valid()
                    print(f"   ✓ Geometries successfully repaired.")
            except Exception as val_err:
                print(f"   ℹ️ Notice during geometry validation: {val_err}")

            print(f"   Uploading to PostGIS table '{tbl_name}' in '{database}'...")
            gdf.to_postgis(tbl_name, engine, if_exists=if_exists, index=False)

            # Create GiST spatial index dynamically on the actual geometry column
            with engine.connect() as conn:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "idx_{tbl_name}_{geom_col}" ON "{tbl_name}" USING GIST("{geom_col}");'))
                conn.commit()

            print(f"   ✅ Table '{tbl_name}' created successfully with spatial GiST index on '{geom_col}'.")

        return overall_success

    except Exception as e:
        print(f"   ❌ Error importing {file_path}: {e}")
        return False


def collect_files(target_path: str) -> List[str]:
    files = []
    if os.path.isfile(target_path):
        return [target_path]
    elif os.path.isdir(target_path):
        for root, _, filenames in os.walk(target_path):
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    files.append(os.path.join(root, fn))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Automated PostGIS Spatial Importer (Files or Batch Folders)"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to a spatial file or directory of spatial files (defaults to ./input_data/)"
    )
    parser.add_argument("--table", "-t", default=None, help="Custom destination table (only for single file import)")
    parser.add_argument("--if-exists", choices=["replace", "append", "fail"], default="replace", help="Action if table exists")

    args = parser.parse_args()

    # Determine target path
    if args.path:
        target = args.path
    else:
        # Default to ./input_data/
        target = DEFAULT_INPUT_DIR
        if not os.path.exists(target):
            os.makedirs(target, exist_ok=True)
            print(f"📁 Created '{target}' folder.")
            print(f"Tip: Place your .shp, .geojson, or .csv files inside '{target}/' and re-run:")
            print("     python import_gis_data.py\n")
            return

    if not os.path.exists(target):
        print(f"❌ Error: Path '{target}' does not exist.")
        sys.exit(1)

    files = collect_files(target)
    if not files:
        print(f"ℹ️ No spatial files (.shp, .geojson, .gpkg, .csv) found in: {target}")
        print(f"Place your GIS files there and run 'python import_gis_data.py' again.")
        return

    print("==================================================")
    print(f"🚀 Postmate Automated Batch Importer")
    print(f"Found {len(files)} spatial file(s) to import.")
    print("==================================================")

    engine, database = get_db_engine()

    success_count = 0
    for f in files:
        custom_tbl = args.table if len(files) == 1 else None
        if import_single_file(f, engine, database, custom_table=custom_tbl, if_exists=args.if_exists):
            success_count += 1

    print("\n==================================================")
    print(f"🎉 Import Complete: {success_count}/{len(files)} file(s) successfully loaded into PostGIS!")
    print("==================================================")
    print("\nYou can now start Postmate:")
    print("   python postmate.py")
    print("All imported tables are automatically ready to query!\n")


if __name__ == "__main__":
    main()
