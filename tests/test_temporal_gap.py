"""Regressões da conversão temporal compartilhada e dos rótulos de GAP."""
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb
from streamlit.testing.v1 import AppTest

for name in ("streamlit.runtime.scriptrunner_utils.script_run_context",
             "streamlit.runtime.caching.cache_data_api", "streamlit.runtime.state.session_state_proxy"):
    logging.getLogger(name).setLevel(logging.ERROR)
APP_PATH = Path(os.environ.get("SIH_TEST_APP", Path(__file__).resolve().parents[1] / "streamlit_app.py"))
spec = importlib.util.spec_from_file_location("temporal_gap_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class TemporalGap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="temporal_gap_")
        cls.db = str(Path(cls.directory.name) / "fixture.duckdb")
        connection = duckdb.connect(cls.db)
        connection.execute("""
            CREATE TABLE fixture AS SELECT * FROM (VALUES
                ('20110101', '2011', '1', 'a', 'G009'),
                ('31122011', '2011', '12', 'b', 'G009'),
                ('01/01/2024', '2024', '1', 'a', 'G009'),
                ('31-12-2024', '2024', '12', 'b', 'G009'),
                ('2025-01-01', '2025', '1', 'a', 'G009'),
                ('20111301', '2011', '13', 'bad', 'G009'),
                (NULL, '2024', '0', 'bad', 'G009')
            ) AS rows(raw_date, ANO_CMPT, MES_CMPT, category, DIAG_PRINC)
        """)
        connection.execute("""
            CREATE TABLE sinan_hospital AS SELECT * FROM (VALUES
                ('2024-01-01', '1', '1', NULL),
                ('2024-01-02', '1', '2', '2024-01-02'),
                ('2024-01-03', '2', '1', NULL),
                ('2024-01-04', NULL, '1', NULL),
                ('2024-01-05', '1', NULL, '2024-01-05'),
                ('2024-01-06', '1', '9', '2024-01-06'),
                ('2024-12-31', '1.0', '1.0', NULL),
                ('2024-06-10', '2', '2', NULL)
            ) AS rows(DT_NOTIFIC, CLASSI_FIN, ATE_HOSPIT, ATE_INTERN)
        """)
        connection.execute("""
            CREATE TABLE ciha_modality AS SELECT * FROM (VALUES
                ('2024-01-01', '01', 'G009'),
                ('2024-01-02', '1', 'G009'),
                ('2024-01-03', '1.0', 'G009'),
                ('2024-01-04', '02', 'G009'),
                ('2024-01-05', '2.0', 'G009'),
                ('2024-12-31', NULL, 'G009'),
                ('2024-12-31', '01', 'J189')
            ) AS rows(DT_ATEND, MODALIDADE, DIAG_PRINC)
        """)
        connection.close()
        cls.dt = app.date_expr("raw_date")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def table(self, source):
        return app.LoadedTable(source, "duckdb", '"fixture"', db_path=self.db, table_name="fixture")

    def test_shared_parser_and_annual_monthly_totals_for_all_sources(self):
        for source in ("SINAN", "SIM", "CIHA", "SIH"):
            with self.subTest(source=source):
                table = self.table(source)
                dates = app.run_query(table, f"SELECT {self.dt} AS dt FROM {table.ref_sql}").dt
                self.assertEqual(int(dates.isna().sum()), 2)
                annual = app.query_timeseries(table, self.dt, "", "year")
                monthly = app.query_timeseries(table, self.dt, "", "month")
                expected = {2011: 2, 2024: 2, 2025: 1}
                self.assertEqual(dict(zip(annual.periodo.dt.year, annual.n)), expected)
                self.assertEqual(monthly.groupby(monthly.periodo.dt.year).n.sum().to_dict(), expected)
                category = app.query_yearly_category(table, self.dt, '"category"', "")
                heatmap = app.query_heatmap(table, self.dt, "", "month")
                self.assertEqual(category.groupby("ano").n.sum().to_dict(), expected)
                self.assertEqual(heatmap.groupby("ano").n.sum().to_dict(), expected)

    def test_dmy_compact_dates_are_not_lost_when_first_digits_look_like_year(self):
        with duckdb.connect() as connection:
            sql = app.date_expr("value")
            for value, expected in [("20112024", "2024-11-20"), ("20102011", "2011-10-20"),
                                    ("20240229", "2024-02-29"), ("20250229", None),
                                    ("202400", None), ("202413", None)]:
                with self.subTest(value=value):
                    date = connection.execute(f"SELECT {sql} FROM (SELECT ? AS value)", [value]).fetchone()[0]
                    self.assertEqual(str(date) if date else None, expected)

    def test_sih_competence_reconciles_with_calendar_years(self):
        table = self.table("SIH")
        dt = app.sih_reference_dates(["ANO_CMPT", "MES_CMPT"])["Competência de processamento"]
        annual = app.query_sih_cid_distribution(table, ["DIAG_PRINC"], dt_sql=dt, frequency="year")
        monthly = app.query_sih_cid_distribution(table, ["DIAG_PRINC"], dt_sql=dt, frequency="month")
        expected = {2011: 2, 2024: 2, 2025: 1}
        self.assertEqual(dict(zip(annual.periodo.dt.year, annual.n)), expected)
        self.assertEqual(monthly.groupby(monthly.periodo.dt.year).n.sum().to_dict(), expected)
        union = app.query_sih_diagnosis_union(table, {"principal": ["DIAG_PRINC"]}, dt_sql=dt)
        self.assertEqual(int(union.loc[union.periodo.notna(), "n"].sum()), 5)
        self.assertEqual(int(union.loc[union.periodo.isna(), "n"].sum()), 2)

    def test_weekly_heatmap_keeps_intentional_iso_year(self):
        heat = app.query_heatmap(self.table("SINAN"), self.dt, "", "week")
        self.assertEqual(int(heat.n.sum()), 5)
        self.assertEqual(int(heat.loc[(heat.ano == 2010) & (heat.semana == 52), "n"].sum()), 1)
        # 01/01/2011 belongs to ISO week 52/2010, but calendar annual charts stay in 2011.

    def test_gap_includes_partial_edge_years_and_filters_exact_common_dates(self):
        context = {"table": self.table("SINAN"), "exprs": {"dt": self.dt}, "base_where": ""}
        contexts = [context, {**context, "base_where": f"WHERE ({self.dt}) <= DATE '2024-12-31'"}]
        specs = [(context, "SINAN — todos", ""), (contexts[1], "SIM — óbitos", contexts[1]["base_where"])]
        counts, gaps, _ = app.comparison_gap_frames(specs, "year", contexts)
        self.assertEqual((counts.periodo.min().year, counts.periodo.max().year), (2011, 2024))
        self.assertEqual(counts.set_index(counts.periodo.dt.year).loc[[2011, 2024], "SINAN — todos"].tolist(), [2, 2])
        self.assertEqual(int(counts["SINAN — todos"].sum()), 4)
        self.assertEqual(int(gaps.gap_comparador_menos_referencia.sum()), 0)

    def test_gap_labels_preserve_counts_signs_and_export_columns(self):
        counts = app.pd.DataFrame({"periodo": app.pd.to_datetime(["2011-01-01", "2024-01-01", "2025-01-01"]),
                                   "SINAN — todos": [1200, 1, 0], "SIM — óbitos": [20, 4, 0]})
        gaps = app.pd.DataFrame({"periodo": counts.periodo, "comparador": ["SIM − SINAN"] * 3,
                                "n_referencia": [1200, 1, 0], "n_comparador": [20, 4, 0],
                                "gap_comparador_menos_referencia": [-1180, 3, 0],
                                "gap_pct_sobre_referencia": [-98.3, 300, float("nan")]})
        rendered = []
        with patch.object(app, "comparison_gap_frames", return_value=(counts, gaps, [])), \
             patch.object(app, "render_plotly_chart", side_effect=lambda fig, **kw: rendered.append((fig, kw))), \
             patch.object(app, "copyable_dataframe"), patch.object(app, "download_button") as download, \
             patch.object(app.st, "markdown"):
            app.render_gap_pair("SINAN × SIM", [({}, "SINAN", ""), ({}, "SIM", "")], "year", [], "test")
        line = rendered[0][0]
        self.assertEqual(list(line.data[0].text), ["1.200", "1", "0"])
        self.assertIn("text", line.data[0].mode)
        self.assertEqual(list(rendered[2][0].data[0].text), ["-1.180", "3", "0"])
        self.assertEqual(rendered[2][0].data[0].textposition, "outside")
        self.assertIn("Somatórios", rendered[0][1]["como_ler"])
        self.assertNotIn("rotulo", gaps.columns)
        self.assertNotIn("rotulo", download.call_args_list[0].args[0].columns)

    def hospital_context(self, source):
        name = "sinan_hospital" if source == "SINAN" else "ciha_modality"
        table = app.LoadedTable(source, "duckdb", f'"{name}"', db_path=self.db, table_name=name)
        selection = SimpleNamespace(ate_hospit_col="ATE_HOSPIT", modalidade_col="MODALIDADE", cid_cols=["DIAG_PRINC"])
        date_col = "DT_NOTIFIC" if source == "SINAN" else "DT_ATEND"
        return {"table": table, "sel": selection, "exprs": {"dt": app.date_expr(date_col),
                "classi_code": app.clean_code_expr("CLASSI_FIN") if source == "SINAN" else None}, "base_where": ""}

    def test_sinan_strata_require_hospital_flag_and_preserve_unknown_class_in_total(self):
        context = self.hospital_context("SINAN")
        expected = {"Confirmados com internação": 2, "Todos os casos com internação": 4, "Descartados com internação": 1}
        for mode, n in expected.items():
            with self.subTest(mode=mode):
                _, label, where = app.comparison_sinan_hospital_spec(context, mode)
                self.assertIn("com internação", label)
                self.assertEqual(app.count_rows(context["table"], where), n)
        missing_flag = {**context, "sel": SimpleNamespace(ate_hospit_col=None)}
        with self.assertRaisesRegex(ValueError, "ATE_HOSPIT"):
            app.comparison_sinan_hospital_spec(missing_flag, "Todos os casos com internação")
        missing_class = {**context, "exprs": {"dt": context["exprs"]["dt"]}}
        with self.assertRaisesRegex(ValueError, "CLASSI_FIN"):
            app.comparison_sinan_hospital_spec(missing_class, "Descartados com internação")
        self.assertEqual(app.count_rows(context["table"], app.comparison_sinan_hospital_spec(missing_class, "Todos os casos com internação")[2]), 4)

    def test_ciha_total_hospital_ambulatory_and_missing_modality(self):
        context = self.hospital_context("CIHA")
        for mode, n in [("Total de atendimentos", 6), ("Apenas hospitalar", 3), ("Apenas ambulatorial", 2)]:
            with self.subTest(mode=mode):
                _, _, where = app.comparison_ciha_modality_spec(context, mode)
                self.assertEqual(app.count_rows(context["table"], where), n)
        no_modality = {**context, "sel": SimpleNamespace(cid_cols=["DIAG_PRINC"], modalidade_col=None)}
        with self.assertRaisesRegex(ValueError, "MODALIDADE"):
            app.comparison_ciha_modality_spec(no_modality, "Apenas hospitalar")
        _, _, where = app.comparison_ciha_modality_spec(no_modality, "Total de atendimentos")
        self.assertEqual(app.count_rows(context["table"], where), 6)
        filtered = {**context, "base_where": "WHERE DT_ATEND <= '2024-01-02'"}
        self.assertEqual(app.count_rows(context["table"], app.comparison_ciha_modality_spec(filtered, "Total de atendimentos")[2]), 2)

    def test_combined_gap_is_sum_of_counts_minus_reference_once(self):
        sinan, ciha = self.hospital_context("SINAN"), self.hospital_context("CIHA")
        sih = {"table": self.table("SIH"), "exprs": {"dt": self.dt}, "base_where": f"WHERE EXTRACT(YEAR FROM ({self.dt})) = 2024"}
        specs = [app.comparison_sinan_hospital_spec(sinan, "Todos os casos com internação"),
                 app.comparison_ciha_modality_spec(ciha, "Apenas hospitalar"), (sih, "SIH — internações", sih["base_where"])]
        combined = [("CIHA + SIH (soma aritmética)", [specs[1][1], specs[2][1]])]
        for freq in ("year", "month"):
            with self.subTest(freq=freq):
                counts, gaps, _ = app.comparison_gap_frames(specs, freq, [sinan, ciha, sih], combined)
                self.assertEqual(counts[combined[0][0]].tolist(), (counts[specs[1][1]] + counts[specs[2][1]]).tolist())
                combined_gaps = gaps[gaps.comparador.str.startswith("CIHA + SIH")]
                self.assertEqual(combined_gaps.gap_comparador_menos_referencia.tolist(), (counts[combined[0][0]] - counts[specs[0][1]]).tolist())
                self.assertEqual(len(gaps), 3 * len(counts))
                rendered = []
                with patch.object(app, "render_plotly_chart", side_effect=lambda fig, **kw: rendered.append(fig)), \
                     patch.object(app, "copyable_dataframe"), patch.object(app, "download_button"), patch.object(app.st, "markdown"):
                    app.render_gap_pair("Assistência", specs, freq, [sinan, ciha, sih], "test", combined_series=combined)
                self.assertEqual(len(rendered[0].data), 4)
                self.assertEqual(len(rendered[2].data), 3)
                self.assertEqual(sum(len(trace.y) for trace in rendered[1].data), 4)

    def test_gap_ui_controls_update_counts_and_migrate_legacy_sinan_choice(self):
        wrapper = f'''
import importlib.util, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("gap_ui_test", {str(APP_PATH)!r})
a = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = a
spec.loader.exec_module(a)
def context(source, name, date_col, selection, classi=None):
    t = a.LoadedTable(source, "duckdb", '"' + name + '"', db_path={self.db!r}, table_name=name)
    return {{"source": source, "table": t, "sel": selection, "exprs": {{"dt": a.date_expr(date_col), "classi_code": classi}}, "base_where": ""}}
sinan = context("SINAN", "sinan_hospital", "DT_NOTIFIC", SimpleNamespace(ate_hospit_col="ATE_HOSPIT"), a.clean_code_expr("CLASSI_FIN"))
ciha = context("CIHA", "ciha_modality", "DT_ATEND", SimpleNamespace(modalidade_col="MODALIDADE", cid_cols=["DIAG_PRINC"]))
sih = context("SIH", "fixture", "raw_date", None)
sih["sih_roles"] = {{"principal": ["DIAG_PRINC"]}}
if "comp_gap_sinan_assist_mode" not in a.st.session_state:
    a.st.session_state["comp_gap_sinan_assist_mode"] = "Apenas confirmados"
a.render_gap_comparison([sinan, ciha, sih], ["SINAN", "CIHA", "SIH"])
'''
        at = AppTest.from_string(wrapper, default_timeout=60).run()
        self.assertFalse(at.exception, [str(x.value) for x in at.exception])
        self.assertEqual(at.selectbox(key="comp_gap_sinan_assist_mode").value, "Confirmados com internação")
        self.assertEqual(at.selectbox(key="comp_gap_ciha_mode").value, "Apenas hospitalar")
        self.assertEqual(len(at.get("plotly_chart")), 3)
        for mode, n in [("Total de atendimentos", 6), ("Apenas ambulatorial", 2), ("Apenas hospitalar", 3)]:
            at.selectbox(key="comp_gap_ciha_mode").select(mode).run()
            self.assertFalse(at.exception, [str(x.value) for x in at.exception])
            counts = at.dataframe[1].value
            self.assertEqual(int(counts.iloc[:, 2].sum()), n)
            self.assertEqual(int(counts.iloc[:, 4].sum()), n + 2)
        for mode, n in [("Descartados com internação", 1), ("Todos os casos com internação", 4), ("Confirmados com internação", 2)]:
            at.selectbox(key="comp_gap_sinan_assist_mode").select(mode).run()
            self.assertFalse(at.exception, [str(x.value) for x in at.exception])
            self.assertEqual(int(at.dataframe[1].value.iloc[:, 1].sum()), n)


if __name__ == "__main__":
    unittest.main()
