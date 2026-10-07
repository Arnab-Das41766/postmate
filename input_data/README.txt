==================================================
 POSTMATE DATA DROP FOLDER (input_data/)
==================================================

How to use:
1. Drop any spatial files directly into this folder:
   - Shapefiles: .shp (make sure to also include .dbf, .shx, .prj)
   - GeoJSON: .geojson or .json
   - GeoPackage: .gpkg
   - CSV: .csv (with latitude and longitude columns)

2. Run in terminal:
   python import_gis_data.py

3. All files will be automatically loaded into PostGIS,
   indexed with spatial GiST indexes, and immediately
   discoverable by Postmate!
