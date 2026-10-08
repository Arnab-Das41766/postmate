# Postmate 📮

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-14+-336791.svg)](https://www.postgresql.org/)
[![PostGIS](https://img.shields.io/badge/PostGIS-3.0+-green.svg)](https://postgis.net/)
[![Groq & Grok](https://img.shields.io/badge/AI-Groq%20%7C%20xAI%20Grok-orange.svg)](https://groq.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Postmate** is an intelligent, standalone natural language interface built specifically for **PostgreSQL** and **PostGIS**. It translates plain English questions in your terminal into spatial and relational SQL queries, enforces an autonomous **4-tier safety criticality system**, auto-discovers live database schemas, self-corrects SQL errors, and visualizes spatial results on an interactive map.

---

## 🌟 Key Features

- **🗣️ Plain English to Spatial SQL**: Converts everyday questions into optimized PostgreSQL & PostGIS queries using **Groq** (free, ultra-fast LPU) or **xAI Grok**.
- **🛡️ 4-Tier Criticality Safety System**: Autonomous safety checks protect your database from accidental data loss. Destructive queries (`DROP`, `TRUNCATE`, unconstrained `DELETE`) require explicit typing of `CONFIRM`.
- **🔍 Zero-Config Live Schema Discovery**: On startup, Postmate introspects `information_schema` and PostGIS `geometry_columns` directly. It always knows your current tables, columns, geometry types, and SRIDs with zero manual schema files.
- **🗺️ Interactive Map (`map.html`)**: Automatically renders spatial results onto an interactive Leaflet map (powered by Esri WorldStreetMap tiles) with a floating **Refresh Map** button. No API key or signup required.
- **🔄 Autonomous Self-Correction**: If PostgreSQL returns an error, Postmate sends the error back to the AI for an automatic fix and retries once before failing.
- **🧠 In-Memory Rolling Context**: Maintains a rolling buffer of recent questions in memory (never saved to disk), enabling natural follow-ups like *"now only show the ones with High School in the name"*.
- **📁 Automated Batch GIS Importer**: Drop any Shapefiles (`.shp`), GeoJSON (`.geojson`), GeoPackage (`.gpkg`), or CSVs into `input_data/` and import them in one command.
- **🔒 Zero Data Leakage**: Postmate only sends your schema definitions and questions to the AI—**never** your actual table rows or query results.

---

## 🏛️ Architecture

```
User (Plain English)
        │
        ▼
┌──────────────────┐      Introspects Schema & SRIDs
│   Postmate CLI   │ ◀──────────────────────────────────┐
└────────┬─────────┘                                    │
         │ (Schema + Question + Memory)                 ▼
         ▼                                     ┌──────────────────┐
┌──────────────────┐                           │    PostgreSQL    │
│ Groq / Grok API  │                           │        +         │
└────────┬─────────┘                           │     PostGIS      │
         │ (Generated SQL + Risk Assessment)   └────────┬─────────┘
         ▼                                              │
┌──────────────────┐                                    │
│ Criticality Check│ ──[ Evaluates Higher Risk Tier ]   │
└────────┬─────────┘                                    │
         │ (Pass / Confirmed)                           │
         ▼                                              │
┌──────────────────┐      Executes Query                │
│ Database Runner  │ ───────────────────────────────────┘
└────────┬─────────┘
         │
         ├──▶ Terminal Table Output (Syntax Highlighted)
         └──▶ Spatial GeoJSON ──▶ map.html (Interactive Leaflet Map)
```

---

## 🛡️ 4-Tier Criticality Safety System

Every generated SQL query is inspected by code-level rule parsers. If the AI model and the code disagree, **the higher risk tier always wins**:

| Tier | Operations | Confirmation Required | Execution Behavior |
| :--- | :--- | :--- | :--- |
| **`neutral`** | `SELECT`, `EXPLAIN`, `SHOW`, `DESCRIBE` | None | Runs immediately with syntax highlighting |
| **`normal`** | Read-only utilities, session settings | None | Runs immediately without pause |
| **`critical`** | `INSERT`, `CREATE`, `UPDATE ... WHERE`, `DELETE ... WHERE` | `yes` / `no` prompt | Shows SQL, asks for confirmation |
| **`very critical`** | `DROP`, `TRUNCATE`, `ALTER`, `UPDATE` (no WHERE), `DELETE` (no WHERE) | Type `CONFIRM` | Explains risk in plain English; requires typing **`CONFIRM`** |

---

## 🚀 Quickstart Guide

### 1. Prerequisites
- Python 3.10+
- PostgreSQL with the PostGIS extension installed (`CREATE EXTENSION postgis;`)

### 2. Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/Arnab-Das41766/postmate.git
cd postmate
pip install -r requirements.txt
```

### 3. Configuration

#### A. Database Connection (`config.json`)
Copy `config.json.example` to `config.json` and set your credentials:
```json
{
  "default_connection": "local",
  "connections": {
    "local": {
      "host": "localhost",
      "port": 5432,
      "database": "postmate_demo",
      "user": "postgres",
      "password": "your_postgres_password"
    }
  },
  "grok": {
    "model": "openai/gpt-oss-120b",
    "temperature": 0.1
  }
}
```

#### B. API Key (`.env`)
Create a `.env` file and add your free **Groq** API key (or **xAI Grok** key):
```bash
# For Groq AI (Free, ultra-fast at https://console.groq.com/):
GROQ_API_KEY=gsk_your_groq_api_key_here

# Or for xAI Grok (https://console.x.ai/):
# GROK_API_KEY=xai_your_xai_api_key_here
```

---

## 📥 Loading Your Spatial Data

### The One-Click "Drop Folder" Importer
Postmate includes a universal batch importer:

1. Drop your files into the **`input_data/`** directory:
   - **Shapefiles**: `.shp` *(plus `.dbf`, `.shx`, `.prj`)*
   - **GeoJSON**: `.geojson` or `.json`
   - **GeoPackage**: `.gpkg` *(multi-layer extracts supported!)*
   - **CSV**: `.csv` *(with latitude and longitude columns)*

2. Run the importer:
   ```bash
   python import_gis_data.py
   ```
   *Automatically sets EPSG:4326, builds spatial GiST indexes, and creates PostGIS tables.*

---

## 💻 Usage

### Interactive REPL Mode (Default)
Start Postmate:
```bash
python postmate.py
```

Inside the session:
```text
postmate> Show all hospitals in osm_points
postmate> Highlight dbms school
postmate> Which schools are within 2km of Tata Main Hospital?
postmate> Show all lakes and water bodies in osm_multipolygons
postmate> Calculate the total length in kilometers of primary roads
```

### Multi-turn Conversational Memory
Postmate remembers recent context without you having to retype queries:
```text
postmate> Find all schools in osm_points
postmate> now only show the ones with High School in the name
postmate> order them by name ascending
```

### Built-in Session Commands
- **`help`**: View available commands.
- **`schema`**: Show live auto-discovered tables, columns, geometry types, and SRIDs.
- **`history`**: View current in-memory conversation context.
- **`reset`**: Clear rolling conversational memory.
- **`clean`**: Drop all user tables from the database.
- **`import <path>`**: Import a spatial file or directory on the fly.
- **`exit`** / **`quit`**: Exit Postmate.

### One-Off Command Mode
Execute a single question directly from your terminal:
```bash
python postmate.py "Calculate the area of jamshedpur_boundary in square kilometers"
```

---

## 🗺️ Spatial Map Visualization (`map.html`)

Whenever a query retrieves spatial geometries or coordinates:
1. Postmate automatically writes and updates **`map.html`**.
2. Open `map.html` in your web browser (`file:///.../map.html`).
3. **Multi-Basemap Switcher (100% Free, No API Keys):** Use the top-right layer control to switch between:
   - 🗺️ **Street Map** (Esri World Street Map)
   - 🛰️ **Satellite Imagery** (Esri World Imagery)
   - 🌙 **Dark Mode** (CARTO Dark Matter)
   - 🏔️ **Topographic** (Esri World Topo Map)
   - 🔘 **Toggle Query Features** overlay on/off
4. **Auto-sync & Live Reload:** Check the **`[x] Auto-sync (2s)`** checkbox in the top-left toolbar to have the map automatically reload as you query in the terminal without clicking refresh!

---

## 🧪 Testing Suite

Run the full automated test suite (verifies criticality rules, precedence, retry loop, memory, and map rendering):
```bash
python test_postmate.py
```

Run rule-based self-tests:
```bash
python postmate.py --test-rules
```

---

## 📂 Repository Structure

```text
postmate/
├── postmate.py              # Main monolithic CLI & engine
├── import_gis_data.py       # Batch spatial data importer (SHP, GPKG, GeoJSON, CSV)
├── clean_db.py              # Database cleaning utility
├── config.json.example      # Sample connection configuration
├── .env.example             # Sample API key configuration
├── requirements.txt         # Python dependencies
├── test_postmate.py         # Comprehensive unit & integration test suite
├── DEMO_QUESTIONS.txt       # Ready-to-use live presentation demo questions
├── input_data/              # Drop folder for automated spatial datasets
└── README.md                # Project documentation
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
