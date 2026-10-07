#!/usr/bin/env python3
"""
Postmate: Natural Language to PostgreSQL & PostGIS CLI
Author: Postmate Team
Description:
  Translates plain English questions into PostgreSQL and PostGIS queries via the Grok API,
  introspects live schema and spatial columns, enforces a 4-tier criticality safety system,
  maintains in-memory rolling conversational context, handles single-retry self-correction,
  and renders spatial query results to an interactive Folium map.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Third-party dependencies
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import psycopg2
    from psycopg2 import sql as pg_sql
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

try:
    import requests
except ImportError:
    requests = None

try:
    import folium
    from branca.element import Element
except ImportError:
    folium = None
    Element = None

try:
    from shapely import wkb, wkt
    from shapely.geometry import mapping
except ImportError:
    wkb = None
    wkt = None
    mapping = None

try:
    from rich import print as rprint
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt
    console = Console()
except ImportError:
    console = None
    def rprint(*args, **kwargs):
        print(*args)


# ==============================================================================
# SECTION 1: CONSTANTS & CRITICALITY TIERS
# ==============================================================================

TIER_NEUTRAL = "neutral"
TIER_NORMAL = "normal"
TIER_CRITICAL = "critical"
TIER_VERY_CRITICAL = "very critical"

TIER_RANKS = {
    TIER_NEUTRAL: 1,
    TIER_NORMAL: 2,
    TIER_CRITICAL: 3,
    TIER_VERY_CRITICAL: 4,
}

TIER_STYLES = {
    TIER_NEUTRAL: ("green", "NEUTRAL"),
    TIER_NORMAL: ("blue", "NORMAL"),
    TIER_CRITICAL: ("yellow bold", "CRITICAL"),
    TIER_VERY_CRITICAL: ("red bold reverse", "VERY CRITICAL"),
}

DEFAULT_GROK_MODEL = "grok-2-latest"
DEFAULT_GROK_API_URL = "https://api.x.ai/v1/chat/completions"

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MAP_FILENAME = "map.html"


# ==============================================================================
# SECTION 2: CONFIGURATION & CREDENTIAL MANAGEMENT
# ==============================================================================

class ConfigError(Exception):
    """Raised for missing or malformed configuration."""
    pass


class ApiKeyError(Exception):
    """Raised when Grok API key is not found."""
    pass


class ConfigManager:
    """Manages database connection profiles and Grok API keys."""

    @staticmethod
    def find_config_path() -> str:
        candidates = [
            "config.json",
            os.path.join(os.path.dirname(__file__), "config.json"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return "config.json"

    @classmethod
    def load_config(cls, connection_name: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Loads config.json and returns (connection_params, grok_options).
        """
        config_path = cls.find_config_path()
        if not os.path.isfile(config_path):
            raise ConfigError(
                f"Configuration file '{config_path}' not found.\n"
                f"Please create a 'config.json' file using 'config.json.example' as a template."
            )

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Malformed JSON in '{config_path}': {e}")
        except Exception as e:
            raise ConfigError(f"Failed to read '{config_path}': {e}")

        connections = data.get("connections", {})
        if not connections:
            raise ConfigError(f"No database connections defined in '{config_path}'.")

        selected = connection_name or data.get("default_connection")
        if not selected:
            selected = next(iter(connections.keys()))

        if selected not in connections:
            available = ", ".join(connections.keys())
            raise ConfigError(
                f"Connection profile '{selected}' not found in '{config_path}'.\n"
                f"Available profiles: {available}"
            )

        conn_params = connections[selected]
        conn_params["_profile_name"] = selected
        grok_opts = data.get("grok", {})
        return conn_params, grok_opts

    @staticmethod
    def get_api_key() -> Tuple[str, str]:
        """
        Retrieves API key from environment variables or .env.
        Returns (api_key, provider_type) where provider_type is 'groq' or 'grok'.
        """
        key = (
            os.environ.get("GROQ_API_KEY")
            or os.environ.get("GROK_API_KEY")
            or os.environ.get("XAI_API_KEY")
        )
        if not key or key.strip() == "" or "your-api-key" in key:
            raise ApiKeyError(
                "API key not found or unset.\n"
                "Please set GROQ_API_KEY (for Groq AI) or GROK_API_KEY (for xAI Grok) in your .env file."
            )
        clean_key = key.strip()
        provider = "groq" if clean_key.startswith("gsk_") else "grok"
        return clean_key, provider


# ==============================================================================
# SECTION 3: SCHEMA DISCOVERY (PostgreSQL & PostGIS)
# ==============================================================================

