"""Regressões da conversão temporal compartilhada e dos rótulos de GAP."""
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import duckdb

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


if __name__ == "__main__":
    unittest.main()
