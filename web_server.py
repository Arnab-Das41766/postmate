#!/usr/bin/env python3
"""
Postmate Web GIS Dashboard Server
Serves the modern interactive NLP map dashboard and handles REST API requests.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

# Ensure postmate directory is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from postmate import (
    TIER_NEUTRAL,
    TIER_NORMAL,
    TIER_CRITICAL,
    TIER_VERY_CRITICAL,
    CriticalityEngine,
    DatabaseRunner,
    MapVisualizer,
    Postmate,
    QueryResult,
)

DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


def extract_geojson_and_bounds(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extracts GeoJSON features and bounds [min_lat, min_lon, max_lat, max_lon] from query rows."""
    if not rows:
        return {"features": [], "bounds": None}

    features = []
    bounds = []

    for row in rows:
        geojson_str = None
        properties = {}

        for k, v in row.items():
            if k in ["__geojson__", "geometry_geojson"] or k.endswith("_geojson"):
                geojson_str = v
            else:
                properties[k] = v

        # If not explicit column, check for WKB, WKT, or lat/lon coordinates
        if not geojson_str:
            lower_keys = {k.lower(): (k, v) for k, v in row.items()}
            lon_val = None
            lat_val = None

            for l_candidate in ["longitude", "lon", "lng", "st_x", "x", "long"]:
                if l_candidate in lower_keys:
                    val = lower_keys[l_candidate][1]
                    try:
                        lon_val = float(val)
                        break
                    except (ValueError, TypeError):
                        pass

            for l_candidate in ["latitude", "lat", "st_y", "y"]:
                if l_candidate in lower_keys:
                    val = lower_keys[l_candidate][1]
                    try:
                        lat_val = float(val)
                        break
                    except (ValueError, TypeError):
                        pass

            if lon_val is not None and lat_val is not None:
                if -180.0 <= lon_val <= 180.0 and -90.0 <= lat_val <= 90.0:
                    geojson_str = json.dumps({
                        "type": "Point",
                        "coordinates": [lon_val, lat_val]
                    })

        if geojson_str:
            try:
                geom_dict = json.loads(geojson_str) if isinstance(geojson_str, str) else geojson_str
                coords = MapVisualizer._extract_coordinates(geom_dict)
                for pt in coords:
                    bounds.append([pt[1], pt[0]])  # [lat, lon]

                features.append({
                    "type": "Feature",
                    "geometry": geom_dict,
                    "properties": properties
                })
            except Exception:
                continue

    bounds_bbox = None
    if bounds:
        lats = [b[0] for b in bounds]
        lons = [b[1] for b in bounds]
        bounds_bbox = [min(lats), min(lons), max(lats), max(lons)]

    return {
        "features": features,
        "bounds": bounds_bbox
    }