class SchemaDiscovery:
    """Introspects tables, columns, geometry types, and SRIDs directly from PostgreSQL/PostGIS."""

    def __init__(self, connection_params: Dict[str, Any]):
        self.params = connection_params
        self.schema_summary = ""
        self.spatial_columns: Dict[str, List[Dict[str, Any]]] = {}  # table -> [{column, srid, type}]

    def _get_connection(self):
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary is not installed. Please run: pip install psycopg2-binary")
        clean_params = {k: v for k, v in self.params.items() if not k.startswith("_")}
        return psycopg2.connect(**clean_params)

    def discover(self) -> str:
        """Discovers tables, columns, data types, and PostGIS geometry metadata."""
        try:
            conn = self._get_connection()
        except psycopg2.OperationalError as e:
            host = self.params.get("host", "localhost")
            port = self.params.get("port", 5432)
            db = self.params.get("database", "unknown")
            user = self.params.get("user", "unknown")
            raise ConnectionError(
                f"Could not connect to PostgreSQL database '{db}' on {host}:{port} as user '{user}'.\n"
                f"PostgreSQL error: {e}"
            )

        try:
            with conn.cursor() as cur:
                # 1. Fetch tables and columns from information_schema
                cur.execute("""
                    SELECT 
                        c.table_schema,
                        c.table_name,
                        c.column_name,
                        c.data_type,
                        c.is_nullable
                    FROM information_schema.columns c
                    JOIN information_schema.tables t 
                        ON c.table_schema = t.table_schema AND c.table_name = t.table_name
                    WHERE t.table_type = 'BASE TABLE'
                      AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
                      AND c.table_name NOT IN ('spatial_ref_sys')
                    ORDER BY c.table_schema, c.table_name, c.ordinal_position;
                """)
                columns_rows = cur.fetchall()

                # 2. Fetch PostGIS geometry columns if available
                geom_meta: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
                try:
                    cur.execute("""
                        SELECT 
                            f_table_schema,
                            f_table_name,
                            f_geometry_column,
                            coord_dimension,
                            srid,
                            type
                        FROM geometry_columns;
                    """)
                    for r in cur.fetchall():
                        geom_meta[(r[0], r[1], r[2])] = {
                            "coord_dimension": r[3],
                            "srid": r[4],
                            "type": r[5]
                        }
                except Exception:
                    # PostGIS might not be installed or geometry_columns view missing
                    conn.rollback()

                # Also try geography_columns
                try:
                    cur.execute("""
                        SELECT 
                            f_table_schema,
                            f_table_name,
                            f_geography_column,
                            coord_dimension,
                            srid,
                            type
                        FROM geography_columns;
                    """)
                    for r in cur.fetchall():
                        geom_meta[(r[0], r[1], r[2])] = {
                            "coord_dimension": r[3],
                            "srid": r[4],
                            "type": f"GEOGRAPHY({r[5]})"
                        }
                except Exception:
                    conn.rollback()

        finally:
            conn.close()

        # Build schema representation
        tables_dict: Dict[str, List[Dict[str, Any]]] = {}
        self.spatial_columns.clear()

        for schema_name, table_name, col_name, data_type, is_nullable in columns_rows:
            full_tbl = f"{schema_name}.{table_name}" if schema_name != "public" else table_name
            if full_tbl not in tables_dict:
                tables_dict[full_tbl] = []

            col_info = {
                "name": col_name,
                "data_type": data_type,
                "is_nullable": is_nullable == "YES",
            }

            lookup_key = (schema_name, table_name, col_name)
            if lookup_key in geom_meta:
                g_info = geom_meta[lookup_key]
                col_info["geometry_type"] = g_info["type"]
                col_info["srid"] = g_info["srid"]
                col_info["is_spatial"] = True
                
                if full_tbl not in self.spatial_columns:
                    self.spatial_columns[full_tbl] = []
                self.spatial_columns[full_tbl].append({
                    "column": col_name,
                    "srid": g_info["srid"],
                    "type": g_info["type"]
                })
            elif "geometry" in data_type.lower() or "geography" in data_type.lower():
                col_info["is_spatial"] = True
                if full_tbl not in self.spatial_columns:
                    self.spatial_columns[full_tbl] = []
                self.spatial_columns[full_tbl].append({
                    "column": col_name,
                    "srid": 0,
                    "type": "GEOMETRY"
                })
            else:
                col_info["is_spatial"] = False

            tables_dict[full_tbl].append(col_info)

        # Format into clean string for Grok
        lines = []
        for tbl, cols in tables_dict.items():
            lines.append(f"Table: {tbl}")
            for c in cols:
                if c.get("is_spatial"):
                    lines.append(
                        f"  - {c['name']} (SPATIAL: {c.get('geometry_type', 'GEOMETRY')}, SRID: {c.get('srid', 'unknown')})"
                    )
                else:
                    lines.append(f"  - {c['name']} ({c['data_type']})")
            lines.append("")

        self.schema_summary = "\n".join(lines).strip()
        return self.schema_summary


# ==============================================================================
# SECTION 4: CRITICALITY SYSTEM (4 Tiers & Precedence)
# ==============================================================================

class CriticalityEngine:
    """
    Evaluates SQL criticality across 4 tiers:
    neutral < normal < critical < very critical.
    Enforces rule-based checks where higher rank always wins.
    """

    @staticmethod
    def normalize_sql(sql_query: str) -> str:
        """Removes comments and extraneous whitespace from SQL."""
        # Strip block comments /* ... */
        no_block = re.sub(r"/\*.*?\*/", " ", sql_query, flags=re.DOTALL)
        # Strip line comments -- ...
        no_line = re.sub(r"--[^\n]*", " ", no_block)
        return " ".join(no_line.split())

    @classmethod
    def evaluate_rules(cls, sql_query: str) -> Tuple[str, str]:
        """
        Rule-based check returning (tier, explanation).
        Rules:
          - DROP, TRUNCATE, ALTER -> very critical
          - UPDATE or DELETE without WHERE -> very critical
          - UPDATE or DELETE with WHERE -> critical
          - INSERT, CREATE, GRANT, REVOKE -> critical
          - Plain SELECT, EXPLAIN, SHOW -> neutral
          - Other commands -> normal
        """
        clean = cls.normalize_sql(sql_query).upper()
        if not clean:
            return TIER_NEUTRAL, "Empty query"

        # Check for multiple statements separated by semicolon
        statements = [s.strip() for s in re.split(r";\s*", clean) if s.strip()]
        
        highest_tier = TIER_NEUTRAL
        highest_reason = "Read-only SELECT / query."

        for stmt in statements:
            tokens = stmt.split()
            first_keyword = tokens[0] if tokens else ""

            # Check Very Critical keywords
            if any(stmt.startswith(kw) or f" {kw} " in f" {stmt} " for kw in ["DROP ", "TRUNCATE ", "ALTER "]):
                return TIER_VERY_CRITICAL, "Contains destructive structural DDL (DROP, TRUNCATE, or ALTER)."

            if stmt.startswith("DELETE") or stmt.startswith("UPDATE"):
                has_where = " WHERE " in f" {stmt} "
                if not has_where:
                    return TIER_VERY_CRITICAL, f"{first_keyword} statement without a WHERE clause will modify or delete ALL table rows!"
                else:
                    if TIER_RANKS[TIER_CRITICAL] > TIER_RANKS[highest_tier]:
                        highest_tier = TIER_CRITICAL
                        highest_reason = f"{first_keyword} statement modifying table rows with a WHERE clause."

            elif stmt.startswith("INSERT") or stmt.startswith("CREATE") or stmt.startswith("GRANT") or stmt.startswith("REVOKE"):
                if TIER_RANKS[TIER_CRITICAL] > TIER_RANKS[highest_tier]:
                    highest_tier = TIER_CRITICAL
                    highest_reason = f"{first_keyword} operation that mutates data or database schema."

            elif stmt.startswith("SELECT") or stmt.startswith("EXPLAIN") or stmt.startswith("SHOW") or stmt.startswith("DESCRIBE"):
                # Check if it contains a destructive CTE, function, or subquery like DO, pg_sleep, etc.
                if any(w in stmt for w in ["DELETE ", "UPDATE ", "DROP ", "INSERT "]):
                    if TIER_RANKS[TIER_CRITICAL] > TIER_RANKS[highest_tier]:
                        highest_tier = TIER_CRITICAL
                        highest_reason = "SELECT query contains data-modifying CTE or subqueries."
                # Remains neutral unless something higher was found
            else:
                if TIER_RANKS[TIER_NORMAL] > TIER_RANKS[highest_tier]:
                    highest_tier = TIER_NORMAL
                    highest_reason = f"Database operation: {first_keyword}"

        return highest_tier, highest_reason

    @classmethod
    def resolve_tier(cls, grok_proposed: str, rule_tier: str, rule_reason: str, grok_reason: str) -> Tuple[str, str]:
        """
        Compares Grok's proposed label with code-based rule.
        The HIGHER of the two always wins!
        """
        grok_clean = (grok_proposed or "").strip().lower()
        if grok_clean not in TIER_RANKS:
            # Fallback if Grok returned unknown label
            grok_clean = TIER_NEUTRAL

        grok_rank = TIER_RANKS[grok_clean]
        rule_rank = TIER_RANKS[rule_tier]

        if rule_rank >= grok_rank:
            final_tier = rule_tier
            final_reason = rule_reason
        else:
            final_tier = grok_clean
            final_reason = grok_reason or f"Elevated by Grok model assessment to {grok_clean}."

        return final_tier, final_reason


