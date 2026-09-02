from pathlib import Path

p = Path("streamlit_app.py")
s = p.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global s
    count = s.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: esperado 1 alvo, encontrados {count}")
    s = s.replace(old, new, 1)


replace_once(
    'APP_VERSION = "2026-09-01-v80-metodo-unico-resultados-validos"',
    'APP_VERSION = "2026-09-02-v81-completude-quadros-raincloud"',
    "APP_VERSION",
)

replace_once(
'''    return run_query(table, sql)\n\n\ndef query_sinan_sheet_column_errors(''',
'''    result = run_query(table, sql)\n    if result.empty:\n        return result\n\n    # O SQL acima resume apenas categorias observadas. Para a auditoria de\n    # completude, porém, o gráfico deve mostrar TODO o domínio oficial do quadro\n    # correspondente, inclusive possibilidades com contagem zero. Isso evita que\n    # códigos válidos (p.ex. 03/08/28 na bacterioscopia) desapareçam visualmente\n    # apenas porque não ocorreram no recorte atual.\n    total_elegivel = int(pd.to_numeric(result["total_elegivel"], errors="coerce").max() or 0)\n    total_validamente_preenchido = int(\n        pd.to_numeric(result["total_validamente_preenchido"], errors="coerce").max() or 0\n    )\n    observed = {str(row.categoria): row for row in result.itertuples(index=False)}\n    expanded_rows: List[Dict[str, object]] = []\n\n    def append_category(categoria: str, status: str) -> None:\n        row = observed.get(categoria)\n        n = int(getattr(row, "n", 0) or 0) if row is not None else 0\n        pct_valid = (\n            round(100.0 * n / total_validamente_preenchido, 2)\n            if status == "Válida" and total_validamente_preenchido > 0\n            else np.nan\n        )\n        pct_total = round(100.0 * n / total_elegivel, 2) if total_elegivel > 0 else np.nan\n        expanded_rows.append({\n            "categoria": categoria,\n            "status_preenchimento": status,\n            "n": n,\n            "total_elegivel": total_elegivel,\n            "total_validamente_preenchido": total_validamente_preenchido,\n            "pct_entre_validamente_preenchidos": pct_valid,\n            "pct_total_elegivel": pct_total,\n        })\n\n    for code, label in mapping.items():\n        append_category(f"{code} — {label}", "Válida")\n    append_category("Célula vazia", "Vazia")\n    append_category("Valor fora do dicionário", "Fora do dicionário")\n    return pd.DataFrame(expanded_rows)\n\n\ndef query_sinan_sheet_column_errors(''',
    "expandir domínio completo da completude",
)

replace_once(
'''    return run_query(table, sql)\n\n\ndef query_sinan_lcr_parameter_coverage(''',
'''    return run_query(table, sql)\n\n\ndef query_sinan_lcr_aberrant_cases(\n    table: LoadedTable,\n    exprs: Dict[str, Optional[str]],\n    where_sql: str,\n    parameter_label: str,\n    upper_bound: float,\n) -> pd.DataFrame:\n    """Lista valores acima do limite operacional exibido no raincloud.\n\n    Os registros não são corrigidos nem descartados do arquivo-fonte. A tabela\n    existe justamente para tornar visíveis os valores que ficam fora do eixo\n    solicitado (0-100% para diferenciais celulares; 0-999 mg/dL para glicose).\n    """\n    param_key = SINAN_LCR_COMPLETENESS_PARAMS.get(parameter_label)\n    raw_expr = _sinan_lcr_completeness_param_expr(exprs, parameter_label)\n    value_expr = exprs.get(f"lab_{param_key}") if param_key else None\n    classi_expr = exprs.get("classi_raw") or exprs.get("classi_code")\n    if not param_key or not raw_expr or not value_expr or not classi_expr:\n        return pd.DataFrame()\n    identifiers = _sinan_sheet_identifier_select(exprs)\n    field_name = str(SINAN_QUIMIO_PARAMS.get(param_key, {}).get("default_col", param_key))\n    group_sql = f"""\n        CASE\n            WHEN ({classi_expr}) = '1' THEN 'Casos confirmados'\n            WHEN ({classi_expr}) = '2' THEN 'Casos descartados'\n            ELSE NULL\n        END\n    """\n    sql = f"""\n        WITH fonte AS (\n            SELECT ROW_NUMBER() OVER () AS __linha_fonte, *\n            FROM {table.ref_sql}\n        ), avaliados AS (\n            SELECT __linha_fonte AS linha_fonte,\n                   {qstr(loaded_table_origin_format(table))} AS formato_origem,\n                   {qstr(table.label or table.source)} AS origem,\n                   {identifiers},\n                   {group_sql} AS grupo,\n                   {raw_expr} AS valor_informado,\n                   ({value_expr}) AS valor_numerico\n            FROM fonte\n            {where_sql}\n        )\n        SELECT linha_fonte, formato_origem, origem, NU_NOTIFIC, NM_PACIENT,\n               CLASSI_FIN, CON_DIAGES, grupo,\n               {qstr(field_name)} AS campo, valor_informado, valor_numerico,\n               {float(upper_bound)} AS limite_maximo_exibido\n        FROM avaliados\n        WHERE grupo IS NOT NULL\n          AND valor_numerico IS NOT NULL\n          AND valor_numerico > {float(upper_bound)}\n        ORDER BY valor_numerico DESC, linha_fonte\n    """\n    return run_query(table, sql)\n\n\ndef query_sinan_lcr_parameter_coverage(''',
    "helper de casos aberrantes",
)

