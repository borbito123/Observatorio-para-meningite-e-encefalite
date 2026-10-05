"""Regressões SIH: executar python -m unittest discover -s tests -v."""
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest

import duckdb
from streamlit.testing.v1 import AppTest

for name in ("streamlit.runtime.scriptrunner_utils.script_run_context",
             "streamlit.runtime.caching.cache_data_api", "streamlit.runtime.state.session_state_proxy"):
    logging.getLogger(name).setLevel(logging.ERROR)
APP_PATH = Path(os.environ.get("SIH_TEST_APP", Path(__file__).resolve().parents[1] / "streamlit_app.py"))
spec = importlib.util.spec_from_file_location("sih_test_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class SihQueries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="sih_tests_")
        cls.db = str(Path(cls.directory.name) / "fixture.duckdb")
        connection = duckdb.connect(cls.db)
        connection.execute("""
            CREATE TABLE sih_rd_fixture AS SELECT * FROM (
                VALUES
                ('same', 'G00.9', 'G00.9', 'A39.0', 'G009', 'B58.2', NULL, '00394544021182', '2025', '12', 'RD', 'qualquer_cid'),
                ('same', 'G039', 'B24', NULL, NULL, NULL, NULL, '00394544021182', '2026', '1', 'RD', 'qualquer_cid'),
                ('third', 'A170', '0000', NULL, NULL, '0000', '0000', NULL, '2026', '2', 'RD', 'qualquer_cid'),
                ('bad', NULL, 'inválido', NULL, NULL, NULL, NULL, '00123456000199', '2026', '13', 'RD', 'qualquer_cid')
            ) AS input(N_AIH, DIAG_PRINC, DIAG_SECUN, DIAGSEC1, CID_MORTE,
                       CID_ASSO, CID_NOTIF, CGC_HOSP, ANO_COMPETENCIA, MES_COMPETENCIA, TIPO_SIH, CRITERIO_CID)
        """)
        connection.execute("CREATE TABLE metadados_execucao AS SELECT 'metadata' AS texto")
        connection.close()
        cls.table = app.LoadedTable("SIH", "duckdb", '"sih_rd_fixture"', db_path=cls.db, table_name="sih_rd_fixture")
        cls.columns = app.schema_df(cls.table).coluna.tolist()

    @classmethod
    def tearDownClass(cls):
        app.st.cache_resource.clear()
        cls.directory.cleanup()

    def test_fields_and_metadata_exclusion(self):
        roles = app.detect_sih_cid_fields(self.columns + ["FILTRO_CID", "CID_DESCR", "CID_GENERICO", "TPDISEC1", "CGC_Hospital"])
        self.assertEqual(roles["secundario"], ["DIAG_SECUN", "DIAGSEC1"])
        self.assertEqual(roles["outro"], ["CID_GENERICO"])
        self.assertNotIn("FILTRO_CID", sum(roles.values(), []))
        self.assertEqual(app.choose_candidate(self.columns, ["CGC_HOSPITAL", "CGC_HOSP"]), "CGC_HOSP")

    def test_one_code_per_row_without_deduplicating_repeated_aih(self):
        fields = ["DIAG_PRINC", "DIAG_SECUN", "DIAGSEC1", "CID_MORTE", "CID_ASSO"]
        counts = app.query_sih_cid_distribution(self.table, fields).set_index("cid")
        self.assertEqual(counts.loc["G009", "n"], 1)
        self.assertEqual(counts.loc["G039", "n"], 1)
        self.assertEqual(counts.loc["B24", "n"], 1)
        self.assertTrue((counts.n_registros_denominador == 4).all())
        self.assertEqual(counts.loc["G009", "pct_registros"], 25.0)
        self.assertEqual(app.count_rows(self.table), 4)

    def test_compound_cids_keep_all_tokens_and_no_false_match_inside_text(self):
        expression = app.sih_cid_values_expr(["value"])
        connection = duckdb.connect()
        result = connection.execute(f"SELECT {expression} FROM (SELECT '*G00.9; A39.0 / g009' AS value)").fetchone()[0]
        self.assertEqual(set(result), {"G009", "A390"})
        result = connection.execute(f"SELECT {expression} FROM (SELECT 'NOG039BAD' AS value)").fetchone()[0]
        self.assertEqual(result, [])
        connection.close()

    def test_absent_zero_and_non_code_have_distinct_denominators(self):
        row = app.query_sih_cid_coverage(self.table, ["DIAG_SECUN"]).iloc[0]
        self.assertEqual((row.n_registros, row.n_vazio, row.n_zero, row.n_sem_formato_cid10, row.n_com_formato_cid10), (4, 0, 1, 1, 2))
        self.assertEqual(row.n_recorte_meningite, 1)
        self.assertEqual(row.pct_recorte_meningite, 25.0)

    def test_hospital_percentages_use_all_rows_and_keep_leading_zeros(self):
        frame = app.query_sih_hospitals(self.table, "CGC_HOSP", top_n=1)
        self.assertEqual(frame.n.sum(), 4)
        self.assertIn("00394544021182", frame.cgc_hospital.tolist())
        self.assertEqual(frame.pct_registros.sum(), 100.0)

    def test_secondary_only_recorte_not_limited_to_primary(self):
        where = app.sql_where([app.sih_meningitis_condition(["DIAG_SECUN", "DIAGSEC1"])])
        self.assertEqual(app.count_rows(self.table, where), 1)
        frame = app.query_sih_cid_distribution(self.table, ["DIAG_SECUN"], meningitis_only=True)
        self.assertEqual(frame.cid.tolist(), ["G009"])

    def test_temporal_reference_validates_month_and_uses_chosen_fields(self):
        dt = app.sih_reference_dates(self.columns)["Competência de processamento"]
        series = app.query_sih_cid_distribution(self.table, ["DIAG_PRINC"], dt_sql=dt, frequency="month")
        self.assertEqual(int(series.n.sum()), 3)
        self.assertEqual({str(x.date()) for x in series.periodo}, {"2025-12-01", "2026-01-01", "2026-02-01"})
        with self.assertRaises(ValueError):
            app.query_sih_cid_distribution(self.table, ["DIAG_PRINC"], dt_sql=dt, frequency="unsafe")

    def test_empty_recorte_is_supported(self):
        where = "WHERE FALSE"
        coverage = app.query_sih_cid_coverage(self.table, ["DIAG_PRINC"], where)
        self.assertEqual(coverage.iloc[0].n_registros, 0)
        self.assertTrue(app.query_sih_cid_distribution(self.table, ["DIAG_PRINC"], where).empty)
        self.assertTrue(app.query_sih_hospitals(self.table, "CGC_HOSP", where).empty)

    def test_old_and_alternate_layouts(self):
        old = app.detect_sih_cid_fields(["DIAG_PRI", "DIAG_SEC", "CGC_Hospital"])
        self.assertEqual(old, {"principal": ["DIAG_PRI"], "secundario": ["DIAG_SEC"]})
        newer = app.detect_sih_cid_fields(["DIAG_PRINC"] + [f"DIAGSEC{i}" for i in range(1, 10)] + ["CID_MORTE"])
        self.assertEqual(len(newer["secundario"]), 9)

    def test_native_duckdb_loader_skips_metadata_table(self):
        wrapper = f"""
import importlib.util, io, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('sih_upload_test', {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
class Upload(io.BytesIO):
    name = 'sih_rd_fixture.duckdb'
    file_id = 'sih-loader-regression'
    size = Path({self.db!r}).stat().st_size
upload = Upload(Path({self.db!r}).read_bytes())
a.st.file_uploader = lambda *args, **kwargs: upload
a.st.session_state['main_section'] = 'SIH'
a.main()
"""
        at = AppTest.from_string(wrapper, default_timeout=60).run()
        self.assertFalse(at.exception, [str(x.value) for x in at.exception])
        self.assertFalse(at.error, [str(x.value) for x in at.error])
        self.assertEqual(at.selectbox(key="upload_duckdb_table_SIH").value, "sih_rd_fixture")
        self.assertEqual(at.radio(key="load_mode_SIH").value, "Upload DuckDB")
        self.assertEqual(len(at.get("plotly_chart")), 2)
        at.radio(key="sih_analysis_section").set_value("Outros campos propostos").run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.get("plotly_chart")), 0)

    def test_native_csv_and_parquet_uploads_preserve_sih_identifiers(self):
        connection = duckdb.connect(self.db, read_only=True)
        fixture = connection.execute("SELECT * FROM sih_rd_fixture").df()
        connection.close()
        csv_path = Path(self.directory.name) / "fixture.csv"
        parquet_path = Path(self.directory.name) / "fixture.parquet"
        fixture.to_csv(csv_path, index=False)
        fixture.to_parquet(parquet_path, index=False)
        for mode, path in [("Upload CSV", csv_path), ("Upload Parquet", parquet_path)]:
            with self.subTest(mode=mode):
                wrapper = f"""
import importlib.util, io, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('sih_format_test', {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
class Upload(io.BytesIO):
    name = {path.name!r}
    file_id = {mode!r}
    size = Path({str(path)!r}).stat().st_size
upload = Upload(Path({str(path)!r}).read_bytes())
a.st.file_uploader = lambda *args, **kwargs: [upload]
a.st.session_state['main_section'] = 'SIH'
a.st.session_state['load_mode_SIH'] = {mode!r}
a.main()
"""
                at = AppTest.from_string(wrapper, default_timeout=60).run()
                self.assertFalse(at.exception, [str(x.value) for x in at.exception])
                self.assertFalse(at.error, [str(x.value) for x in at.error])
                self.assertEqual(at.metric[0].value, "4")
                self.assertIn("00394544021182", at.multiselect(key="sih_hospital_filter").options)


if __name__ == "__main__":
    unittest.main()