# ==============================================================================
# SECTION 5: ROLLING IN-MEMORY CONVERSATION HISTORY
# ==============================================================================

class ConversationMemory:
    """Maintains a rolling memory of the last 3-5 QA turns in-memory only."""

    def __init__(self, maxlen: int = 5):
        self.history: deque = deque(maxlen=maxlen)

    def add(self, question: str, sql_query: str):
        self.history.append({"question": question, "sql": sql_query})

    def clear(self):
        self.history.clear()

    def get_context_for_prompt(self) -> str:
        if not self.history:
            return "No previous conversation history."
        lines = []
        for idx, item in enumerate(self.history, 1):
            lines.append(f"Turn {idx}:")
            lines.append(f"  User: {item['question']}")
            lines.append(f"  Generated SQL: {item['sql']}")
        return "\n".join(lines)

    def __len__(self):
        return len(self.history)


# ==============================================================================
# SECTION 6: GROK API CLIENT
# ==============================================================================

class GrokApiError(Exception):
    """Raised when Grok API fails or times out."""
    pass


class GrokClient:
    """Interacts with xAI's Grok API."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GROK_MODEL,
        api_url: str = DEFAULT_GROK_API_URL,
        provider: str = "grok"
    ):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.provider = provider
        self.provider_label = "Groq" if provider == "groq" else "Grok"

    def generate_sql(
        self,
        question: str,
        schema: str,
        memory_context: str,
        error_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends question, schema, and rolling memory to Grok.
        Returns a dict: {"sql": ..., "criticality": ..., "risk_explanation": ...}
        """
        if requests is None:
            raise RuntimeError("requests package is not installed. Please run: pip install requests")

        system_prompt = (
            "You are Postmate, an expert assistant that converts natural language into PostgreSQL and PostGIS SQL queries.\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. You only output valid PostgreSQL/PostGIS SQL.\n"
            "2. Be PostGIS aware: utilize spatial functions (e.g., ST_DWithin, ST_Intersects, ST_Contains, ST_Distance, ST_Transform, ST_Area) where appropriate.\n"
            "3. If spatial columns exist and geometry is selected or relevant for visualization, automatically include a GeoJSON conversion in WGS84 (SRID 4326) as an aliased column '__geojson__', for example: ST_AsGeoJSON(ST_Transform(<geom_column>, 4326)) AS __geojson__ alongside other requested attributes.\n"
            "4. Propose a criticality tier from exactly one of: 'neutral', 'normal', 'critical', 'very critical'.\n"
            "   - 'neutral': Plain SELECT, EXPLAIN, SHOW queries that read data.\n"
            "   - 'normal': Non-destructive utility operations.\n"
            "   - 'critical': INSERT, or UPDATE/DELETE with a WHERE clause, CREATE, etc.\n"
            "   - 'very critical': DROP, TRUNCATE, ALTER, or UPDATE/DELETE without a WHERE clause.\n"
            "5. If 'very critical' or 'critical', provide a concise plain English explanation in 'risk_explanation'.\n"
            "6. Output MUST be valid JSON with keys: 'sql', 'criticality', 'risk_explanation'. Do NOT wrap in markdown code blocks like ```json ... ```, just pure JSON.\n"
            "7. OPENSTREETMAP / SPATIAL DATA TIPS:\n"
            "   - When querying tables like 'osm_points', 'osm_lines', 'osm_multipolygons', the data is ALREADY scoped to the user's local area. Do NOT invent artificial boundary subqueries like 'WITH boundary AS (SELECT ... LIMIT 1)' unless explicitly requested by the user.\n"
            "   - Search flexibly using both 'name ILIKE %keyword%' and 'other_tags ILIKE %keyword%' (e.g. for airport: name ILIKE '%airport%' OR other_tags ILIKE '%aeroway%').\n"
            "   - For acronyms or abbreviations (e.g. 'DBMS', 'NIT', 'TMH'), match flexibly with dots, wildcards, and common expansions (e.g. for 'TMH': name ILIKE '%TMH%' OR name ILIKE '%Tata Main Hospital%'; for 'DBMS': name ILIKE '%DBMS%' OR name ILIKE '%D.B.M.S.%' OR name ILIKE '%D%B%M%S%'). When matching phrases like 'X school', separate keywords with wildcards (e.g. '%DBMS%school%' or '(name ILIKE %DBMS% AND name ILIKE %school%)') so intermediate words do not cause 0 results.\n"
        )

        user_content = f"DATABASE SCHEMA:\n{schema}\n\nCONVERSATION HISTORY:\n{memory_context}\n\nUSER QUESTION:\n{question}\n"

        if error_context:
            user_content += (
                f"\nNOTE: The previous query failed with this PostgreSQL error:\n{error_context}\n"
                "Please fix the error and generate a corrected SQL query that will execute successfully."
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        try:
            resp = requests.post(self.api_url, headers=headers, json=payload, timeout=45)
        except requests.exceptions.Timeout:
            raise GrokApiError(f"{self.provider_label} API request timed out (45s). Please check your internet connection.")
        except requests.exceptions.ConnectionError as e:
            raise GrokApiError(f"Could not connect to {self.provider_label} API: {e}")
        except Exception as e:
            raise GrokApiError(f"Network error while calling {self.provider_label} API: {e}")

        if resp.status_code != 200:
            error_body = resp.text
            try:
                err_json = resp.json()
                if "error" in err_json:
                    error_body = err_json["error"].get("message", error_body)
            except Exception:
                pass
            raise GrokApiError(f"{self.provider_label} API returned HTTP {resp.status_code}: {error_body}")

        try:
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise GrokApiError(f"Unexpected response format from {self.provider_label} API: {e}")

        # Parse JSON from model output
        # Handle cases where model might enclose in ```json ... ```
        clean_text = raw_text
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
            clean_text = re.sub(r"\s*```$", "", clean_text)

        try:
            parsed = json.loads(clean_text)
            sql_query = parsed.get("sql", "").strip()
            criticality = parsed.get("criticality", "neutral").strip().lower()
            risk_explanation = parsed.get("risk_explanation", "")
            return {
                "sql": sql_query,
                "criticality": criticality,
                "risk_explanation": risk_explanation
            }
        except json.JSONDecodeError:
            # Fallback heuristic if Grok didn't return strict JSON
            # Extract SQL directly
            sql_match = re.search(r"(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)[\s\S]+", raw_text, re.IGNORECASE)
            sql_query = sql_match.group(0).strip() if sql_match else raw_text
            return {
                "sql": sql_query,
                "criticality": "neutral",
                "risk_explanation": "Extracted from freeform response"
            }


# ==============================================================================
# SECTION 7: SPATIAL MAP VISUALIZER (Folium)
# ==============================================================================

class MapVisualizer:
    """
    Renders query results containing geometry to a single persistent 'map.html'.
    Uses a plain OpenStreetMap basemap, no preloaded database layers, and includes
    a manual refresh button.
    """

    @classmethod
    def generate_map(cls, rows: List[Dict[str, Any]], spatial_meta: Dict[str, Any], output_path: str = MAP_FILENAME) -> bool:
        """
        Inspects query rows for '__geojson__' or spatial columns and writes map.html.
        Returns True if a map was generated, False if no geometry was present.
        """
        if folium is None:
            return False

        if not rows:
            return False

        features = []
        bounds = []  # [[lat, lon], ...]

        for row in rows:
            geojson_str = None
            properties = {}

            # 1. Look for '__geojson__' column generated by Grok
            for k, v in row.items():
                if k == "__geojson__" or k.endswith("_geojson"):
                    geojson_str = v
                else:
                    # Strip binary representations from tooltip properties
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        properties[k] = v
                    else:
                        properties[k] = str(v)

            # 2. If no explicit __geojson__ column, check if any column contains GeoJSON or WKB
            if not geojson_str and wkb is not None:
                for k, v in row.items():
                    if isinstance(v, str) and (v.startswith("{") and "coordinates" in v):
                        geojson_str = v
                        break
                    elif isinstance(v, (bytes, memoryview)):
                        try:
                            geom_obj = wkb.loads(bytes(v))
                            geojson_str = json.dumps(mapping(geom_obj))
                            break
                        except Exception:
                            pass
                    elif isinstance(v, str) and len(v) > 16 and re.match(r"^[0-9A-Fa-f]+$", v):
                        # Hex-encoded WKB
                        try:
                            geom_obj = wkb.loads(bytes.fromhex(v))
                            geojson_str = json.dumps(mapping(geom_obj))
                            break
                        except Exception:
                            pass

            if geojson_str:
                try:
                    if isinstance(geojson_str, str):
                        geom_dict = json.loads(geojson_str)
                    elif isinstance(geojson_str, dict):
                        geom_dict = geojson_str
                    else:
                        continue

                    # Extract sample coordinate for bounds
                    coords = cls._extract_coordinates(geom_dict)
                    for pt in coords:
                        # Leaflet bounds are [lat, lon]
                        bounds.append([pt[1], pt[0]])

                    feature = {
                        "type": "Feature",
                        "geometry": geom_dict,
                        "properties": properties
                    }
                    features.append(feature)
                except Exception:
                    continue

        if not features:
            return False

        # Create Folium Map with clean OpenStreetMap tiles that support local file:///
        if bounds:
            # Center around midpoint of bounds
            lats = [b[0] for b in bounds]
            lons = [b[1] for b in bounds]
            center_lat = sum(lats) / len(lats)
            center_lon = sum(lons) / len(lons)
            m = folium.Map(location=[center_lat, center_lon], zoom_start=13, tiles=None)
        else:
            m = folium.Map(location=[0, 0], zoom_start=2, tiles=None)

        # Use Esri World Street Map tiles (100% free, NO API key required, zero watermarks)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            attr="Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), Esri (Thailand), TomTom",
            name="World Street Map",
            max_zoom=19,
            control=False
        ).add_to(m)

        geojson_layer = {
            "type": "FeatureCollection",
            "features": features
        }

        # Style function for clean highlighted display
        def style_fn(x):
            return {
                "fillColor": "#3388ff",
                "color": "#0044cc",
                "weight": 2.5,
                "fillOpacity": 0.45,
            }

        # Tooltip for attributes
        fields = [k for k in rows[0].keys() if k not in ["__geojson__"] and not k.endswith("_geojson")]
        tooltip = folium.GeoJsonTooltip(fields=fields[:8]) if fields else None

        folium.GeoJson(
            geojson_layer,
            name="Current Query Result",
            style_function=style_fn,
            tooltip=tooltip,
            popup=folium.GeoJsonPopup(fields=fields[:8]) if fields else None
        ).add_to(m)

        # Fit bounds
        if bounds:
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

        # Add manual refresh button in the top-right corner
        refresh_html = """
        <div style="
            position: fixed; 
            top: 15px; 
            right: 15px; 
            z-index: 9999; 
            background: white; 
            padding: 8px 14px; 
            border-radius: 8px; 
            box-shadow: 0 3px 12px rgba(0,0,0,0.3);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 13px;
            font-weight: 600;
        ">
            <button onclick="window.location.reload();" style="
                background: #2563eb; 
                color: white; 
                border: none; 
                padding: 6px 12px; 
                border-radius: 6px; 
                cursor: pointer;
                display: flex;
                align-items: center;
                gap: 6px;
                font-weight: 500;
            ">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/>
                </svg>
                Refresh Map
            </button>
        </div>
        """
        if Element:
            m.get_root().html.add_child(Element(refresh_html))

        m.save(output_path)
        return True

    @classmethod
    def _extract_coordinates(cls, geom: Dict[str, Any]) -> List[List[float]]:
        """Recursively extracts [lon, lat] pairs from GeoJSON geometry."""
        coords = []
        g_type = geom.get("type", "")
        raw_coords = geom.get("coordinates", [])

        def flatten(c):
            if not isinstance(c, list):
                return
            if len(c) >= 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                coords.append([c[0], c[1]])
            else:
                for sub in c:
                    flatten(sub)

        flatten(raw_coords)
        return coords