replace_once(
'''        marker={"color": color, "size": 4, "opacity": 0.30},''',
'''        marker={"color": color, "size": 5, "opacity": 0.34},''',
    "marcadores raincloud",
)

replace_once(
'''        height=max(420, 235 * max(1, len(groups_present))),\n        showlegend=False,''',
'''        height=max(560, 315 * max(1, len(groups_present))),\n        showlegend=False,''',
    "altura raincloud",
)

replace_once(
'''        hovermode="closest",\n    )\n    return fig, pd.DataFrame(summary_rows), sampled_rain\n''',
'''        hovermode="closest",\n    )\n    fig.update_xaxes(tickfont={"size": 13}, title_font={"size": 14}, automargin=True)\n    fig.update_yaxes(tickfont={"size": 13}, title_font={"size": 14}, automargin=True)\n    if lower_bound is not None and upper_bound is not None:\n        fig.update_xaxes(range=[float(lower_bound), float(upper_bound)])\n    return fig, pd.DataFrame(summary_rows), sampled_rain\n''',
    "eixos raincloud",
)

replace_once(
'''            frequency = query_sinan_lcr_raincloud_frequency(table, exprs, rain_where, rain_label)\n            if coverage.empty:\n                st.info("Não há casos confirmados ou descartados com punção no recorte atual.")\n                continue\n''',
'''            frequency = query_sinan_lcr_raincloud_frequency(table, exprs, rain_where, rain_label)\n            fixed_upper_bound = {\n                "glico": 999.0,\n                "neutro": 100.0,\n                "linfo": 100.0,\n                "eosi": 100.0,\n            }.get(rain_param_key)\n            aberrant_df = (\n                query_sinan_lcr_aberrant_cases(\n                    table, exprs, rain_where, rain_label, fixed_upper_bound\n                )\n                if fixed_upper_bound is not None\n                else pd.DataFrame()\n            )\n            if coverage.empty:\n                st.info("Não há casos confirmados ou descartados com punção no recorte atual.")\n                continue\n''',
    "limites dos rainclouds",
)

replace_once(
'''            copyable_dataframe(coverage, width="stretch", hide_index=True)\n            if frequency.empty:\n                st.info("Não há valores numéricos válidos para gerar o raincloud deste parâmetro.")\n                continue\n            unit = meta_rain.unidade if meta_rain else "valor registrado"\n''',
'''            copyable_dataframe(coverage, width="stretch", hide_index=True)\n\n            if fixed_upper_bound is not None:\n                if not aberrant_df.empty:\n                    if rain_param_key in {"neutro", "linfo", "eosi"}:\n                        limit_text = "100%"\n                        warning_text = (\n                            f"⚠️ ATENÇÃO: foram encontrados {format_int_br(len(aberrant_df))} caso(s) com "\n                            f"{rain_label.lower()} acima de {limit_text}. Esses valores são incompatíveis com uma "\n                            "proporção percentual e foram retirados apenas do raincloud; permanecem preservados no banco "\n                            "e estão listados integralmente abaixo."\n                        )\n                    else:\n                        limit_text = "999 mg/dL"\n                        warning_text = (\n                            f"⚠️ ATENÇÃO: foram encontrados {format_int_br(len(aberrant_df))} caso(s) com "\n                            f"{rain_label.lower()} acima de {limit_text}. O raincloud é limitado a 0-999 mg/dL; "\n                            "os valores acima desse teto não são alterados no banco e estão listados integralmente abaixo."\n                        )\n                    st.error(warning_text)\n                    copyable_dataframe(aberrant_df, width="stretch", hide_index=True)\n                    download_button(\n                        aberrant_df,\n                        f"sinan_lcr_{rain_param_key}_casos_aberrantes.csv",\n                        label="Baixar casos aberrantes (CSV)",\n                        max_rows=len(aberrant_df),\n                    )\n                else:\n                    st.success(\n                        f"Nenhum caso acima do limite de {format_int_br(int(fixed_upper_bound))}"\n                        + ("%" if rain_param_key in {"neutro", "linfo", "eosi"} else " mg/dL")\n                        + " foi encontrado no recorte atual."\n                    )\n                if not frequency.empty:\n                    frequency = frequency[pd.to_numeric(frequency["valor"], errors="coerce") <= fixed_upper_bound].copy()\n\n            if frequency.empty:\n                st.info("Não há valores numéricos dentro do intervalo exibido para gerar o raincloud deste parâmetro.")\n                continue\n            unit = meta_rain.unidade if meta_rain else "valor registrado"\n''',
    "alerta e tabela de aberrantes",
)

