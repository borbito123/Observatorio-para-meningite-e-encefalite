"""Retenção de sessão e preservação de bancos ao limpar consultas."""
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from cachetools import TTLCache
import duckdb
from streamlit.runtime.memory_session_storage import MemorySessionStorage
from streamlit.testing.v1 import AppTest

for name in ("streamlit.runtime.scriptrunner_utils.script_run_context",
             "streamlit.runtime.caching.cache_data_api", "streamlit.runtime.state.session_state_proxy"):
    logging.getLogger(name).setLevel(logging.ERROR)

REPO = Path(__file__).resolve().parents[1]
APP_PATH = Path(os.environ.get("SIH_TEST_APP", REPO / "streamlit_app.py"))
spec = importlib.util.spec_from_file_location("session_retention_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class SessionRetention(unittest.TestCase):
    def tearDown(self):
        app._run_query_cached.clear()
        app.get_shared_db.clear()
        app.get_duckdb_file_db.clear()

    def test_server_loads_25_minute_retention(self):
        # Start a real Server object in a fresh process so configuration is read
        # before its session storage is created.
        result = subprocess.run(
            [sys.executable, "-c", "from streamlit.web.server.server import Server; "
             "server = Server('streamlit_app.py', False); "
             "ttl = server._runtime._session_mgr._session_storage._cache.ttl; "
             "assert ttl == 1500, ttl; print('session TTL =', ttl)"],
            cwd=APP_PATH.parent, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("session TTL = 1500", result.stdout)

    def test_disconnected_session_is_available_at_24_minutes(self):
        # Use Streamlit's real storage with a controlled clock; no wall-clock wait.
        clock = [0.0]
        factory = lambda *args, **kwargs: TTLCache(*args, **kwargs, timer=lambda: clock[0])
        with patch("streamlit.runtime.memory_session_storage.TTLCache", factory):
            storage = MemorySessionStorage(ttl_seconds=1500)
        info = SimpleNamespace(session=SimpleNamespace(id="same-session", uploads=["base.parquet"]))
        storage.save(info)
        clock[0] = 24 * 60
        self.assertIs(storage.get("same-session"), info)
        self.assertEqual(storage.get("same-session").session.uploads, ["base.parquet"])
        clock[0] = 26 * 60
        self.assertIsNone(storage.get("same-session"))

    def test_query_cache_clear_preserves_materialized_parquet(self):
        settings = ("512MB", 1, str(Path(tempfile.gettempdir()) / "meningite_retention_test"))
        shared = app.get_shared_db(settings)
        try:
            with tempfile.TemporaryDirectory(prefix="retention_parquet_") as directory:
                parquet = Path(directory) / "base.parquet"
                writer = duckdb.connect()
                try:
                    writer.execute("CREATE TABLE fixture AS SELECT '0001' AS id")
                    writer.execute(f"COPY fixture TO {app.qstr(str(parquet))} (FORMAT PARQUET)")
                finally:
                    writer.close()
                table = app.LoadedTable(source="SINAN", kind="parquet", parquet_paths=[str(parquet)],
                                        ref_sql=app.parquet_object_name("SINAN", [str(parquet)]), label="test")
                app._ensure_parquet_object(shared, table.ref_sql, table.parquet_paths, True)
                sql = f"SELECT count(*) AS n FROM {app.qident(table.ref_sql)}"
                query = lambda: app._run_query_cached(app.table_cache_key(table), "parquet", None, sql, settings)
                self.assertEqual(int(query().iloc[0, 0]), 1)
                shared.con.execute(f"INSERT INTO {app.qident(table.ref_sql)} VALUES ('0002')")
                self.assertEqual(int(query().iloc[0, 0]), 1)
                app._run_query_cached.clear()
                self.assertIs(app.get_shared_db(settings), shared)
                self.assertEqual(int(query().iloc[0, 0]), 2)
        finally:
            shared.con.close()

    def test_clear_queries_button_keeps_database_resources(self):
        ui = AppTest.from_string("from session_retention_app import render_performance_controls\nrender_performance_controls()")
        ui.run(timeout=30)
        self.assertEqual(len(ui.exception), 0)
        settings = ("512MB", 1, str(Path(tempfile.gettempdir()) / "meningite_retention_ui"))
        shared = app.get_shared_db(settings)
        try:
            shared.con.execute("CREATE TABLE retained AS SELECT 7 AS value")
            ui.button(key="clear_query_cache").click().run(timeout=30)
            self.assertEqual(len(ui.exception), 0)
            self.assertIs(app.get_shared_db(settings), shared)
            self.assertEqual(shared.con.execute("SELECT value FROM retained").fetchone()[0], 7)
        finally:
            shared.con.close()

    def test_clear_queries_button_keeps_uploaded_duckdb(self):
        settings = ("512MB", 1, str(Path(tempfile.gettempdir()) / "meningite_retention_duckdb"))
        with tempfile.TemporaryDirectory(prefix="retention_duckdb_") as directory:
            database = str(Path(directory) / "fixture.duckdb")
            writer = duckdb.connect(database)
            try:
                writer.execute("CREATE TABLE retained AS SELECT '0001' AS id")
            finally:
                writer.close()
            fingerprint = app._file_fingerprint(database)
            shared = app.get_duckdb_file_db(database, settings, fingerprint)
            try:
                ui = AppTest.from_string("from session_retention_app import render_performance_controls\nrender_performance_controls()")
                ui.run(timeout=30)
                ui.button(key="clear_query_cache").click().run(timeout=30)
                self.assertEqual(len(ui.exception), 0)
                self.assertIs(app.get_duckdb_file_db(database, settings, fingerprint), shared)
                self.assertEqual(shared.con.execute("SELECT id FROM retained").fetchone()[0], "0001")
            finally:
                shared.con.close()
                app.get_duckdb_file_db.clear()

    def test_release_databases_button_recreates_resources(self):
        ui = AppTest.from_string("from session_retention_app import render_performance_controls\nrender_performance_controls()")
        ui.run(timeout=30)
        settings = ("512MB", 1, str(Path(tempfile.gettempdir()) / "meningite_retention_release"))
        before = app.get_shared_db(settings)
        after = None
        try:
            ui.button(key="clear_database_cache").click().run(timeout=30)
            self.assertEqual(len(ui.exception), 0)
            after = app.get_shared_db(settings)
            self.assertIsNot(after, before)
        finally:
            before.con.close()
            if after is not None:
                after.con.close()


if __name__ == "__main__":
    unittest.main()