# ==============================================================================
# SECTION 8: QUERY EXECUTOR & DATABASE RUNNER
# ==============================================================================

class QueryResult:
    def __init__(self, rows: List[Dict[str, Any]], column_names: List[str], rowcount: int, execution_time_ms: float):
        self.rows = rows
        self.column_names = column_names
        self.rowcount = rowcount
        self.execution_time_ms = execution_time_ms


class DatabaseRunner:
    """Executes SQL queries against PostgreSQL and returns results."""

    def __init__(self, connection_params: Dict[str, Any]):
        self.params = connection_params

    def execute_query(self, sql_query: str) -> QueryResult:
        """Executes a query and returns structured results."""
        import time
        clean_params = {k: v for k, v in self.params.items() if not k.startswith("_")}
        
        start_time = time.time()
        with psycopg2.connect(**clean_params) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql_query)
                elapsed_ms = (time.time() - start_time) * 1000

                rows = []
                col_names = []
                rowcount = cur.rowcount

                if cur.description:
                    col_names = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()
                    # Convert to standard dicts
                    rows = [dict(r) for r in rows]

                conn.commit()
                return QueryResult(rows, col_names, rowcount, elapsed_ms)


# ==============================================================================
# SECTION 9: TERMINAL UI & DISPLAY HELPERS (Rich)
# ==============================================================================

