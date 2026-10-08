#!/usr/bin/env python3
"""
Unit and Integration Tests for Postmate CLI
Tests:
  - Criticality Tier Rules and Precedence
  - Rolling Conversation Memory
  - Folium Map Visualizer with Refresh Button
  - Config and API Key Error Handling
  - Grok Response Parsing and Single-Retry Logic
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from postmate import (
    TIER_NEUTRAL,
    TIER_NORMAL,
    TIER_CRITICAL,
    TIER_VERY_CRITICAL,
    CriticalityEngine,
    ConversationMemory,
    ConfigManager,
    ConfigError,
    ApiKeyError,
    GrokClient,
    GrokApiError,
    MapVisualizer,
    DatabaseRunner,
    Postmate,
    QueryResult,
)


class TestCriticalityEngine(unittest.TestCase):
    def test_rules(self):
        # Neutral queries
        tier, _ = CriticalityEngine.evaluate_rules("SELECT * FROM parks")
        self.assertEqual(tier, TIER_NEUTRAL)

        tier, _ = CriticalityEngine.evaluate_rules("SELECT name, ST_Area(geom) FROM zones WHERE population > 100")
        self.assertEqual(tier, TIER_NEUTRAL)

        tier, _ = CriticalityEngine.evaluate_rules("EXPLAIN ANALYZE SELECT id FROM roads")
        self.assertEqual(tier, TIER_NEUTRAL)

        # Critical queries
        tier, _ = CriticalityEngine.evaluate_rules("INSERT INTO points (name) VALUES ('P1')")
        self.assertEqual(tier, TIER_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("UPDATE zones SET name = 'Zone A' WHERE id = 1")
        self.assertEqual(tier, TIER_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("DELETE FROM logs WHERE timestamp < '2020-01-01'")
        self.assertEqual(tier, TIER_CRITICAL)

        # Very Critical queries
        tier, _ = CriticalityEngine.evaluate_rules("DROP TABLE parcels")
        self.assertEqual(tier, TIER_VERY_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("TRUNCATE TABLE audit_log")
        self.assertEqual(tier, TIER_VERY_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("ALTER TABLE roads ADD COLUMN lanes int")
        self.assertEqual(tier, TIER_VERY_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("UPDATE zones SET status = 'inactive'")  # No WHERE!
        self.assertEqual(tier, TIER_VERY_CRITICAL)

        tier, _ = CriticalityEngine.evaluate_rules("DELETE FROM customers")  # No WHERE!
        self.assertEqual(tier, TIER_VERY_CRITICAL)

    def test_precedence_higher_wins(self):
        # Rule is critical, Grok says neutral -> critical wins
        tier, _ = CriticalityEngine.resolve_tier("neutral", "critical", "rule", "grok")
        self.assertEqual(tier, TIER_CRITICAL)

        # Grok says very critical, Rule says neutral -> very critical wins
        tier, _ = CriticalityEngine.resolve_tier("very critical", "neutral", "rule", "grok")
        self.assertEqual(tier, TIER_VERY_CRITICAL)

        # Grok says normal, Rule says critical -> critical wins
        tier, _ = CriticalityEngine.resolve_tier("normal", "critical", "rule", "grok")
        self.assertEqual(tier, TIER_CRITICAL)

        # Both agree on neutral -> neutral
        tier, _ = CriticalityEngine.resolve_tier("neutral", "neutral", "rule", "grok")
        self.assertEqual(tier, TIER_NEUTRAL)


class TestConversationMemory(unittest.TestCase):
    def test_rolling_limit(self):
        mem = ConversationMemory(maxlen=3)
        mem.add("q1", "SELECT 1")
        mem.add("q2", "SELECT 2")
        mem.add("q3", "SELECT 3")
        self.assertEqual(len(mem), 3)

        mem.add("q4", "SELECT 4")
        self.assertEqual(len(mem), 3)
        ctx = mem.get_context_for_prompt()
        self.assertNotIn("SELECT 1", ctx)
        self.assertIn("SELECT 4", ctx)

    def test_clear_reset(self):
        mem = ConversationMemory(maxlen=5)
        mem.add("q1", "SELECT 1")
        self.assertEqual(len(mem), 1)
        mem.clear()
        self.assertEqual(len(mem), 0)
        self.assertEqual(mem.get_context_for_prompt(), "No previous conversation history.")


class TestMapVisualizer(unittest.TestCase):
    def test_generate_map_with_geojson(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_map_path = os.path.join(tmpdir, "map.html")
            rows = [
                {
                    "name": "Central Park",
                    "category": "Park",
                    "__geojson__": json.dumps({
                        "type": "Polygon",
                        "coordinates": [[
                            [-73.981, 40.768],
                            [-73.958, 40.800],
                            [-73.949, 40.796],
                            [-73.973, 40.764],
                            [-73.981, 40.768]
                        ]]
                    })
                }
            ]
            generated = MapVisualizer.generate_map(rows, {}, output_path=test_map_path)
            self.assertTrue(generated)
            self.assertTrue(os.path.isfile(test_map_path))

            with open(test_map_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Verify Leaflet & Folium output
            self.assertIn("leaflet", content.lower())
            # Verify refresh button presence
            self.assertIn("Refresh Map", content)
            self.assertIn("window.location.reload()", content)
            # Verify property presence
            self.assertIn("Central Park", content)

    def test_generate_map_from_coordinate_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_map_path = os.path.join(tmpdir, "map.html")
            rows = [
                {"name": "TMH Hospital", "latitude": 22.8015, "longitude": 86.1829}
            ]
            generated = MapVisualizer.generate_map(rows, {}, output_path=test_map_path)
            self.assertTrue(generated)
            self.assertTrue(os.path.isfile(test_map_path))
            with open(test_map_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("TMH Hospital", content)
            self.assertIn("postmate-autosync", content)

    def test_generate_map_from_wkt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_map_path = os.path.join(tmpdir, "map.html")
            rows = [
                {"name": "Beldih Club", "location": "POINT(86.185 22.798)"}
            ]
            generated = MapVisualizer.generate_map(rows, {}, output_path=test_map_path)
            self.assertTrue(generated)
            self.assertTrue(os.path.isfile(test_map_path))
            with open(test_map_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Beldih Club", content)

    def test_no_geometry_skips_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_map_path = os.path.join(tmpdir, "map.html")
            rows = [{"id": 1, "name": "Non-spatial row"}]
            generated = MapVisualizer.generate_map(rows, {}, output_path=test_map_path)
            self.assertFalse(generated)
            self.assertFalse(os.path.isfile(test_map_path))


class TestConfigAndSecurity(unittest.TestCase):
    def test_missing_config_raises_error(self):
        with patch.object(ConfigManager, "find_config_path", return_value="nonexistent_config.json"):
            with self.assertRaises(ConfigError):
                ConfigManager.load_config()

    def test_missing_api_key_raises_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ApiKeyError):
                ConfigManager.get_api_key()


class TestGrokClientParsing(unittest.TestCase):
    @patch("requests.post")
    def test_clean_json_parsing(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "sql": "SELECT name FROM parks WHERE ST_Area(geom) > 1000",
                        "criticality": "neutral",
                        "risk_explanation": "Read-only spatial query."
                    })
                }
            }]
        }
        mock_post.return_value = mock_resp

        client = GrokClient(api_key="mock_key")
        result = client.generate_sql("find big parks", "schema", "memory")
        self.assertEqual(result["sql"], "SELECT name FROM parks WHERE ST_Area(geom) > 1000")
        self.assertEqual(result["criticality"], "neutral")

    @patch("requests.post")
    def test_markdown_wrapped_json_parsing(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": "```json\n{\n  \"sql\": \"DROP TABLE test\",\n  \"criticality\": \"very critical\",\n  \"risk_explanation\": \"Drops table\"\n}\n```"
                }
            }]
        }
        mock_post.return_value = mock_resp

        client = GrokClient(api_key="mock_key")
        result = client.generate_sql("drop test", "schema", "memory")
        self.assertEqual(result["sql"], "DROP TABLE test")
        self.assertEqual(result["criticality"], "very critical")


class TestExecutionAndRetry(unittest.TestCase):
    @patch.object(ConfigManager, "load_config")
    @patch.object(ConfigManager, "get_api_key", return_value=("test_key", "grok"))
    def test_single_retry_on_db_error(self, mock_key, mock_cfg):
        mock_cfg.return_value = ({"host": "localhost", "database": "testdb"}, {"model": "grok-2-latest"})
        postmate = Postmate()
        postmate.cached_schema = "Table: parcels"

        # Mock Grok returning first SQL then second SQL
        first_resp = {"sql": "SELECT non_existent_col FROM parcels", "criticality": "neutral", "risk_explanation": ""}
        corrected_resp = {"sql": "SELECT id FROM parcels", "criticality": "neutral", "risk_explanation": ""}
        postmate.grok_client.generate_sql = MagicMock(side_effect=[first_resp, corrected_resp])

        # Mock DatabaseRunner failing on first, succeeding on second
        from psycopg2 import ProgrammingError
        first_call_exc = ProgrammingError("column non_existent_col does not exist")
        second_call_res = QueryResult(rows=[{"id": 1}], column_names=["id"], rowcount=1, execution_time_ms=5.0)
        postmate.db_runner.execute_query = MagicMock(side_effect=[first_call_exc, second_call_res])

        success = postmate.process_query("get parcel id")
        self.assertTrue(success)
        self.assertEqual(postmate.db_runner.execute_query.call_count, 2)
        self.assertEqual(postmate.grok_client.generate_sql.call_count, 2)
        self.assertEqual(len(postmate.memory), 1)

    @patch.object(ConfigManager, "load_config")
    @patch.object(ConfigManager, "get_api_key", return_value=("test_key", "grok"))
    def test_retry_failure_stops_gracefully(self, mock_key, mock_cfg):
        mock_cfg.return_value = ({"host": "localhost", "database": "testdb"}, {"model": "grok-2-latest"})
        postmate = Postmate()
        postmate.cached_schema = "Table: parcels"

        # Mock Grok returning queries
        first_resp = {"sql": "SELECT col1 FROM parcels", "criticality": "neutral", "risk_explanation": ""}
        corrected_resp = {"sql": "SELECT col2 FROM parcels", "criticality": "neutral", "risk_explanation": ""}
        postmate.grok_client.generate_sql = MagicMock(side_effect=[first_resp, corrected_resp])

        # Mock DatabaseRunner failing both times
        from psycopg2 import ProgrammingError
        postmate.db_runner.execute_query = MagicMock(side_effect=[
            ProgrammingError("syntax error at col1"),
            ProgrammingError("syntax error at col2")
        ])

        success = postmate.process_query("get parcel id")
        self.assertFalse(success)
        # Should call DB runner exactly twice (initial + 1 retry) and then stop
        self.assertEqual(postmate.db_runner.execute_query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
