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
        connection.execute("""
            CREATE TABLE sih_rd_demo AS SELECT * FROM (
                VALUES
                ('G009','1','2','1','03','0000','0','20191231','20200101','0000001','00000000000001','00000000000002','330455','330100','010','2025','12'),
                ('G009','6','3','3','04','0000','2','20190601','20191201','0000001','00000000000001','00000000000002','330455','330100','010','2025','12'),
                ('G009','25','4','0','05','0001','3','20000101','20250101','0000002','00000000000000',NULL,'330100','330455','021','2026','1'),
                ('G009','1','5','3','99',NULL,'4','19200101','20210101','0000002',NULL,'00000000000003','330100','330455','010','2026','1'),
                ('G009','5','0','2','0',NULL,'1','19000000','20200101','0000000',NULL,NULL,'000000','330455','000','2026','1'),
                ('G009','9','9',NULL,'bad',NULL,'9',NULL,'20200101',NULL,NULL,NULL,NULL,'330455',NULL,'2026','2'),
                ('G009','999','4','1',NULL,NULL,NULL,NULL,'20200101','0000003',NULL,NULL,'330455','330455','010','2026','2')
            ) AS input(DIAG_PRINC, IDADE, COD_IDADE, SEXO, RACA_COR, ETNIA, INSTRU, NASC, DT_INTER,
                       CNES, CGC_MANT, CNPJ_MANT, MUNIC_RES, MUNIC_MOV, NACIONAL, ANO_COMPETENCIA, MES_COMPETENCIA)
        """)
        connection.execute("""
            CREATE TABLE sih_rd_union AS SELECT * FROM (
                VALUES
                ('same', 'G00.9; A39.0', 'G039', 'G009', 'G009', 'G009', 'G009', '001', '2025', '12'),
                ('secondary', 'J189', 'G009', 'G039', NULL, NULL, NULL, '001', '2025', '12'),
                ('primary', 'G039', 'B24', NULL, NULL, NULL, NULL, '002', '2026', '1'),
                ('other', 'B24', NULL, NULL, 'G009', 'G039', 'A390', '002', '2026', '1'),
                ('invalid', NULL, '0000', 'invalid', NULL, NULL, NULL, '002', '2026', '1'),
                ('same', 'G009', 'G009', 'A390', NULL, NULL, NULL, '002', '2026', '1'),
                ('undated', 'J189', NULL, 'B58.2', NULL, NULL, NULL, '003', '2026', '13')
            ) AS input(N_AIH, DIAG_PRINC, DIAG_SECUN, DIAGSEC9, CID_MORTE,
                       CID_ASSO, CID_NOTIF, CGC_HOSP, ANO_COMPETENCIA, MES_COMPETENCIA)
        """)
        connection.close()
        cls.table = app.LoadedTable("SIH", "duckdb", '"sih_rd_fixture"', db_path=cls.db, table_name="sih_rd_fixture")
        cls.columns = app.schema_df(cls.table).coluna.tolist()
        cls.union_table = app.LoadedTable("SIH", "duckdb", '"sih_rd_union"', db_path=cls.db, table_name="sih_rd_union")
        cls.union_columns = app.schema_df(cls.union_table).coluna.tolist()
        cls.union_roles = app.detect_sih_cid_fields(cls.union_columns)
        cls.demo_table = app.LoadedTable("SIH", "duckdb", '"sih_rd_demo"', db_path=cls.db, table_name="sih_rd_demo")
        cls.demo_columns = app.schema_df(cls.demo_table).coluna.tolist()

    def test_sih_age_units_century_unknown_and_invalid(self):
        age = app.sih_age_expr("IDADE", "COD_IDADE")
        values = app.run_query(self.demo_table, f"SELECT {age} AS age FROM {self.demo_table.ref_sql}").age.tolist()
        self.assertAlmostEqual(values[0], 1 / 365.25)
        self.assertEqual(values[1:4], [0.5, 25.0, 101.0])
        self.assertTrue(all(app.pd.isna(x) for x in values[4:]))
        expressions = app.sih_demography_expressions(self.demo_columns)
        frame = app.query_sih_age_profile(self.demo_table, age)
        self.assertEqual(int(frame.n.sum()), 7)
        self.assertEqual(int(frame.set_index("faixa").loc["Idade ausente/inválida", "n"]), 3)
        pyramid = app.query_sih_age_profile(self.demo_table, age, sex_sql=expressions["Sexo"])
        self.assertEqual(int(pyramid.n.sum()), 3)
        self.assertTrue(set(pyramid.sexo).issubset({"Masculino", "Feminino"}))

    def test_sih_missing_columns_are_not_fuzzy_matched_to_other_meanings(self):
        self.assertEqual(app.sih_demography_expressions(["COD_IDADE"]), {})
        self.assertEqual(app.sih_demography_expressions(["IDADE"]), {})
        self.assertIsNone(app.sih_choose_column(["SP_CNES"], ["CNES"]))
        self.assertIsNone(app.sih_choose_column(["CNPJ_MANT"], ["CGC_MANT"]))
        self.assertEqual(app.sih_choose_column(["CGC_Hospital"], ["CGC_HOSPITAL"]), "CGC_Hospital")

    def test_union_without_dates_renders_total_and_no_secondary_cids_does_not_inflate(self):
        wrapper = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('sih_union_nodate', {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
table = a.LoadedTable('SIH', 'duckdb', '"sih_rd_union"', db_path={self.db!r}, table_name='sih_rd_union')
a.render_sih_diagnosis_union(table, {{"principal": ["DIAG_PRINC"], "secundario": ["DIAG_SECUN", "DIAGSEC9"]}}, '', None)
"""
        at = AppTest.from_string(wrapper, default_timeout=60).run()
        self.assertFalse(at.exception, [str(x.value) for x in at.exception])
        self.assertEqual(at.metric[0].value, "5")
        self.assertEqual(len(at.get("plotly_chart")), 1)

    def test_sih_demographic_domains_do_not_reuse_sim_sinan_codes(self):
        expressions = app.sih_demography_expressions(self.demo_columns)
        race = app.query_sih_categories(self.demo_table, expressions["Raça/cor"]).set_index("categoria")
        self.assertEqual((race.loc["Parda", "n"], race.loc["Amarela", "n"]), (1, 1))
        sex = app.query_sih_categories(self.demo_table, expressions["Sexo"]).set_index("categoria")
        self.assertEqual((sex.loc["Masculino", "n"], sex.loc["Feminino", "n"]), (2, 2))
        self.assertIn("Código não interpretado: 2", sex.index)
        education = app.query_sih_categories(self.demo_table, expressions["Escolaridade (INSTRU)"])
        self.assertIn("Sem informação (código 0)", education.categoria.tolist())
        ethnicity = app.query_sih_categories(self.demo_table, expressions["Etnia (códigos originais)"])
        self.assertIn("Não aplicável (raça/cor não indígena)", ethnicity.categoria.tolist())
        self.assertIn("Código de etnia: 0001", ethnicity.categoria.tolist())
        for key, expression in expressions.items():
            if not key.startswith("Idade "):
                frame = app.query_sih_categories(self.demo_table, expression)
                self.assertEqual(int(frame.n.sum()), 7, key)
                self.assertTrue((frame.denominador == 7).all(), key)

    def test_sih_identifier_categories_keep_zeros_full_denominator_and_filters(self):
        frame = app.query_sih_categories(self.demo_table, app.clean_str_expr("CNES"), top_n=1)
        self.assertEqual(int(frame.n.sum()), 7)
        self.assertTrue((frame.denominador == 7).all())
        self.assertAlmostEqual(float(frame.pct.sum()), 100.0, places=1)
        self.assertIn("0000001", frame.categoria.tolist())
        selected = app.query_sih_categories(self.demo_table, app.clean_str_expr("CGC_MANT"), 'WHERE "CNES" = \'0000001\'')
        self.assertEqual(selected.categoria.tolist(), ["00000000000001"])
        self.assertEqual(int(selected.n.sum()), 2)
        self.assertTrue(app.query_sih_categories(self.demo_table, '"CNES"', "WHERE FALSE").empty)

    def test_sih_demography_and_facility_interface_all_options_and_filter_reset(self):
        wrapper = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('sih_demo_ui', {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
table = a.LoadedTable('SIH', 'duckdb', '"sih_rd_demo"', db_path={self.db!r}, table_name='sih_rd_demo')
a.render_sih_source(table)
"""
        at = AppTest.from_string(wrapper, default_timeout=60).run()
        self.assertFalse(at.exception)
        at.radio(key="sih_analysis_section").set_value("Estabelecimentos e mantenedoras").run()
        for option in list(at.selectbox(key="sih_facility_field").options):
            at.selectbox(key="sih_facility_field").select(option).run()
            self.assertFalse(at.exception, [str(x.value) for x in at.exception])
            self.assertEqual(len(at.get("plotly_chart")), 1)
            frame = next(x.value for x in at.dataframe if "denominador" in x.value.columns)
            self.assertEqual(int(frame.n.sum()), 7)
        at.radio(key="sih_analysis_section").set_value("Análise demográfica").run()
        for option in list(at.selectbox(key="sih_demography_chart").options):
            at.selectbox(key="sih_demography_chart").select(option).run()
            self.assertFalse(at.exception, [str(x.value) for x in at.exception])
            self.assertEqual(len(at.get("plotly_chart")), 1)
            expected = 3 if option == "Pirâmide etária por sexo" else 7
            frame = next(x.value for x in at.dataframe if "denominador" in x.value.columns)
            self.assertEqual(int(frame.n.sum()), expected, option)
        at.selectbox(key="sih_demography_chart").select("Faixa etária").run()
        at.selectbox(key="sih_demography_age_reference").select("Idade calculada (NASC/DT_INTER, aproximada)").run()
        self.assertFalse(at.exception)
        at.multiselect(key="sih_years").select(2026).run()
        self.assertEqual(int(next(x.value for x in at.dataframe if "denominador" in x.value.columns).n.sum()), 5)
        at.multiselect(key="sih_years").set_value([]).run()
        self.assertEqual(int(next(x.value for x in at.dataframe if "denominador" in x.value.columns).n.sum()), 7)

    def test_union_counts_each_row_once_even_with_multiple_different_cids(self):
        row = app.query_sih_diagnosis_union(self.union_table, self.union_roles).iloc[0]
        self.assertEqual((row.n, row.n_principal_apenas, row.n_secundario_apenas, row.n_ambos), (5, 1, 2, 2))
        self.assertEqual(row.n, row.n_principal_apenas + row.n_secundario_apenas + row.n_ambos)
        # CID_MORTE/ASSO/NOTIF-only and invalid/zero rows do not qualify.
        # Same N_AIH on distinct source rows is not treated as a unique episode.
        mentions = app.query_sih_cid_distribution(
            self.union_table, self.union_roles["principal"] + self.union_roles["secundario"], meningitis_only=True
        )
        self.assertGreater(int(mentions.n.sum()), row.n)

    def test_union_temporal_filters_missing_dates_and_reconciliation(self):
        dt = app.sih_reference_dates(self.union_columns)["Competência de processamento"]
        series = app.query_sih_diagnosis_union(self.union_table, self.union_roles, dt_sql=dt, frequency="month")
        self.assertEqual(int(series.n.sum()), 5)
        self.assertEqual(int(series.loc[series.periodo.isna(), "n"].sum()), 1)
        self.assertEqual(series.loc[series.periodo.notna(), "n"].tolist(), [2, 2])
        self.assertTrue((series.n == series.n_principal_apenas + series.n_secundario_apenas + series.n_ambos).all())
        annual = app.query_sih_diagnosis_union(self.union_table, self.union_roles, dt_sql=dt)
        self.assertEqual(int(annual.n.sum()), 5)
        where = app.sql_where([f"EXTRACT(YEAR FROM {dt}) = 2026", '"CGC_HOSP" = \'002\''])
        selected = app.query_sih_diagnosis_union(self.union_table, self.union_roles, where, dt)
        self.assertEqual(int(selected.n.sum()), 2)
        with self.assertRaises(ValueError):
            app.query_sih_diagnosis_union(self.union_table, self.union_roles, dt_sql=dt, frequency="unsafe")

    def test_union_empty_missing_roles_and_single_role_layouts(self):
        for roles, expected in [({}, 0), ({"principal": ["DIAG_PRINC"]}, 3),
                                ({"secundario": ["DIAG_SECUN", "DIAGSEC9"]}, 4)]:
            with self.subTest(roles=roles):
                result = app.query_sih_diagnosis_union(self.union_table, roles)
                self.assertEqual(int(result.iloc[0].n), expected)
        empty = app.query_sih_diagnosis_union(self.union_table, self.union_roles, "WHERE FALSE")
        self.assertEqual(int(empty.iloc[0].n), 0)
        dt = app.sih_reference_dates(self.union_columns)["Competência de processamento"]
        self.assertTrue(app.query_sih_diagnosis_union(self.union_table, self.union_roles, "WHERE FALSE", dt).empty)

    def test_union_render_filters_monthly_and_undated_total(self):
        wrapper = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location('sih_union_ui', {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
table = a.LoadedTable('SIH', 'duckdb', '"sih_rd_union"', db_path={self.db!r}, table_name='sih_rd_union')
a.render_sih_source(table)
"""
        at = AppTest.from_string(wrapper, default_timeout=60).run()
        self.assertFalse(at.exception, [str(x.value) for x in at.exception])
        self.assertEqual(at.metric[1].value, "5")
        self.assertEqual(len(at.get("plotly_chart")), 3)
        at.selectbox(key="sih_union_frequency").select("Mês").run()
        self.assertFalse(at.exception)
        frame = next(x.value for x in at.dataframe if "n_ambos" in x.value.columns)
        self.assertEqual(int(frame.n.sum()), 5)
        self.assertEqual(int(frame.loc[frame.periodo.isna(), "n"].sum()), 1)
        at.multiselect(key="sih_years").select(2026).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[1].value, "2")
        at.multiselect(key="sih_years").set_value([]).run()
        at.multiselect(key="sih_hospital_filter").select("001").run()
        self.assertFalse(at.exception)
        self.assertEqual(at.metric[1].value, "2")
        at.multiselect(key="sih_hospital_filter").set_value([]).run()
        self.assertEqual(at.metric[1].value, "5")

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
        at.selectbox(key="upload_duckdb_table_SIH").select("sih_rd_fixture").run()
        self.assertFalse(at.exception, [str(x.value) for x in at.exception])
        self.assertEqual(at.selectbox(key="upload_duckdb_table_SIH").value, "sih_rd_fixture")
        self.assertEqual(at.radio(key="load_mode_SIH").value, "Upload DuckDB")
        self.assertEqual(len(at.get("plotly_chart")), 3)
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