class TerminalUI:
    """Handles formatted terminal outputs with rich visuals."""

    @staticmethod
    def print_welcome(profile_name: str, host: str, db: str, model: str):
        if console:
            welcome_text = Text()
            welcome_text.append("🚀 POSTMATE ", style="bold cyan")
            welcome_text.append("— PostgreSQL & PostGIS AI Assistant\n", style="bold white")
            welcome_text.append(f"Connected to: ", style="dim")
            welcome_text.append(f"{db}@{host} ", style="bold green")
            welcome_text.append(f"[{profile_name}]", style="cyan")
            welcome_text.append(f"  |  Model: ", style="dim")
            welcome_text.append(f"{model}\n", style="bold magenta")
            welcome_text.append("Commands: ", style="dim")
            welcome_text.append("reset ", style="yellow")
            welcome_text.append("(clear memory), ")
            welcome_text.append("schema ", style="yellow")
            welcome_text.append("(view tables), ")
            welcome_text.append("help", style="yellow")
            welcome_text.append(", ")
            welcome_text.append("exit", style="yellow")
            console.print(Panel(welcome_text, border_style="cyan"))
        else:
            print(f"=== POSTMATE: PostgreSQL & PostGIS AI Assistant ===")
            print(f"Connected: {db}@{host} [{profile_name}] | Model: {model}\n")

    @staticmethod
    def print_sql(sql_query: str, tier: str, reason: str):
        """Always prints the generated SQL to terminal for EVERY query."""
        style, label = TIER_STYLES.get(tier, ("white", tier.upper()))
        
        if console:
            header = Text()
            header.append("Generated SQL  ", style="bold white")
            header.append(f"[{label}]", style=style)
            
            syntax = Syntax(sql_query.strip(), "sql", theme="monokai", line_numbers=False)
            panel_content = Text()
            if reason and tier in [TIER_CRITICAL, TIER_VERY_CRITICAL]:
                panel_content.append(f"Notice: {reason}\n\n", style="italic " + style)
            
            console.print(Panel(syntax, title=header, subtitle=f"[dim]{reason}[/dim]" if reason else None, border_style=style.split()[0]))
        else:
            print(f"\n--- Generated SQL [{label}] ---")
            if reason:
                print(f"Notice: {reason}")
            print(sql_query.strip())
            print("-----------------------------\n")

    @staticmethod
    def print_results(result: QueryResult):
        """Renders query results in a formatted table or displays rowcount."""
        if not console:
            if not result.rows:
                print(f"Query executed successfully. Affected rows: {result.rowcount}")
                return
            for row in result.rows[:50]:
                print(row)
            if len(result.rows) > 50:
                print(f"... and {len(result.rows) - 50} more rows.")
            return

        if not result.rows:
            if result.rowcount >= 0:
                console.print(f"[green]✓ Query executed successfully.[/green] ([dim]{result.rowcount} row(s) affected in {result.execution_time_ms:.1f}ms[/dim])\n")
            else:
                console.print(f"[yellow]ℹ Query returned 0 rows.[/yellow] ([dim]{result.execution_time_ms:.1f}ms[/dim])\n")
            return

        # Display table
        table = Table(title=f"Query Results ({len(result.rows)} rows in {result.execution_time_ms:.1f}ms)", show_lines=False)

        # Exclude '__geojson__' from the terminal table to keep it neat
        display_cols = [c for c in result.column_names if c not in ["__geojson__"]]
        for col in display_cols:
            table.add_column(col, style="cyan", overflow="fold")

        # Display up to 25 rows
        max_display = 25
        for row in result.rows[:max_display]:
            row_vals = []
            for col in display_cols:
                val = row.get(col)
                if val is None:
                    row_vals.append("[dim]NULL[/dim]")
                else:
                    val_str = str(val)
                    if len(val_str) > 60:
                        val_str = val_str[:57] + "..."
                    row_vals.append(val_str)
            table.add_row(*row_vals)

        console.print(table)
        if len(result.rows) > max_display:
            console.print(f"[dim]Showing {max_display} of {len(result.rows)} rows.[/dim]\n")
        else:
            console.print()

    @staticmethod
    def print_map_notice(output_path: str):
        if console:
            console.print(f"🗺️  [bold green]Spatial visualization generated:[/bold green] [underline cyan]{os.path.abspath(output_path)}[/underline cyan]")
            console.print("[dim]Open map.html in your browser to view the interactive map. Click 'Refresh Map' anytime to reload.[/dim]\n")
        else:
            print(f"🗺️ Spatial visualization generated: {os.path.abspath(output_path)}\n")

    @staticmethod
    def print_error(title: str, message: str):
        if console:
            console.print(Panel(Text(message, style="bold red"), title=f"❌ {title}", border_style="red"))
        else:
            print(f"\n❌ ERROR - {title}: {message}\n")

    @staticmethod
    def print_retry_notice(attempt: int, err_msg: str):
        if console:
            console.print(f"[yellow]⚠️ Query failed: {err_msg}[/yellow]")
            console.print("[cyan]🔄 Sending error to Grok for automatic correction (retry 1/1)...[/cyan]")
        else:
            print(f"⚠️ Query failed: {err_msg}")
            print("🔄 Sending error to Grok for automatic correction (retry 1/1)...")