class PostmateApiHandler(BaseHTTPRequestHandler):
    postmate: Postmate = None  # Injected before server starts

    def _set_headers(self, status: int = 200, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(200)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html", "/dashboard", "/dashboard.html"]:
            if os.path.exists(DASHBOARD_FILE):
                with open(DASHBOARD_FILE, "rb") as f:
                    content = f.read()
                self._set_headers(200, "text/html; charset=utf-8")
                self.wfile.write(content)
            else:
                self._set_headers(404, "text/plain")
                self.wfile.write(b"Dashboard file not found.")
            return

        elif path == "/api/schema":
            payload = {
                "status": "success",
                "schema": self.postmate.cached_schema,
                "tables": list(self.postmate.schema_discovery.spatial_columns.keys())
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        elif path == "/api/history":
            history_list = list(self.postmate.memory.history)
            payload = {
                "status": "success",
                "history": history_list
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        elif path == "/api/extent":
            qs = urllib.parse.parse_qs(parsed.query)
            tbl = qs.get("table", [None])[0]
            extent_info = self.postmate.schema_discovery.get_dataset_extent(tbl)
            payload = {
                "status": "success",
                "extent": extent_info
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        elif path == "/api/suggestions":
            suggestions = self.postmate.schema_discovery.generate_dynamic_suggestions()
            payload = {
                "status": "success",
                "suggestions": suggestions
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return



        elif path == "/api/layers":
            layers_info = []
            try:
                conn = self.postmate.schema_discovery._get_connection()
                with conn.cursor() as cur:
                    for tbl, cols in self.postmate.schema_discovery.spatial_columns.items():
                        col = cols[0]["column"]
                        gtype = cols[0].get("type", "GEOMETRY")
                        srid = cols[0].get("srid", 4326)
                        try:
                            cur.execute(f"SELECT COUNT(*) FROM {tbl};")
                            cnt = cur.fetchone()[0]
                        except Exception:
                            conn.rollback()
                            cnt = 0
                        layers_info.append({
                            "table": tbl,
                            "geometry_column": col,
                            "geometry_type": gtype,
                            "srid": srid,
                            "row_count": cnt
                        })
                conn.close()
            except Exception:
                pass
            payload = {
                "status": "success",
                "layers": layers_info
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        elif path == "/api/last_result":

            res = self.postmate.last_result
            if not res or not res.rows:
                self._set_headers(200)
                self.wfile.write(json.dumps({"status": "empty"}).encode("utf-8"))
                return
            spatial = extract_geojson_and_bounds(res.rows)
            cols = [c for c in res.column_names if c not in ["__geojson__", "geometry_geojson"]]
            clean_rows = [{k: v for k, v in r.items() if k not in ["__geojson__", "geometry_geojson"]} for r in res.rows]
            payload = {
                "status": "success",
                "columns": cols,
                "rows": clean_rows,
                "execution_time_ms": res.execution_time_ms,
                "row_count": len(res.rows),
                "is_truncated": res.is_truncated,
                "geojson": {
                    "type": "FeatureCollection",
                    "features": spatial["features"]
                },
                "bounds": spatial["bounds"]
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        self._set_headers(404, "text/plain")
        self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        if path == "/api/reset":
            self.postmate.memory.clear()
            self._set_headers(200)
            self.wfile.write(json.dumps({"status": "success", "message": "History cleared"}).encode("utf-8"))
            return

        elif path == "/api/export":
            fmt = req_data.get("format", "csv").lower()
            res = self.postmate.last_result
            if not res or not res.rows:
                self._set_headers(400)
                self.wfile.write(json.dumps({"status": "error", "message": "No query results to export"}).encode("utf-8"))
                return

            if fmt == "csv":
                output = io.StringIO()
                display_cols = [c for c in res.column_names if c not in ["__geojson__", "geometry_geojson"]]
                writer = csv.DictWriter(output, fieldnames=display_cols, extrasaction="ignore")
                writer.writeheader()
                for r in res.rows:
                    writer.writerow({k: v for k, v in r.items() if k in display_cols})
                csv_bytes = output.getvalue().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="postmate_results.csv"')
                self.end_headers()
                self.wfile.write(csv_bytes)
                return

            elif fmt in ["geojson", "json"]:
                spatial = extract_geojson_and_bounds(res.rows)
                fc = {
                    "type": "FeatureCollection",
                    "features": spatial["features"]
                }
                geo_bytes = json.dumps(fc, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/geo+json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="postmate_features.geojson"')
                self.end_headers()
                self.wfile.write(geo_bytes)
                return

        elif path == "/api/spatial_tool":
            tool_type = req_data.get("tool", "radius")
            if tool_type == "radius":
                lat = float(req_data.get("lat", 0))
                lng = float(req_data.get("lng", 0))
                radius = float(req_data.get("radius_meters", 1000))
                target_table = req_data.get("table")

                if not target_table or target_table not in self.postmate.schema_discovery.spatial_columns:
                    for t, cols in self.postmate.schema_discovery.spatial_columns.items():
                        if "POINT" in cols[0].get("type", "").upper():
                            target_table = t
                            break
                    if not target_table and self.postmate.schema_discovery.spatial_columns:
                        target_table = list(self.postmate.schema_discovery.spatial_columns.keys())[0]

                if not target_table:
                    self._set_headers(400)
                    self.wfile.write(json.dumps({"status": "error", "message": "No spatial tables available in database."}).encode("utf-8"))
                    return

                geom_col = self.postmate.schema_discovery.spatial_columns[target_table][0]["column"]
                query_sql = f"""
                    SELECT 
                        *,
                        ST_AsGeoJSON(ST_Transform({geom_col}, 4326)) AS __geojson__,
                        ROUND(ST_Distance({geom_col}::geography, ST_SetSRID(ST_MakePoint({lng}, {lat}), 4326)::geography)::numeric, 1)::float AS distance_meters
                    FROM {target_table}
                    WHERE ST_DWithin({geom_col}::geography, ST_SetSRID(ST_MakePoint({lng}, {lat}), 4326)::geography, {radius})
                    ORDER BY distance_meters ASC
                    LIMIT 200;
                """.strip()

                try:
                    result = self.postmate.db_runner.execute_query(query_sql)
                    self.postmate.last_result = result
                    self.postmate.memory.add(f"Radius search {radius}m around [{lat:.4f}, {lng:.4f}]", query_sql)
                    MapVisualizer.generate_map(
                        rows=result.rows,
                        spatial_meta=self.postmate.schema_discovery.spatial_columns,
                        output_path="map.html"
                    )
                    spatial = extract_geojson_and_bounds(result.rows)
                    cols = [c for c in result.column_names if c not in ["__geojson__", "geometry_geojson"]]
                    clean_rows = [{k: v for k, v in r.items() if k not in ["__geojson__", "geometry_geojson"]} for r in result.rows]

                    payload = {
                        "status": "success",
                        "sql": query_sql,
                        "tier": TIER_NEUTRAL,
                        "risk_explanation": f"Radius search within {radius}m",
                        "execution_time_ms": round(result.execution_time_ms, 1),
                        "row_count": len(result.rows),
                        "columns": cols,
                        "rows": clean_rows[:500],
                        "geojson": {
                            "type": "FeatureCollection",
                            "features": spatial["features"]
                        },
                        "bounds": spatial["bounds"],
                        "center": [lat, lng],
                        "radius_meters": radius
                    }
                    self._set_headers(200)
                    self.wfile.write(json.dumps(payload, default=str).encode("utf-8"))
                    return
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"status": "error", "message": f"Spatial tool execution error: {e}"}, default=str).encode("utf-8"))
                    return


        elif path == "/api/layer/delete":
            tbl_to_delete = req_data.get("table", "").strip()
            confirm = req_data.get("confirm_name", "").strip()

            if not tbl_to_delete or tbl_to_delete != confirm:
                self._set_headers(400)
                self.wfile.write(json.dumps({"status": "error", "message": "Confirmation name does not match table name."}).encode("utf-8"))
                return

            try:
                conn = self.postmate.schema_discovery._get_connection()
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {tbl_to_delete} CASCADE;")
                conn.commit()
                conn.close()

                # Refresh schema in memory
                self.postmate.cached_schema = self.postmate.schema_discovery.discover()
                self._set_headers(200)
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": f"Table '{tbl_to_delete}' deleted successfully.",
                    "remaining_tables": list(self.postmate.schema_discovery.spatial_columns.keys())
                }).encode("utf-8"))
                return
            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"status": "error", "message": f"Failed to delete table: {e}"}).encode("utf-8"))
                return

        elif path == "/api/upload":
            filename = req_data.get("filename", "")
            base64_data = req_data.get("file_data", "")
            custom_table = req_data.get("table_name", "").strip()

            if not filename or not base64_data:
                self._set_headers(400)
                self.wfile.write(json.dumps({"status": "error", "message": "Missing file data or filename."}).encode("utf-8"))
                return

            try:
                import base64
                import tempfile
                import zipfile
                import geopandas as gpd
                import pandas as pd
                from import_gis_data import get_db_engine

                raw_bytes = base64.b64decode(base64_data.split(",")[-1] if "," in base64_data else base64_data)
                ext = os.path.splitext(filename)[1].lower()
                base_name = os.path.splitext(filename)[0].lower()
                tbl_name = custom_table or "".join(c if c.isalnum() or c == "_" else "_" for c in base_name).strip("_")

                engine, db_name = get_db_engine()

                with tempfile.TemporaryDirectory() as temp_dir:
                    file_path = os.path.join(temp_dir, filename)
                    with open(file_path, "wb") as f:
                        f.write(raw_bytes)

                    if ext == ".zip":
                        with zipfile.ZipFile(file_path, 'r') as z:
                            z.extractall(temp_dir)
                        shp_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.lower().endswith(".shp")]
                        if not shp_files:
                            for root, _, files in os.walk(temp_dir):
                                for f in files:
                                    if f.lower().endswith(".shp"):
                                        shp_files.append(os.path.join(root, f))
                        if not shp_files:
                            raise ValueError("No .shp file found inside zip archive.")
                        gdf = gpd.read_file(shp_files[0])

                    elif ext in [".geojson", ".json"]:
                        gdf = gpd.read_file(file_path)

                    elif ext == ".csv":
                        df = pd.read_csv(file_path)
                        cols = {c.lower(): c for c in df.columns}
                        lon_col = cols.get("lon") or cols.get("longitude") or cols.get("x")
                        lat_col = cols.get("lat") or cols.get("latitude") or cols.get("y")
                        if not lon_col or not lat_col:
                            raise ValueError("CSV must contain latitude and longitude columns.")
                        gdf = gpd.GeoDataFrame(
                            df,
                            geometry=gpd.points_from_xy(df[lon_col], df[lat_col]),
                            crs="EPSG:4326"
                        )
                    else:
                        raise ValueError(f"Unsupported file format: {ext}")

                    if gdf.crs is None:
                        gdf.set_crs(epsg=4326, inplace=True)
                    elif gdf.crs.to_epsg() != 4326:
                        gdf = gdf.to_crs(epsg=4326)

                    gdf.columns = [c.lower().replace(" ", "_").replace("-", "_") for c in gdf.columns]
                    if "geometry" not in gdf.columns:
                        gdf.rename_geometry("geometry", inplace=True)

                    gdf.to_postgis(tbl_name, engine, if_exists="replace", index=False)

                self.postmate.cached_schema = self.postmate.schema_discovery.discover()
                new_extent = self.postmate.schema_discovery.get_dataset_extent(tbl_name)
                new_suggestions = self.postmate.schema_discovery.generate_dynamic_suggestions()

                payload = {
                    "status": "success",
                    "message": f"Dataset imported successfully into table '{tbl_name}' ({len(gdf)} features).",
                    "table": tbl_name,
                    "row_count": len(gdf),
                    "extent": new_extent,
                    "suggestions": new_suggestions
                }
                self._set_headers(200)
                self.wfile.write(json.dumps(payload).encode("utf-8"))
                return

            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"status": "error", "message": f"Upload import failed: {e}"}).encode("utf-8"))
                return

        elif path == "/api/query":
            question = req_data.get("question", "").strip()
            confirmed = req_data.get("confirmed", False)
            raw_sql = req_data.get("raw_sql")
            viewport_bounds = req_data.get("viewport_bounds")

            if not question and not raw_sql:
                self._set_headers(400)
                self.wfile.write(json.dumps({"status": "error", "message": "Query or SQL is required"}).encode("utf-8"))
                return

            # Check if direct SQL
            is_direct = bool(raw_sql) or question.startswith(";") or any(question.lower().startswith(p) for p in [
                "select ", "with ", "explain ", "show ", "insert ", "update ", "delete ", "create ", "drop ", "truncate ", "alter "
            ])

            if viewport_bounds and len(viewport_bounds) == 4 and not is_direct:
                s, w, n, e = viewport_bounds
                question += f" (RESTRICT SEARCH GEOGRAPHICALLY TO VISIBLE VIEWPORT: ST_Intersects(geometry, ST_MakeEnvelope({w:.5f}, {s:.5f}, {e:.5f}, {n:.5f}, 4326)))"

            if is_direct:
                sql_query = (raw_sql or (question[1:].strip() if question.startswith(";") else question)).strip()
                grok_crit = TIER_NEUTRAL
                grok_risk = "Direct SQL statement"

            else:
                try:
                    memory_ctx = self.postmate.memory.get_context_for_prompt()
                    grok_response = self.postmate.grok_client.generate_sql(
                        question=question,
                        schema=self.postmate.cached_schema,
                        memory_context=memory_ctx
                    )
                    sql_query = grok_response.get("sql", "").strip()
                    grok_crit = grok_response.get("criticality", TIER_NEUTRAL)
                    grok_risk = grok_response.get("risk_explanation", "")
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"status": "error", "message": f"AI Generation Error: {e}"}).encode("utf-8"))
                    return

            if not sql_query:
                self._set_headers(400)
                self.wfile.write(json.dumps({"status": "error", "message": "Unable to generate valid SQL"}).encode("utf-8"))
                return

            # Evaluate criticality
            rule_tier, rule_reason = CriticalityEngine.evaluate_rules(sql_query)
            final_tier, final_reason = CriticalityEngine.resolve_tier(
                grok_proposed=grok_crit,
                rule_tier=rule_tier,
                rule_reason=rule_reason,
                grok_reason=grok_risk
            )

            # Check confirmation for CRITICAL or VERY CRITICAL
            if final_tier in [TIER_CRITICAL, TIER_VERY_CRITICAL] and not confirmed:
                payload = {
                    "status": "confirmation_required",
                    "tier": final_tier,
                    "risk_explanation": final_reason,
                    "sql": sql_query,
                    "question": question
                }
                self._set_headers(200)
                self.wfile.write(json.dumps(payload).encode("utf-8"))
                return

            # Execute query
            try:
                result = self.postmate.db_runner.execute_query(sql_query)
                self.postmate.last_result = result
                self.postmate.memory.add(question or "[Direct SQL]", sql_query)

                # Update map.html as well for seamless file view sync
                MapVisualizer.generate_map(
                    rows=result.rows,
                    spatial_meta=self.postmate.schema_discovery.spatial_columns,
                    output_path="map.html"
                )

                spatial = extract_geojson_and_bounds(result.rows)
                cols = [c for c in result.column_names if c not in ["__geojson__", "geometry_geojson"]]
                clean_rows = [{k: v for k, v in r.items() if k not in ["__geojson__", "geometry_geojson"]} for r in result.rows]

                payload = {
                    "status": "success",
                    "sql": sql_query,
                    "tier": final_tier,
                    "risk_explanation": final_reason,
                    "execution_time_ms": round(result.execution_time_ms, 1),
                    "row_count": len(result.rows),
                    "affected_rows": result.rowcount,
                    "is_truncated": result.is_truncated,
                    "columns": cols,
                    "rows": clean_rows[:500],
                    "geojson": {
                        "type": "FeatureCollection",
                        "features": spatial["features"]
                    },
                    "bounds": spatial["bounds"]
                }
                self._set_headers(200)
                self.wfile.write(json.dumps(payload).encode("utf-8"))
                return

            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"status": "error", "message": f"Database execution error: {e}", "sql": sql_query}).encode("utf-8"))
                return

        self._set_headers(404, "text/plain")
        self.wfile.write(b"Not Found")


def start_dashboard_server(postmate: Postmate, port: int = 8080, open_browser: bool = True):
    """Starts the ThreadingHTTPServer serving the dashboard and opens browser."""
    PostmateApiHandler.postmate = postmate
    server = ThreadingHTTPServer(("localhost", port), PostmateApiHandler)
    url = f"http://localhost:{port}"

    print("\n" + "=" * 60)
    print("🚀 POSTMATE LIVE GIS INTELLIGENCE DASHBOARD")
    print(f"Server running at: {url}")
    print("Press Ctrl+C in terminal to stop.")
    print("=" * 60 + "\n")

    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Postmate Dashboard Server. Goodbye!")
        server.server_close()


if __name__ == "__main__":
    from postmate import Postmate
    p = Postmate()
    p.initialize()
    start_dashboard_server(p, port=8080)