anchor = 'title_rain = f"Raincloud half-eye de {rain_label.lower()} — confirmados e descartados"'
start = s.find(anchor)
if start < 0:
    raise RuntimeError("âncora do raincloud do LCR não encontrada")
old = '''                unit_suffix=f" {unit}",\n                lower_bound=0.0,\n            )\n'''
new = '''                unit_suffix=f" {unit}",\n                lower_bound=0.0,\n                upper_bound=fixed_upper_bound,\n            )\n'''
pos = s.find(old, start)
if pos < 0:
    raise RuntimeError("chamada do raincloud do LCR não encontrada")
s = s[:pos] + s[pos:].replace(old, new, 1)

replace_once(
'''            category_order = completeness_df["categoria"].tolist()\n            fig_complete = px.bar(\n                completeness_df,\n                x="categoria",\n                y="n",\n                color="status_preenchimento",\n                text="texto",\n                title=f"Completude e conteúdo de {selected_method} — {completeness_group.lower()}",\n                labels={\n                    "categoria": "Conteúdo da célula segundo o dicionário",\n                    "n": "Casos elegíveis",\n                    "status_preenchimento": "Situação",\n                    "percentual_exibido": "Percentual exibido",\n                    "referencia_percentual": "Denominador do percentual",\n                },\n                hover_data={\n                    "texto": False,\n                    "percentual_exibido": ":.2f",\n                    "referencia_percentual": True,\n                    "total_elegivel": True,\n                    "total_validamente_preenchido": True,\n                    "pct_entre_validamente_preenchidos": ":.2f",\n                    "pct_total_elegivel": ":.2f",\n                },\n                category_orders={"categoria": category_order, "status_preenchimento": ["Válida", "Vazia", "Fora do dicionário"]},\n                color_discrete_map={"Válida": PLOTLY_DEFAULT_BLUE, "Vazia": "#7F7F7F", "Fora do dicionário": "#D62728"},\n            )\n            fig_complete.update_xaxes(tickangle=-35)\n            fig_complete.update_traces(textposition="outside", cliponaxis=False)\n''',
'''            category_order = completeness_df["categoria"].tolist()\n            fig_complete = px.bar(\n                completeness_df,\n                x="n",\n                y="categoria",\n                orientation="h",\n                color="status_preenchimento",\n                text="texto",\n                title=f"Completude e conteúdo de {selected_method} — {completeness_group.lower()}",\n                labels={\n                    "categoria": "Conteúdo da célula segundo o dicionário",\n                    "n": "Casos elegíveis",\n                    "status_preenchimento": "Situação",\n                    "percentual_exibido": "Percentual exibido",\n                    "referencia_percentual": "Denominador do percentual",\n                },\n                hover_data={\n                    "texto": False,\n                    "percentual_exibido": ":.2f",\n                    "referencia_percentual": True,\n                    "total_elegivel": True,\n                    "total_validamente_preenchido": True,\n                    "pct_entre_validamente_preenchidos": ":.2f",\n                    "pct_total_elegivel": ":.2f",\n                },\n                category_orders={"status_preenchimento": ["Válida", "Vazia", "Fora do dicionário"]},\n                color_discrete_map={"Válida": PLOTLY_DEFAULT_BLUE, "Vazia": "#7F7F7F", "Fora do dicionário": "#D62728"},\n            )\n            fig_complete.update_yaxes(\n                categoryorder="array",\n                categoryarray=list(reversed(category_order)),\n                automargin=True,\n                tickfont={"size": 12},\n            )\n            fig_complete.update_xaxes(automargin=True)\n            fig_complete.update_layout(height=max(560, 36 * len(category_order) + 190))\n            fig_complete.update_traces(textposition="outside", cliponaxis=False)\n''',
    "dimensionamento do gráfico de completude",
)

replace_once(
'''            st.caption(\n                "Para códigos válidos, a porcentagem usa exclusivamente as células validamente preenchidas como denominador. "\n                "Para ‘Célula vazia’ e ‘Valor fora do dicionário’, a porcentagem usa todos os casos elegíveis, "\n                "pois essas células não podem integrar o denominador dos valores válidos."\n            )\n''',
'''            st.caption(\n                "Todas as possibilidades previstas no quadro oficial correspondente são exibidas, inclusive categorias com contagem zero. "\n                "Para códigos válidos, a porcentagem usa exclusivamente as células validamente preenchidas como denominador. "\n                "Para ‘Célula vazia’ e ‘Valor fora do dicionário’, a porcentagem usa todos os casos elegíveis, "\n                "pois essas células não podem integrar o denominador dos valores válidos."\n            )\n''',
    "legenda de completude",
)

p.write_text(s, encoding="utf-8")
print("streamlit_app.py atualizado para v81")