# ==============================================================================
# SECTION 10: CORE WORKFLOW ORCHESTRATOR
# ==============================================================================

class Postmate:
    """Main Postmate controller."""

    def __init__(self, connection_name: Optional[str] = None):
        self.conn_params, self.grok_opts = ConfigManager.load_config(connection_name)
        self.api_key, self.provider = ConfigManager.get_api_key()
        
        # Configure model and API URL based on provider (Groq vs xAI Grok)
        if self.provider == "groq":
            default_model = DEFAULT_GROQ_MODEL
            default_url = DEFAULT_GROQ_API_URL
        else:
            default_model = DEFAULT_GROK_MODEL
            default_url = DEFAULT_GROK_API_URL

        # Allow user config to override if explicitly set to a matching provider model
        config_model = self.grok_opts.get("model")
        if self.provider == "groq" and config_model and "grok" in config_model.lower():
            # Config had a grok model but user supplied a Groq key
            model_name = default_model
        else:
            model_name = config_model or default_model

        api_url = self.grok_opts.get("api_url", default_url)
        self.grok_client = GrokClient(api_key=self.api_key, model=model_name, api_url=api_url, provider=self.provider)
        
        self.schema_discovery = SchemaDiscovery(self.conn_params)
        self.db_runner = DatabaseRunner(self.conn_params)
        self.memory = ConversationMemory(maxlen=5)
        self.cached_schema = ""

    def initialize(self):
        """Discovers database schema on startup."""
        if console:
            console.print("[dim cyan]Discovering PostgreSQL & PostGIS schema...[/dim cyan]")
            self.cached_schema = self.schema_discovery.discover()
            console.print("[dim green]✓ Schema discovered successfully.[/dim green]\n")
        else:
            print("Discovering PostgreSQL & PostGIS schema...")
            self.cached_schema = self.schema_discovery.discover()
            print("✓ Schema discovered successfully.\n")

    def process_query(self, user_question: str) -> bool:
        """
        Processes a single user question:
        1. Calls Grok to generate SQL & propose criticality.
        2. Evaluates rule-based criticality.
        3. Prints SQL to terminal.
        4. Enforces confirmation if critical / very critical.
        5. Executes query.
        6. On failure, retries ONCE with Grok.
        7. Generates map if spatial data is present.
        8. Updates conversation memory.
        """
        # Step 1: Call Grok
        try:
            memory_ctx = self.memory.get_context_for_prompt()
            grok_response = self.grok_client.generate_sql(
                question=user_question,
                schema=self.cached_schema,
                memory_context=memory_ctx
            )
        except GrokApiError as e:
            TerminalUI.print_error("Grok API Error", str(e))
            return False

        sql_query = grok_response.get("sql", "").strip()
        grok_crit = grok_response.get("criticality", TIER_NEUTRAL)
        grok_risk = grok_response.get("risk_explanation", "")

        if not sql_query:
            TerminalUI.print_error("Generation Error", "Grok was unable to generate a valid SQL query.")
            return False

        # Execute with retry loop (max 1 retry)
        success = self._handle_execution_with_retry(
            original_question=user_question,
            sql_query=sql_query,
            grok_crit=grok_crit,
            grok_risk=grok_risk
        )
        return success

    def _handle_execution_with_retry(
        self,
        original_question: str,
        sql_query: str,
        grok_crit: str,
        grok_risk: str,
        retry_count: int = 0
    ) -> bool:
        """Evaluates criticality, prompts user if needed, executes query, and retries once on DB error."""
        # 1. Criticality evaluation
        rule_tier, rule_reason = CriticalityEngine.evaluate_rules(sql_query)
        final_tier, final_reason = CriticalityEngine.resolve_tier(
            grok_proposed=grok_crit,
            rule_tier=rule_tier,
            rule_reason=rule_reason,
            grok_reason=grok_risk
        )

        # 2. ALWAYS print generated SQL to terminal
        TerminalUI.print_sql(sql_query, final_tier, final_reason)

        # 3. Confirmation checks
        if final_tier == TIER_VERY_CRITICAL:
            if console:
                console.print(Panel(
                    f"[bold red]POTENTIAL RISK EXPLANATION:[/bold red]\n{final_reason}\n\n"
                    "This is a VERY CRITICAL query that can permanently alter or destroy data/schema.\n"
                    "To proceed, you must type [bold white]CONFIRM[/bold white].",
                    border_style="red"
                ))
                confirm_input = console.input("[bold red]Enter 'CONFIRM' to execute: [/bold red]").strip()
            else:
                print(f"POTENTIAL RISK: {final_reason}")
                confirm_input = input("Enter 'CONFIRM' to execute: ").strip()

            if confirm_input != "CONFIRM":
                if console:
                    console.print("[yellow]Execution cancelled by user.[/yellow]\n")
                else:
                    print("Execution cancelled by user.\n")
                return False

        elif final_tier == TIER_CRITICAL:
            if console:
                confirm_input = console.input("[bold yellow]Execute this critical query? [yes/no]: [/bold yellow]").strip().lower()
            else:
                confirm_input = input("Execute this critical query? [yes/no]: ").strip().lower()

            if confirm_input not in ["yes", "y"]:
                if console:
                    console.print("[yellow]Execution cancelled by user.[/yellow]\n")
                else:
                    print("Execution cancelled by user.\n")
                return False

        # 4. Neutral / Normal: Runs immediately without pause
        try:
            result = self.db_runner.execute_query(sql_query)
            TerminalUI.print_results(result)

            # 5. Check if geometry exists and update map.html
            has_map = MapVisualizer.generate_map(
                rows=result.rows,
                spatial_meta=self.schema_discovery.spatial_columns,
                output_path=MAP_FILENAME
            )
            if has_map:
                TerminalUI.print_map_notice(MAP_FILENAME)

            # 6. Save to rolling memory
            self.memory.add(original_question, sql_query)
            return True

        except psycopg2.Error as e:
            pg_err_msg = str(e).strip()
            if retry_count == 0:
                # Single retry loop
                TerminalUI.print_retry_notice(1, pg_err_msg)
                try:
                    memory_ctx = self.memory.get_context_for_prompt()
                    correction_response = self.grok_client.generate_sql(
                        question=original_question,
                        schema=self.cached_schema,
                        memory_context=memory_ctx,
                        error_context=f"SQL: {sql_query}\nPostgres Error: {pg_err_msg}"
                    )
                    corrected_sql = correction_response.get("sql", "").strip()
                    corrected_crit = correction_response.get("criticality", TIER_NEUTRAL)
                    corrected_risk = correction_response.get("risk_explanation", "")

                    if not corrected_sql:
                        TerminalUI.print_error("Retry Failed", "Grok was unable to suggest a corrected SQL query.")
                        return False

                    return self._handle_execution_with_retry(
                        original_question=original_question,
                        sql_query=corrected_sql,
                        grok_crit=corrected_crit,
                        grok_risk=corrected_risk,
                        retry_count=1
                    )
                except GrokApiError as api_err:
                    TerminalUI.print_error("Grok Retry Error", str(api_err))
                    return False
            else:
                # Retry also failed -> show clear message and stop
                TerminalUI.print_error("Query Execution Failed", f"Database error after retry:\n{pg_err_msg}")
                return False

        except Exception as e:
            TerminalUI.print_error("Unexpected Error", str(e))
            return False


# ==============================================================================
# SECTION 11: INTERACTIVE REPL & CLI ENTRYPOINT
# ==============================================================================

def run_repl(postmate: Postmate):
    """Runs the interactive REPL loop."""
    profile = postmate.conn_params.get("_profile_name", "default")
    host = postmate.conn_params.get("host", "localhost")
    db = postmate.conn_params.get("database", "postgres")
    model = postmate.grok_client.model

    TerminalUI.print_welcome(profile, host, db, model)

    while True:
        try:
            if console:
                user_input = console.input("[bold cyan]postmate>[/bold cyan] ").strip()
            else:
                user_input = input("postmate> ").strip()

            if not user_input:
                continue

            cmd_lower = user_input.lower()

            if cmd_lower in ["exit", "quit", "q"]:
                if console:
                    console.print("[dim]Goodbye![/dim]")
                else:
                    print("Goodbye!")
                break

            elif cmd_lower in ["reset", "/reset", "clear"]:
                postmate.memory.clear()
                if console:
                    console.print("[green]🔄 In-memory conversation context has been reset.[/green]\n")
                else:
                    print("🔄 In-memory conversation context has been reset.\n")
                continue

            elif cmd_lower in ["schema", "/schema"]:
                if console:
                    console.print(Panel(postmate.cached_schema, title="Discovered Database Schema", border_style="cyan"))
                else:
                    print("\n--- Discovered Schema ---\n" + postmate.cached_schema + "\n")
                continue

            elif cmd_lower in ["history", "/history"]:
                if len(postmate.memory) == 0:
                    rprint("[dim]No rolling memory recorded yet.[/dim]\n")
                else:
                    rprint(Panel(postmate.memory.get_context_for_prompt(), title="Rolling Memory (Last turns)", border_style="yellow"))
                continue

            elif cmd_lower in ["clean", "/clean"]:
                from clean_db import main as run_clean
                run_clean()
                postmate.initialize()
                continue

            elif cmd_lower.startswith("import ") or cmd_lower.startswith("/import "):
                import_arg = user_input.split(" ", 1)[1].strip().strip('"').strip("'")
                from import_gis_data import collect_files, import_single_file, get_db_engine
                files = collect_files(import_arg)
                if not files:
                    rprint(f"[yellow]No spatial files found at: {import_arg}[/yellow]\n")
                else:
                    engine, database = get_db_engine()
                    for f in files:
                        import_single_file(f, engine, database)
                    postmate.initialize()
                    rprint(f"[green]✓ Import complete and schema reloaded![/green]\n")
                continue

            elif cmd_lower in ["help", "/help", "?"]:
                help_msg = (
                    "Available Commands:\n"
                    "  [cyan]<natural language question>[/cyan]  - Ask any database question (e.g. 'Calculate area of parcels')\n"
                    "  [yellow]import <path>[/yellow]                  - Import a shapefile, geojson, csv, or folder directly\n"
                    "  [yellow]clean[/yellow]                          - Wipe all user tables from the database\n"
                    "  [yellow]schema[/yellow]                         - Show auto-discovered database & PostGIS tables\n"
                    "  [yellow]reset[/yellow]                          - Clear rolling conversation memory\n"
                    "  [yellow]history[/yellow]                        - Show current in-memory conversation context\n"
                    "  [yellow]exit / quit[/yellow]                    - Exit Postmate\n"
                )
                if console:
                    console.print(Panel(help_msg, title="Postmate Help", border_style="blue"))
                else:
                    print(help_msg)
                continue

            # Process natural language question
            postmate.process_query(user_input)

        except (KeyboardInterrupt, EOFError):
            if console:
                console.print("\n[dim]Session interrupted. Goodbye![/dim]")
            else:
                print("\nSession interrupted. Goodbye!")
            break


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Postmate: Plain English to PostgreSQL and PostGIS CLI powered by Grok"
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Optional plain English query for one-off execution."
    )
    parser.add_argument(
        "-c", "--connection",
        dest="connection",
        default=None,
        help="Named database connection profile from config.json."
    )
    parser.add_argument(
        "--show-schema",
        action="store_true",
        help="Display auto-discovered schema and exit."
    )
    parser.add_argument(
        "--test-rules",
        action="store_true",
        help="Run self-test on criticality rules and exit."
    )

    args = parser.parse_args()

    # Self-test mode for criticality checks
    if args.test_rules:
        run_criticality_self_tests()
        sys.exit(0)

    try:
        postmate = Postmate(connection_name=args.connection)
        postmate.initialize()
    except (ConfigError, ApiKeyError, ConnectionError) as e:
        TerminalUI.print_error("Initialization Error", str(e))
        sys.exit(1)
    except Exception as e:
        TerminalUI.print_error("Fatal Error", f"Unexpected error on startup: {e}")
        sys.exit(1)

    if args.show_schema:
        if console:
            console.print(Panel(postmate.cached_schema, title="Discovered Schema", border_style="cyan"))
        else:
            print(postmate.cached_schema)
        sys.exit(0)

    # One-off query execution
    if args.query:
        success = postmate.process_query(args.query)
        sys.exit(0 if success else 1)

    # Interactive REPL session
    run_repl(postmate)


def run_criticality_self_tests():
    """Internal test suite verifying criticality tier rules."""
    print("Running Postmate Criticality Rule Tests...\n")
    test_cases = [
        ("SELECT * FROM parcels WHERE area > 500", TIER_NEUTRAL),
        ("EXPLAIN ANALYZE SELECT id FROM buildings", TIER_NEUTRAL),
        ("SHOW max_connections", TIER_NEUTRAL),
        ("INSERT INTO roads (name, geom) VALUES ('Main St', ST_GeomFromText('LINESTRING(0 0, 1 1)'))", TIER_CRITICAL),
        ("UPDATE parcels SET owner = 'Jane' WHERE id = 12", TIER_CRITICAL),
        ("DELETE FROM parcels WHERE id = 99", TIER_CRITICAL),
        ("CREATE TABLE test_geom (id int, geom geometry)", TIER_CRITICAL),
        ("DROP TABLE parcels", TIER_VERY_CRITICAL),
        ("TRUNCATE TABLE logs", TIER_VERY_CRITICAL),
        ("ALTER TABLE roads ADD COLUMN lanes int", TIER_VERY_CRITICAL),
        ("UPDATE parcels SET owner = 'Hacked'", TIER_VERY_CRITICAL),  # No WHERE!
        ("DELETE FROM parcels", TIER_VERY_CRITICAL),                  # No WHERE!
    ]

    all_passed = True
    for sql, expected in test_cases:
        tier, reason = CriticalityEngine.evaluate_rules(sql)
        passed = (tier == expected)
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] Expected: {expected:14} Got: {tier:14} | SQL: {sql[:45]}...")
        if not passed:
            all_passed = False
            print(f"    Reason: {reason}")

    # Test precedence rule: higher rank always wins
    print("\nTesting Precedence Resolution (Higher Always Wins):")
    p1_tier, _ = CriticalityEngine.resolve_tier("neutral", "critical", "rule reason", "grok reason")
    p2_tier, _ = CriticalityEngine.resolve_tier("very critical", "neutral", "rule reason", "grok reason")
    p3_tier, _ = CriticalityEngine.resolve_tier("normal", "critical", "rule reason", "grok reason")

    assert p1_tier == TIER_CRITICAL, f"Failed: expected critical, got {p1_tier}"
    assert p2_tier == TIER_VERY_CRITICAL, f"Failed: expected very critical, got {p2_tier}"
    assert p3_tier == TIER_CRITICAL, f"Failed: expected critical, got {p3_tier}"
    print("[PASS] All precedence tests passed!")

    if all_passed:
        print("\nAll Criticality Rule Tests Passed Successfully!")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
