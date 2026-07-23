# -*- coding: utf-8 -*-
"""
Painel epidemiológico para meningite — SINAN, SIM e CIHA

O app aceita upload de DuckDB, Parquet, CSV ou DBF, além de bancos hospedados no github em
Parquet, calcula indicadores descritivos e separa:
- CID-10 bruto do caso/óbito/atendimento;
- classificação epidemiológica específica do SINAN, especialmente CON_DIAGES;
- definições operacionais de série: total de notificações, confirmados, descartados e sem classificação/ignorados.

Arquivos CSV e DBF enviados são convertidos internamente para Parquet (mantendo todos os
campos como texto, para preservar zeros à esquerda em identificadores como NU_NOTIFIC) e passam
a usar exatamente o mesmo caminho de consulta dos demais formatos.

Executar:
    streamlit run app_meningite_epidemiologico.py

Dependências:
    pip install streamlit duckdb pandas numpy plotly fastparquet dbfread
"""

from __future__ import annotations

import csv
import hashlib
import html as html_lib
import json
import re
import tempfile
import threading
import textwrap
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

try:
    import fastparquet as fp
except Exception:  # fallback defensivo: o app segue com DuckDB nativo se fastparquet não estiver instalado
    fp = None

try:
    from dbfread import DBF as _DBFReader
except Exception:  # fallback defensivo: upload de DBF fica indisponível, mas o resto do app segue funcionando
    _DBFReader = None



# =============================================================================
# Configuração geral
# =============================================================================

st.set_page_config(
    page_title="Meningite — SINAN, SIM e CIHA",
    page_icon="🧫",
    layout="wide",
)

APP_VERSION = "2026-07-23-v49r3-sobreposicao-nu-notific"

# =============================================================================
# Controles de desempenho e limites defensivos
# =============================================================================

DEFAULT_MAX_PARQUET_FILES_PER_LOAD = 20
DEFAULT_DISPLAY_ROW_LIMIT = 1000
DEFAULT_COPY_ROW_LIMIT = 300
DEFAULT_DOWNLOAD_ROW_LIMIT = 50000
DEFAULT_PREVIEW_ROW_LIMIT = 200
DEFAULT_MAX_PREVIEW_ROWS = 5000
DEFAULT_SQL_LAB_ROW_LIMIT = 5000
DEFAULT_FULL_EXPORT_ROW_LIMIT = 100000
UPLOAD_CHUNK_SIZE = 1024 * 1024
DEFAULT_DUCKDB_MEMORY_LIMIT = "3GB"
DEFAULT_DUCKDB_THREADS = 2
DEFAULT_QUERY_CACHE_MAX_ENTRIES = 128
DEFAULT_FASTPARQUET_ROW_LIMIT = 1500000
DUCKDB_TEMP_SUBDIR = "meningite_duckdb_tmp"
DEATH_RED = "#D62728"
LETHALITY_RED = DEATH_RED
LETHALITY_LABEL = "Letalidade — óbitos por meningite / confirmados"
LETHALITY_KNOWN_EVOL_LABEL = "Letalidade — óbitos por meningite / confirmados com evolução conhecida"
DARK_GRAY = "#4D4D4D"
COVID_CONTEXT_NOTE = (
    "Anotação de contexto 2020-2021: pandemia de COVID-19, reorganização assistencial, "
    "alteração da circulação de agentes respiratórios e possibilidade de subnotificação. "
    "A faixa é contextual e não atribui causalidade às variações observadas."
)
PLOTLY_DEFAULT_BLUE = "#636EFA"
APP_COLOR_SEQUENCE = (
    "#1F77B4",  # azul
    "#FF7F0E",  # laranja
    "#2CA02C",  # verde
    "#9467BD",  # roxo
    "#17BECF",  # ciano
    "#8C564B",  # marrom
    "#7F7F7F",  # cinza
    "#BCBD22",  # oliva
    "#E377C2",  # rosa
    "#000000",  # preto
)
DEATH_COLOR_TERMS = (
    "obit",
    "obito",
    "obitos",
    "morte",
    "mortes",
    "mortal",
    "mortalidade",
    "letal",
    "letalidade",
    "fatal",
    "fatalidade",
    "death",
    "deaths",
)
PLOTLY_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}


# =============================================================================
# Aparência geral da aplicação e dos gráficos
# =============================================================================

def _norm_ui_text(value: object) -> str:
    """Normaliza texto apenas para testes internos de rótulos/cores."""
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.lower()


def _text_mentions_death(value: object) -> bool:
    text = _norm_ui_text(value)
    return any(term in text for term in DEATH_COLOR_TERMS)


def render_app_css() -> None:
    """Aplica ajustes discretos de legibilidade sem alterar a navegação do app."""
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.25rem;
            padding-bottom: 2.5rem;
            max-width: 1480px;
        }
        h1, h2, h3 {
            letter-spacing: -0.015em;
            line-height: 1.18;
        }
        div[data-testid="stCaptionContainer"] p {
            line-height: 1.45;
        }
        div[data-testid="stMetric"] {
            background: rgba(250, 250, 250, 0.72);
            border: 1px solid rgba(49, 51, 63, 0.10);
            border-radius: 0.75rem;
            padding: 0.65rem 0.8rem;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid rgba(49, 51, 63, 0.10);
            border-radius: 0.55rem;
        }
        section[data-testid="stSidebar"] .stRadio label,
        section[data-testid="stSidebar"] .stCheckbox label {
            line-height: 1.3;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _axis_text_values(fig: go.Figure) -> List[str]:
    values: List[str] = []
    for axis_name in ("xaxis", "yaxis"):
        try:
            axis = getattr(fig.layout, axis_name)
            values.append(str(axis.title.text or ""))
        except Exception:
            pass
    return values


def _layout_text_values(fig: go.Figure) -> List[str]:
    values: List[str] = []
    try:
        values.append(str(fig.layout.title.text or ""))
    except Exception:
        pass
    values.extend(_axis_text_values(fig))
    try:
        values.append(str(fig.layout.legend.title.text or ""))
    except Exception:
        pass
    return values


def _figure_axis_mentions_death(fig: go.Figure) -> bool:
    return any(_text_mentions_death(value) for value in _axis_text_values(fig))


def _figure_title_or_legend_mentions_death(fig: go.Figure) -> bool:
    values: List[str] = []
    try:
        values.append(str(fig.layout.title.text or ""))
    except Exception:
        pass
    try:
        values.append(str(fig.layout.legend.title.text or ""))
    except Exception:
        pass
    return any(_text_mentions_death(value) for value in values)


def _figure_context_mentions_death(fig: go.Figure) -> bool:
    return any(_text_mentions_death(value) for value in _layout_text_values(fig))


def _figure_skip_death_red(fig: go.Figure) -> bool:
    try:
        meta = fig.layout.meta
    except Exception:
        return False
    if isinstance(meta, dict):
        return bool(meta.get("skip_death_red"))
    return False


def disable_death_red(fig: go.Figure) -> go.Figure:
    """Impede que a regra global de óbito force vermelho em gráficos específicos."""
    if fig is None:
        return fig
    try:
        meta = fig.layout.meta if isinstance(fig.layout.meta, dict) else {}
        meta = dict(meta)
        meta["skip_death_red"] = True
        fig.update_layout(meta=meta)
    except Exception:
        pass
    return fig


def _sequence_values(values: object) -> List[object]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [values]
    try:
        return list(values)
    except Exception:
        return []


def _expand_marker_colors(existing_color: object, n: int) -> List[object]:
    if n <= 0:
        return []
    if isinstance(existing_color, (str, bytes)):
        return [existing_color] * n
    values = _sequence_values(existing_color)
    if values:
        if len(values) >= n:
            return values[:n]
        return values + [values[-1]] * (n - len(values))
    return [PLOTLY_DEFAULT_BLUE] * n


def _bar_category_values(trace: go.BaseTraceType) -> List[object]:
    x_vals = _sequence_values(getattr(trace, "x", None))
    y_vals = _sequence_values(getattr(trace, "y", None))
    orientation = str(getattr(trace, "orientation", "") or "")
    if orientation == "h" and y_vals:
        return y_vals
    if orientation != "h" and x_vals:
        return x_vals
    return y_vals or x_vals


def _apply_death_red_to_bar_points(trace: go.BaseTraceType) -> bool:
    if str(getattr(trace, "type", "")) != "bar":
        return False
    category_values = _bar_category_values(trace)
    if not category_values:
        return False
    mask = [_text_mentions_death(value) for value in category_values]
    if not any(mask):
        return False
    try:
        existing_color = trace.marker.color
    except Exception:
        existing_color = None
    base_colors = _expand_marker_colors(existing_color, len(mask))
    colors = [DEATH_RED if is_death else base_colors[idx] for idx, is_death in enumerate(mask)]
    try:
        trace.update(marker_color=colors)
        return True
    except Exception:
        return False


def _trace_mentions_death(trace: go.BaseTraceType) -> bool:
    values = [
        getattr(trace, "name", ""),
        getattr(trace, "legendgroup", ""),
        getattr(trace, "hovertemplate", ""),
    ]
    return any(_text_mentions_death(value) for value in values)



def _set_trace_color(trace: go.BaseTraceType, color: str) -> None:
    """Aplica cor coerente em linhas, marcadores e barras sem depender do default do Plotly."""
    for update_kwargs in (
        {"line_color": color},
        {"marker_color": color},
    ):
        try:
            trace.update(**update_kwargs)
        except Exception:
            pass


def _figure_preserve_trace_colors(fig: go.Figure) -> bool:
    try:
        meta = fig.layout.meta
    except Exception:
        return False
    if isinstance(meta, dict):
        return bool(meta.get("preserve_trace_colors"))
    return False


def preserve_trace_colors(fig: go.Figure) -> go.Figure:
    """Mantém cores definidas manualmente em gráficos com semântica própria."""
    if fig is None:
        return fig
    try:
        meta = fig.layout.meta if isinstance(fig.layout.meta, dict) else {}
        meta = dict(meta)
        meta["preserve_trace_colors"] = True
        fig.update_layout(meta=meta)
    except Exception:
        pass
    return fig


def _apply_distinct_trace_colors(fig: go.Figure) -> None:
    """Usa uma paleta de alto contraste quando há múltiplas séries no mesmo gráfico."""
    if _figure_preserve_trace_colors(fig):
        return
    data = list(getattr(fig, "data", []) or [])
    if len(data) <= 1:
        return
    color_idx = 0
    force_death_red = not _figure_skip_death_red(fig)
    for trace in data:
        if force_death_red and _trace_mentions_death(trace):
            _set_trace_color(trace, DEATH_RED)
            continue
        color = APP_COLOR_SEQUENCE[color_idx % len(APP_COLOR_SEQUENCE)]
        color_idx += 1
        _set_trace_color(trace, color)


def enforce_death_related_red(fig: go.Figure) -> None:
    """Garante vermelho apenas quando o traço/categoria é explicitamente de óbito/morte/letalidade."""
    if fig is None or _figure_skip_death_red(fig):
        return
    data = list(getattr(fig, "data", []) or [])
    title_or_legend_mentions_death = _figure_title_or_legend_mentions_death(fig)
    for trace in data:
        point_level_colored = _apply_death_red_to_bar_points(trace)
        is_death_trace = (
            _trace_mentions_death(trace)
            or (title_or_legend_mentions_death and len(data) == 1 and not point_level_colored)
        )
        if not is_death_trace:
            continue
        for update_kwargs in (
            {"line_color": DEATH_RED},
            {"marker_color": DEATH_RED},
        ):
            try:
                trace.update(**update_kwargs)
            except Exception:
                pass


TITLE_DATABASE_PREFIX_RE = re.compile(r"^\s*(SINAN|SIM|CIHA)\s*[:\-–—]\s*", flags=re.IGNORECASE)
TITLE_SOURCE_PREFIX_RE = re.compile(r"^\s*\{source\}\s*[:\-–—]\s*", flags=re.IGNORECASE)
TITLE_MULTI_SPACE_RE = re.compile(r"\s+")

TITLE_EXACT_FIXES = {
    "total de notificações, confirmados, descartados e sem classificação / ignorados": "Total de notificações, confirmados, descartados e sem classificação/ignorados",
    "distribuição por escolaridade": "Distribuição por escolaridade",
    "escolaridade — casos confirmados e descartados": "Escolaridade — casos confirmados e descartados",
    "ocorrência de hospitalização por definição de caso": "Ocorrência de hospitalização por definição de caso",
    "prevalência acumulada dos sinais e sintomas entre confirmados": "Prevalência acumulada dos sinais e sintomas entre confirmados",
    "número de comunicantes por realização de quimioprofilaxia": "Número de comunicantes por realização de quimioprofilaxia",
    "vacinação informada como 'sim' por classificação final do caso": "Vacinação informada como “Sim” por classificação final do caso",
    "critério de confirmação entre casos confirmados": "Critério de confirmação entre casos confirmados",
    "classificação etiológica convertida para cid-10": "Classificação etiológica convertida para CID-10",
    "conversão para adequação ao cid-10 de meningite / encefalite": "Conversão para adequação ao CID-10 de meningite/encefalite",
    "registros classificados como g01 ou g02": "Registros classificados como G01 ou G02",
    "cid-10 dos registros com morte administrativa": "CID-10 dos registros com morte administrativa",
    "atendimentos e mortes administrativas": "Atendimentos e mortes administrativas",
    "atendimentos por modalidade hospitalar e ambulatorial": "Atendimentos por modalidade hospitalar e ambulatorial",
    "dias de permanência": "Dias de permanência",
    "média dos parâmetros do exame quimiocitológico do líquor (lcr)": "Média dos parâmetros do exame quimiocitológico do líquor (LCR)",
}

TITLE_TOKEN_FIXES = (
    (re.compile(r"\bcid\s*-\s*10\b", flags=re.IGNORECASE), "CID-10"),
    (re.compile(r"\bsinan\b", flags=re.IGNORECASE), "SINAN"),
    (re.compile(r"\bsim\b", flags=re.IGNORECASE), "SIM"),
    (re.compile(r"\bciha\b", flags=re.IGNORECASE), "CIHA"),
    (re.compile(r"\blcr\b", flags=re.IGNORECASE), "LCR"),
    (re.compile(r"\bnu_notific\b", flags=re.IGNORECASE), "NU_NOTIFIC"),
    (re.compile(r"\bnm_pacient\b", flags=re.IGNORECASE), "NM_PACIENT"),
    (re.compile(r"\bclassi_fin\b", flags=re.IGNORECASE), "CLASSI_FIN"),
    (re.compile(r"\bcon_diages\b", flags=re.IGNORECASE), "CON_DIAGES"),
    (re.compile(r"\bg0([0-9])\b", flags=re.IGNORECASE), lambda m: f"G0{m.group(1)}"),
    (re.compile(r"\ba([0-9]{2})\.([0-9])\b", flags=re.IGNORECASE), lambda m: f"A{m.group(1)}.{m.group(2)}"),
)


def clean_chart_title_text(title_text: object) -> str:
    """Padroniza títulos de gráficos sem mexer em rótulos técnicos dos eixos/séries."""
    if title_text is None:
        return ""
    cleaned = str(title_text).replace("\xa0", " ").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"<br\s*/?>", " — ", cleaned, flags=re.IGNORECASE)
    cleaned = TITLE_DATABASE_PREFIX_RE.sub("", cleaned)
    cleaned = TITLE_SOURCE_PREFIX_RE.sub("", cleaned)
    cleaned = cleaned.replace(" / ", "/")
    cleaned = re.sub(r"\s*[:;]\s*", lambda m: ": " if m.group(0).strip().startswith(":") else "; ", cleaned)
    cleaned = re.sub(r"\s*[–—-]\s*", " — ", cleaned)
    cleaned = TITLE_MULTI_SPACE_RE.sub(" ", cleaned).strip(" —:-")

    # Remove duplicidade literal, comum quando seção e gráfico recebem o mesmo texto.
    repeated = re.match(r"^(.+?)\s+—\s+\1$", cleaned, flags=re.IGNORECASE)
    if repeated:
        cleaned = repeated.group(1)

    exact = TITLE_EXACT_FIXES.get(cleaned.lower())
    if exact:
        cleaned = exact
    elif cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]

    for pattern, replacement in TITLE_TOKEN_FIXES:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = TITLE_MULTI_SPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _clean_figure_annotations(fig: go.Figure) -> None:
    """Limpa títulos de subplots/annotations quando existirem."""
    try:
        annotations = list(fig.layout.annotations or [])
    except Exception:
        return
    for annotation in annotations:
        try:
            if annotation.text:
                annotation.text = clean_chart_title_text(annotation.text)
        except Exception:
            pass


def _strip_database_prefix_from_title(fig: go.Figure) -> None:
    """Remove prefixos de base, corrige capitalização e normaliza espaçamento dos títulos."""
    if fig is None:
        return
    try:
        title_text = fig.layout.title.text
    except Exception:
        title_text = None
    if title_text:
        cleaned = clean_chart_title_text(title_text)
        if cleaned != title_text:
            try:
                fig.update_layout(title_text=cleaned)
            except Exception:
                pass
    _clean_figure_annotations(fig)


def style_plotly_figure(fig: go.Figure) -> go.Figure:
    """Padroniza margem, legenda, fonte e cor de óbito/letalidade em todos os gráficos."""
    if fig is None:
        return fig
    _strip_database_prefix_from_title(fig)
    _apply_distinct_trace_colors(fig)
    enforce_death_related_red(fig)
    trace_types = {str(getattr(trace, "type", "")) for trace in (getattr(fig, "data", []) or [])}
    is_line_like = bool(trace_types) and trace_types.issubset({"scatter", "scattergl"})
    fig.update_layout(
        template="plotly_white",
        margin={"l": 36, "r": 28, "t": 104, "b": 72},
        font={"size": 13},
        title={"x": 0.0, "xanchor": "left", "y": 0.98, "yanchor": "top", "pad": {"b": 18}},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.04, "xanchor": "left", "x": 0, "itemsizing": "constant"},
        hoverlabel={"align": "left"},
        hovermode="x unified" if is_line_like else "closest",
    )
    fig.update_xaxes(automargin=True, showgrid=True, zeroline=False)
    fig.update_yaxes(automargin=True, zeroline=False)
    return fig


def render_plotly_chart(fig: go.Figure) -> None:
    """Renderiza Plotly com configuração leve e consistente."""
    st.plotly_chart(style_plotly_figure(fig), width="stretch", config=PLOTLY_CONFIG)


def _session_int(key: str, default: int) -> int:
    """Lê um inteiro de session_state com fallback seguro."""
    try:
        value = int(st.session_state.get(key, default))
    except Exception:
        return default
    return max(0, value)


def perf_int(key: str, default: int) -> int:
    """Atalho para parâmetros de desempenho configuráveis na barra lateral."""
    return _session_int(key, default)


def render_performance_controls() -> None:
    """Expõe limites para evitar carregamento, renderização e exportação excessivos."""
    with st.expander("Desempenho e memória", expanded=False):
        st.number_input(
            "Máximo de Parquets por carregamento",
            min_value=1,
            max_value=60,
            value=perf_int("perf_max_parquet_files", DEFAULT_MAX_PARQUET_FILES_PER_LOAD),
            step=1,
            key="perf_max_parquet_files",
            help="Evita abrir muitos arquivos/anos de uma vez. Aumente gradualmente se o ambiente suportar.",
        )
        st.text_input(
            "Limite de memória do DuckDB",
            value=str(st.session_state.get("perf_duckdb_memory_limit", DEFAULT_DUCKDB_MEMORY_LIMIT)),
            key="perf_duckdb_memory_limit",
            help="Exemplos válidos: 1GB, 2GB, 4096MB ou 75%. O DuckDB fará spill para disco quando possível.",
        )
        st.number_input(
            "Threads do DuckDB",
            min_value=1,
            max_value=8,
            value=perf_int("perf_duckdb_threads", DEFAULT_DUCKDB_THREADS),
            step=1,
            key="perf_duckdb_threads",
            help="Menos threads reduzem picos de memória; mais threads podem acelerar consultas em máquinas com RAM suficiente.",
        )
        st.number_input(
            "Máximo de linhas renderizadas em tabelas",
            min_value=100,
            max_value=20000,
            value=perf_int("perf_display_row_limit", DEFAULT_DISPLAY_ROW_LIMIT),
            step=100,
            key="perf_display_row_limit",
            help="A tabela na tela é truncada para proteger o navegador.",
        )
        st.number_input(
            "Máximo de linhas no botão copiar",
            min_value=50,
            max_value=5000,
            value=perf_int("perf_copy_row_limit", DEFAULT_COPY_ROW_LIMIT),
            step=50,
            key="perf_copy_row_limit",
            help="O botão de cópia injeta HTML/TSV no navegador; mantenha baixo para tabelas grandes.",
        )
        st.number_input(
            "Máximo de linhas em downloads genéricos",
            min_value=1000,
            max_value=500000,
            value=perf_int("perf_download_row_limit", DEFAULT_DOWNLOAD_ROW_LIMIT),
            step=1000,
            key="perf_download_row_limit",
            help="Downloads de tabelas agregadas normalmente ficam muito abaixo deste limite.",
        )
        st.number_input(
            "Máximo de linhas por página na prévia",
            min_value=100,
            max_value=50000,
            value=perf_int("perf_max_preview_rows", DEFAULT_MAX_PREVIEW_ROWS),
            step=100,
            key="perf_max_preview_rows",
            help="A prévia é paginada. Evite enviar muitas linhas ao frontend.",
        )
        st.number_input(
            "Máximo de linhas no SQL Lab",
            min_value=100,
            max_value=100000,
            value=perf_int("perf_sql_lab_row_limit", DEFAULT_SQL_LAB_ROW_LIMIT),
            step=100,
            key="perf_sql_lab_row_limit",
            help="O SQL Lab sempre encapsula SELECT/WITH em LIMIT para evitar resultados gigantes.",
        )
        st.number_input(
            "Máximo de linhas na exportação completa",
            min_value=1000,
            max_value=1000000,
            value=perf_int("perf_full_export_row_limit", DEFAULT_FULL_EXPORT_ROW_LIMIT),
            step=1000,
            key="perf_full_export_row_limit",
            help="A exportação completa só é habilitada quando os filtros reduzem o total para este limite.",
        )
        st.checkbox(
            "Materializar bases Parquet em memória (mais rápido)",
            value=bool(st.session_state.get("perf_materialize_tables", True)),
            key="perf_materialize_tables",
            help=(
                "Lê cada Parquet uma única vez para uma tabela nativa do DuckDB; as consultas "
                "seguintes não reprocessam o Parquet. Desmarque para usar VIEW (lazy) em ambientes "
                "com pouca memória — ainda assim a conexão é reaproveitada entre consultas."
            ),
        )
        st.checkbox(
            "Usar fastparquet na materialização dos Parquets",
            value=bool(st.session_state.get("perf_use_fastparquet", True)),
            key="perf_use_fastparquet",
            help=(
                "Quando a base está materializada, o app tenta ler os arquivos com fastparquet, "
                "registrar o DataFrame no DuckDB e só então executar as consultas SQL. Se a leitura "
                "falhar, se fastparquet não estiver instalado ou se o volume exceder o limite abaixo, "
                "o app usa automaticamente o leitor nativo read_parquet do DuckDB."
            ),
        )
        st.number_input(
            "Limite estimado de linhas para materialização via fastparquet",
            min_value=1000,
            max_value=10000000,
            value=perf_int("perf_fastparquet_row_limit", DEFAULT_FASTPARQUET_ROW_LIMIT),
            step=10000,
            key="perf_fastparquet_row_limit",
            help="Acima deste limite, o app preserva memória e usa DuckDB read_parquet diretamente.",
        )
        st.caption(fastparquet_status())
        if st.button("Limpar cache de consultas", key="clear_query_cache"):
            st.cache_data.clear()
            try:
                st.cache_resource.clear()
            except Exception:
                pass
            st.success("Cache limpo. As próximas consultas serão recalculadas.")


# =============================================================================
# Integração GitHub Release — Parquets empacotados com o painel
# =============================================================================

GITHUB_RELEASE_OWNER = "borbito123"
GITHUB_RELEASE_REPO = "Teste---Dados-Epidemiol-gicos-para-meningite-SINAN-CIHA-SIM---Rio-de-Janeiro"
GITHUB_RELEASE_TAG = "Release1"
GITHUB_HOSTED_PARQUETS_LABEL = "Bancos hospedados no github (Parquets)"
GITHUB_RELEASE_PAGE_URL = (
    f"https://github.com/{GITHUB_RELEASE_OWNER}/{GITHUB_RELEASE_REPO}/releases/tag/{GITHUB_RELEASE_TAG}"
)
GITHUB_RELEASE_EXPANDED_ASSETS_URL = (
    f"https://github.com/{GITHUB_RELEASE_OWNER}/{GITHUB_RELEASE_REPO}/releases/expanded_assets/{GITHUB_RELEASE_TAG}"
)
GITHUB_RELEASE_API_URL = (
    "https://api.github.com/repos/"
    f"{GITHUB_RELEASE_OWNER}/{urllib.parse.quote(GITHUB_RELEASE_REPO, safe='')}/releases/tags/{GITHUB_RELEASE_TAG}"
)
GITHUB_RELEASE_SOURCE_PREFIX = {
    "SINAN": "SINAN_MENINGITE_RIO_ESTADO_",
    "SIM": "SIM_DO_RIO_ESTADO_",
    "CIHA": "CIHA_RIO_ESTADO_",
}
GITHUB_RELEASE_FALLBACK_PARQUETS = (
    [f"CIHA_RIO_ESTADO_{year}.parquet" for year in range(2011, 2026)]
    + [f"SIM_DO_RIO_ESTADO_{year}.parquet" for year in range(2007, 2025)]
    + [f"SINAN_MENINGITE_RIO_ESTADO_{year}.parquet" for year in range(2007, 2026)]
)


CID_RULES = [
    {
        "grupo": "A17.0",
        "prefixo": "A170",
        "rotulo": "A17.0 — meningite tuberculosa",
        "padrao": "A17.0",
    },
    {
        "grupo": "A22.8",
        "prefixo": "A228",
        "rotulo": "A22.8 — meningite por carbúnculo",
        "padrao": "A22.8",
    },
    {
        "grupo": "A32.1",
        "prefixo": "A321",
        "rotulo": "A32.1 — meningite e meningoencefalite por listéria",
        "padrao": "A32.1",
    },
    {
        "grupo": "A39.0",
        "prefixo": "A390",
        "rotulo": "A39.0 — meningite meningocócica",
        "padrao": "A39.0",
    },
    {
        "grupo": "A83",
        "prefixo": "A83",
        "rotulo": "A83 — encefalite por vírus transmitidos por mosquitos",
        "padrao": "A83*",
    },
    {
        "grupo": "A84",
        "prefixo": "A84",
        "rotulo": "A84 — encefalite por vírus transmitido por carrapatos",
        "padrao": "A84*",
    },
    {
        "grupo": "A85",
        "prefixo": "A85",
        "rotulo": "A85 — outras encefalites virais, não classificadas em outra parte",
        "padrao": "A85*",
    },
    {
        "grupo": "A86",
        "prefixo": "A86",
        "rotulo": "A86 — encefalite viral não especificada",
        "padrao": "A86*",
    },
    {
        "grupo": "A87",
        "prefixo": "A87",
        "rotulo": "A87 — meningite viral",
        "padrao": "A87*",
    },
    {
        "grupo": "B00.3",
        "prefixo": "B003",
        "rotulo": "B00.3 — meningite devida ao vírus do herpes",
        "padrao": "B00.3",
    },
    {
        "grupo": "B00.4",
        "prefixo": "B004",
        "rotulo": "B00.4 — encefalite devida ao vírus do herpes",
        "padrao": "B00.4",
    },
    {
        "grupo": "B01.0",
        "prefixo": "B010",
        "rotulo": "B01.0 — meningite por varicela",
        "padrao": "B01.0",
    },
    {
        "grupo": "B01.1",
        "prefixo": "B011",
        "rotulo": "B01.1 — encefalite por varicela",
        "padrao": "B01.1",
    },
    {
        "grupo": "B02.0",
        "prefixo": "B020",
        "rotulo": "B02.0 — encefalite pelo vírus do herpes zoster",
        "padrao": "B02.0",
    },
    {
        "grupo": "B02.1",
        "prefixo": "B021",
        "rotulo": "B02.1 — meningite pelo vírus do herpes zoster",
        "padrao": "B02.1",
    },
    {
        "grupo": "B05.0",
        "prefixo": "B050",
        "rotulo": "B05.0 — sarampo complicado por encefalite",
        "padrao": "B05.0",
    },
    {
        "grupo": "B05.1",
        "prefixo": "B051",
        "rotulo": "B05.1 — sarampo complicado por meningite",
        "padrao": "B05.1",
    },
    {
        "grupo": "B06",
        "prefixo": "B06",
        "rotulo": "B06 — rubéola com complicações neurológicas",
        "padrao": "B06*",
    },
    {
        "grupo": "B26.1",
        "prefixo": "B261",
        "rotulo": "B26.1 — meningite por caxumba / parotidite epidêmica",
        "padrao": "B26.1",
    },
    {
        "grupo": "B26.2",
        "prefixo": "B262",
        "rotulo": "B26.2 — encefalite por caxumba / parotidite epidêmica",
        "padrao": "B26.2",
    },
    {
        "grupo": "B37.5",
        "prefixo": "B375",
        "rotulo": "B37.5 — meningite por Candida",
        "padrao": "B37.5",
    },
    {
        "grupo": "B38.4",
        "prefixo": "B384",
        "rotulo": "B38.4 — meningite por coccidioidomicose",
        "padrao": "B38.4",
    },
    {
        "grupo": "B45.1",
        "prefixo": "B451",
        "rotulo": "B45.1 — criptococose cerebral",
        "padrao": "B45.1",
    },
    {
        "grupo": "B57.4",
        "prefixo": "B574",
        "rotulo": "B57.4 — doença de Chagas crônica com comprometimento do sistema nervoso",
        "padrao": "B57.4",
    },
    {
        "grupo": "B58.2",
        "prefixo": "B582",
        "rotulo": "B58.2 — meningoencefalite por Toxoplasma",
        "padrao": "B58.2",
    },
    {
        "grupo": "B60.2",
        "prefixo": "B602",
        "rotulo": "B60.2 — naegleríase",
        "padrao": "B60.2",
    },
    {
        "grupo": "G00",
        "prefixo": "G00",
        "rotulo": "G00 — meningite bacteriana",
        "padrao": "G00*",
    },
    {
        "grupo": "G01",
        "prefixo": "G01",
        "rotulo": "G01 — meningite bacteriana em doença classificada em outra parte",
        "padrao": "G01*",
    },
    {
        "grupo": "G02",
        "prefixo": "G02",
        "rotulo": "G02 — meningite em outras doenças infecciosas/parasitárias",
        "padrao": "G02*",
    },
    {
        "grupo": "G03",
        "prefixo": "G03",
        "rotulo": "G03 — meningite por outras causas / não especificada",
        "padrao": "G03*",
    },
    {
        "grupo": "G04",
        "prefixo": "G04",
        "rotulo": "G04 — encefalite, mielite e encefalomielite",
        "padrao": "G04*",
    },
    {
        "grupo": "G05",
        "prefixo": "G05",
        "rotulo": "G05 — encefalite, mielite e encefalomielite em doenças classificadas em outra parte",
        "padrao": "G05*",
    },
]

# Aceita CIDs com ponto, sem ponto, precedidos de * e dentro de campos compostos.
# G04 e G05 são tratados como prefixos; os códigos A/B abaixo ampliam o recorte para encefalite/meningoencefalite.
CID_MENINGITE_REGEX = (
    r"(A17[\.]?0|A22[\.]?8|A32[\.]?1|A39[\.]?0|A83[\.]?[0-9A-Z]?|"
    r"A84[\.]?[0-9A-Z]?|A85[\.]?[0-9A-Z]?|A86[\.]?[0-9A-Z]?|A87[\.]?[0-9A-Z]?|"
    r"B00[\.]?[34]|B01[\.]?[01]|B02[\.]?[01]|B05[\.]?[01]|B06[\.]?[0-9A-Z]?|"
    r"B26[\.]?[12]|B37[\.]?5|B38[\.]?4|B45[\.]?1|B57[\.]?4|B58[\.]?2|B60[\.]?2|"
    r"G00[\.]?[0-9A-Z]?|G01[\.]?[0-9A-Z]?|G02[\.]?[0-9A-Z]?|G03[\.]?[0-9A-Z]?|"
    r"G04[\.]?[0-9A-Z]?|G05[\.]?[0-9A-Z]?)"
)

CID_G01_PRESENT_REGEX = r"\*?G01[\.]?[0-9A-Z]?\*?"
CID_G02_PRESENT_REGEX = r"\*?G02[\.]?[0-9A-Z]?\*?"

CID10_ADEQUACY_TARGET_LABELS = {
    "G01": "G01 — meningite bacteriana em doença classificada em outra parte",
    "G02": "G02 — meningite em outras doenças infecciosas/parasitárias",
    "G05": "G05 — encefalite, mielite e encefalomielite em doenças classificadas em outra parte",
}

CID10_ADEQUACY_CONVERSION_RULES = [
    {
        "origem_grupo": "A22.8", "origem_prefixo": "A228", "origem_padrao": "A22.8", "match": "exact",
        "origem_rotulo": "A22.8 — meningite por carbúnculo",
        "destino_grupo": "G01", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G01"],
        "observacao": "A22.8 — meningite por carbúnculo convertida para G01.",
    },
    {
        "origem_grupo": "A32.1", "origem_prefixo": "A321", "origem_padrao": "A32.1", "match": "exact",
        "origem_rotulo": "A32.1 — meningite e meningoencefalite por listéria",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "A32.1 — meningite/meningoencefalite por listéria convertida para G05.",
    },
    {
        "origem_grupo": "A83", "origem_prefixo": "A83", "origem_padrao": "A83*", "match": "prefix",
        "origem_rotulo": "A83 — encefalite por vírus transmitidos por mosquitos",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "A83* — encefalite por vírus transmitidos por mosquitos convertida para G05.",
    },
    {
        "origem_grupo": "A84", "origem_prefixo": "A84", "origem_padrao": "A84*", "match": "prefix",
        "origem_rotulo": "A84 — encefalite por vírus transmitido por carrapatos",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "A84* — encefalite por vírus transmitido por carrapatos convertida para G05.",
    },
    {
        "origem_grupo": "A85", "origem_prefixo": "A85", "origem_padrao": "A85*", "match": "prefix",
        "origem_rotulo": "A85 — outras encefalites virais, não classificadas em outra parte",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "A85* — outras encefalites virais convertidas para G05.",
    },
    {
        "origem_grupo": "A86", "origem_prefixo": "A86", "origem_padrao": "A86*", "match": "prefix",
        "origem_rotulo": "A86 — encefalite viral não especificada",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "A86* — encefalite viral não especificada convertida para G05.",
    },
    {
        "origem_grupo": "B00.3", "origem_prefixo": "B003", "origem_padrao": "B00.3", "match": "exact",
        "origem_rotulo": "B00.3 — meningite devida ao vírus do herpes",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B00.3 — meningite devida ao vírus do herpes convertida para G02.",
    },
    {
        "origem_grupo": "B00.4", "origem_prefixo": "B004", "origem_padrao": "B00.4", "match": "exact",
        "origem_rotulo": "B00.4 — encefalite devida ao vírus do herpes",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B00.4 — encefalite devida ao vírus do herpes convertida para G02.",
    },
    {
        "origem_grupo": "B01.0", "origem_prefixo": "B010", "origem_padrao": "B01.0", "match": "exact",
        "origem_rotulo": "B01.0 — meningite por varicela",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B01.0 — meningite por varicela convertida para G02.",
    },
    {
        "origem_grupo": "B01.1", "origem_prefixo": "B011", "origem_padrao": "B01.1", "match": "exact",
        "origem_rotulo": "B01.1 — encefalite por varicela",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B01.1 — encefalite por varicela convertida para G05.",
    },
    {
        "origem_grupo": "B02.0", "origem_prefixo": "B020", "origem_padrao": "B02.0", "match": "exact",
        "origem_rotulo": "B02.0 — encefalite pelo vírus do herpes zoster",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B02.0 — encefalite pelo vírus do herpes zoster convertida para G05.",
    },
    {
        "origem_grupo": "B02.1", "origem_prefixo": "B021", "origem_padrao": "B02.1", "match": "exact",
        "origem_rotulo": "B02.1 — meningite pelo vírus do herpes zoster",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B02.1 — meningite pelo vírus do herpes zoster convertida para G05.",
    },
    {
        "origem_grupo": "B05.0", "origem_prefixo": "B050", "origem_padrao": "B05.0", "match": "exact",
        "origem_rotulo": "B05.0 — sarampo complicado por encefalite",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B05.0 — sarampo complicado por encefalite convertido para G05.",
    },
    {
        "origem_grupo": "B05.1", "origem_prefixo": "B051", "origem_padrao": "B05.1", "match": "exact",
        "origem_rotulo": "B05.1 — sarampo complicado por meningite",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B05.1 — sarampo complicado por meningite convertido para G02.",
    },
    {
        "origem_grupo": "B06", "origem_prefixo": "B06", "origem_padrao": "B06*", "match": "prefix",
        "origem_rotulo": "B06 — rubéola com complicações neurológicas",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B06* — rubéola com complicações neurológicas convertida para G05.",
    },
    {
        "origem_grupo": "B26.1", "origem_prefixo": "B261", "origem_padrao": "B26.1", "match": "exact",
        "origem_rotulo": "B26.1 — meningite por caxumba / parotidite epidêmica",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B26.1 — meningite por caxumba convertida para G02.",
    },
    {
        "origem_grupo": "B26.2", "origem_prefixo": "B262", "origem_padrao": "B26.2", "match": "exact",
        "origem_rotulo": "B26.2 — encefalite por caxumba / parotidite epidêmica",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B26.2 — encefalite por caxumba convertida para G05.",
    },
    {
        "origem_grupo": "B37.5", "origem_prefixo": "B375", "origem_padrao": "B37.5", "match": "exact",
        "origem_rotulo": "B37.5 — meningite por Candida",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B37.5 — meningite por Candida convertida para G02.",
    },
    {
        "origem_grupo": "B38.4", "origem_prefixo": "B384", "origem_padrao": "B38.4", "match": "exact",
        "origem_rotulo": "B38.4 — meningite por coccidioidomicose",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B38.4 — meningite por coccidioidomicose convertida para G02.",
    },
    {
        "origem_grupo": "B45.1", "origem_prefixo": "B451", "origem_padrao": "B45.1", "match": "exact",
        "origem_rotulo": "B45.1 — criptococose cerebral",
        "destino_grupo": "G02", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G02"],
        "observacao": "B45.1 — criptococose cerebral convertida para G02.",
    },
    {
        "origem_grupo": "B58.2", "origem_prefixo": "B582", "origem_padrao": "B58.2", "match": "exact",
        "origem_rotulo": "B58.2 — meningoencefalite por Toxoplasma",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B58.2 — meningoencefalite por Toxoplasma convertida para G05.",
    },
    {
        "origem_grupo": "B57.4", "origem_prefixo": "B574", "origem_padrao": "B57.4", "match": "exact",
        "origem_rotulo": "B57.4 — doença de Chagas crônica com comprometimento do sistema nervoso",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B57.4 — doença de Chagas crônica com comprometimento do sistema nervoso convertida para G05.",
    },
    {
        "origem_grupo": "B60.2", "origem_prefixo": "B602", "origem_padrao": "B60.2", "match": "exact",
        "origem_rotulo": "B60.2 — naegleríase",
        "destino_grupo": "G05", "destino_rotulo": CID10_ADEQUACY_TARGET_LABELS["G05"],
        "observacao": "B60.2 — naegleríase convertida para G05.",
    },
]

CID10_ADEQUACY_MAPPING_ROWS = [
    {
        "CID-10 original": rule["origem_padrao"],
        "Descrição original": rule["origem_rotulo"],
        "CID-10 convertido": rule["destino_grupo"],
        "Categoria convertida": rule["destino_rotulo"],
        "Observação": rule["observacao"],
    }
    for rule in CID10_ADEQUACY_CONVERSION_RULES
]

CID10_ADEQUACY_OBSERVATION = (
    "Observação: A22.8 é convertido para G01; A32.1, A83*, A84*, A85*, A86*, "
    "B01.1, B02.0, B02.1, B05.0, B06*, B26.2, B57.4, B58.2 e B60.2 são convertidos para G05; "
    "B00.3, B00.4, B01.0, B05.1, B26.1, B37.5, B38.4 e B45.1 são convertidos para G02. "
    "Os demais CID-10 detectados ficam fora da conversão e permanecem no denominador para preservar o total de casos do recorte/ano."
)


SINAN_CON_DIAGES = {
    "01": "01 — meningococcemia",
    "02": "02 — meningite meningocócica",
    "03": "03 — meningite meningocócica com meningococcemia",
    "04": "04 — meningite tuberculosa",
    "05": "05 — meningite por outras bactérias",
    "06": "06 — meningite não especificada",
    "07": "07 — meningite asséptica",
    "08": "08 — meningite por outra etiologia",
    "09": "09 — meningite por Haemophilus influenzae",
    "10": "10 — meningite por Streptococcus pneumoniae / pneumocócica",
}

SINAN_CON_GROUP = {
    "01": "Meningocócica / meningococcemia",
    "02": "Meningocócica / meningococcemia",
    "03": "Meningocócica / meningococcemia",
    "04": "Tuberculosa",
    "05": "Outras bacterianas",
    "06": "Não especificada",
    "07": "Asséptica / viral provável",
    "08": "Outra etiologia",
    "09": "Haemophilus influenzae",
    "10": "Pneumocócica",
}

# Conversão operacional CON_DIAGES (SINAN) -> CID-10 para comparação com SIM/CIHA.
# A categoria 01 (meningococcemia isolada) é propositalmente não convertida,
# pois não representa meningite quando aparece sem a forma meningítica.
SINAN_CID10_FROM_CON_DIAGES = {
    "02": {
        "grupo": "A39.0",
        "rotulo": "A39.0 — meningite meningocócica",
        "origem": "02 — meningite meningocócica",
    },
    "03": {
        "grupo": "A39.0",
        "rotulo": "A39.0 — meningite meningocócica",
        "origem": "03 — meningite meningocócica com meningococcemia",
    },
    "04": {
        "grupo": "A17.0",
        "rotulo": "A17.0 — meningite tuberculosa",
        "origem": "04 — meningite tuberculosa",
    },
    "05": {
        "grupo": "G00",
        "rotulo": "G00 — meningite bacteriana não classificada em outra parte",
        "origem": "05 — meningite por outras bactérias",
    },
    "06": {
        "grupo": "G03",
        "rotulo": "G03 — meningite por outras causas / não especificada",
        "origem": "06 — meningite não especificada",
    },
    "07": {
        "grupo": "A87",
        "rotulo": "A87 — meningite viral",
        "origem": "07 — meningite asséptica (operacionalmente viral no SINAN)",
    },
    "08": {
        "grupo": "G02",
        "rotulo": "G02 — meningite em outras doenças infecciosas/parasitárias",
        "origem": "08 — meningite por outra etiologia",
    },
    "09": {
        "grupo": "G00",
        "rotulo": "G00 — meningite bacteriana não classificada em outra parte",
        "origem": "09 — meningite por Haemophilus influenzae",
    },
    "10": {
        "grupo": "G00",
        "rotulo": "G00 — meningite bacteriana não classificada em outra parte",
        "origem": "10 — meningite pneumocócica",
    },
}

SINAN_CID10_NOT_CONVERTED = {
    "01": "Não convertido — meningococcemia isolada",
}

SINAN_CID10_MAPPING_ROWS = [
    {
        "CON_DIAGES": "04",
        "Grupo SINAN": "Meningite tuberculosa",
        "CID-10 convertido": "A17.0",
        "Observação": "Mantida como A17.0/A170 para comparação com CID bruto.",
    },
    {
        "CON_DIAGES": "02, 03",
        "Grupo SINAN": "Meningite meningocócica; meningite meningocócica com meningococcemia",
        "CID-10 convertido": "A39.0",
        "Observação": "Meningococcemia isolada (01) não entra nesta conversão.",
    },
    {
        "CON_DIAGES": "07",
        "Grupo SINAN": "Meningite asséptica",
        "CID-10 convertido": "A87",
        "Observação": "No SINAN, a categoria asséptica é tratada operacionalmente como viral; usar G03 somente para asséptica sem evidência/definição viral em CID bruto externo.",
    },
    {
        "CON_DIAGES": "05",
        "Grupo SINAN": "Meningite por outras bactérias",
        "CID-10 convertido": "G00 ou G01",
        "Observação": "Regra corrigida: não converter automaticamente para G04.2. Usa G01 quando o agente/doença é classificado em outra parte; caso contrário, usa G00, incluindo bactéria não especificada.",
    },
    {
        "CON_DIAGES": "09, 10",
        "Grupo SINAN": "Haemophilus influenzae; pneumocócica",
        "CID-10 convertido": "G00",
        "Observação": "Mantém Haemophilus influenzae e pneumocócica agregadas em G00.0/G00.1 para comparação por família CID.",
    },
    {
        "CON_DIAGES": "08",
        "Grupo SINAN": "Meningite por outra etiologia",
        "CID-10 convertido": "G02",
        "Observação": "Correção lógica: no dicionário SINAN, esta categoria cobre principalmente fungos/protozoários/parasitas; portanto é mais compatível com G02 do que com G03.",
    },
    {
        "CON_DIAGES": "06",
        "Grupo SINAN": "Meningite não especificada",
        "CID-10 convertido": "G03",
        "Observação": "Usada para causa não especificada/outras causas não melhor classificadas.",
    },
    {
        "CON_DIAGES": "01",
        "Grupo SINAN": "Meningococcemia",
        "CID-10 convertido": "Não convertido",
        "Observação": "Excluído para evitar incluir pacientes sem meningite na comparação.",
    },
]


# Especificações auxiliares do SINAN para refinar CON_DIAGES.
# CLA_ME_BAC vem do Quadro II do dicionário SINAN NET Meningite.
SINAN_CLA_ME_BAC = {
    "09": "09 — Shigella sp",
    "10": "10 — Staphylococcus (aureus, sp, epidermidis)",
    "11": "11 — Salmonella sp",
    "12": "12 — Escherichia coli",
    "13": "13 — Klebsiella (sp, pneumoniae)",
    "14": "14 — Streptococcus (sp, pyogenes, agalactiae)",
    "15": "15 — Enterococcus",
    "16": "16 — Pseudomonas (aeruginosa, sp)",
    "18": "18 — Serratia (marcescens, sp)",
    "19": "19 — Alcaligenes (sp, faecalis)",
    "20": "20 — Proteus (sp, vulgaris, mirabilis)",
    "21": "21 — Listeria monocytogenes",
    "22": "22 — Enterobacter (sp, cloacae)",
    "23": "23 — Acinetobacter (sp, baumannii)",
    "26": "26 — Neisseria sp",
    "28": "28 — outras bactérias",
    "45": "45 — Treponema pallidum",
    "46": "46 — Rickettsiae",
    "49": "49 — Leptospira",
    "81": "81 — bactéria não especificada",
}

# Códigos de CLA_ME_BAC que, para a finalidade de comparação por família CID-10,
# são mais compatíveis com G01 por remeterem a doenças bacterianas classificadas em outra parte.
SINAN_CLA_ME_BAC_G01_CODES = {"11", "21", "45", "49"}

SINAN_CLA_ME_ASS = {
    "37": "37 — caxumba",
    "38": "38 — sarampo",
    "39": "39 — herpes simples",
    "40": "40 — varicela/catapora/herpes zoster",
    "41": "41 — rubéola",
    "55": "55 — influenza",
    "56": "56 — echovirus",
    "59": "59 — outros enterovírus",
    "63": "63 — coxsackie",
    "70": "70 — adenovírus",
    "71": "71 — vírus do Nilo Ocidental",
    "72": "72 — dengue",
    "73": "73 — outros arbovírus",
    "74": "74 — outros vírus",
    "75": "75 — não identificado",
}

SINAN_CLA_ME_ETI = {
    "42": "42 — outros fungos",
    "43": "43 — Cryptococcus/Torula",
    "44": "44 — Candida albicans/sp",
    "47": "47 — Trypanosoma cruzi",
    "48": "48 — Toxoplasma gondii/sp",
    "50": "50 — cisticerco",
    "52": "52 — outros parasitas",
    "64": "64 — Aspergillus",
    "76": "76 — Plasmodium sp",
    "77": "77 — Taenia solium",
}

SINAN_OTHER_BACTERIA_CID10_RULE_ROWS = [
    {
        "Cenário": "CON_DIAGES 05 + CLA_ME_BAC 11, 21, 45 ou 49; ou texto compatível com salmonela, listeriose, neurossífilis/sífilis ou leptospirose",
        "CID-10 convertido": "G01",
        "Justificativa": "Meningite em doença bacteriana classificada em outra parte.",
    },
    {
        "Cenário": "CON_DIAGES 05 + texto compatível com carbúnculo/antraz, Lyme/Borrelia, febre tifóide ou gonocócica",
        "CID-10 convertido": "G01",
        "Justificativa": "A doença bacteriana de base tem código próprio; a meningite entra como manifestação associada.",
    },
    {
        "Cenário": "CON_DIAGES 05 + Streptococcus, Staphylococcus, Escherichia coli, Klebsiella/Friedländer ou demais bactérias do Quadro II não remetidas a G01",
        "CID-10 convertido": "G00",
        "Justificativa": "Meningite bacteriana não classificada em outra parte; usar subcategoria específica quando disponível.",
    },
    {
        "Cenário": "CON_DIAGES 05 sem bactéria especificada ou CLA_ME_BAC 81",
        "CID-10 convertido": "G00",
        "Justificativa": "Equivale operacionalmente a meningite bacteriana não especificada/pyogênica/purulenta/supurativa SOE.",
    },
]


SINAN_G01_BASE_DISEASE_REFERENCE_ROWS = [
    {
        "Critério no SINAN": "CLA_ME_BAC 11 ou texto com Salmonella/salmonela",
        "Doença de base provável": "Infecção por Salmonella sp / salmonelose invasiva",
        "CID-10 da doença de base": "A02.2†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "CLA_ME_BAC 21 ou texto com Listeria/listeriose",
        "Doença de base provável": "Listeriose / Listeria monocytogenes",
        "CID-10 da doença de base": "A32.1†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "CLA_ME_BAC 45 ou texto com Treponema, sífilis ou neurossífilis",
        "Doença de base provável": "Sífilis / neurossífilis",
        "CID-10 da doença de base": "A52.1†; avaliar A50.4†/A51.4† conforme contexto",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "CLA_ME_BAC 49 ou texto com Leptospira/leptospirose",
        "Doença de base provável": "Leptospirose",
        "CID-10 da doença de base": "A27.-†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "texto com carbúnculo/antraz",
        "Doença de base provável": "Carbúnculo / antraz",
        "CID-10 da doença de base": "A22.8†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "texto com Lyme/Borrelia",
        "Doença de base provável": "Doença de Lyme / borreliose",
        "CID-10 da doença de base": "A69.2†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "texto com febre tifóide/typhoid",
        "Doença de base provável": "Febre tifóide",
        "CID-10 da doença de base": "A01.0†",
        "CID-10 da manifestação": "G01*",
    },
    {
        "Critério no SINAN": "texto com gonococo/gonocócica",
        "Doença de base provável": "Infecção gonocócica",
        "CID-10 da doença de base": "A54.8†",
        "CID-10 da manifestação": "G01*",
    },
]

# Regex operacional para pistas textuais. Usado somente em campos auxiliares detectados automaticamente.
SINAN_G01_DETAIL_REGEX = (
    r"CARB[UÚ]NCULO|ANTRAZ|ANTHRAX|LYME|BORREL|TIF[OÓ]IDE|TYPHOID|"
    r"GONOCOC|GONOCOCO|SALMONEL|LEPTOSPI|LISTERI|NEUROSS[ÍI]FIL|NEUROSYPH|"
    r"S[ÍI]FIL|SYPHIL|TREPONEMA"
)

# Campos que o app tenta selecionar automaticamente como auxiliares para refino etiológico/textual.
SINAN_AUXILIARY_CID10_CANDIDATES = [
    "CLA_ME_BAC", "CLA_ME_ASS", "CLA_ME_ETI", "DS_OBSERVACAO", "OBSERVACAO", "OBSERVACOES",
    "OUTROS_SINTOMAS", "OUTRO_SINTOMA", "OUTR_SINT", "SIN_OUT", "SINTOMAS", "SINAIS",
    "DIAGNOSTICO", "DIAG_FINAL", "CLASSIFICACAO", "EVOLUCAO", "CID",
]

SINAN_QUIMIO_INTERPRETATION_ROWS = [
    {
        "Parâmetro": "Leucócitos (céls/mm³)",
        "LCR normal": "0–5",
        "Viral": "50–1.000 (a)",
        "Bacteriana": "1.000–5.000 (b)",
        "Tuberculosa": "50–300",
        "Fúngica/criptocócica": "20–500 (f)",
    },
    {
        "Parâmetro": "Tipo celular predominante",
        "LCR normal": "Mononuclear/linfocitário",
        "Viral": "Mononuclear/linfocitário; pode iniciar neutrofílico (a)",
        "Bacteriana": "Neutrofílico (c, g)",
        "Tuberculosa": "Mononuclear (e)",
        "Fúngica/criptocócica": "Mononuclear",
    },
    {
        "Parâmetro": "Glicose",
        "LCR normal": ">50% da glicemia sérica",
        "Viral": "Geralmente normal; em geral >45 mg/dL",
        "Bacteriana": "Reduzida; <40 mg/dL ou razão LCR/soro ≤0,4 (d)",
        "Tuberculosa": "Reduzida; em geral <45 mg/dL",
        "Fúngica/criptocócica": "Reduzida; em geral <40 mg/dL",
    },
    {
        "Parâmetro": "Proteínas (mg/dL)",
        "LCR normal": "<40–45",
        "Viral": "<200",
        "Bacteriana": "100–500",
        "Tuberculosa": "50–300",
        "Fúngica/criptocócica": ">45",
    },
    {
        "Parâmetro": "Aspecto do líquor",
        "LCR normal": "Límpido",
        "Viral": "Límpido",
        "Bacteriana": "Turvo ou purulento",
        "Tuberculosa": "Límpido ou turvo",
        "Fúngica/criptocócica": "Frequentemente límpido",
    },
]

SINAN_QUIMIO_NOTE_ROWS = [
    {
        "Índice": "(a)",
        "Texto": "Na meningite viral, o LCR pode ser neutrofílico no início da apresentação, especialmente nas primeiras 24–48h. Por isso, viral recente pode se parecer com bacteriana quando se olha apenas o diferencial celular.",
    },
    {
        "Índice": "(b)",
        "Texto": "Na meningite bacteriana, 1.000–5.000 céls/mm³ é a faixa típica. O Mandell descreve extremos de <100 a >10.000 céls/mm³; portanto, o limite superior de 5.000 não deve ser tratado como teto diagnóstico.",
    },
    {
        "Índice": "(c)",
        "Texto": "Cerca de 10% dos pacientes com meningite bacteriana podem apresentar predomínio linfocitário no LCR. Predomínio linfocitário, isoladamente, não exclui bactéria.",
    },
    {
        "Índice": "(d)",
        "Texto": "A glicose do LCR deve ser comparada com glicemia sérica colhida no mesmo momento da punção lombar. Na meningite bacteriana, a razão LCR/soro ≤0,4 ocorre na maioria dos casos e costuma ser mais informativa que a glicose absoluta isolada.",
    },
    {
        "Índice": "(e)",
        "Texto": "Na meningite tuberculosa, pode ocorrer paradoxo terapêutico: um LCR inicialmente mononuclear pode se tornar neutrofílico durante o tratamento antituberculoso, sem que isso indique necessariamente troca de etiologia.",
    },
    {
        "Índice": "(f)",
        "Texto": "Na meningite criptocócica associada à AIDS, mais de 75% dos pacientes podem ter <20 céls/mm³. Assim, aplicar rigidamente a faixa 20–500 em imunossuprimidos pode subestimar casos reais.",
    },
    {
        "Índice": "(g)",
        "Texto": "Listeriose fica operacionalmente dentro de 'outras bactérias' no SINAN e pode cursar com predomínio mononuclear/linfocitário em 25–30% dos casos; em pacientes previamente tratados, essa proporção pode ser ainda maior. No gráfico de predomínio compatível vs. discordante, parte da discordância pode refletir comportamento conhecido da doença, não erro de classificação.",
    },
]

SINAN_LCR_AGE_DIFFERENCES_ROWS = [
    {
        "Parâmetro": "Leucócitos",
        "Neonatos até seis meses de idade": "Estrato operacional ampliado no painel; valores de referência variam rapidamente nos primeiros meses e devem ser interpretados com idade exata/contexto clínico.",
        "Crianças >6 meses/adultos": "0–5 céls/mm³",
    },
    {
        "Parâmetro": "Proteínas",
        "Neonatos até seis meses de idade": "Estrato operacional ampliado no painel; proteínas do LCR tendem a ser mais altas no início da vida e devem ser interpretadas com idade exata/contexto clínico.",
        "Crianças >6 meses/adultos": "15–45 mg/dL",
    },
    {
        "Parâmetro": "Glicose",
        "Neonatos até seis meses de idade": "Estrato operacional ampliado no painel; interpretar preferencialmente com glicemia sérica pareada e idade exata.",
        "Crianças >6 meses/adultos": "Geralmente ~2/3 da glicemia; em geral 45–80 mg/dL",
    },
]

SINAN_QUIMIO_REFERENCES = [
    {
        "Referência": "Bennett JE, Dolin R, Blaser MJ, eds. Mandell, Douglas, and Bennett's Principles and Practice of Infectious Diseases. Elsevier. Tabela 88.2.",
        "Uso no painel": "Faixas típicas de leucócitos, glicose, proteína e predomínio celular por etiologia, incluindo as notas de exceção (a–g).",
    },
    {
        "Referência": "MSD/Merck Manual Professional. Cerebrospinal Fluid Findings in Meningitis. https://www.msdmanuals.com/professional/multimedia/table/cerebrospinal-fluid-findings-in-meningitis",
        "Uso no painel": "Resumo prático de tipo celular predominante, proteína, glicose e comparação da glicose do LCR com a glicemia.",
    },
    {
        "Referência": "Tunkel AR et al. Practice Guidelines for the Management of Bacterial Meningitis. Clinical Infectious Diseases. 2004;39:1267-1284.",
        "Uso no painel": "Limitações da interpretação isolada dos marcadores quimiocitológicos do LCR.",
    },
    {
        "Referência": "WHO. Guidelines on meningitis diagnosis, treatment and care. 2025.",
        "Uso no painel": "Reforço do papel inicial de celularidade, diferencial, glicose, proteína, hemácias e Gram na investigação do LCR.",
    },
]

SINAN_CLASSI_FIN = {
    "1": "1 — confirmado",
    "2": "2 — descartado",
}

SINAN_EVOLUCAO = {
    "1": "1 — alta",
    "2": "2 — óbito por meningite",
    "3": "3 — óbito por outra causa",
    "9": "9 — ignorado",
}

SINAN_CRITERIO = {
    "1": "1 — cultura",
    "2": "2 — CIE",
    "3": "3 — látex",
    "4": "4 — clínico",
    "5": "5 — bacterioscopia",
    "6": "6 — quimiocitológico",
    "7": "7 — clínico-epidemiológico",
    "8": "8 — isolamento viral",
    "9": "9 — PCR",
    "10": "10 — outro",
}

YES_NO_IGN = {
    "1": "Sim",
    "2": "Não",
    "9": "Ignorado",
}


SINAN_SYMPTOM_FIELDS = [
    ("CLI_CEFALE", "Cefaleia"),
    ("CLI_FEBRE", "Febre"),
    ("CLI_VOMITO", "Vômitos"),
    ("CLI_CONVUL", "Convulsões"),
    ("CLI_RIGIDE", "Rigidez de nuca"),
    ("CLI_KERNIG", "Kernig/Brudzinski"),
    ("CLI_ABAULA", "Abaulamento de fontanela"),
    ("CLI_PETEQU", "Petequias/sufusões hemorrágicas"),
    ("CLI_COMA", "Coma"),
    ("CLI_OUTRAS", "Outras manifestações"),
]

SINAN_VACCINE_FIELDS = [
    ("ANT_AC", "Polissacarídica A/C"),
    ("ANT_BC", "Polissacarídica B/C"),
    ("ANT_CONJ_C", "Conjugada meningo C"),
    ("ANT_BCG", "BCG"),
    ("ANT_TRIPLI", "Tríplice viral"),
    ("ANT_HEMO_T", "Hemófilo/Tetravalente ou Hib"),
    ("ANT_PNEUMO", "Pneumococo"),
    ("ANT_OUTRA", "Outra vacina"),
]

# Campos do exame quimiocitológico do líquor no SINAN.
# Os nomes abaixo seguem o dicionário SINAN NET para meningite; os seletores do app
# também aceitam variações próximas caso o banco venha renomeado.
SINAN_QUIMIO_MATERIAL = "Líquor (LCR)"
SINAN_QUIMIO_PARAMS = {
    "hema": {"label": "Hemácias", "default_col": "LAB_HEMA"},
    "neutro": {"label": "Neutrófilos", "default_col": "LAB_NEUTRO"},
    "glico": {"label": "Glicose", "default_col": "LAB_GLICO"},
    "leuco": {"label": "Leucócitos", "default_col": "LAB_LEUCO"},
    "eosi": {"label": "Eosinófilos", "default_col": "LAB_EOSI"},
    "prot": {"label": "Proteínas", "default_col": "LAB_PROT"},
    "mono": {"label": "Monócitos", "default_col": "LAB_MONO"},
    "linfo": {"label": "Linfócitos", "default_col": "LAB_LINFO"},
    "clor": {"label": "Cloreto", "default_col": "LAB_CLOR"},
}

# Aspecto do líquor (campo 48 da ficha de investigação — LAB_ASPECT / tp_aspector_liquor).
# Categorias oficiais conforme dicionário de dados SINAN NET Meningite v5.0.
SINAN_LAB_ASPECT = {
    "1": "Límpido",
    "2": "Purulento",
    "3": "Hemorrágico",
    "4": "Turvo",
    "5": "Xantocrômico",
    "6": "Outro",
    "9": "Ignorado",
}

SINAN_LAB_ASPECT_ORDER = [
    "Límpido",
    "Purulento",
    "Hemorrágico",
    "Turvo",
    "Xantocrômico",
    "Outro",
    "Ignorado",
    "Sem informação/ignorado",
]

# Leitura operacional do aspecto do líquor por etiologia. O objetivo é comparar
# o aspecto observado no campo 48 da ficha do SINAN com o padrão esperado na
# literatura, sem transformar essa comparação em critério diagnóstico isolado.
SINAN_LCR_EXPECTED_ASPECT = {
    "Viral": {"descricao": "Límpido", "compatible_codes": {"1"}},
    "Bacteriana": {"descricao": "Turvo ou purulento", "compatible_codes": {"2", "4"}},
    "Tuberculosa": {"descricao": "Límpido ou turvo", "compatible_codes": {"1", "4"}},
    "Fúngica": {"descricao": "Frequentemente límpido", "compatible_codes": {"1"}},
}

# =============================================================================
# Classificação etiológica do LCR por faixas de referência (independente do
# CLASSI_FIN/CON_DIAGES oficial do SINAN) — análise exploratória de quimiocitologia.
# =============================================================================
#
# IMPORTANTE — limitações reconhecidas na literatura-fonte:
# - As faixas abaixo são típicas, não limites absolutos. O próprio Mandell descreve
#   meningite bacteriana com contagem de leucócitos variando de <100 a >10.000 céls/mm³,
#   embora 1.000–5.000 seja a apresentação clássica usada como faixa operacional.
# - Meningite viral pode ser neutrofílica nas primeiras 24–48h; meningite bacteriana
#   pode ter predomínio linfocitário em uma minoria dos casos, incluindo apresentações
#   atípicas, fases iniciais e casos parcialmente tratados.
# - O SINAN registra apenas a glicose do LCR em mg/dL (LAB_GLICO), sem campo de
#   glicemia sérica pareada. Clinicamente, a glicose deve ser comparada com o soro;
#   na ausência desse dado, o painel usa glicose absoluta <40 mg/dL como proxy
#   operacional para redução, sempre apresentada como limitação do banco.
# - LAB_NEUTRO e LAB_LINFO no SINAN são percentuais do total de leucócitos (campo
#   numeric(3), 0–100), não contagens absolutas. "Predomínio" é definido aqui como
#   o maior percentual entre LAB_NEUTRO e LAB_LINFO para o mesmo registro.
SINAN_ETIOLOGY_GROUPS = ["Viral", "Bacteriana", "Tuberculosa", "Fúngica"]
SINAN_ETIOLOGY_COLOR_MAP = {
    "Viral": "#1F77B4",
    "Bacteriana": "#FF7F0E",
    "Tuberculosa": "#2CA02C",
    "Fúngica": "#9467BD",
}
SINAN_LCR_RANGE_POSITION_ORDER = [
    "Abaixo da faixa típica",
    "Dentro da faixa típica",
    "Acima da faixa típica",
]
SINAN_LCR_RANGE_POSITION_COLOR_MAP = {
    "Abaixo da faixa típica": "#1F77B4",
    "Dentro da faixa típica": "#2CA02C",
    "Acima da faixa típica": "#FF7F0E",
}
SINAN_LCR_PREDOMINIO_STATUS_ORDER = [
    "Compatível com o esperado",
    "Discordante do esperado",
    "Empate/indefinido",
]
SINAN_LCR_PREDOMINIO_STATUS_COLOR_MAP = {
    "Compatível com o esperado": "#2CA02C",
    "Discordante do esperado": "#D62728",
    "Empate/indefinido": "#7F7F7F",
}
SINAN_LCR_GLUCOSE_POSITION_ORDER = [
    "Preservada (esperado)",
    "Reduzida (esperado)",
    "Reduzida (atípico p/ viral)",
    "Preservada (atípico)",
]
SINAN_LCR_GLUCOSE_POSITION_COLOR_MAP = {
    "Preservada (esperado)": "#2CA02C",
    "Reduzida (esperado)": "#1F77B4",
    "Reduzida (atípico p/ viral)": "#D62728",
    "Preservada (atípico)": "#FF7F0E",
}
SINAN_LCR_ASPECT_STATUS_ORDER = [
    "Compatível com o esperado",
    "Discordante do esperado",
    "Outro aspecto/atípico",
    "Ignorado/sem informação",
]
SINAN_LCR_ASPECT_STATUS_COLOR_MAP = {
    "Compatível com o esperado": "#2CA02C",
    "Discordante do esperado": "#D62728",
    "Outro aspecto/atípico": "#FF7F0E",
    "Ignorado/sem informação": "#7F7F7F",
}
SINAN_LCR_INDEPENDENT_CLASS_ORDER = SINAN_ETIOLOGY_GROUPS + [
    "Indeterminado/empate",
    "Indeterminado/baixo suporte",
    "Sem dados suficientes",
]
SINAN_LCR_INDEPENDENT_CLASS_COLOR_MAP = {
    **SINAN_ETIOLOGY_COLOR_MAP,
    "Indeterminado/empate": "#7F7F7F",
    "Indeterminado/baixo suporte": "#BCBD22",
    "Sem dados suficientes": "#BDBDBD",
}
SINAN_LCR_VS_SINAN_STATUS_ORDER = [
    "Concordante com SINAN",
    "Discordante do SINAN",
    "Indeterminado pelo LCR",
    "Sem grupo SINAN comparável",
]
SINAN_LCR_VS_SINAN_STATUS_COLOR_MAP = {
    "Concordante com SINAN": "#2CA02C",
    "Discordante do SINAN": "#D62728",
    "Indeterminado pelo LCR": "#7F7F7F",
    "Sem grupo SINAN comparável": "#BDBDBD",
}
SINAN_LCR_SMALL_DENOMINATOR_WARNING_N = 20

# Estratos etários operacionais para gráficos de distribuição do LCR.
# Conforme ajuste solicitado, a estratificação etária volta a ser binária:
# neonatos até seis meses de idade versus crianças/adultos (>6 meses).
SINAN_LCR_NEONATAL_CUTOFF_YEARS = 6 / 12
SINAN_LCR_AGE_STRATIFICATION_LABEL = "Neonatos até seis meses x crianças/adultos"
SINAN_LCR_AGE_STRATIFICATION_WITH_INTERVAL_LABEL = "Neonatos até seis meses x crianças/adultos + tempo sintoma-punção"
SINAN_LCR_AGE_STRATA_ORDER = [
    "Neonatos até seis meses de idade",
    "Crianças/adultos (>6 meses)",
    "Idade sem informação",
]
SINAN_LCR_AGE_STRATIFICATION_NOTE = (
    "Estratificação etária operacional: neonatos até seis meses de idade = indivíduos com idade menor ou igual a 6 meses completos; "
    "crianças/adultos = >6 meses, isto é, a partir do mês seguinte ao sexto mês. "
    "A opção 'Sem estratificação' mantém a distribuição geral, sem separar por idade."
)

# Faixas operacionais usadas nos algoritmos do painel. Leucócitos em células/mm³,
# proteínas em mg/dL e glicose em mg/dL absoluto no LCR (proxy quando não há soro).
SINAN_LCR_ETIOLOGY_RANGES = {
    "Viral": {
        "leuco": (50, 1000),
        "prot": (0, 200),
        "glico_min": 45,
        "glico_desc": "Usualmente normal; idealmente >45% da glicose sanguínea",
        "predominio": "Linfócitos",
        "aspecto": "Límpido",
        "nota": "Pode haver predomínio neutrofílico nas primeiras 24–48h.",
    },
    "Bacteriana": {
        "leuco": (1000, 5000),
        "prot": (100, 500),
        "glico_max": 40,
        "glico_desc": "Reduzida; em geral <40–50% da glicose sanguínea; razão LCR/soro ≤0,4 na maioria dos casos",
        "predominio": "Neutrófilos",
        "aspecto": "Turvo/purulento",
        "nota": "Faixa típica; extremos documentados de <100 a >10.000 céls/mm³.",
    },
    "Tuberculosa": {
        "leuco": (50, 300),
        "prot": (50, 300),
        "glico_max": 40,
        "glico_desc": "Reduzida; em geral <45–50% da glicose sanguínea",
        "predominio": "Linfócitos",
        "aspecto": "Turvo ou límpido",
        "nota": "Pode haver padrão misto; durante o tratamento pode ocorrer paradoxo terapêutico com neutrofilia.",
    },
    "Fúngica": {
        "leuco": (20, 500),
        "prot": (45, 10_000),
        "glico_max": 40,
        "glico_desc": "Reduzida; em geral <40–50% da glicose sanguínea",
        "predominio": "Linfócitos",
        "aspecto": "Frequentemente límpido",
        "nota": "Em criptococose associada à AIDS, a contagem pode ser <20 céls/mm³ na maioria dos casos.",
    },
}

SINAN_LCR_RANGES_SOURCE_NOTE = (
    "Faixas de uso operacional/didático baseadas principalmente no Mandell, Douglas, and Bennett's Principles "
    "and Practice of Infectious Diseases, na tabela de achados do LCR do MSD/Merck Manual Professional e em "
    "literatura complementar sobre valores por idade. Elas descrevem padrões típicos e exceções relevantes; "
    "não devem ser usadas como corte diagnóstico isolado. A glicose deve ser interpretada preferencialmente "
    "pela razão LCR/soro, mas o SINAN geralmente só traz LAB_GLICO absoluto."
)

# Classes clínicas usadas nos gráficos de distribuição dos parâmetros do LCR.
# A ideia é substituir bins automáticos por intervalos interpretáveis, alinhados às
# faixas da tabela-resumo e às limitações descritas nas notas (a–g).
SINAN_LCR_DISTRIBUTION_BIN_SPECS = {
    "leuco": [
        {"label": "<20", "condition": "valor < 20", "start": None, "end": 20, "leitura": "Abaixo da menor faixa típica infecciosa; atenção à criptococose em imunossuprimidos."},
        {"label": "20–49", "condition": "valor >= 20 AND valor < 50", "start": 20, "end": 49, "leitura": "Faixa compatível com criptocócica/fúngica inicial ou baixa celularidade."},
        {"label": "50–300", "condition": "valor >= 50 AND valor <= 300", "start": 50, "end": 300, "leitura": "Sobrepõe viral, tuberculosa e parte das fúngicas."},
        {"label": "301–999", "condition": "valor > 300 AND valor < 1000", "start": 301, "end": 999, "leitura": "Faixa ainda compatível com viral; também pode ocorrer em bacteriana atípica/tratada."},
        {"label": "1.000–5.000", "condition": "valor >= 1000 AND valor <= 5000", "start": 1000, "end": 5000, "leitura": "Faixa típica da meningite bacteriana no Mandell."},
        {"label": "5.001–10.000", "condition": "valor > 5000 AND valor <= 10000", "start": 5001, "end": 10000, "leitura": "Acima da faixa bacteriana típica; ainda dentro dos extremos descritos para bacteriana."},
        {"label": ">10.000", "condition": "valor > 10000", "start": 10000, "end": None, "leitura": "Extremo alto descrito em bacteriana; revisar plausibilidade e contexto clínico."},
    ],
    "prot": [
        {"label": "<45", "condition": "valor < 45", "start": None, "end": 45, "leitura": "Normal/baixo para meningite; não exclui infecção em imunossuprimidos ou fases iniciais."},
        {"label": "45–99", "condition": "valor >= 45 AND valor < 100", "start": 45, "end": 99, "leitura": "Elevação leve; compatível com viral, tuberculosa ou fúngica."},
        {"label": "100–200", "condition": "valor >= 100 AND valor <= 200", "start": 100, "end": 200, "leitura": "Sobrepõe viral alta, bacteriana e tuberculosa."},
        {"label": "201–300", "condition": "valor > 200 AND valor <= 300", "start": 201, "end": 300, "leitura": "Compatível com bacteriana/tuberculosa; acima do típico viral."},
        {"label": "301–500", "condition": "valor > 300 AND valor <= 500", "start": 301, "end": 500, "leitura": "Faixa típica alta de bacteriana."},
        {"label": ">500", "condition": "valor > 500", "start": 500, "end": None, "leitura": "Muito elevada; revisar contexto, bloqueio de fluxo/hemorragia/coleta traumática e plausibilidade."},
    ],
    "glico": [
        {"label": "<40", "condition": "valor < 40", "start": None, "end": 40, "leitura": "Reduzida em termos absolutos; compatível com bacteriana, tuberculosa ou fúngica, mas deve ser comparada ao soro."},
        {"label": "40–44", "condition": "valor >= 40 AND valor < 45", "start": 40, "end": 44, "leitura": "Zona limítrofe; a razão LCR/soro é mais adequada que o valor absoluto."},
        {"label": "45–49", "condition": "valor >= 45 AND valor < 50", "start": 45, "end": 49, "leitura": "Geralmente preservada em termos absolutos, mas ainda depende da glicemia sérica."},
        {"label": "≥50", "condition": "valor >= 50", "start": 50, "end": None, "leitura": "Preservada em termos absolutos; mais compatível com viral, sem excluir outras etiologias."},
    ],
    "neutro": [
        {"label": "0", "condition": "valor = 0", "start": 0, "end": 0, "leitura": "Sem neutrófilos registrados."},
        {"label": "1–24%", "condition": "valor > 0 AND valor < 25", "start": 1, "end": 24, "leitura": "Baixa participação neutrofílica."},
        {"label": "25–49%", "condition": "valor >= 25 AND valor < 50", "start": 25, "end": 49, "leitura": "Componente neutrofílico sem predomínio."},
        {"label": "50–74%", "condition": "valor >= 50 AND valor < 75", "start": 50, "end": 74, "leitura": "Predomínio neutrofílico moderado."},
        {"label": "75–100%", "condition": "valor >= 75 AND valor <= 100", "start": 75, "end": 100, "leitura": "Predomínio neutrofílico forte."},
        {"label": ">100%", "condition": "valor > 100", "start": 100, "end": None, "leitura": "Valor incompatível com percentual; provável problema de preenchimento/unidade."},
    ],
    "linfo": [
        {"label": "0", "condition": "valor = 0", "start": 0, "end": 0, "leitura": "Sem linfócitos registrados."},
        {"label": "1–24%", "condition": "valor > 0 AND valor < 25", "start": 1, "end": 24, "leitura": "Baixa participação linfocitária."},
        {"label": "25–49%", "condition": "valor >= 25 AND valor < 50", "start": 25, "end": 49, "leitura": "Componente linfocitário sem predomínio."},
        {"label": "50–74%", "condition": "valor >= 50 AND valor < 75", "start": 50, "end": 74, "leitura": "Predomínio linfocitário moderado."},
        {"label": "75–100%", "condition": "valor >= 75 AND valor <= 100", "start": 75, "end": 100, "leitura": "Predomínio linfocitário forte."},
        {"label": ">100%", "condition": "valor > 100", "start": 100, "end": None, "leitura": "Valor incompatível com percentual; provável problema de preenchimento/unidade."},
    ],
    "mono": [
        {"label": "0", "condition": "valor = 0", "start": 0, "end": 0, "leitura": "Sem monócitos registrados."},
        {"label": "1–24%", "condition": "valor > 0 AND valor < 25", "start": 1, "end": 24, "leitura": "Baixa participação monocitária."},
        {"label": "25–49%", "condition": "valor >= 25 AND valor < 50", "start": 25, "end": 49, "leitura": "Componente monocitário relevante."},
        {"label": "50–74%", "condition": "valor >= 50 AND valor < 75", "start": 50, "end": 74, "leitura": "Predomínio monocitário."},
        {"label": "75–100%", "condition": "valor >= 75 AND valor <= 100", "start": 75, "end": 100, "leitura": "Predomínio monocitário forte."},
        {"label": ">100%", "condition": "valor > 100", "start": 100, "end": None, "leitura": "Valor incompatível com percentual; provável problema de preenchimento/unidade."},
    ],
    "eosi": [
        {"label": "0", "condition": "valor = 0", "start": 0, "end": 0, "leitura": "Sem eosinófilos registrados."},
        {"label": "1–4%", "condition": "valor > 0 AND valor < 5", "start": 1, "end": 4, "leitura": "Pequena participação eosinofílica."},
        {"label": "5–9%", "condition": "valor >= 5 AND valor < 10", "start": 5, "end": 9, "leitura": "Eosinófilos presentes; revisar contexto clínico."},
        {"label": "10–49%", "condition": "valor >= 10 AND valor < 50", "start": 10, "end": 49, "leitura": "Eosinofilia liquórica; pode sugerir etiologias específicas conforme contexto."},
        {"label": "50–100%", "condition": "valor >= 50 AND valor <= 100", "start": 50, "end": 100, "leitura": "Predomínio eosinofílico."},
        {"label": ">100%", "condition": "valor > 100", "start": 100, "end": None, "leitura": "Valor incompatível com percentual; provável problema de preenchimento/unidade."},
    ],
}


def sinan_lcr_distribution_bin_specs(param_key: str) -> List[Dict[str, object]]:
    """Retorna classes clínicas de distribuição para o parâmetro do LCR."""
    return list(SINAN_LCR_DISTRIBUTION_BIN_SPECS.get(param_key, []))


def sinan_lcr_distribution_bin_order(param_key: str) -> List[str]:
    return [str(spec["label"]) for spec in sinan_lcr_distribution_bin_specs(param_key)]


# =============================================================================
# Metadados, códigos sentinela, tetos e auditoria — parâmetros quimiocitológicos do LCR
# =============================================================================
# A revisão crítica v45 recomendou separar explicitamente três fenômenos que antes
# podiam ser misturados: valor ausente/sentinela, valor acima do teto de
# plausibilidade e valor no teto operacional/sistêmico. O cadastro abaixo é a
# fonte única para unidade, tipo, faixa operacional, regra de sentinela, teto
# plausível, teto de sistema/truncamento e uso permitido de cada LAB_*.
#
# ATENÇÃO: os códigos sentinela e tetos permanecem uma camada defensiva
# operacional; devem ser validados contra o dicionário oficial do SINAN da versão
# em uso antes de relatórios formais.
SINAN_LCR_SENTINEL_CODES = {999, 9999, 99999}

# Mantido por compatibilidade com trechos antigos e com a tabela-resumo.
SINAN_LCR_PLAUSIBLE_MAX = {
    "leuco": 50000,
    "prot": 2000,
    "hema": 1000000,
    "glico": 500,
    "clor": 200,
    "neutro": 100,
    "linfo": 100,
    "mono": 100,
    "eosi": 100,
}


@dataclass(frozen=True)
class SinanLcrParameterMetadata:
    key: str
    unidade: str
    tipo_valor: str
    faixa_operacional: str
    regra_sentinela: str
    sentinel_codes: Tuple[int, ...]
    teto_plausivel: Optional[float]
    teto_sistema: Tuple[float, ...]
    comportamento_truncamento: str
    uso_permitido: str


SINAN_LCR_PARAM_METADATA: Dict[str, SinanLcrParameterMetadata] = {
    "hema": SinanLcrParameterMetadata(
        key="hema",
        unidade="céls/mm³",
        tipo_valor="contagem absoluta",
        faixa_operacional=">=0; extremos devem ser auditados",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=SINAN_LCR_PLAUSIBLE_MAX["hema"],
        teto_sistema=(99999,),
        comportamento_truncamento="LAB_HEMA pode atingir 99999; sinalizar como possível teto/codificação, sem descarte silencioso.",
        uso_permitido="auditoria e contexto; não entra como critério etiológico principal",
    ),
    "neutro": SinanLcrParameterMetadata(
        key="neutro",
        unidade="% dos leucócitos",
        tipo_valor="percentual diferencial",
        faixa_operacional="0-100",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=100,
        teto_sistema=(100,),
        comportamento_truncamento="Valores >100 são incompatíveis com percentual e ficam sinalizados.",
        uso_permitido="distribuição e predomínio celular quando 0-100",
    ),
    "glico": SinanLcrParameterMetadata(
        key="glico",
        unidade="mg/dL",
        tipo_valor="concentração absoluta",
        faixa_operacional=">=0; interpretar com glicemia sérica quando disponível",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=SINAN_LCR_PLAUSIBLE_MAX["glico"],
        teto_sistema=(99,),
        comportamento_truncamento="Dicionário operacional indicou máximo 99; sinalizar possível truncamento/teto.",
        uso_permitido="distribuição, tabela-resumo e classificação exploratória após limpeza de sentinelas",
    ),
    "leuco": SinanLcrParameterMetadata(
        key="leuco",
        unidade="céls/mm³",
        tipo_valor="contagem absoluta",
        faixa_operacional=">=0; faixas clínicas fixas no gráfico",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=SINAN_LCR_PLAUSIBLE_MAX["leuco"],
        teto_sistema=(9999,),
        comportamento_truncamento="Valores no teto 9999 e acima do teto plausível são sinalizados, não suavizados.",
        uso_permitido="distribuição, tabela-resumo e classificação exploratória após limpeza de sentinelas",
    ),
    "eosi": SinanLcrParameterMetadata(
        key="eosi",
        unidade="% dos leucócitos",
        tipo_valor="percentual diferencial",
        faixa_operacional="0-100",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=100,
        teto_sistema=(100,),
        comportamento_truncamento="Valores >100 são incompatíveis com percentual e ficam sinalizados.",
        uso_permitido="distribuição; baixa completude deve ser explicitada",
    ),
    "prot": SinanLcrParameterMetadata(
        key="prot",
        unidade="mg/dL",
        tipo_valor="concentração absoluta",
        faixa_operacional=">=0; faixas clínicas fixas no gráfico",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=SINAN_LCR_PLAUSIBLE_MAX["prot"],
        teto_sistema=(999,),
        comportamento_truncamento="Valores no teto 999 e acima do teto plausível são sinalizados, não descartados.",
        uso_permitido="distribuição, tabela-resumo e classificação exploratória após limpeza de sentinelas",
    ),
    "mono": SinanLcrParameterMetadata(
        key="mono",
        unidade="% dos leucócitos, quando 0-100; unidade ambígua se >100",
        tipo_valor="percentual diferencial com ambiguidade operacional",
        faixa_operacional="0-100 para interpretação percentual; >100 apenas como auditoria",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=100,
        teto_sistema=(100, 994),
        comportamento_truncamento="LAB_MONO teve máximo operacional 994; não assumir percentual puro sem sinalizar >100.",
        uso_permitido="distribuição com flag de auditoria; não usar >100 como composição percentual",
    ),
    "linfo": SinanLcrParameterMetadata(
        key="linfo",
        unidade="% dos leucócitos",
        tipo_valor="percentual diferencial",
        faixa_operacional="0-100",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=100,
        teto_sistema=(100,),
        comportamento_truncamento="Valores >100 são incompatíveis com percentual e ficam sinalizados.",
        uso_permitido="distribuição e predomínio celular quando 0-100",
    ),
    "clor": SinanLcrParameterMetadata(
        key="clor",
        unidade="mEq/L",
        tipo_valor="concentração absoluta",
        faixa_operacional=">=0; interpretar com cautela pela alta ausência",
        regra_sentinela="999/9999/99999 tratados como ausente/sentinela operacional",
        sentinel_codes=tuple(sorted(SINAN_LCR_SENTINEL_CODES)),
        teto_plausivel=SINAN_LCR_PLAUSIBLE_MAX["clor"],
        teto_sistema=(99,),
        comportamento_truncamento="Dicionário operacional mostrou inconsistência entre máximo 99 e top valores 120-122; sinalizar e auditar.",
        uso_permitido="auditoria/completude; uso clínico limitado pela alta ausência",
    ),
}


def sinan_lcr_param_metadata(param_key: str) -> Optional[SinanLcrParameterMetadata]:
    return SINAN_LCR_PARAM_METADATA.get(param_key)


def _sql_numeric_tuple(values: Sequence[float | int]) -> str:
    return ", ".join(str(int(v)) if float(v).is_integer() else repr(float(v)) for v in values)


def sinan_lcr_neutralize_sentinel_expr(value_expr: str) -> str:
    """Compatibilidade: neutraliza sentinelas gerais quando o parâmetro não é conhecido."""
    codes = ", ".join(str(c) for c in sorted(SINAN_LCR_SENTINEL_CODES))
    return f"CASE WHEN ({value_expr}) IN ({codes}) THEN NULL ELSE ({value_expr}) END"


def sinan_lcr_clean_value_expr(value_expr: str, param_key: Optional[str] = None) -> str:
    """Valor limpo para uso analítico: preserva o bruto fora desta expressão,
    mas converte sentinelas configuradas para NULL.
    """
    meta = sinan_lcr_param_metadata(param_key or "")
    codes = meta.sentinel_codes if meta else tuple(sorted(SINAN_LCR_SENTINEL_CODES))
    codes_sql = _sql_numeric_tuple(codes)
    return f"CASE WHEN ({value_expr}) IN ({codes_sql}) THEN NULL ELSE ({value_expr}) END"


def sinan_lcr_analysis_value_expr(value_expr: str, param_key: Optional[str] = None, *, enforce_percent_range: bool = False) -> str:
    clean = sinan_lcr_clean_value_expr(value_expr, param_key)
    meta = sinan_lcr_param_metadata(param_key or "")
    if enforce_percent_range and meta and "percentual" in meta.tipo_valor:
        return f"CASE WHEN ({clean}) IS NULL OR ({clean}) < 0 OR ({clean}) > 100 THEN NULL ELSE ({clean}) END"
    return clean


def sinan_lcr_numeric_audit_exprs(value_expr: str, param_key: str) -> Dict[str, str]:
    """Expressões SQL padronizadas para valor bruto, valor limpo e flags de auditoria."""
    meta = sinan_lcr_param_metadata(param_key)
    bruto = f"({value_expr})"
    limpo = sinan_lcr_clean_value_expr(value_expr, param_key)
    codes = meta.sentinel_codes if meta else tuple(sorted(SINAN_LCR_SENTINEL_CODES))
    sent_sql = _sql_numeric_tuple(codes)
    flag_sentinela = f"CASE WHEN {bruto} IN ({sent_sql}) THEN TRUE ELSE FALSE END"
    if meta and meta.teto_plausivel is not None:
        flag_acima = f"CASE WHEN ({limpo}) IS NOT NULL AND ({limpo}) > {repr(float(meta.teto_plausivel))} THEN TRUE ELSE FALSE END"
    else:
        flag_acima = "FALSE"
    if meta and meta.teto_sistema:
        teto_sql = _sql_numeric_tuple(meta.teto_sistema)
        flag_teto = f"CASE WHEN {bruto} IN ({teto_sql}) THEN TRUE ELSE FALSE END"
    else:
        flag_teto = "FALSE"
    if meta and "percentual" in meta.tipo_valor:
        flag_percentual = f"CASE WHEN ({limpo}) IS NOT NULL AND (({limpo}) < 0 OR ({limpo}) > 100) THEN TRUE ELSE FALSE END"
    else:
        flag_percentual = "FALSE"
    return {
        "valor_bruto": bruto,
        "valor_limpo": limpo,
        "flag_sentinela": flag_sentinela,
        "flag_acima_teto_plausivel": flag_acima,
        "flag_teto_sistema": flag_teto,
        "flag_percentual_invalido": flag_percentual,
    }


def sinan_lcr_metadata_dataframe(param_keys: Optional[Sequence[str]] = None) -> pd.DataFrame:
    keys = list(param_keys) if param_keys else list(SINAN_LCR_PARAM_METADATA.keys())
    rows = []
    for key in keys:
        meta = sinan_lcr_param_metadata(key)
        if not meta:
            continue
        rows.append({
            "parametro_id": key,
            "parametro": str(SINAN_QUIMIO_PARAMS.get(key, {}).get("label", key)),
            "unidade": meta.unidade,
            "tipo_valor": meta.tipo_valor,
            "faixa_operacional": meta.faixa_operacional,
            "regra_sentinela": meta.regra_sentinela,
            "teto_plausivel": meta.teto_plausivel,
            "teto_sistema": ", ".join(str(int(v)) if float(v).is_integer() else str(v) for v in meta.teto_sistema) if meta.teto_sistema else "",
            "comportamento_truncamento": meta.comportamento_truncamento,
            "uso_permitido": meta.uso_permitido,
        })
    return pd.DataFrame(rows)


def sinan_lcr_plausible_max(param_key: str) -> Optional[float]:
    meta = sinan_lcr_param_metadata(param_key)
    return meta.teto_plausivel if meta else SINAN_LCR_PLAUSIBLE_MAX.get(param_key)


# Mapeia CON_DIAGES (+ CLA_ME_ETI para refinar a categoria 08 "outra etiologia")
# para um dos 4 grupos etiológicos usados na comparação por faixas de LCR.
# 01 (meningococcemia isolada) é deixado fora por não representar, isoladamente,
# meningite confirmada por LCR. 06 (meningite não especificada) também fica fora
# das faixas esperadas do LCR: na conversão CID ela vai para G03 e não deve ser
# avaliada contra o padrão bacteriano. 05/09/10 entram como bacterianas; 07 segue
# como viral operacional apenas no bloco exploratório legado.
SINAN_CLA_ME_ETI_FUNGAL_CODES = {"42", "43", "44", "64"}  # outros fungos, Cryptococcus, Candida, Aspergillus


def sinan_expected_etiology_group_expr(con_code_sql: str, cla_me_eti_code_sql: Optional[str]) -> str:
    fungal_codes = ", ".join(qstr(code) for code in sorted(SINAN_CLA_ME_ETI_FUNGAL_CODES))
    eti_expr = cla_me_eti_code_sql or "NULL"
    return f"""
        CASE
            WHEN {con_code_sql} = '04' THEN 'Tuberculosa'
            WHEN {con_code_sql} = '07' THEN 'Viral'
            WHEN {con_code_sql} IN ('02', '03', '05', '09', '10') THEN 'Bacteriana'
            WHEN {con_code_sql} = '08' AND {eti_expr} IN ({fungal_codes}) THEN 'Fúngica'
            WHEN {con_code_sql} = '08' THEN 'Outra etiologia (não fúngica)'
            ELSE NULL
        END
    """

RACA_COR = {
    "1": "Branca",
    "2": "Preta",
    "3": "Amarela",
    "4": "Parda",
    "5": "Indígena",
    "9": "Ignorada",
}

# CS_ESCOL_N é exportado em muitos DBF/SINAN como código de 2 caracteres
# com zero à esquerda (00–09). Manter também as chaves sem zero cobre bases
# que chegam tipadas como inteiro/numérico após conversão para DuckDB/Parquet.
SINAN_ESCOLARIDADE = {
    "0": "0 — analfabeto",
    "00": "0 — analfabeto",
    "1": "1 — 1ª a 4ª série incompleta do EF",
    "01": "1 — 1ª a 4ª série incompleta do EF",
    "2": "2 — 4ª série completa do EF",
    "02": "2 — 4ª série completa do EF",
    "3": "3 — 5ª à 8ª série incompleta do EF",
    "03": "3 — 5ª à 8ª série incompleta do EF",
    "4": "4 — ensino fundamental completo",
    "04": "4 — ensino fundamental completo",
    "5": "5 — ensino médio incompleto",
    "05": "5 — ensino médio incompleto",
    "6": "6 — ensino médio completo",
    "06": "6 — ensino médio completo",
    "7": "7 — educação superior incompleta",
    "07": "7 — educação superior incompleta",
    "8": "8 — educação superior completa",
    "08": "8 — educação superior completa",
    "9": "9 — ignorado",
    "09": "9 — ignorado",
    "10": "10 — não se aplica",
}

SIM_ESCOLARIDADE_2010 = {
    "0": "0 — sem escolaridade",
    "1": "1 — ensino fundamental I",
    "2": "2 — ensino fundamental II",
    "3": "3 — ensino médio",
    "4": "4 — superior incompleto",
    "5": "5 — superior completo",
    "9": "9 — ignorado",
}

SIM_ESCOLARIDADE_ANTIGA = {
    "0": "0 — sem escolaridade",
    "1": "1 — nenhuma",
    "2": "2 — 1 a 3 anos de estudo",
    "3": "3 — 4 a 7 anos de estudo",
    "4": "4 — 8 a 11 anos de estudo",
    "5": "5 — 12 anos ou mais de estudo",
    "9": "9 — ignorado",
}

SIM_ESCOLARIDADE_AGREGADA = {
    "0": "0 — sem escolaridade",
    "1": "1 — ensino fundamental I",
    "2": "2 — ensino fundamental II",
    "3": "3 — ensino médio",
    "4": "4 — superior",
    "5": "5 — superior",
    "9": "9 — ignorado",
}

CIHA_MODALIDADE = {
    "01": "01 — hospitalar",
    "02": "02 — ambulatorial",
}

SIM_LOCOCOR = {
    "1": "1 — hospital",
    "2": "2 — outro estabelecimento de saúde",
    "3": "3 — domicílio",
    "4": "4 — via pública",
    "5": "5 — outros",
    "9": "9 — ignorado",
}


SIM_OBITOGRAV = {
    "1": "1 — durante a gravidez",
    "2": "2 — durante o parto",
    "3": "3 — durante o aborto",
    "4": "4 — até 42 dias após o término da gestação",
    "5": "5 — 43 dias a 1 ano após o término da gestação",
    "8": "8 — não ocorreu no ciclo gravídico-puerperal",
    "9": "9 — ignorado",
}

SIM_OBITOPUERP = {
    "1": "1 — até 42 dias após o parto",
    "2": "2 — de 43 dias a 1 ano após o parto",
    "3": "3 — não ocorreu no puerpério",
    "8": "8 — não se aplica",
    "9": "9 — ignorado",
}


# Municípios — suporte interno
# -----------------------------------------------------------------------------
# Os gráficos territoriais usam diretamente o código municipal presente na base;
# não há dependência de arquivo auxiliar de municípios.


def _ensure_municipios_ibge_view(shared: "_SharedDB") -> None:
    """Mantém compatibilidade com chamadas antigas; não registra arquivo auxiliar."""
    return


@dataclass(frozen=True)
class SourceConfig:
    name: str
    title: str
    default_db: str
    default_table: str
    expected_period: str
    date_candidates: List[str]
    sex_candidates: List[str]
    age_candidates: List[str]
    age_unit_candidates: List[str]
    race_candidates: List[str]
    municipality_res_candidates: List[str]
    municipality_event_candidates: List[str]
    cid_candidates: List[str]
    field_notes: List[str]


SOURCE_CONFIG: Dict[str, SourceConfig] = {
    "SINAN": SourceConfig(
        name="SINAN",
        title="Notificações e investigação de casos",
        default_db="sinan_meningite_rio_estado.duckdb",
        default_table="sinan_meningite_rio_estado_data",
        expected_period="2007–2026",
        date_candidates=["DT_NOTIFIC", "DT_SIN_PRI", "DT_INVEST", "DT_ENCERRA", "DT_DIGITA"],
        sex_candidates=["CS_SEXO", "SEXO"],
        age_candidates=["NU_IDADE_N", "IDADE", "IDADE_ANOS", "IDADEANOS"],
        age_unit_candidates=[],
        race_candidates=["CS_RACA", "RACACOR"],
        municipality_res_candidates=["ID_MN_RESI", "CODMUNRES", "MUNIC_RES", "MUN_RES"],
        municipality_event_candidates=["ID_MUNICIP", "ID_MN_OCORR", "CODMUNOCOR", "MUNIC_MOV"],
        cid_candidates=["ID_AGRAVO", "CID10", "CID", "AGRAVO"],
        field_notes=[
            "No recorte enviado, o SINAN tende a ter ID_AGRAVO constante como G039.",
            "Para etiologia/forma clínica no SINAN, priorize CON_DIAGES; complemente com CLA_ME_BAC, CLA_ME_ASS, CLA_ME_ETI, CRITERIO e EVOLUCAO.",
            "G04.2 não é inferido por CON_DIAGES=05; a classificação de 'outras bactérias' é refinada como G00 ou G01 conforme CLA_ME_BAC e campos complementares.",
        ],
    ),
    "SIM": SourceConfig(
        name="SIM",
        title="Óbitos e causas de morte",
        default_db="sim_do_rio_estado.duckdb",
        default_table="sim_do_rio_estado_data",
        expected_period="2007–2026",
        date_candidates=["DTOBITO", "DT_OBITO", "DTATESTADO", "DTNASC", "DT_NASC"],
        sex_candidates=["SEXO", "CS_SEXO"],
        age_candidates=["IDADE", "IDADEANOS", "IDADE_ANOS"],
        age_unit_candidates=[],
        race_candidates=["RACACOR", "CS_RACA"],
        municipality_res_candidates=["CODMUNRES", "MUNRES", "ID_MN_RESI"],
        municipality_event_candidates=["CODMUNOCOR", "MUNOCOR", "ID_MN_OCORR"],
        cid_candidates=["CAUSABAS", "CAUSABAS_O", "LINHAA", "LINHAB", "LINHAC", "LINHAD", "LINHAII", "ATESTADO", "CB_PRE"],
        field_notes=[
            "CAUSABAS/CAUSABAS_O representam causa básica; LINHAA–LINHAII e ATESTADO podem capturar menções associadas.",
            "Compare causa básica versus qualquer menção de CID de meningite para investigar concordância com o SINAN.",
        ],
    ),
    "CIHA": SourceConfig(
        name="CIHA",
        title="Atendimentos/internações informados à CIHA",
        default_db="ciha_rio_estado.duckdb",
        default_table="ciha_rio_estado_data",
        expected_period="2007–2026",
        date_candidates=["DT_ATEND", "DT_SAIDA", "DT_INTER", "DT_INTERNA", "DT_COMPET", "COMPET", "ANO_CMPT"],
        sex_candidates=["SEXO", "CS_SEXO"],
        age_candidates=["IDADE", "IDADE_ANOS", "IDADEANOS", "NU_IDADE_N"],
        age_unit_candidates=["COD_IDADE"],
        race_candidates=["RACACOR", "CS_RACA"],
        municipality_res_candidates=["MUNIC_RES", "CODMUNRES", "MUN_RES", "ID_MN_RESI"],
        municipality_event_candidates=["MUNIC_MOV", "CODMUNOCOR", "CODMUN", "MUN_MOV"],
        cid_candidates=["DIAG_PRINC", "DIAG_SECUN", "CIDPRI", "CID_PRINC", "CID", "DIAG"],
        field_notes=[
            "DIAG_PRINC é o campo CID-10 mais importante; DIAG_SECUN pode capturar diagnósticos secundários, mas costuma ter menor completude.",
            "CIHA deve ser lida como utilização de serviços/produção assistencial, não como incidência populacional.",
        ],
    ),
}


FIELD_GUIDE = {
    "SINAN": [
        ("DT_SIN_PRI", "data principal", "início dos sintomas"),
        ("DT_NOTIFIC", "data alternativa", "notificação"),
        ("NU_NOTIFIC", "identificador operacional", "número da notificação; usado para verificar sobreposição e possíveis duplicidades"),
        ("CLASSI_FIN", "definição de caso", "confirmado, descartado; demais valores/ausência são tratados como sem classificação"),
        ("CON_DIAGES", "etiologia/forma", "conclusão diagnóstica específica"),
        ("CLA_ME_BAC", "bactéria em outras bacterianas", "refina CON_DIAGES=05 em G00 ou G01"),
        ("CLA_ME_ASS", "agente viral/asséptico", "detalha meningite asséptica/viral"),
        ("CLA_ME_ETI", "outra etiologia", "detalha fungos, protozoários e parasitas"),
        ("EVOLUCAO", "desfecho", "alta, óbito por meningite, óbito por outra causa"),
        ("CRITERIO", "critério de confirmação", "cultura, PCR, clínico, quimiocitológico etc."),
        ("LAB_PUNCAO", "investigação", "realização da punção laboratorial/lombar"),
        ("DT_PUNCA / DT_PUNCAO", "data", "data da punção lombar, quando disponível no banco"),
        ("LAB_LIQUOR", "exame", "quimiocitológico do líquor (LCR) realizado"),
        ("LAB_HEMA / LAB_NEUTRO / LAB_GLICO / LAB_LEUCO / LAB_EOSI / LAB_PROT / LAB_MONO / LAB_LINFO / LAB_CLOR", "parâmetros do LCR", "hemácias, diferenciais celulares, glicose, proteínas e cloreto"),
        ("ID_AGRAVO", "CID bruto", "geralmente G039 neste recorte"),
    ],
    "SIM": [
        ("DTOBITO", "data principal", "data do óbito"),
        ("CAUSABAS", "CID principal", "causa básica codificada"),
        ("CAUSABAS_O", "CID complementar", "causa básica original/complementar"),
        ("LINHAA–LINHAII", "menções", "linhas da Declaração de Óbito"),
        ("IDADE", "idade", "idade codificada no padrão DATASUS"),
        ("CODMUNRES", "território", "município de residência"),
        ("CODMUNOCOR", "território", "município de ocorrência"),
        ("OBITOGRAV", "ciclo gravídico", "óbito durante gravidez/parto/aborto ou período pós-gestacional"),
        ("OBITOPUERP", "ciclo gravídico", "óbito no puerpério quando disponível"),
    ],
    "CIHA": [
        ("DT_ATEND", "data principal", "data de atendimento"),
        ("DT_SAIDA", "data alternativa", "data de saída"),
        ("DIAG_PRINC", "CID principal", "diagnóstico principal"),
        ("DIAG_SECUN", "CID complementar", "diagnóstico secundário"),
        ("MORTE", "desfecho administrativo", "morte no atendimento"),
        ("DIAS_PERM", "uso de serviço", "dias de permanência"),
        ("MODALIDADE", "uso de serviço", "hospitalar/ambulatorial"),
        ("PROC_REA / PROCEDIMENTO", "procedimento", "procedimento informado e sua quantidade"),
        ("IDADE + COD_IDADE", "idade", "idade e unidade da idade"),
    ],
}


# =============================================================================
# Utilitários SQL/texto
# =============================================================================


def normalize_name(text: object) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.upper().strip() if ch.isalnum())


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def qstr(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def clean_str_expr(col: str) -> str:
    return f"NULLIF(TRIM(CAST({qident(col)} AS VARCHAR)), '')"


def clean_code_expr(col: str, pad2: bool = False) -> str:
    """Normaliza campos de códigos categóricos do DATASUS.

    Alguns arquivos DuckDB/Parquet chegam com códigos como 1.0, 07.0 ou 5,0
    depois da conversão de tipos. Se apenas removêssemos pontuação, 1.0 viraria
    10 e 07.0 viraria 070, quebrando CLASSI_FIN, CON_DIAGES, EVOLUCAO etc.

    Valores textuais de ausência importados como NA/NAN/NULL são tratados como
    sem preenchimento, não como uma categoria válida de escolaridade ou evolução.
    """
    txt = f"UPPER(COALESCE({clean_str_expr(col)}, ''))"
    missing_like = f"TRIM({txt}) IN ('NA', 'N/A', 'NAN', 'NONE', 'NULL', '<NA>')"
    numeric_like = f"regexp_matches({txt}, '^\\s*[0-9]+([\\.,]0+)?\\s*$')"
    numeric_code = f"regexp_replace(TRIM({txt}), '[\\.,]0+$', '')"
    alnum_code = f"regexp_replace({txt}, '[^0-9A-Z]', '', 'g')"
    code = f"NULLIF(CASE WHEN {missing_like} THEN '' WHEN {numeric_like} THEN {numeric_code} ELSE {alnum_code} END, '')"
    if pad2:
        return f"CASE WHEN {code} IS NULL THEN NULL WHEN LENGTH({code}) = 1 THEN '0' || {code} ELSE {code} END"
    return code



def sqlsafe(expr: object) -> str:
    if expr is None:
        return "NULL"
    return str(expr)


def case_from_mapping(code_sql: str, mapping: Dict[str, str], default: str) -> str:
    parts = [f"WHEN {qstr(k)} THEN {qstr(v)}" for k, v in mapping.items()]
    return f"CASE {code_sql} {' '.join(parts)} ELSE {qstr(default)} END"


def education_label_expr(source: str, col: str) -> str:
    code = clean_code_expr(col)
    if source == "SINAN":
        return case_from_mapping(code, SINAN_ESCOLARIDADE, "Sem informação/ignorado")
    if source == "SIM":
        col_norm = normalize_name(col)
        if "2010" in col_norm:
            mapping = SIM_ESCOLARIDADE_2010
        elif "AGR" in col_norm:
            mapping = SIM_ESCOLARIDADE_AGREGADA
        else:
            mapping = SIM_ESCOLARIDADE_ANTIGA
        return case_from_mapping(code, mapping, "Sem informação/ignorado")
    return clean_str_expr(col)


def unique_mapping_labels(mapping: Dict[str, str]) -> List[str]:
    """Retorna rótulos categóricos preservando a ordem e removendo duplicatas."""
    labels: List[str] = []
    seen: set[str] = set()
    for label in mapping.values():
        if label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def education_category_labels(source: str, col_or_expr: Optional[str] = None, include_missing: bool = True) -> List[str]:
    """Lista todas as categorias operacionais de escolaridade esperadas para o campo detectado."""
    if source == "SINAN":
        labels = unique_mapping_labels(SINAN_ESCOLARIDADE)
    elif source == "SIM":
        col_norm = normalize_name(col_or_expr or "")
        if "2010" in col_norm:
            labels = unique_mapping_labels(SIM_ESCOLARIDADE_2010)
        elif "AGR" in col_norm:
            labels = unique_mapping_labels(SIM_ESCOLARIDADE_AGREGADA)
        else:
            labels = unique_mapping_labels(SIM_ESCOLARIDADE_ANTIGA)
    else:
        labels = []
    if include_missing and "Sem informação/ignorado" not in labels:
        labels.append("Sem informação/ignorado")
    return labels


def values_cte_from_labels(labels: Sequence[str], label_col: str, order_col: str) -> str:
    """Constrói uma lista VALUES segura para CTEs de categorias categóricas."""
    if not labels:
        return f"SELECT {qstr('Sem informação/ignorado')} AS {label_col}, 1 AS {order_col}"
    values = ", ".join(f"({qstr(label)}, {idx})" for idx, label in enumerate(labels, start=1))
    return f"SELECT * FROM (VALUES {values}) AS t({label_col}, {order_col})"


def category_label_expr(category_sql: str, default: str = "Sem informação") -> str:
    """Normaliza categorias textuais para evitar rótulos vazios/Undefined nos gráficos."""
    cleaned = f"NULLIF(TRIM(CAST(({category_sql}) AS VARCHAR)), '')"
    return f"""
    CASE
        WHEN {cleaned} IS NULL THEN {qstr(default)}
        WHEN UPPER({cleaned}) IN ('UNDEFINED', 'NONE', 'NULL', 'NAN') THEN {qstr(default)}
        ELSE {cleaned}
    END
    """


def municipality_display_expr(col: str) -> str:
    """Rótulo municipal estável baseado somente no código IBGE informado na própria base.

    Os gráficos usam o código IBGE de 6 dígitos informado na própria base,
    sem dependência de arquivo externo de municípios.
    """
    raw = clean_str_expr(col)
    digits = f"regexp_replace(COALESCE({raw}, ''), '[^0-9]', '', 'g')"
    code6 = f"SUBSTR({digits}, 1, 6)"
    return f"""
    CASE
        WHEN {raw} IS NULL THEN 'Sem informação'
        WHEN LENGTH({digits}) >= 6 THEN 'IBGE ' || {code6}
        WHEN {digits} <> '' THEN 'Código informado: ' || {digits}
        ELSE {raw}
    END
    """


# ================================
# MUNICÍPIOS — TOP 15 POR CÓDIGO IBGE
# ================================

def _municipality_code_expr_from_sql(value_sql: str) -> str:
    """Extrai código IBGE de 6 dígitos de uma expressão SQL já montada.

    `value_sql` pode ser uma coluna bruta, uma expressão `clean_str_expr(...)`
    ou o retorno de `municipality_display_expr(...)`. Por isso a função não usa
    `qident`; ela trata o parâmetro como expressão SQL.
    """
    raw = f"NULLIF(TRIM(CAST(({value_sql}) AS VARCHAR)), '')"
    digits = f"regexp_replace(COALESCE({raw}, ''), '[^0-9]', '', 'g')"
    return f"""
    CASE
        WHEN {raw} IS NULL THEN NULL
        WHEN LENGTH({digits}) >= 6 THEN SUBSTR({digits}, 1, 6)
        ELSE NULL
    END
    """


def query_municipality_top(table, municipality_sql, where_sql, top_n=15):
    """Lista os principais municípios por código IBGE, sem depender de arquivo auxiliar."""
    top_n = max(1, int(top_n or 15))
    code_expr = _municipality_code_expr_from_sql(municipality_sql)

    sql = f"""
        WITH base AS (
            SELECT {code_expr} AS codigo_ibge_6
            FROM {table.ref_sql}
            {where_sql}
        ),
        agg AS (
            SELECT
                CASE
                    WHEN codigo_ibge_6 IS NULL THEN 'Sem informação'
                    ELSE 'IBGE ' || codigo_ibge_6
                END AS categoria,
                codigo_ibge_6,
                COUNT(*) AS n
            FROM base
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (ORDER BY n DESC, categoria) AS rn,
                   SUM(n) OVER () AS total
            FROM agg
        ),
        final AS (
            SELECT
                CASE WHEN rn <= {top_n} THEN categoria ELSE 'Outros municípios' END AS categoria,
                CASE WHEN rn <= {top_n} THEN codigo_ibge_6 ELSE NULL END AS codigo_ibge_6,
                SUM(n) AS n,
                MAX(total) AS denominador,
                MIN(rn) AS ordem
            FROM ranked
            GROUP BY 1, 2
        )
        SELECT categoria, codigo_ibge_6, n, denominador,
               ROUND(100.0 * n / NULLIF(denominador, 0), 2) AS pct
        FROM final
        ORDER BY ordem, n DESC, categoria
    """
    return run_query(table, sql)


def date_expr(col: str) -> str:
    txt = clean_str_expr(col)
    q = qident(col)
    return f"""
    CAST(COALESCE(
        TRY_CAST({q} AS DATE),
        CASE WHEN regexp_matches({txt}, '^\\d{{4}}-\\d{{2}}-\\d{{2}}$') THEN CAST(try_strptime({txt}, '%Y-%m-%d') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{8}}$') AND SUBSTR({txt}, 1, 4) BETWEEN '1900' AND '2099' THEN CAST(try_strptime({txt}, '%Y%m%d') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{8}}$') THEN CAST(try_strptime({txt}, '%d%m%Y') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{6}}$') AND SUBSTR({txt}, 1, 4) BETWEEN '1900' AND '2099' THEN CAST(try_strptime({txt} || '01', '%Y%m%d') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{4}}$') AND {txt} BETWEEN '1900' AND '2099' THEN CAST(try_strptime({txt} || '0101', '%Y%m%d') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{2}}/\\d{{2}}/\\d{{4}}$') THEN CAST(try_strptime({txt}, '%d/%m/%Y') AS DATE) END,
        CASE WHEN regexp_matches({txt}, '^\\d{{2}}-\\d{{2}}-\\d{{4}}$') THEN CAST(try_strptime({txt}, '%d-%m-%Y') AS DATE) END
    ) AS DATE)
    """


def datasus_age_expr(col: str) -> str:
    txt = clean_str_expr(col)
    return f"""
    CASE
        WHEN {txt} IS NULL THEN NULL
        WHEN regexp_matches({txt}, '^\\d{{3,4}}$') AND SUBSTR({txt}, 1, 1) IN ('0','1','2','3','4','5') THEN
            CASE SUBSTR({txt}, 1, 1)
                WHEN '0' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE) / (365.25 * 24)
                WHEN '1' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE) / (365.25 * 24)
                WHEN '2' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE) / 365.25
                WHEN '3' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE) / 12
                WHEN '4' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE)
                WHEN '5' THEN TRY_CAST(SUBSTR({txt}, 2) AS DOUBLE)
                ELSE NULL
            END
        WHEN regexp_matches({txt}, '^\\d{{1,3}}$') AND TRY_CAST({txt} AS DOUBLE) BETWEEN 0 AND 130 THEN TRY_CAST({txt} AS DOUBLE)
        ELSE NULL
    END
    """


def age_with_unit_expr(age_col: str, unit_col: str) -> str:
    age_txt = clean_str_expr(age_col)
    unit_txt = clean_str_expr(unit_col)
    age_num = f"TRY_CAST(REPLACE({age_txt}, ',', '.') AS DOUBLE)"
    return f"""
    CASE
        WHEN {age_txt} IS NULL OR {age_num} IS NULL THEN NULL
        WHEN {unit_txt} IN ('0', '1') THEN {age_num} / (365.25 * 24)
        WHEN {unit_txt} = '2' THEN {age_num} / 365.25
        WHEN {unit_txt} = '3' THEN {age_num} / 12
        WHEN {unit_txt} IN ('4', '5') THEN {age_num}
        ELSE {age_num}
    END
    """


def direct_age_expr(col: str) -> str:
    txt = clean_str_expr(col)
    return f"TRY_CAST(REPLACE({txt}, ',', '.') AS DOUBLE)"


def numeric_expr(col: str) -> str:
    """Expressão SQL segura para converter campos numéricos DATASUS em DOUBLE.

    Alguns campos laboratoriais chegam como inteiros, outros como texto, e alguns
    podem vir com vírgula decimal. TRY_CAST evita que valores sujos interrompam
    a execução do painel.
    """
    txt = clean_str_expr(col)
    cleaned = f"regexp_replace({txt}, '[^0-9,\\.\\-+]', '', 'g')"
    return f"""
    CASE
        WHEN {txt} IS NULL THEN NULL
        WHEN regexp_matches({txt}, '^\\s*[-+]?\\d{{1,3}}(\\.\\d{{3}})+(,\\d+)?\\s*$')
            THEN TRY_CAST(REPLACE(REPLACE({txt}, '.', ''), ',', '.') AS DOUBLE)
        WHEN regexp_matches({txt}, '^\\s*[-+]?\\d+(,\\d+)?\\s*$')
            THEN TRY_CAST(REPLACE({txt}, ',', '.') AS DOUBLE)
        ELSE TRY_CAST(REPLACE({cleaned}, ',', '.') AS DOUBLE)
    END
    """


def sex_expr(col: str) -> str:
    txt = clean_str_expr(col)
    return f"""
    CASE UPPER({txt})
        WHEN 'M' THEN 'Masculino'
        WHEN '1' THEN 'Masculino'
        WHEN 'F' THEN 'Feminino'
        WHEN '2' THEN 'Feminino'
        WHEN '3' THEN 'Feminino'
        WHEN 'I' THEN 'Ignorado/outro'
        WHEN '0' THEN 'Ignorado/outro'
        WHEN '9' THEN 'Ignorado/outro'
        ELSE COALESCE({txt}, 'Ignorado/outro')
    END
    """


def cid_extract_expr_for_col(col: str) -> str:
    txt = f"UPPER(COALESCE({clean_str_expr(col)}, ''))"
    raw = f"regexp_extract({txt}, '{CID_MENINGITE_REGEX}', 1)"
    return f"NULLIF(regexp_replace({raw}, '\\.', '', 'g'), '')"


def cid_extract_expr(cols: Sequence[str]) -> Optional[str]:
    exprs = [cid_extract_expr_for_col(c) for c in cols if c]
    if not exprs:
        return None
    return exprs[0] if len(exprs) == 1 else "COALESCE(" + ", ".join(exprs) + ")"


def cid_source_expr(cols: Sequence[str]) -> Optional[str]:
    tests = []
    for col in cols:
        cid = cid_extract_expr_for_col(col)
        tests.append(f"WHEN {cid} IS NOT NULL THEN {qstr(col)}")
    return None if not tests else "CASE " + " ".join(tests) + " ELSE NULL END"


def cid_presence_expr(cols: Sequence[str], pattern: str) -> Optional[str]:
    tests = []
    for col in cols:
        txt = f"UPPER(COALESCE({clean_str_expr(col)}, ''))"
        tests.append(f"regexp_matches({txt}, {qstr(pattern)})")
    if not tests:
        return None
    return " OR ".join(f"({t})" for t in tests)


def cid_group_expr(cid_sql: str) -> str:
    clauses = [f"WHEN {cid_sql} LIKE {qstr(rule['prefixo'] + '%')} THEN {qstr(rule['grupo'])}" for rule in CID_RULES]
    return f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID de meningite detectado' {' '.join(clauses)} ELSE 'Outro CID capturado' END"


def cid_type_expr(cid_sql: str) -> str:
    clauses = [f"WHEN {cid_sql} LIKE {qstr(rule['prefixo'] + '%')} THEN {qstr(rule['rotulo'])}" for rule in CID_RULES]
    return f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID de meningite detectado' {' '.join(clauses)} ELSE 'Outro CID capturado' END"


def _cid10_adequacy_condition(cid_sql: str, rule: Dict[str, str]) -> str:
    if rule.get("match") == "prefix":
        return f"{cid_sql} LIKE {qstr(rule['origem_prefixo'] + '%')}"
    return f"{cid_sql} = {qstr(rule['origem_prefixo'])}"


def cid10_adequacy_original_display_expr(cid_sql: str) -> str:
    clauses = [
        f"WHEN {_cid10_adequacy_condition(cid_sql, rule)} THEN {qstr(rule['origem_padrao'])}"
        for rule in CID10_ADEQUACY_CONVERSION_RULES
    ]
    return f"CASE WHEN {cid_sql} IS NULL THEN NULL {' '.join(clauses)} ELSE {cid_group_expr(cid_sql)} END"


def cid10_adequacy_group_expr(cid_sql: str) -> str:
    clauses = [
        f"WHEN {_cid10_adequacy_condition(cid_sql, rule)} THEN {qstr(rule['destino_grupo'])}"
        for rule in CID10_ADEQUACY_CONVERSION_RULES
    ]
    return f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID de meningite/encefalite detectado' {' '.join(clauses)} ELSE {cid_group_expr(cid_sql)} END"


def cid10_adequacy_type_expr(cid_sql: str) -> str:
    clauses = [
        f"WHEN {_cid10_adequacy_condition(cid_sql, rule)} THEN {qstr(rule['destino_rotulo'])}"
        for rule in CID10_ADEQUACY_CONVERSION_RULES
    ]
    return f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID de meningite/encefalite detectado' {' '.join(clauses)} ELSE {cid_type_expr(cid_sql)} END"


def cid10_adequacy_status_expr(cid_sql: str) -> str:
    clauses = [
        f"WHEN {_cid10_adequacy_condition(cid_sql, rule)} THEN 'Convertido'"
        for rule in CID10_ADEQUACY_CONVERSION_RULES
    ]
    return f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID detectado' {' '.join(clauses)} ELSE 'Fora da conversão — mantido no total' END"


def cid10_adequacy_reason_expr(cid_sql: str) -> str:
    clauses = [
        f"WHEN {_cid10_adequacy_condition(cid_sql, rule)} THEN {qstr(rule['observacao'])}"
        for rule in CID10_ADEQUACY_CONVERSION_RULES
    ]
    return (
        f"CASE WHEN {cid_sql} IS NULL THEN 'Sem CID de meningite/encefalite detectado.' "
        f"{' '.join(clauses)} "
        f"ELSE CONCAT('Fora da conversão operacional: ', {cid_type_expr(cid_sql)}, ' foi mantido no total como CID-10 detectado/não mapeado.') END"
    )


def cid10_adequacy_plot_label_expr(cid_sql: str) -> str:
    """Categoria usada no gráfico resumido de adequação e na comparação.

    Retorna o CID-10 adequado prefixado final: códigos presentes na tabela de
    conversão são deslocados para o destino; códigos já prefixados e não
    convertidos permanecem em seu próprio grupo CID-10. Registros sem CID
    retornam NULL para não entrar no gráfico nem na comparação estratificada.
    """
    converted_or_original_group = cid10_adequacy_group_expr(cid_sql)
    return f"CASE WHEN {cid_sql} IS NULL THEN NULL ELSE {converted_or_original_group} END"


def text_concat_expr(cols: Sequence[str]) -> Optional[str]:
    """Concatena campos textuais detectados automaticamente em uma expressão SQL única.

    Usado para procurar nomes de agentes e doenças de base que não aparecem de forma estruturada em CON_DIAGES.
    """
    clean_cols = [c for c in cols if c]
    if not clean_cols:
        return None
    parts = [f"COALESCE({clean_str_expr(c)}, '')" for c in clean_cols]
    if len(parts) == 1:
        return f"UPPER({parts[0]})"
    return "UPPER(" + " || ' | ' || ".join(parts) + ")"


def _regex_bool_expr(text_sql: Optional[str], pattern: str) -> str:
    if not text_sql:
        return "FALSE"
    return f"regexp_matches(COALESCE({text_sql}, ''), {qstr(pattern)})"


def _sinan_other_bacteria_g01_condition(bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    conditions: List[str] = []
    if bacteria_code_sql:
        g01_codes = ", ".join(qstr(code) for code in sorted(SINAN_CLA_ME_BAC_G01_CODES))
        conditions.append(f"{bacteria_code_sql} IN ({g01_codes})")
    if aux_text_sql:
        conditions.append(_regex_bool_expr(aux_text_sql, SINAN_G01_DETAIL_REGEX))
    return " OR ".join(f"({c})" for c in conditions) if conditions else "FALSE"


def _sinan_con05_detail_expr(bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    g01_condition = _sinan_other_bacteria_g01_condition(bacteria_code_sql, aux_text_sql)
    return f"""
    CASE
        WHEN ({g01_condition}) THEN 'CON_DIAGES=05 refinado como G01 por CLA_ME_BAC/texto: agente/doença bacteriana classificada em outra parte.'
        WHEN {bacteria_code_sql or 'NULL'} IS NULL THEN 'CON_DIAGES=05 sem CLA_ME_BAC detectado/preenchido: classificado conservadoramente como G00.'
        WHEN {bacteria_code_sql or 'NULL'} IN ('81') THEN 'CON_DIAGES=05 com bactéria não especificada: G00.'
        ELSE 'CON_DIAGES=05 com bactéria comum/outra bactéria: G00.'
    END
    """


def sinan_cid10_conversion_group_expr(con_code_sql: str, bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    g01_condition = _sinan_other_bacteria_g01_condition(bacteria_code_sql, aux_text_sql)
    return f"""
    CASE
        WHEN {con_code_sql} IN ('02', '03') THEN 'A39.0'
        WHEN {con_code_sql} = '04' THEN 'A17.0'
        WHEN {con_code_sql} = '05' AND ({g01_condition}) THEN 'G01'
        WHEN {con_code_sql} = '05' THEN 'G00'
        WHEN {con_code_sql} = '06' THEN 'G03'
        WHEN {con_code_sql} = '07' THEN 'A87'
        WHEN {con_code_sql} = '08' THEN 'G02'
        WHEN {con_code_sql} IN ('09', '10') THEN 'G00'
        WHEN {con_code_sql} = '01' THEN 'Não convertido — meningococcemia isolada'
        ELSE 'Sem conversão — CON_DIAGES ausente ou não mapeado'
    END
    """


def sinan_cid10_conversion_type_expr(con_code_sql: str, bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    g01_condition = _sinan_other_bacteria_g01_condition(bacteria_code_sql, aux_text_sql)
    return f"""
    CASE
        WHEN {con_code_sql} IN ('02', '03') THEN 'A39.0 — meningite meningocócica'
        WHEN {con_code_sql} = '04' THEN 'A17.0 — meningite tuberculosa'
        WHEN {con_code_sql} = '05' AND ({g01_condition}) THEN 'G01 — meningite bacteriana em doença classificada em outra parte'
        WHEN {con_code_sql} = '05' THEN 'G00 — meningite bacteriana não classificada em outra parte'
        WHEN {con_code_sql} = '06' THEN 'G03 — meningite por outras causas / não especificada'
        WHEN {con_code_sql} = '07' THEN 'A87 — meningite viral'
        WHEN {con_code_sql} = '08' THEN 'G02 — meningite em outras doenças infecciosas/parasitárias'
        WHEN {con_code_sql} IN ('09', '10') THEN 'G00 — meningite bacteriana não classificada em outra parte'
        WHEN {con_code_sql} = '01' THEN 'Não convertido — meningococcemia isolada'
        ELSE 'Sem conversão — CON_DIAGES ausente ou não mapeado'
    END
    """


def sinan_cid10_conversion_reason_expr(con_code_sql: str, bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    con05_reason = _sinan_con05_detail_expr(bacteria_code_sql, aux_text_sql)
    return f"""
    CASE
        WHEN {con_code_sql} IN ('02', '03') THEN 'Forma meningítica meningocócica: A39.0.'
        WHEN {con_code_sql} = '04' THEN 'Meningite tuberculosa: A17.0.'
        WHEN {con_code_sql} = '05' THEN ({con05_reason})
        WHEN {con_code_sql} = '06' THEN 'Meningite não especificada: G03.'
        WHEN {con_code_sql} = '07' THEN 'Meningite asséptica no SINAN: A87 como leitura operacional viral; validar etiologia quando disponível.'
        WHEN {con_code_sql} = '08' THEN 'Outra etiologia infecciosa/parasitária: G02 como família comparável; validar CLA_ME_ETI.'
        WHEN {con_code_sql} IN ('09', '10') THEN 'Haemophilus influenzae/pneumocócica: G00.'
        WHEN {con_code_sql} = '01' THEN 'Meningococcemia isolada: fora da comparação de meningite.'
        ELSE 'CON_DIAGES ausente ou sem regra.'
    END
    """


def sinan_cid10_conversion_include_expr(con_code_sql: str) -> str:
    mapped = ", ".join(qstr(code) for code in SINAN_CID10_FROM_CON_DIAGES)
    return f"CASE WHEN {con_code_sql} IN ({mapped}) THEN 'Sim' ELSE 'Não' END"



def sinan_g01_base_disease_expr(bacteria_code_sql: Optional[str] = None, aux_text_sql: Optional[str] = None) -> str:
    """Retorna a doença bacteriana de base provável quando a conversão do SINAN cai em G01.

    G01 é uma manifestação em doença bacteriana classificada em outra parte. Assim, esta
    expressão não tenta transformar todos os casos bacterianos em G01; ela só descreve a
    doença de base provável quando os mesmos sinais usados no refinamento G01 aparecem em
    CLA_ME_BAC ou em campos textuais/auxiliares.
    """
    bacteria = bacteria_code_sql or "NULL"
    salmonella_text = _regex_bool_expr(aux_text_sql, r"SALMONEL")
    listeria_text = _regex_bool_expr(aux_text_sql, r"LISTERI")
    syphilis_text = _regex_bool_expr(aux_text_sql, r"NEUROSS[ÍI]FIL|NEUROSYPH|S[ÍI]FIL|SYPHIL|TREPONEMA")
    leptospirosis_text = _regex_bool_expr(aux_text_sql, r"LEPTOSPI")
    anthrax_text = _regex_bool_expr(aux_text_sql, r"CARB[UÚ]NCULO|ANTRAZ|ANTHRAX")
    lyme_text = _regex_bool_expr(aux_text_sql, r"LYME|BORREL")
    typhoid_text = _regex_bool_expr(aux_text_sql, r"TIF[OÓ]IDE|TYPHOID")
    gonococcal_text = _regex_bool_expr(aux_text_sql, r"GONOCOC|GONOCOCO")
    return f"""
    CASE
        WHEN {bacteria} = '11' OR ({salmonella_text}) THEN 'Infecção por Salmonella sp / salmonelose invasiva — A02.2†'
        WHEN {bacteria} = '21' OR ({listeria_text}) THEN 'Listeriose / Listeria monocytogenes — A32.1†'
        WHEN {bacteria} = '45' OR ({syphilis_text}) THEN 'Sífilis / neurossífilis — A52.1†; avaliar A50.4†/A51.4† conforme contexto'
        WHEN {bacteria} = '49' OR ({leptospirosis_text}) THEN 'Leptospirose — A27.-†'
        WHEN ({anthrax_text}) THEN 'Carbúnculo / antraz — A22.8†'
        WHEN ({lyme_text}) THEN 'Doença de Lyme / borreliose — A69.2†'
        WHEN ({typhoid_text}) THEN 'Febre tifóide — A01.0†'
        WHEN ({gonococcal_text}) THEN 'Infecção gonocócica — A54.8†'
        ELSE 'G01 sem doença de base provável identificada nos campos disponíveis'
    END
    """


def age_band_expr(age_sql: str, width: int = 5) -> str:
    return f"FLOOR(({age_sql}) / {width}) * {width}"


# Granularidade etária primária das pirâmides/distribuições demográficas (NU_IDADE_N
# e derivadas). A pedido, o primeiro corte etário foi alinhado ao estrato usado na
# análise quimiocitológica do LCR (neonatos até 6 meses): abre-se uma faixa
# "até 6 meses" e uma faixa "> 6 meses e até 4 anos", mantendo as faixas quinquenais
# normais (5–9, 10–14, ...) para as demais idades. Isso torna a distribuição etária
# coerente com a estratificação neonatal do LCR. Observação: o corte em 6 meses só
# distingue corretamente lactentes quando a idade vem em resolução sub-anual (idade
# DATASUS codificada em meses/dias); quando a base traz idade apenas em anos inteiros,
# todos os menores de 1 ano caem em "até 6 meses" — limitação inerente ao dado.
_AGE_PYRAMID_BAND_6M_LABEL = "até 6 meses"
_AGE_PYRAMID_BAND_6M_4Y_LABEL = "> 6 meses e até 4 anos"
_AGE_PYRAMID_6M_CUTOFF = 0.5  # 6 meses em anos fracionários (= SINAN_LCR_NEONATAL_CUTOFF_YEARS)


def _age_pyramid_band_label_sql(age_alias: str = "idade") -> str:
    return f"""
    CASE
        WHEN {age_alias} < 0 OR {age_alias} > 130 THEN NULL
        WHEN {age_alias} < {_AGE_PYRAMID_6M_CUTOFF} THEN '{_AGE_PYRAMID_BAND_6M_LABEL}'
        WHEN {age_alias} < 5 THEN '{_AGE_PYRAMID_BAND_6M_4Y_LABEL}'
        ELSE
            CAST(CAST(FLOOR({age_alias} / 5) * 5 AS INTEGER) AS VARCHAR)
            || '–' ||
            CAST(CAST(FLOOR({age_alias} / 5) * 5 + 4 AS INTEGER) AS VARCHAR)
    END
    """


def _age_pyramid_band_order_sql(age_alias: str = "idade") -> str:
    return f"""
    CASE
        WHEN {age_alias} < 0 OR {age_alias} > 130 THEN NULL
        WHEN {age_alias} < {_AGE_PYRAMID_6M_CUTOFF} THEN 0
        WHEN {age_alias} < 5 THEN 1
        ELSE CAST(FLOOR({age_alias} / 5) * 5 AS INTEGER)
    END
    """


def choose_candidate(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    if not columns:
        return None
    norm_to_col = {normalize_name(c): c for c in columns}
    for cand in candidates:
        if normalize_name(cand) in norm_to_col:
            return norm_to_col[normalize_name(cand)]
    cand_norms = [normalize_name(c) for c in candidates]
    for col in columns:
        ncol = normalize_name(col)
        if any(cn in ncol or ncol in cn for cn in cand_norms):
            return col
    return None


def choose_candidates(columns: Sequence[str], candidates: Sequence[str], max_items: int = 12) -> List[str]:
    result: List[str] = []
    norm_to_col = {normalize_name(c): c for c in columns}
    for cand in candidates:
        col = norm_to_col.get(normalize_name(cand))
        if col and col not in result:
            result.append(col)
    for col in columns:
        if len(result) >= max_items:
            break
        if col in result:
            continue
        ncol = normalize_name(col)
        if any(normalize_name(cand) in ncol or ncol in normalize_name(cand) for cand in candidates):
            result.append(col)
    return result[:max_items]


def sql_where(clauses: Iterable[Optional[str]]) -> str:
    valid = [c.strip() for c in clauses if c and c.strip()]
    return "" if not valid else "WHERE " + " AND ".join(f"({c})" for c in valid)


def append_clause(where_sql: str, clause: Optional[str]) -> str:
    if not clause:
        return where_sql
    if not where_sql:
        return f"WHERE ({clause})"
    return where_sql + f" AND ({clause})"


def after_where_keyword(where_sql: str) -> str:
    if not where_sql:
        return "1=1"
    return where_sql.replace("WHERE", "", 1).strip()


def pct_expr(numer: str, denom: str) -> str:
    return f"CASE WHEN {denom} > 0 THEN ROUND(100.0 * ({numer}) / ({denom}), 2) ELSE NULL END"


def first_existing_path(filename: str) -> str:
    candidates = [Path.cwd() / filename, Path("/mnt/data") / filename, Path(__file__).parent / filename]
    for p in candidates:
        if p.exists():
            return str(p)
    return filename


def safe_filename(text: str) -> str:
    n = normalize_name(text).lower()
    return n or "saida"



# =============================================================================
# Integração com assets Parquet da release do GitHub
# =============================================================================


def github_release_download_url(asset_name: str) -> str:
    encoded_name = urllib.parse.quote(asset_name, safe="")
    return (
        f"https://github.com/{GITHUB_RELEASE_OWNER}/{GITHUB_RELEASE_REPO}/"
        f"releases/download/{GITHUB_RELEASE_TAG}/{encoded_name}"
    )


def _asset_source(asset_name: str) -> str:
    upper = asset_name.upper()
    for source, prefix in GITHUB_RELEASE_SOURCE_PREFIX.items():
        if upper.startswith(prefix.upper()):
            return source
    return "OUTROS"


def _asset_year(asset_name: str) -> Optional[int]:
    match = re.search(r"(?:19|20)\d{2}", asset_name)
    if not match:
        return None
    return int(match.group(0))


def _format_bytes(size: object) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _github_request(url: str, accept: str = "application/vnd.github+json") -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "meningite-streamlit-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _normalise_github_asset(raw: Dict[str, object]) -> Dict[str, object]:
    name = str(raw.get("name") or "").strip()
    return {
        "name": name,
        "source": _asset_source(name),
        "year": _asset_year(name),
        "size": raw.get("size"),
        "digest": raw.get("digest") or raw.get("sha256") or "",
        "download_url": raw.get("browser_download_url") or github_release_download_url(name),
        "updated_at": raw.get("updated_at") or raw.get("created_at") or "",
    }


@st.cache_data(show_spinner=False, ttl=3600)
def list_github_release_parquets() -> List[Dict[str, object]]:
    """Lista os Parquets da release GitHub.

    Usa a API pública do GitHub quando disponível, tenta a página HTML expandida
    como segunda opção e, por fim, usa a lista esperada da Release1. A lista de
    fallback evita que o painel quebre em ambiente com rate limit temporário da API.
    """
    assets: List[Dict[str, object]] = []

    try:
        payload = json.loads(_github_request(GITHUB_RELEASE_API_URL))
        for item in payload.get("assets", []):
            name = str(item.get("name") or "")
            if name.lower().endswith(".parquet"):
                assets.append(_normalise_github_asset(item))
    except Exception:
        assets = []

    if not assets:
        try:
            html = _github_request(GITHUB_RELEASE_EXPANDED_ASSETS_URL, accept="text/html")
            seen = set()
            pattern = r'href="([^"]*/releases/download/[^"]+?\.parquet)"[^>]*>\s*([^<]+\.parquet)\s*</a>'
            for href, raw_name in re.findall(pattern, html, flags=re.IGNORECASE):
                name = html_lib.unescape(raw_name).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                url = href if href.startswith("http") else f"https://github.com{href}"
                assets.append(_normalise_github_asset({"name": name, "browser_download_url": url}))
        except Exception:
            assets = []

    if not assets:
        assets = [_normalise_github_asset({"name": name}) for name in GITHUB_RELEASE_FALLBACK_PARQUETS]

    source_order = {"SINAN": 0, "SIM": 1, "CIHA": 2, "OUTROS": 9}
    return sorted(
        assets,
        key=lambda asset: (
            source_order.get(str(asset.get("source")), 9),
            asset.get("year") or 9999,
            str(asset.get("name")),
        ),
    )


def github_asset_label(asset: Dict[str, object]) -> str:
    year = asset.get("year")
    name = str(asset.get("name") or "")
    size = _format_bytes(asset.get("size"))
    prefix = f"{year} — " if year else ""
    suffix = f" ({size})" if size else ""
    return f"{prefix}{name}{suffix}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def download_github_release_asset_to_path(asset_name: str, download_url: str, out_path: Path, digest: str = "") -> None:
    """Baixa um asset para disco em streaming, sem manter o Parquet inteiro em memória."""
    req = urllib.request.Request(
        download_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "meningite-streamlit-dashboard",
        },
    )
    expected_sha256 = ""
    if digest and str(digest).startswith("sha256:"):
        expected_sha256 = str(digest).split(":", 1)[1].lower()

    tmp_path = out_path.with_suffix(out_path.suffix + ".download")
    h = hashlib.sha256() if expected_sha256 else None
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, tmp_path.open("wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if h is not None:
                    h.update(chunk)

        if total == 0:
            raise ValueError(f"Download vazio para {asset_name}.")

        if expected_sha256:
            observed = h.hexdigest().lower() if h is not None else ""
            if observed != expected_sha256:
                raise ValueError(
                    f"SHA-256 divergente para {asset_name}: esperado {expected_sha256}, obtido {observed}."
                )

        tmp_path.replace(out_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


@st.cache_data(show_spinner=False, ttl=24 * 3600)
def materialize_github_release_asset_cached(asset_name: str, download_url: str, digest: str = "") -> str:
    digest_key = hashlib.sha1(f"{GITHUB_RELEASE_TAG}|{download_url}|{digest}".encode("utf-8")).hexdigest()[:16]
    out_dir = Path(tempfile.gettempdir()) / "meningite_github_release"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{safe_filename(Path(asset_name).stem)}_{digest_key}.parquet"

    should_download = not out.exists() or out.stat().st_size == 0
    if not should_download and digest and str(digest).startswith("sha256:"):
        expected = str(digest).split(":", 1)[1].lower()
        should_download = _sha256_file(out) != expected

    if should_download:
        download_github_release_asset_to_path(asset_name, download_url, out, digest)
    return str(out)


def materialize_github_release_asset(asset: Dict[str, object]) -> str:
    name = str(asset.get("name") or "")
    if not name:
        raise ValueError("Asset GitHub sem nome.")
    url = str(asset.get("download_url") or github_release_download_url(name))
    digest = str(asset.get("digest") or "")
    return materialize_github_release_asset_cached(name, url, digest)


def github_selection_summary(assets: Sequence[Dict[str, object]]) -> str:
    years = sorted({a.get("year") for a in assets if a.get("year")})
    if not years:
        return f"{len(assets)} parquet(s)"
    if len(years) == 1:
        return f"{len(assets)} parquet(s), ano {years[0]}"
    return f"{len(assets)} parquet(s), {years[0]}–{years[-1]}"


# =============================================================================
# Tabelas carregadas e consultas
# =============================================================================


@dataclass
class LoadedTable:
    source: str
    kind: str  # duckdb | parquet
    ref_sql: str
    db_path: Optional[str] = None
    table_name: Optional[str] = None
    parquet_paths: Optional[List[str]] = None
    label: str = ""


@st.cache_data(show_spinner=False)
def list_duckdb_tables(path: str) -> List[str]:
    runtime_settings = (
        DEFAULT_DUCKDB_MEMORY_LIMIT,
        DEFAULT_DUCKDB_THREADS,
        str(Path(tempfile.gettempdir()) / DUCKDB_TEMP_SUBDIR),
    )
    con = open_duckdb_connection(path, read_only=True, runtime_settings=runtime_settings)
    try:
        return [r[0] for r in con.execute("SHOW TABLES").fetchall()]
    finally:
        con.close()


def parquet_ref(paths: Sequence[str]) -> str:
    quoted = ", ".join(qstr(p) for p in paths)
    return f"read_parquet([{quoted}], union_by_name=true)"


def materialize_upload(upload, namespace: str) -> str:
    """Materializa upload em arquivo temporário sem duplicar todo o conteúdo em memória."""
    upload_id = str(getattr(upload, "file_id", "") or "")
    upload_size = str(getattr(upload, "size", "") or "")
    session_key = ""
    if upload_id:
        session_key = f"materialized_upload::{namespace}::{upload_id}::{upload.name}::{upload_size}"
        cached_path = st.session_state.get(session_key)
        if cached_path and Path(str(cached_path)).exists():
            return str(cached_path)

    suffix = Path(upload.name).suffix or ".dat"
    clean_name = safe_filename(Path(upload.name).stem)
    temp_dir = Path(tempfile.gettempdir())
    temp_dir.mkdir(parents=True, exist_ok=True)

    digest_obj = hashlib.sha1()
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"meningite_{namespace}_{clean_name}_",
        suffix=f"{suffix}.tmp",
        dir=temp_dir,
        delete=False,
    )
    tmp_path = Path(tmp.name)

    try:
        upload.seek(0)
        with tmp:
            while True:
                chunk = upload.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                digest_obj.update(chunk)
                tmp.write(chunk)
        upload.seek(0)

        digest = digest_obj.hexdigest()[:16]
        out = temp_dir / f"meningite_{namespace}_{clean_name}_{digest}{suffix}"
        if out.exists():
            tmp_path.unlink(missing_ok=True)
        else:
            tmp_path.replace(out)
        if session_key:
            st.session_state[session_key] = str(out)
        return str(out)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        try:
            upload.seek(0)
        except Exception:
            pass
        raise


def _safe_duckdb_memory_limit(value: object) -> str:
    """Normaliza limite de memória aceito pelo DuckDB, com fallback seguro."""
    text = str(value or DEFAULT_DUCKDB_MEMORY_LIMIT).strip().upper().replace(" ", "")
    if re.fullmatch(r"(?:[1-9]\d*)(?:\.\d+)?(?:KB|MB|GB|TB)|(?:[1-9]\d?|100)%", text):
        return text
    return DEFAULT_DUCKDB_MEMORY_LIMIT


# =============================================================================
# Conversão de CSV e DBF para Parquet
# -----------------------------------------------------------------------------
# CSV e DBF não são consultados diretamente: são convertidos uma única vez (com
# cache em disco baseado no hash do arquivo enviado) para Parquet e, a partir
# daí, seguem exatamente o mesmo caminho de carga/consulta já usado pelos
# Parquets nativos (LoadedTable(kind="parquet", ...)). Isso garante que todas
# as análises do app — incluindo a sobreposição de NU_NOTIFIC/NM_PACIENT —
# funcionem de forma idêntica, independentemente do formato de origem.
#
# Todas as colunas são lidas/gravadas como texto (VARCHAR). Campos do
# DATASUS/SINAN como NU_NOTIFIC, CEP, CON_DIAGES etc. podem ter zeros à
# esquerda (ex.: "0007") que seriam perdidos se o tipo fosse inferido como
# número; manter tudo como VARCHAR preserva o valor original e evita falsas
# divergências na verificação de sobreposição.
# =============================================================================

CSV_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "cp1252", "latin1")
DBF_ENCODING_CANDIDATES = ("cp850", "cp1252", "latin1", "utf-8")


def _sniff_csv_delimiter(sample_text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=";,|\t")
        return dialect.delimiter
    except Exception:
        first_line = sample_text.splitlines()[0] if sample_text else ""
        counts = {sep: first_line.count(sep) for sep in (";", ",", "|", "\t")}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def _detect_csv_encoding_and_delimiter(path: str) -> Tuple[str, str]:
    """Detecta encoding e delimitador lendo apenas uma amostra do início do arquivo."""
    sample_bytes = Path(path).open("rb").read(65536)
    for enc in CSV_ENCODING_CANDIDATES:
        try:
            sample_text = sample_bytes.decode(enc)
            delim = _sniff_csv_delimiter(sample_text)
            return enc, delim
        except UnicodeDecodeError:
            continue
    # último recurso: latin1 nunca falha ao decodificar
    sample_text = sample_bytes.decode("latin1", errors="replace")
    return "latin1", _sniff_csv_delimiter(sample_text)


def convert_csv_to_parquet(csv_path: str, namespace: str) -> str:
    """Converte um CSV enviado para Parquet (todas as colunas como VARCHAR), com cache em disco.

    Usa o próprio DuckDB (read_csv) para a leitura, que é robusto a arquivos grandes e detecta
    cabeçalho automaticamente; encoding e delimitador são detectados antes, por amostragem.
    """
    src = Path(csv_path)
    digest = hashlib.sha1(src.read_bytes()[:1 << 20]).hexdigest()[:16] if src.stat().st_size < (1 << 27) else None
    if digest is None:
        # arquivos muito grandes: hash incremental para não carregar tudo em memória
        h = hashlib.sha1()
        with src.open("rb") as fobj:
            while True:
                chunk = fobj.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        digest = h.hexdigest()[:16]

    temp_dir = Path(tempfile.gettempdir())
    temp_dir.mkdir(parents=True, exist_ok=True)
    clean_name = safe_filename(src.stem)
    out_path = temp_dir / f"meningite_{namespace}_{clean_name}_{digest}_from_csv.parquet"
    if out_path.exists():
        return str(out_path)

    encoding, delimiter = _detect_csv_encoding_and_delimiter(str(src))
    # O parâmetro encoding do read_csv só existe a partir do DuckDB 1.2.0 e só aceita 'utf-8'
    # (padrão), 'latin-1' ou 'utf-16'. Para UTF-8 omitimos o parâmetro (funciona em qualquer
    # versão); para os demais, mapeamos para 'latin-1' (cp1252/latin1 são compatíveis o
    # suficiente para os caracteres usados em arquivos do DATASUS). Se a versão do DuckDB
    # disponível não reconhecer o parâmetro, ou o arquivo não for estritamente válido no
    # encoding informado, o except abaixo aciona o fallback via pandas.
    encoding_clause = "" if encoding in ("utf-8", "utf-8-sig") else f", encoding={qstr('latin-1')}"

    con = open_duckdb_connection(runtime_settings=duckdb_runtime_settings())
    try:
        read_csv_sql = (
            f"read_csv({qstr(str(src))}, delim={qstr(delimiter)}, header=true, "
            f"all_varchar=true{encoding_clause}, "
            "ignore_errors=true, null_padding=true, sample_size=-1)"
        )
        con.execute(
            f"COPY (SELECT * FROM {read_csv_sql}) TO {qstr(str(out_path))} (FORMAT PARQUET)"
        )
    except Exception:
        # fallback: leitura via pandas (mais tolerante a CSVs irregulares e a encodings que o
        # DuckDB instalado não reconheça), depois grava Parquet via DuckDB
        try:
            df = pd.read_csv(
                str(src),
                sep=delimiter,
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
                na_values=[""],
                engine="python",
                on_bad_lines="skip",
            )
        except UnicodeDecodeError:
            df = pd.read_csv(
                str(src),
                sep=delimiter,
                encoding=encoding,
                encoding_errors="replace",
                dtype=str,
                keep_default_na=False,
                na_values=[""],
                engine="python",
                on_bad_lines="skip",
            )
        con.register("df_csv_fallback", df)
        con.execute(f"COPY (SELECT * FROM df_csv_fallback) TO {qstr(str(out_path))} (FORMAT PARQUET)")
        con.unregister("df_csv_fallback")
    finally:
        con.close()
    return str(out_path)


def _detect_dbf_encoding(path: str) -> str:
    for enc in DBF_ENCODING_CANDIDATES:
        try:
            table = _DBFReader(path, encoding=enc, char_decode_errors="strict", load=False)
            # força a leitura de alguns registros para validar o encoding escolhido
            for i, _ in enumerate(table):
                if i >= 50:
                    break
            return enc
        except (UnicodeDecodeError, ValueError):
            continue
        except Exception:
            continue
    return "latin1"


def convert_dbf_to_parquet(dbf_path: str, namespace: str) -> str:
    """Converte um DBF enviado (típico do SINAN/DATASUS) para Parquet, todas as colunas como texto.

    Usa dbfread, lendo registro a registro para não carregar o DBF inteiro em memória de uma vez,
    e grava em lotes via DuckDB. O encoding (geralmente cp850 em arquivos do DATASUS) é detectado
    automaticamente entre os candidatos mais comuns.
    """
    if _DBFReader is None:
        raise RuntimeError(
            "Suporte a DBF requer o pacote 'dbfread' (pip install dbfread), que não está instalado neste ambiente."
        )

    src = Path(dbf_path)
    h = hashlib.sha1()
    with src.open("rb") as fobj:
        while True:
            chunk = fobj.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()[:16]

    temp_dir = Path(tempfile.gettempdir())
    temp_dir.mkdir(parents=True, exist_ok=True)
    clean_name = safe_filename(src.stem)
    out_path = temp_dir / f"meningite_{namespace}_{clean_name}_{digest}_from_dbf.parquet"
    if out_path.exists():
        return str(out_path)

    encoding = _detect_dbf_encoding(str(src))
    table = _DBFReader(str(src), encoding=encoding, char_decode_errors="replace", load=False)
    fieldnames = [f.name for f in table.fields if f.name != "DeletionFlag"]

    con = open_duckdb_connection(runtime_settings=duckdb_runtime_settings())
    try:
        batch_rows: List[Dict[str, Optional[str]]] = []
        batch_size = 50000
        first_batch = True
        for record in table:
            row = {}
            for name in fieldnames:
                value = record.get(name)
                if value is None:
                    row[name] = None
                elif isinstance(value, bytes):
                    row[name] = value.decode(encoding, errors="replace").strip()
                else:
                    row[name] = str(value).strip()
            batch_rows.append(row)
            if len(batch_rows) >= batch_size:
                batch_df = pd.DataFrame(batch_rows, columns=fieldnames, dtype="object")
                con.register("dbf_batch", batch_df)
                if first_batch:
                    con.execute(f"CREATE TABLE dbf_accum AS SELECT * FROM dbf_batch")
                    first_batch = False
                else:
                    con.execute("INSERT INTO dbf_accum SELECT * FROM dbf_batch")
                con.unregister("dbf_batch")
                batch_rows = []
        if batch_rows or first_batch:
            batch_df = pd.DataFrame(batch_rows, columns=fieldnames, dtype="object")
            con.register("dbf_batch", batch_df)
            if first_batch:
                con.execute("CREATE TABLE dbf_accum AS SELECT * FROM dbf_batch")
            else:
                con.execute("INSERT INTO dbf_accum SELECT * FROM dbf_batch")
            con.unregister("dbf_batch")
        con.execute(f"COPY dbf_accum TO {qstr(str(out_path))} (FORMAT PARQUET)")
    finally:
        con.close()
    return str(out_path)


def duckdb_runtime_settings() -> Tuple[str, int, str]:
    """Configuração leve para reduzir picos de memória em consultas Parquet/DuckDB."""
    memory_limit = _safe_duckdb_memory_limit(
        st.session_state.get("perf_duckdb_memory_limit", DEFAULT_DUCKDB_MEMORY_LIMIT)
    )
    threads = max(1, perf_int("perf_duckdb_threads", DEFAULT_DUCKDB_THREADS))
    temp_dir = str(Path(tempfile.gettempdir()) / DUCKDB_TEMP_SUBDIR)
    return memory_limit, threads, temp_dir


def configure_duckdb_connection(
    con: duckdb.DuckDBPyConnection,
    runtime_settings: Optional[Tuple[str, int, str]] = None,
) -> None:
    """Aplica limites defensivos ao DuckDB sem interromper o app se uma opção falhar."""
    memory_limit, threads, temp_dir = runtime_settings or duckdb_runtime_settings()
    temp_path = Path(temp_dir)
    try:
        temp_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        temp_path = Path(tempfile.gettempdir())

    statements = [
        f"SET memory_limit={qstr(memory_limit)}",
        f"SET threads={int(max(1, threads))}",
        f"SET temp_directory={qstr(str(temp_path))}",
        "SET preserve_insertion_order=false",
        "SET enable_object_cache=true",
    ]
    for statement in statements:
        try:
            con.execute(statement)
        except Exception:
            # Algumas versões/ambientes bloqueiam opções específicas; a consulta deve seguir funcionando.
            pass


def open_duckdb_connection(
    db_path: Optional[str] = None,
    read_only: bool = False,
    runtime_settings: Optional[Tuple[str, int, str]] = None,
) -> duckdb.DuckDBPyConnection:
    """Abre conexão DuckDB com spill para disco e limite de memória configurável."""
    if db_path:
        con = duckdb.connect(db_path, read_only=read_only)
    else:
        con = duckdb.connect(database=":memory:")
    configure_duckdb_connection(con, runtime_settings)
    return con


def _file_fingerprint(path: Optional[str]) -> Tuple[str, Optional[int], Optional[int]]:
    if not path:
        return "", None, None
    try:
        stat = Path(path).stat()
        return str(path), int(stat.st_size), int(stat.st_mtime_ns)
    except OSError:
        return str(path), None, None


def table_cache_key(table: LoadedTable) -> Tuple[object, ...]:
    """Chave leve para invalidar cache quando arquivos mudam."""
    parquet_meta = tuple(_file_fingerprint(path) for path in (table.parquet_paths or []))
    duckdb_meta = _file_fingerprint(table.db_path)
    return (table.kind, table.ref_sql, duckdb_meta, parquet_meta)


# =============================================================================
# Conexão DuckDB persistente + materialização de Parquets
# -----------------------------------------------------------------------------
# Antes, cada consulta abria uma conexão :memory: nova e embutia
# read_parquet([...]) no FROM, re-decodificando os mesmos Parquets a cada
# query (dezenas por render). Agora mantemos UMA conexão in-memory viva entre
# reruns (cache_resource) e materializamos cada base Parquet em uma tabela
# nativa do DuckDB uma única vez. As consultas seguintes leem armazenamento
# colunar nativo (sem reparse de Parquet, com predicate pushdown e zone maps).
# =============================================================================

class _SharedDB:
    """Encapsula a conexão in-memory persistente, o registro de objetos já
    materializados e o lock de DDL — todos com o mesmo ciclo de vida.

    Usar um único objeto cacheado evita atribuir atributos no tipo C do DuckDB
    e garante que conexão e registro nunca fiquem dessincronizados (ex.: se um
    fosse despejado do cache e o outro não).
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self.con = con
        self.registry: Dict[str, str] = {}
        self.lock = threading.Lock()


@st.cache_resource(show_spinner=False)
def get_duckdb_file_db(
    db_path: str,
    runtime_settings: Tuple[str, int, str],
    file_fingerprint: Tuple[str, Optional[int], Optional[int]],
) -> "_SharedDB":
    """Mantém conexão read-only de DuckDB entre consultas da mesma base.

    O fingerprint entra na chave do cache; se o arquivo mudar, a conexão antiga
    deixa de ser reutilizada para as novas consultas.
    """
    con = duckdb.connect(db_path, read_only=True)
    configure_duckdb_connection(con, runtime_settings)
    return _SharedDB(con)


def _query_duckdb_file(db_path: Optional[str], sql: str, runtime_settings: Tuple[str, int, str]) -> pd.DataFrame:
    if not db_path:
        return pd.DataFrame()
    shared = get_duckdb_file_db(db_path, runtime_settings, _file_fingerprint(db_path))
    _ensure_municipios_ibge_view(shared)
    with shared.lock:
        cur = shared.con.cursor()
        try:
            return cur.execute(sql).df()
        finally:
            cur.close()


def parquet_object_name(source: str, paths: Sequence[str]) -> str:
    """Identificador estável da base Parquet, derivado do conteúdo dos arquivos.

    Inclui caminho + tamanho + mtime no hash: se qualquer arquivo mudar, o nome
    muda, forçando a recriação da tabela materializada e invalidando o cache.
    """
    fingerprints = [_file_fingerprint(p) for p in (paths or [])]
    raw = json.dumps([source, fingerprints], sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    safe_source = re.sub(r"[^0-9A-Za-z]+", "_", str(source)) or "src"
    return f"pq_{safe_source}_{digest}"



def _fastparquet_available() -> bool:
    """Retorna se fastparquet foi importado com sucesso no ambiente atual."""
    return fp is not None


def _should_use_fastparquet() -> bool:
    """Controla o uso de fastparquet na etapa de materialização Parquet -> DuckDB."""
    return bool(st.session_state.get("perf_use_fastparquet", True))


def _fastparquet_row_limit() -> int:
    """Limite defensivo de linhas para evitar carregar Parquets grandes demais em pandas."""
    limit = perf_int("perf_fastparquet_row_limit", DEFAULT_FASTPARQUET_ROW_LIMIT)
    return max(1000, int(limit))


def _fastparquet_file_rows(path: str) -> Optional[int]:
    """Lê apenas metadados de um arquivo Parquet com fastparquet para estimar linhas."""
    if fp is None:
        return None
    try:
        pf = fp.ParquetFile(path)
        row_groups = getattr(pf, "row_groups", None) or []
        total = 0
        for row_group in row_groups:
            total += int(getattr(row_group, "num_rows", 0) or 0)
        if total > 0:
            return total
        info = getattr(pf, "info", None)
        if isinstance(info, dict):
            rows = info.get("rows") or info.get("num_rows")
            if rows is not None:
                return int(rows)
    except Exception:
        return None
    return None


def _fastparquet_total_rows(paths: Sequence[str]) -> Optional[int]:
    """Soma metadados de linhas quando disponíveis; retorna None se a estimativa falhar."""
    total = 0
    for path in paths:
        rows = _fastparquet_file_rows(str(path))
        if rows is None:
            return None
        total += int(rows)
    return total


def fastparquet_status() -> str:
    """Texto curto exibido na barra lateral sobre o motor de leitura Parquet."""
    if not _should_use_fastparquet():
        return "fastparquet desativado: Parquets serão materializados pelo leitor nativo do DuckDB ou usados como VIEW."
    if not _fastparquet_available():
        return "fastparquet não está instalado neste ambiente; o app fará fallback automático para DuckDB read_parquet."
    return (
        "fastparquet ativo para materialização de Parquets até "
        f"{_fastparquet_row_limit():,} linhas estimadas; DuckDB permanece como motor SQL."
    ).replace(",", ".")


def _load_parquets_fastparquet(paths: Sequence[str]) -> pd.DataFrame:
    """Lê Parquets com fastparquet e une por nome de coluna, preservando fallback externo."""
    if fp is None:
        raise RuntimeError("fastparquet não está instalado")
    frames: List[pd.DataFrame] = []
    for path in paths:
        parquet_file = fp.ParquetFile(str(path))
        frames.append(parquet_file.to_pandas())
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0]
    return pd.concat(frames, ignore_index=True, sort=False, copy=False)


def _can_try_fastparquet(paths: Sequence[str]) -> bool:
    """Decide se vale tentar fastparquet antes do leitor nativo do DuckDB."""
    if not _should_use_fastparquet() or not _fastparquet_available():
        return False
    total_rows = _fastparquet_total_rows(paths)
    return total_rows is None or total_rows <= _fastparquet_row_limit()

def _should_materialize() -> bool:
    """Materializar Parquet em tabela nativa (padrão) ou usar VIEW (lazy)."""
    return bool(st.session_state.get("perf_materialize_tables", True))


@st.cache_resource(show_spinner=False)
def get_shared_db(runtime_settings: Tuple[str, int, str]) -> "_SharedDB":
    """Conexão in-memory única, reutilizada entre reruns e consultas.

    Persistir a conexão evita reconectar, reconfigurar PRAGMAs e relistar
    arquivos a cada query, além de manter o catálogo (tabelas materializadas)
    e o cache de metadados de Parquet aquecidos.
    """
    con = duckdb.connect(database=":memory:")
    configure_duckdb_connection(con, runtime_settings)
    return _SharedDB(con)


def _ensure_parquet_object(
    shared: "_SharedDB",
    name: str,
    paths: Sequence[str],
    materialize: bool,
) -> None:
    """Garante que `name` exista na conexão como tabela ou VIEW.

    Se materialização estiver ativa, tenta fastparquet primeiro. O fastparquet
    decodifica os Parquets para pandas; o DataFrame é registrado no DuckDB e
    transformado em tabela nativa. Se houver incompatibilidade de ambiente,
    tipo, volume ou memória, o código cai para o read_parquet nativo do DuckDB.
    """
    desired_kind = (
        "table_fastparquet" if materialize and _should_use_fastparquet()
        else "table_duckdb" if materialize
        else "view"
    )
    if shared.registry.get(name) == desired_kind:
        return
    with shared.lock:
        if shared.registry.get(name) == desired_kind:
            return
        con = shared.con
        ident = qident(name)
        src = parquet_ref(paths)
        # Remove qualquer objeto anterior de mesmo nome (ex.: troca tabela<->view/fastparquet).
        for drop_stmt in (f"DROP VIEW IF EXISTS {ident}", f"DROP TABLE IF EXISTS {ident}"):
            try:
                con.execute(drop_stmt)
            except Exception:
                pass

        made: Optional[str] = None
        if materialize and _can_try_fastparquet(paths):
            temp_view = f"__fp_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}"
            df_fastparquet: Optional[pd.DataFrame] = None
            try:
                df_fastparquet = _load_parquets_fastparquet(paths)
                con.register(temp_view, df_fastparquet)
                con.execute(f"CREATE TABLE {ident} AS SELECT * FROM {qident(temp_view)}")
                made = "table_fastparquet"
            except Exception as exc:
                st.session_state[f"fastparquet_fallback::{name}"] = str(exc)
                made = None
            finally:
                try:
                    con.unregister(temp_view)
                except Exception:
                    pass
                try:
                    del df_fastparquet
                except Exception:
                    pass

        if materialize and made is None:
            try:
                con.execute(f"CREATE TABLE {ident} AS SELECT * FROM {src}")
                made = "table_duckdb"
            except Exception:
                made = None  # fallback para VIEW abaixo
        if made is None:
            con.execute(f"CREATE VIEW {ident} AS SELECT * FROM {src}")
            made = "view"

        # O registro usa a configuração desejada para evitar reprocessar a mesma base.
        # Se fastparquet falhou e houve fallback válido, a tabela/VIEW permanece pronta.
        shared.registry[name] = desired_kind if materialize else made

def _prepare_parquet(table: LoadedTable, runtime_settings: Tuple[str, int, str]) -> None:
    """Registra a base Parquet na conexão persistente antes de consultar."""
    if table.kind == "parquet" and table.parquet_paths:
        shared = get_shared_db(runtime_settings)
        _ensure_parquet_object(shared, table.ref_sql, table.parquet_paths, _should_materialize())


def _query_shared(sql: str, runtime_settings: Tuple[str, int, str]) -> pd.DataFrame:
    """Executa na conexão persistente usando um cursor isolado (seguro p/ threads)."""
    shared = get_shared_db(runtime_settings)
    _ensure_municipios_ibge_view(shared)
    cur = shared.con.cursor()
    try:
        return cur.execute(sql).df()
    finally:
        cur.close()


def _execute_query_uncached(table: LoadedTable, sql: str) -> pd.DataFrame:
    runtime_settings = duckdb_runtime_settings()
    if table.kind == "duckdb":
        return _query_duckdb_file(table.db_path, sql, runtime_settings)
    return _query_shared(sql, runtime_settings)


@st.cache_data(show_spinner=False, ttl=1800, max_entries=DEFAULT_QUERY_CACHE_MAX_ENTRIES)
def _run_query_cached(
    table_key: Tuple[object, ...],
    kind: str,
    db_path: Optional[str],
    sql: str,
    runtime_settings: Tuple[str, int, str],
) -> pd.DataFrame:
    if kind == "duckdb":
        return _query_duckdb_file(db_path, sql, runtime_settings)
    return _query_shared(sql, runtime_settings)


def run_query(table: LoadedTable, sql: str, cache: bool = True) -> pd.DataFrame:
    """Executa SQL; por padrão cacheia apenas resultados de consulta agregada/pequena."""
    runtime_settings = duckdb_runtime_settings()
    _prepare_parquet(table, runtime_settings)
    if cache:
        return _run_query_cached(
            table_cache_key(table),
            table.kind,
            table.db_path,
            sql,
            runtime_settings,
        )
    return _execute_query_uncached(table, sql)


def schema_df(table: LoadedTable) -> pd.DataFrame:
    sql = f"DESCRIBE SELECT * FROM {table.ref_sql}"
    df = run_query(table, sql)
    if "column_name" in df.columns:
        keep = [c for c in ["column_name", "column_type", "null"] if c in df.columns]
        return df[keep].rename(columns={"column_name": "coluna", "column_type": "tipo", "null": "nulo"})
    return df


def count_rows(table: LoadedTable, where_sql: str = "") -> int:
    df = run_query(table, f"SELECT COUNT(*) AS n FROM {table.ref_sql} {where_sql}")
    return int(df.iloc[0, 0]) if not df.empty else 0


def top_values(table: LoadedTable, expr: str, where_sql: str = "", limit: int = 40) -> List[str]:
    if not expr:
        return []
    clause = append_clause(where_sql, f"{expr} IS NOT NULL")
    sql = f"""
        SELECT {expr} AS valor, COUNT(*) AS n
        FROM {table.ref_sql}
        {clause}
        GROUP BY 1
        ORDER BY n DESC, valor
        LIMIT {int(limit)}
    """
    try:
        df = run_query(table, sql)
    except Exception:
        return []
    return [str(x) for x in df["valor"].dropna().tolist()]


def minmax_date(table: LoadedTable, dt_sql: Optional[str], where_sql: str = "") -> Optional[Tuple[pd.Timestamp, pd.Timestamp]]:
    if not dt_sql:
        return None
    clause = append_clause(where_sql, f"{dt_sql} IS NOT NULL")
    sql = f"SELECT MIN({dt_sql}) AS dt_min, MAX({dt_sql}) AS dt_max FROM {table.ref_sql} {clause}"
    df = run_query(table, sql)
    if df.empty or pd.isna(df.iloc[0, 0]) or pd.isna(df.iloc[0, 1]):
        return None
    return pd.to_datetime(df.iloc[0, 0]), pd.to_datetime(df.iloc[0, 1])


# =============================================================================
# Seleção de colunas e expressões por fonte
# =============================================================================


@dataclass
class ColumnSelection:
    date_col: Optional[str]
    sex_col: Optional[str]
    age_col: Optional[str]
    age_unit_col: Optional[str]
    race_col: Optional[str]
    municipality_res_col: Optional[str]
    municipality_event_col: Optional[str]
    cid_cols: List[str]
    age_mode: str
    education_col: Optional[str] = None
    # SINAN
    classi_fin_col: Optional[str] = None
    con_diages_col: Optional[str] = None
    cla_me_bac_col: Optional[str] = None
    cla_me_ass_col: Optional[str] = None
    cla_me_eti_col: Optional[str] = None
    sinan_auxiliary_cid10_cols: Optional[List[str]] = None
    evolucao_col: Optional[str] = None
    criterio_col: Optional[str] = None
    lab_puncao_col: Optional[str] = None
    lab_liquor_col: Optional[str] = None
    lab_aspect_col: Optional[str] = None
    lab_hema_col: Optional[str] = None
    lab_neutro_col: Optional[str] = None
    lab_glico_col: Optional[str] = None
    lab_leuco_col: Optional[str] = None
    lab_eosi_col: Optional[str] = None
    lab_prot_col: Optional[str] = None
    lab_mono_col: Optional[str] = None
    lab_linfo_col: Optional[str] = None
    lab_clor_col: Optional[str] = None
    ate_hospit_col: Optional[str] = None
    dt_encerramento_col: Optional[str] = None
    dt_notificacao_col: Optional[str] = None
    dt_sin_pri_col: Optional[str] = None
    dt_puncao_col: Optional[str] = None
    # SIM
    causabas_col: Optional[str] = None
    causabas_o_col: Optional[str] = None
    obitograv_col: Optional[str] = None
    obitopuerp_col: Optional[str] = None
    # CIHA
    diag_princ_col: Optional[str] = None
    diag_secun_col: Optional[str] = None
    morte_col: Optional[str] = None
    dias_perm_col: Optional[str] = None
    modalidade_col: Optional[str] = None
    procedimento_col: Optional[str] = None


def default_selections(source: str, columns: Sequence[str]) -> ColumnSelection:
    cfg = SOURCE_CONFIG[source]
    date_col = choose_candidate(columns, cfg.date_candidates)
    sex_col = choose_candidate(columns, cfg.sex_candidates)
    age_col = choose_candidate(columns, cfg.age_candidates)
    age_unit_col = choose_candidate(columns, cfg.age_unit_candidates)
    race_col = choose_candidate(columns, cfg.race_candidates)
    mun_res_col = choose_candidate(columns, cfg.municipality_res_candidates)
    mun_event_col = choose_candidate(columns, cfg.municipality_event_candidates)
    cid_cols = choose_candidates(columns, cfg.cid_candidates, max_items=10)
    age_mode = "Automático"
    if source == "CIHA" and age_col and age_unit_col:
        age_mode = "DATASUS com coluna de unidade"
    elif source in {"SINAN", "SIM"} and age_col:
        age_mode = "DATASUS codificada"

    sel = ColumnSelection(
        date_col=date_col,
        sex_col=sex_col,
        age_col=age_col,
        age_unit_col=age_unit_col,
        race_col=race_col,
        municipality_res_col=mun_res_col,
        municipality_event_col=mun_event_col,
        cid_cols=cid_cols,
        age_mode=age_mode,
    )
    education_candidates = {
        "SINAN": ["CS_ESCOL_N", "ESCOLARIDADE", "ESCOLARI", "CS_ESCOL", "ESCOL_N"],
        "SIM": ["ESC2010", "ESC", "ESCOLARIDADE", "ESCOLARI", "ESCFALAGR1"],
        "CIHA": ["ESCOLARIDADE", "ESCOLARI", "ESC2010", "ESC", "INSTRUCAO", "GRAU_INSTRUCAO", "NIVEL_INSTRUCAO"],
    }.get(source, [])
    sel.education_col = choose_candidate(columns, education_candidates)
    if source == "SINAN":
        sel.classi_fin_col = choose_candidate(columns, ["CLASSI_FIN"])
        sel.con_diages_col = choose_candidate(columns, ["CON_DIAGES"])
        sel.cla_me_bac_col = choose_candidate(columns, ["CLA_ME_BAC", "CLASSIFICACAO_BACTERIA", "CLASS_BACTERIA"])
        sel.cla_me_ass_col = choose_candidate(columns, ["CLA_ME_ASS", "CLASSIFICACAO_ASSEPTICA", "CLASS_ASSEPTICA"])
        sel.cla_me_eti_col = choose_candidate(columns, ["CLA_ME_ETI", "CLASSIFICACAO_ETIOLOGIA", "CLASS_ETIOLOGIA"])
        sel.sinan_auxiliary_cid10_cols = choose_candidates(columns, SINAN_AUXILIARY_CID10_CANDIDATES, max_items=12)
        sel.evolucao_col = choose_candidate(columns, ["EVOLUCAO"])
        sel.criterio_col = choose_candidate(columns, ["CRITERIO"])
        sel.lab_puncao_col = choose_candidate(columns, ["LAB_PUNCAO", "PUNCAO", "PUNCAO_LCR", "PUNCAO_LOMBAR"])
        sel.lab_liquor_col = choose_candidate(columns, ["LAB_LIQUOR", "LIQUOR", "QUIMIOCITOLOGICO", "EXAME_QUIMIOCITOLOGICO", "EXAME_LIQUOR"])
        sel.lab_aspect_col = choose_candidate(columns, ["LAB_ASPECT", "ASPECTO_LIQUOR", "TP_ASPECTOR_LIQUOR", "ASPECTO"])
        sel.lab_hema_col = choose_candidate(columns, ["LAB_HEMA", "HEMACIAS", "NU_HEMACIAS"])
        sel.lab_neutro_col = choose_candidate(columns, ["LAB_NEUTRO", "NEUTROFILOS", "NU_NEUTROFILO", "NU_NEUTROFILOS"])
        sel.lab_glico_col = choose_candidate(columns, ["LAB_GLICO", "GLICOSE", "NU_GLICOSE"])
        sel.lab_leuco_col = choose_candidate(columns, ["LAB_LEUCO", "LEUCOCITOS", "NU_LEUCOCITO", "NU_LEUCOCITOS"])
        sel.lab_eosi_col = choose_candidate(columns, ["LAB_EOSI", "EOSINOFILOS", "NU_EOSINOFILO", "NU_EOSINOFILOS"])
        sel.lab_prot_col = choose_candidate(columns, ["LAB_PROT", "PROTEINAS", "PROTEINA", "NU_PROTEINA", "NU_PROTEINAS"])
        sel.lab_mono_col = choose_candidate(columns, ["LAB_MONO", "MONOCITOS", "NU_MONOCITO", "NU_MONOCITOS"])
        sel.lab_linfo_col = choose_candidate(columns, ["LAB_LINFO", "LINFOCITOS", "NU_LINFOCITO", "NU_LINFOCITOS"])
        sel.lab_clor_col = choose_candidate(columns, ["LAB_CLOR", "CLORETO", "CLORETOS", "NU_CLORETO", "NU_CLORETOS"])
        sel.ate_hospit_col = choose_candidate(columns, ["ATE_HOSPIT"])
        sel.dt_encerramento_col = choose_candidate(columns, ["DT_ENCERRA"])
        sel.dt_notificacao_col = choose_candidate(columns, ["DT_NOTIFIC"])
        sel.dt_sin_pri_col = choose_candidate(columns, ["DT_SIN_PRI", "DATA_PRIMEIROS_SINTOMAS", "DT_PRIMEIROS_SINTOMAS", "DATA_SIN_PRI", "INICIO_SINTOMAS"])
        sel.dt_puncao_col = choose_candidate(columns, ["DT_PUNCA", "DT_PUNCAO", "DATA_PUNCAO", "DATA_DA_PUNCAO", "DT_PUNCAO_LOMBAR", "DATA_PUNCAO_LOMBAR", "LAB_DTPUNC", "LAB_DTPUN", "DT_COLETA_LCR", "DATA_COLETA_LCR"])
    elif source == "SIM":
        sel.causabas_col = choose_candidate(columns, ["CAUSABAS"])
        sel.causabas_o_col = choose_candidate(columns, ["CAUSABAS_O"])
        sel.obitograv_col = choose_candidate(columns, ["OBITOGRAV", "OBITO_GRAV", "OBITO_GRAVIDEZ", "GRAVIDEZ"])
        sel.obitopuerp_col = choose_candidate(columns, ["OBITOPUERP", "OBITO_PUERP", "PUERPERIO", "PUERP"])
    elif source == "CIHA":
        sel.diag_princ_col = choose_candidate(columns, ["DIAG_PRINC"])
        sel.diag_secun_col = choose_candidate(columns, ["DIAG_SECUN"])
        sel.morte_col = choose_candidate(columns, ["MORTE"])
        sel.dias_perm_col = choose_candidate(columns, ["DIAS_PERM"])
        sel.modalidade_col = choose_candidate(columns, ["MODALIDADE"])
        sel.procedimento_col = choose_candidate(columns, ["PROC_REA", "PROC_REALIZADO", "PROCEDIMENTO", "PROCED", "COD_PROC", "COD_PROCEDIMENTO", "PROC_SOLIC", "PROC_ID", "PROC_PRINC", "PROCEDIMENTO_PRINCIPAL"])
    return sel


def build_age_sql(sel: ColumnSelection) -> Optional[str]:
    if not sel.age_col:
        return None
    if sel.age_mode == "Anos diretos":
        return direct_age_expr(sel.age_col)
    if sel.age_mode == "DATASUS com coluna de unidade" and sel.age_unit_col:
        return age_with_unit_expr(sel.age_col, sel.age_unit_col)
    if sel.age_mode == "DATASUS codificada":
        return datasus_age_expr(sel.age_col)
    if sel.age_unit_col:
        return f"COALESCE({age_with_unit_expr(sel.age_col, sel.age_unit_col)}, {datasus_age_expr(sel.age_col)}, {direct_age_expr(sel.age_col)})"
    return f"COALESCE({datasus_age_expr(sel.age_col)}, {direct_age_expr(sel.age_col)})"


def sinan_quimio_code_expr(sel: ColumnSelection) -> Optional[str]:
    """Detecta realização do exame quimiocitológico do LCR.

    Algumas bases não trazem um campo explícito LAB_LIQUOR. Quando esse campo
    não existe, o painel infere realização do quimiocitológico pela presença de
    pelo menos um parâmetro do LCR preenchido (LAB_HEMA, LAB_NEUTRO, LAB_GLICO,
    LAB_LEUCO, LAB_EOSI, LAB_PROT, LAB_MONO, LAB_LINFO ou LAB_CLOR). Assim, o
    gráfico deixa de falhar apenas porque o indicador nominal não foi detectado.
    """
    if sel.lab_liquor_col:
        return clean_code_expr(sel.lab_liquor_col)
    param_cols = [
        sel.lab_hema_col,
        sel.lab_neutro_col,
        sel.lab_glico_col,
        sel.lab_leuco_col,
        sel.lab_eosi_col,
        sel.lab_prot_col,
        sel.lab_mono_col,
        sel.lab_linfo_col,
        sel.lab_clor_col,
    ]
    tests: List[str] = []
    for param_key, col in zip(["hema", "neutro", "glico", "leuco", "eosi", "prot", "mono", "linfo", "clor"], param_cols):
        if not col:
            continue
        value_expr = sinan_lcr_clean_value_expr(numeric_expr(col), param_key)
        tests.append(f"(({value_expr}) IS NOT NULL AND ({value_expr}) >= 0)")
    if not tests:
        return None
    return "CASE WHEN " + " OR ".join(tests) + " THEN '1' ELSE '2' END"


def build_expressions(source: str, sel: ColumnSelection) -> Dict[str, Optional[str]]:
    exprs: Dict[str, Optional[str]] = {
        "dt": date_expr(sel.date_col) if sel.date_col else None,
        "sex": sex_expr(sel.sex_col) if sel.sex_col else None,
        "age": build_age_sql(sel),
        "race": case_from_mapping(clean_code_expr(sel.race_col), RACA_COR, "Sem informação/ignorado") if sel.race_col else None,
        "education": education_label_expr(source, sel.education_col) if sel.education_col else None,
        "mun_res": clean_str_expr(sel.municipality_res_col) if sel.municipality_res_col else None,
        "mun_event": clean_str_expr(sel.municipality_event_col) if sel.municipality_event_col else None,
        "mun_res_label": municipality_display_expr(sel.municipality_res_col) if sel.municipality_res_col else None,
        "mun_event_label": municipality_display_expr(sel.municipality_event_col) if sel.municipality_event_col else None,
        "cid": cid_extract_expr(sel.cid_cols),
        "cid_source": cid_source_expr(sel.cid_cols),
        "cid_g01_present": cid_presence_expr(sel.cid_cols, CID_G01_PRESENT_REGEX),
        "cid_g02_present": cid_presence_expr(sel.cid_cols, CID_G02_PRESENT_REGEX),
    }
    if exprs["cid"]:
        exprs["cid_group"] = cid_group_expr(exprs["cid"])
        exprs["cid_type"] = cid_type_expr(exprs["cid"])
        exprs["cid10_adequacy_group"] = cid10_adequacy_group_expr(exprs["cid"])
        exprs["cid10_adequacy_type"] = cid10_adequacy_type_expr(exprs["cid"])
        exprs["cid10_adequacy_status"] = cid10_adequacy_status_expr(exprs["cid"])
        exprs["cid10_adequacy_reason"] = cid10_adequacy_reason_expr(exprs["cid"])
        exprs["cid10_adequacy_plot_label"] = cid10_adequacy_plot_label_expr(exprs["cid"])
    else:
        exprs["cid_group"] = None
        exprs["cid_type"] = None
        exprs["cid10_adequacy_group"] = None
        exprs["cid10_adequacy_type"] = None
        exprs["cid10_adequacy_status"] = None
        exprs["cid10_adequacy_reason"] = None
        exprs["cid10_adequacy_plot_label"] = None

    if source == "SINAN":
        exprs["classi_code"] = clean_code_expr(sel.classi_fin_col) if sel.classi_fin_col else None
        exprs["classi_label"] = case_from_mapping(exprs["classi_code"], SINAN_CLASSI_FIN, "Sem classificação / ignorados") if exprs["classi_code"] else None
        exprs["evol_code"] = clean_code_expr(sel.evolucao_col) if sel.evolucao_col else None
        exprs["evol_label"] = case_from_mapping(exprs["evol_code"], SINAN_EVOLUCAO, "Sem evolução/ignorado") if exprs["evol_code"] else None
        exprs["con_code"] = clean_code_expr(sel.con_diages_col, pad2=True) if sel.con_diages_col else None
        exprs["con_label"] = case_from_mapping(exprs["con_code"], SINAN_CON_DIAGES, "Sem conclusão diagnóstica/ignorado") if exprs["con_code"] else None
        exprs["con_group"] = case_from_mapping(exprs["con_code"], SINAN_CON_GROUP, "Sem conclusão diagnóstica/ignorado") if exprs["con_code"] else None
        exprs["cla_me_bac_code"] = clean_code_expr(sel.cla_me_bac_col, pad2=True) if sel.cla_me_bac_col else None
        exprs["cla_me_bac_label"] = case_from_mapping(exprs["cla_me_bac_code"], SINAN_CLA_ME_BAC, "Sem bactéria especificada/ignorado") if exprs["cla_me_bac_code"] else None
        exprs["cla_me_ass_code"] = clean_code_expr(sel.cla_me_ass_col, pad2=True) if sel.cla_me_ass_col else None
        exprs["cla_me_ass_label"] = case_from_mapping(exprs["cla_me_ass_code"], SINAN_CLA_ME_ASS, "Sem agente viral/asséptico especificado") if exprs["cla_me_ass_code"] else None
        exprs["cla_me_eti_code"] = clean_code_expr(sel.cla_me_eti_col, pad2=True) if sel.cla_me_eti_col else None
        exprs["cla_me_eti_label"] = case_from_mapping(exprs["cla_me_eti_code"], SINAN_CLA_ME_ETI, "Sem outra etiologia especificada") if exprs["cla_me_eti_code"] else None
        exprs["sinan_aux_text"] = text_concat_expr(sel.sinan_auxiliary_cid10_cols or [])
        if exprs["con_code"]:
            exprs["sinan_cid10_conversion_group"] = sinan_cid10_conversion_group_expr(exprs["con_code"], exprs.get("cla_me_bac_code"), exprs.get("sinan_aux_text"))
            exprs["sinan_cid10_conversion_type"] = sinan_cid10_conversion_type_expr(exprs["con_code"], exprs.get("cla_me_bac_code"), exprs.get("sinan_aux_text"))
            exprs["sinan_cid10_conversion_reason"] = sinan_cid10_conversion_reason_expr(exprs["con_code"], exprs.get("cla_me_bac_code"), exprs.get("sinan_aux_text"))
            exprs["sinan_cid10_conversion_include"] = sinan_cid10_conversion_include_expr(exprs["con_code"])
            exprs["sinan_g01_base_disease"] = sinan_g01_base_disease_expr(exprs.get("cla_me_bac_code"), exprs.get("sinan_aux_text"))
        else:
            exprs["sinan_cid10_conversion_group"] = None
            exprs["sinan_cid10_conversion_type"] = None
            exprs["sinan_cid10_conversion_reason"] = None
            exprs["sinan_cid10_conversion_include"] = None
            exprs["sinan_g01_base_disease"] = None
        exprs["criterio_code"] = clean_code_expr(sel.criterio_col) if sel.criterio_col else None
        exprs["criterio_label"] = case_from_mapping(exprs["criterio_code"], SINAN_CRITERIO, "Sem critério/ignorado") if exprs["criterio_code"] else None
        exprs["expected_etiology_group"] = (
            sinan_expected_etiology_group_expr(exprs["con_code"], exprs.get("cla_me_eti_code"))
            if exprs.get("con_code")
            else None
        )
        exprs["puncao_code"] = clean_code_expr(sel.lab_puncao_col) if sel.lab_puncao_col else None
        exprs["puncao_label"] = case_from_mapping(exprs["puncao_code"], YES_NO_IGN, "Sem informação") if exprs["puncao_code"] else None
        exprs["quimio_code"] = sinan_quimio_code_expr(sel)
        exprs["quimio_label"] = case_from_mapping(exprs["quimio_code"], YES_NO_IGN, "Sem informação") if exprs["quimio_code"] else None
        exprs["quimio_inferred_from_params"] = "1" if exprs["quimio_code"] and not sel.lab_liquor_col else None
        exprs["lab_aspect_code"] = clean_code_expr(sel.lab_aspect_col) if sel.lab_aspect_col else None
        exprs["lab_aspect_label"] = case_from_mapping(exprs["lab_aspect_code"], SINAN_LAB_ASPECT, "Sem informação/ignorado") if exprs["lab_aspect_code"] else None
        exprs["lab_hema"] = numeric_expr(sel.lab_hema_col) if sel.lab_hema_col else None
        exprs["lab_neutro"] = numeric_expr(sel.lab_neutro_col) if sel.lab_neutro_col else None
        exprs["lab_glico"] = numeric_expr(sel.lab_glico_col) if sel.lab_glico_col else None
        exprs["lab_leuco"] = numeric_expr(sel.lab_leuco_col) if sel.lab_leuco_col else None
        exprs["lab_eosi"] = numeric_expr(sel.lab_eosi_col) if sel.lab_eosi_col else None
        exprs["lab_prot"] = numeric_expr(sel.lab_prot_col) if sel.lab_prot_col else None
        exprs["lab_mono"] = numeric_expr(sel.lab_mono_col) if sel.lab_mono_col else None
        exprs["lab_linfo"] = numeric_expr(sel.lab_linfo_col) if sel.lab_linfo_col else None
        exprs["lab_clor"] = numeric_expr(sel.lab_clor_col) if sel.lab_clor_col else None
        exprs["hospital_label"] = case_from_mapping(clean_code_expr(sel.ate_hospit_col), YES_NO_IGN, "Sem informação") if sel.ate_hospit_col else None
        exprs["dt_encerramento"] = date_expr(sel.dt_encerramento_col) if sel.dt_encerramento_col else None
        exprs["dt_notificacao"] = date_expr(sel.dt_notificacao_col) if sel.dt_notificacao_col else None
        exprs["dt_sin_pri"] = date_expr(sel.dt_sin_pri_col) if sel.dt_sin_pri_col else None
        exprs["dt_puncao"] = date_expr(sel.dt_puncao_col) if sel.dt_puncao_col else None
    elif source == "SIM":
        exprs["causabas_cid"] = cid_extract_expr([sel.causabas_col] if sel.causabas_col else [])
        exprs["causabas_o_cid"] = cid_extract_expr([sel.causabas_o_col] if sel.causabas_o_col else [])
        exprs["causabas_group"] = cid_group_expr(exprs["causabas_cid"]) if exprs["causabas_cid"] else None
        exprs["causabas_type"] = cid_type_expr(exprs["causabas_cid"]) if exprs["causabas_cid"] else None
        exprs["lococor_label"] = case_from_mapping(clean_code_expr("LOCOCOR"), SIM_LOCOCOR, "Sem informação/ignorado") if "LOCOCOR" in [sel.municipality_event_col, sel.municipality_res_col] else None
        exprs["obitograv_code"] = clean_code_expr(sel.obitograv_col) if sel.obitograv_col else None
        exprs["obitograv_label"] = case_from_mapping(exprs["obitograv_code"], SIM_OBITOGRAV, "Sem informação/ignorado") if exprs.get("obitograv_code") else None
        exprs["obitopuerp_code"] = clean_code_expr(sel.obitopuerp_col) if sel.obitopuerp_col else None
        exprs["obitopuerp_label"] = case_from_mapping(exprs["obitopuerp_code"], SIM_OBITOPUERP, "Sem informação/ignorado") if exprs.get("obitopuerp_code") else None
    elif source == "CIHA":
        exprs["diag_princ_cid"] = cid_extract_expr([sel.diag_princ_col] if sel.diag_princ_col else [])
        exprs["diag_secun_cid"] = cid_extract_expr([sel.diag_secun_col] if sel.diag_secun_col else [])
        exprs["diag_princ_type"] = cid_type_expr(exprs["diag_princ_cid"]) if exprs["diag_princ_cid"] else None
        exprs["morte_code"] = clean_code_expr(sel.morte_col) if sel.morte_col else None
        exprs["dias_perm"] = direct_age_expr(sel.dias_perm_col) if sel.dias_perm_col else None
        exprs["modalidade_label"] = case_from_mapping(clean_code_expr(sel.modalidade_col, pad2=True), CIHA_MODALIDADE, "Sem modalidade/ignorado") if sel.modalidade_col else None
        exprs["procedimento_label"] = clean_str_expr(sel.procedimento_col) if sel.procedimento_col else None
    return exprs


# =============================================================================
# Queries analíticas
# =============================================================================


def query_timeseries(table: LoadedTable, dt_sql: str, where_sql: str, freq: str, category_sql: Optional[str] = None) -> pd.DataFrame:
    if category_sql:
        cat_sql = category_label_expr(category_sql)
        sql = f"""
            WITH base AS (
                SELECT {dt_sql} AS dt, {cat_sql} AS categoria
                FROM {table.ref_sql}
                {where_sql}
            )
            SELECT date_trunc({qstr(freq)}, dt) AS periodo, categoria, COUNT(*) AS n
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
        """
    else:
        sql = f"""
            WITH base AS (
                SELECT {dt_sql} AS dt
                FROM {table.ref_sql}
                {where_sql}
            )
            SELECT date_trunc({qstr(freq)}, dt) AS periodo, COUNT(*) AS n
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """
    return run_query(table, sql)


def query_heatmap(table: LoadedTable, dt_sql: str, where_sql: str, freq: str = "month") -> pd.DataFrame:
    period_expr = "EXTRACT(WEEK FROM dt)" if freq == "week" else "EXTRACT(MONTH FROM dt)"
    period_alias = "semana" if freq == "week" else "mes"
    sql = f"""
        WITH base AS (
            SELECT {dt_sql} AS dt
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT EXTRACT(YEAR FROM dt) AS ano, {period_expr} AS {period_alias}, COUNT(*) AS n
        FROM base
        WHERE dt IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
    """
    return run_query(table, sql)


def query_category(table: LoadedTable, category_sql: str, where_sql: str, top_n: int = 30) -> pd.DataFrame:
    cat_sql = category_label_expr(category_sql)
    sql = f"""
        SELECT {cat_sql} AS categoria, COUNT(*) AS n
        FROM {table.ref_sql}
        {where_sql}
        GROUP BY 1
        ORDER BY n DESC, categoria
        LIMIT {int(top_n)}
    """
    df = run_query(table, sql)
    if not df.empty:
        df["pct"] = (df["n"] / df["n"].sum() * 100).round(2)
    return df


def query_field_coverage(table: LoadedTable, field_sql: str, where_sql: str) -> pd.DataFrame:
    """Total, preenchidos, ausentes e cobertura para subtítulos de gráficos."""
    sql = f"""
        WITH base AS (
            SELECT {field_sql} AS valor
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT COUNT(*) AS n_total,
               COUNT(*) FILTER (WHERE valor IS NOT NULL) AS n_preenchido,
               COUNT(*) FILTER (WHERE valor IS NULL) AS n_ausente,
               {pct_expr("COUNT(*) FILTER (WHERE valor IS NOT NULL)", "COUNT(*)")} AS pct_cobertura
        FROM base
    """
    return run_query(table, sql)


def coverage_subtitle_from_df(df: pd.DataFrame) -> str:
    if df.empty:
        return "N total=0; preenchido=0; ausente=0; cobertura=—"
    row = df.iloc[0]
    total = int(row.get("n_total", 0) or 0)
    filled = int(row.get("n_preenchido", 0) or 0)
    missing = int(row.get("n_ausente", 0) or 0)
    pct = row.get("pct_cobertura")
    pct_text = "—" if pd.isna(pct) else f"{float(pct):.2f}%".replace(".", ",")
    return f"N preenchido={format_int_br(filled)}; N ausente={format_int_br(missing)}; cobertura={pct_text}; N total={format_int_br(total)}"


def render_field_completeness_warning(coverage_df: pd.DataFrame, field_label: str, threshold_pct: float = 30.0) -> None:
    """Aviso automático quando o campo-base de um gráfico tem alta ausência.

    Recebe o dataframe de `query_field_coverage` e emite um `st.warning` quando o
    percentual de ausência supera o limiar (padrão 30%). Complementa — não
    substitui — o subtítulo de cobertura, tornando explícito no próprio gráfico
    que a distribuição dos preenchidos é uma subamostra, não o total.
    """
    if coverage_df is None or coverage_df.empty:
        return
    row = coverage_df.iloc[0]
    total = int(row.get("n_total", 0) or 0)
    missing = int(row.get("n_ausente", 0) or 0)
    if total <= 0:
        return
    pct_missing = 100.0 * missing / total
    if pct_missing >= threshold_pct:
        st.warning(
            f"⚠️ {field_label}: {format_int_br(missing)} de {format_int_br(total)} registros "
            f"({pct_missing:.1f}%) sem informação no recorte atual. Leia a distribuição dos "
            "preenchidos como subamostra, não como o total filtrado."
        )


def query_field_presence(
    table: LoadedTable,
    field_sql: str,
    where_sql: str,
    present_label: str = "Sim — informado",
    absent_label: str = "Não — ausente/sem informação",
) -> pd.DataFrame:
    """Distribuição binária de presença/preenchimento de um campo no recorte informado."""
    sql = f"""
        WITH base AS (
            SELECT {field_sql} AS valor
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE valor IS NOT NULL) AS n_preenchido,
                   COUNT(*) FILTER (WHERE valor IS NULL) AS n_ausente
            FROM base
        )
        SELECT {qstr(present_label)} AS categoria,
               n_preenchido AS n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n_preenchido / denominador, 2) ELSE NULL END AS pct,
               1 AS ordem
        FROM agg
        UNION ALL
        SELECT {qstr(absent_label)} AS categoria,
               n_ausente AS n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n_ausente / denominador, 2) ELSE NULL END AS pct,
               2 AS ordem
        FROM agg
        ORDER BY ordem
    """
    return run_query(table, sql)


def query_category_top_with_outros(table: LoadedTable, category_sql: str, where_sql: str, top_n: int = 15, outros_label: str = "Outros municípios") -> pd.DataFrame:
    cat_sql = category_label_expr(category_sql)
    sql = f"""
        WITH counts AS (
            SELECT {cat_sql} AS categoria, COUNT(*) AS n
            FROM {table.ref_sql}
            {where_sql}
            GROUP BY 1
        ), ranked AS (
            SELECT categoria, n,
                   ROW_NUMBER() OVER (ORDER BY n DESC, categoria) AS rn,
                   SUM(n) OVER () AS denominador
            FROM counts
        ), grouped AS (
            SELECT CASE WHEN rn <= {int(top_n)} THEN categoria ELSE {qstr(outros_label)} END AS categoria,
                   SUM(n) AS n,
                   MAX(denominador) AS denominador,
                   CASE WHEN MIN(rn) <= {int(top_n)} THEN MIN(rn) ELSE 999999 END AS ordem
            FROM ranked
            GROUP BY 1
        )
        SELECT categoria, n, denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM grouped
        ORDER BY ordem, n DESC, categoria
    """
    return run_query(table, sql)


def query_sinan_education_outcomes(
    table: LoadedTable,
    education_sql: str,
    classi_sql: str,
    where_sql: str,
) -> pd.DataFrame:
    escolaridade_labels = education_category_labels("SINAN", include_missing=True)
    cats_cte = values_cte_from_labels(escolaridade_labels, "escolaridade", "ordem_escolaridade")
    group_values = ", ".join(
        f"({qstr(label)}, {idx})"
        for idx, label in enumerate(["Casos confirmados", "Casos descartados"], start=1)
    )
    sql = f"""
        WITH base AS (
            SELECT
                COALESCE({education_sql}, 'Sem informação/ignorado') AS escolaridade,
                {classi_sql} AS classi
            FROM {table.ref_sql}
            {where_sql}
        ), categorias AS (
            {cats_cte}
        ), grupos_ref AS (
            SELECT * FROM (VALUES {group_values}) AS t(grupo, ordem_grupo)
        ), grid AS (
            SELECT g.grupo, g.ordem_grupo, c.escolaridade, c.ordem_escolaridade
            FROM grupos_ref g
            CROSS JOIN categorias c
        ), grupos AS (
            SELECT 'Casos confirmados' AS grupo, escolaridade
            FROM base
            WHERE classi = '1'
            UNION ALL
            SELECT 'Casos descartados' AS grupo, escolaridade
            FROM base
            WHERE classi = '2'
        ), counts AS (
            SELECT grupo, escolaridade, COUNT(*) AS n
            FROM grupos
            GROUP BY 1, 2
        ), totals AS (
            SELECT grupo, COUNT(*) AS denominador
            FROM grupos
            GROUP BY 1
        )
        SELECT
            grid.grupo,
            grid.escolaridade,
            COALESCE(counts.n, 0) AS n,
            COALESCE(totals.denominador, 0) AS denominador,
            CASE WHEN COALESCE(totals.denominador, 0) > 0
                 THEN ROUND(100.0 * COALESCE(counts.n, 0) / totals.denominador, 2)
                 ELSE NULL END AS pct,
            grid.ordem_escolaridade,
            grid.ordem_grupo
        FROM grid
        LEFT JOIN counts
          ON counts.grupo = grid.grupo
         AND counts.escolaridade = grid.escolaridade
        LEFT JOIN totals
          ON totals.grupo = grid.grupo
        ORDER BY grid.ordem_escolaridade, grid.ordem_grupo
    """
    return run_query(table, sql)


SINAN_OUTCOME_GROUP_ORDER = ["Casos confirmados", "Casos descartados", "Sem classificação / ignorados"]


def query_sinan_category_outcomes(
    table: LoadedTable,
    category_sql: str,
    classi_sql: str,
    where_sql: str,
    category_col: str = "categoria",
    default_label: str = "Sem informação/ignorado",
    top_n: Optional[int] = None,
) -> pd.DataFrame:
    """Distribui uma categoria (ex.: sexo, raça/cor) por confirmados, descartados e sem classificação/ignorados.

    'Sem classificação / ignorados' inclui tanto CLASSI_FIN ausente quanto qualquer valor
    diferente de '1' (confirmado) e '2' (descartado), preservando a mesma variável usada
    nos demais indicadores do SINAN para esse grupo.
    """
    cat_ident = qident(category_col)
    base_sql = f"""
        SELECT
            COALESCE({category_sql}, {qstr(default_label)}) AS {cat_ident},
            {classi_sql} AS classi
        FROM {table.ref_sql}
        {where_sql}
    """
    if top_n:
        categorias_cte = f"""
            categorias AS (
                SELECT {cat_ident}, ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, {cat_ident}) AS ordem_categoria
                FROM ({base_sql}) base_para_top
                GROUP BY 1
                LIMIT {int(top_n)}
            )
        """
    else:
        categorias_cte = f"""
            categorias AS (
                SELECT DISTINCT {cat_ident}, DENSE_RANK() OVER (ORDER BY {cat_ident}) AS ordem_categoria
                FROM ({base_sql}) base_para_categorias
            )
        """
    group_values = ", ".join(
        f"({qstr(label)}, {idx})"
        for idx, label in enumerate(SINAN_OUTCOME_GROUP_ORDER, start=1)
    )
    sql = f"""
        WITH base AS (
            {base_sql}
        ), {categorias_cte}, grupos_ref AS (
            SELECT * FROM (VALUES {group_values}) AS t(grupo, ordem_grupo)
        ), grid AS (
            SELECT g.grupo, g.ordem_grupo, c.{cat_ident}, c.ordem_categoria
            FROM grupos_ref g
            CROSS JOIN categorias c
        ), grupos AS (
            SELECT 'Casos confirmados' AS grupo, {cat_ident}
            FROM base
            WHERE classi = '1'
            UNION ALL
            SELECT 'Casos descartados' AS grupo, {cat_ident}
            FROM base
            WHERE classi = '2'
            UNION ALL
            SELECT 'Sem classificação / ignorados' AS grupo, {cat_ident}
            FROM base
            WHERE classi IS NULL OR classi NOT IN ('1', '2')
        ), counts AS (
            SELECT grupo, {cat_ident}, COUNT(*) AS n
            FROM grupos
            GROUP BY 1, 2
        ), totals AS (
            SELECT grupo, COUNT(*) AS denominador
            FROM grupos
            GROUP BY 1
        )
        SELECT
            grid.grupo,
            grid.{cat_ident},
            COALESCE(counts.n, 0) AS n,
            COALESCE(totals.denominador, 0) AS denominador,
            CASE WHEN COALESCE(totals.denominador, 0) > 0
                 THEN ROUND(100.0 * COALESCE(counts.n, 0) / totals.denominador, 2)
                 ELSE NULL END AS pct,
            grid.ordem_categoria,
            grid.ordem_grupo
        FROM grid
        LEFT JOIN counts
          ON counts.grupo = grid.grupo
         AND counts.{cat_ident} = grid.{cat_ident}
        LEFT JOIN totals
          ON totals.grupo = grid.grupo
        ORDER BY grid.ordem_categoria, grid.ordem_grupo
    """
    return run_query(table, sql)


def _education_labels_are_fixed(source: str) -> bool:
    """Indica se a fonte possui lista fechada de categorias de escolaridade."""
    return source in {"SINAN", "SIM"}


def _age_stratum_label_sql(age_alias: str = "idade") -> str:
    """Rótulo de faixa etária quinquenal a partir de uma coluna/alias em anos."""
    return f"""
    CASE
        WHEN {age_alias} BETWEEN 0 AND 130 THEN
            CAST(CAST(FLOOR({age_alias} / 5) * 5 AS INTEGER) AS VARCHAR)
            || '–' ||
            CAST(CAST(FLOOR({age_alias} / 5) * 5 + 4 AS INTEGER) AS VARCHAR)
        ELSE 'Idade sem informação/inválida'
    END
    """


def _age_stratum_order_sql(age_alias: str = "idade") -> str:
    """Ordem numérica da faixa etária quinquenal; valores sem idade ficam no final."""
    return f"""
    CASE
        WHEN {age_alias} BETWEEN 0 AND 130 THEN CAST(FLOOR({age_alias} / 5) * 5 AS INTEGER)
        ELSE 9999
    END
    """


def query_education_distribution_all_categories(
    table: LoadedTable,
    source: str,
    education_sql: str,
    where_sql: str,
) -> pd.DataFrame:
    fixed_labels = _education_labels_are_fixed(source)
    if fixed_labels:
        labels = education_category_labels(source, education_sql, include_missing=True)
        categorias_cte = values_cte_from_labels(labels, "categoria", "ordem_categoria")
    else:
        categorias_cte = """
            SELECT categoria, ROW_NUMBER() OVER (ORDER BY n DESC, categoria) AS ordem_categoria
            FROM (
                SELECT categoria, COUNT(*) AS n
                FROM base
                GROUP BY 1
            ) categorias_dinamicas
        """
    sql = f"""
        WITH base AS (
            SELECT COALESCE({education_sql}, 'Sem informação/ignorado') AS categoria
            FROM {table.ref_sql}
            {where_sql}
        ), categorias AS (
            {categorias_cte}
        ), counts AS (
            SELECT categoria, COUNT(*) AS n
            FROM base
            GROUP BY 1
        ), total AS (
            SELECT COUNT(*) AS denominador
            FROM base
        )
        SELECT
            categorias.categoria,
            COALESCE(counts.n, 0) AS n,
            total.denominador,
            CASE WHEN total.denominador > 0
                 THEN ROUND(100.0 * COALESCE(counts.n, 0) / total.denominador, 2)
                 ELSE NULL END AS pct,
            categorias.ordem_categoria
        FROM categorias
        CROSS JOIN total
        LEFT JOIN counts USING (categoria)
        ORDER BY categorias.ordem_categoria
    """
    return run_query(table, sql)


def query_education_distribution_by_age(
    table: LoadedTable,
    source: str,
    education_sql: str,
    age_sql: str,
    where_sql: str,
) -> pd.DataFrame:
    """Distribuição de escolaridade por faixa etária quinquenal.

    Para SINAN e SIM, preserva as categorias operacionais fechadas de escolaridade.
    Para CIHA, usa as categorias existentes no campo detectado, pois não há lista
    padronizada no app equivalente às tabelas de SINAN/SIM.
    """
    fixed_labels = _education_labels_are_fixed(source)
    if fixed_labels:
        labels = education_category_labels(source, education_sql, include_missing=True)
        categorias_cte = values_cte_from_labels(labels, "categoria", "ordem_categoria")
    else:
        categorias_cte = """
            SELECT categoria, ROW_NUMBER() OVER (ORDER BY n DESC, categoria) AS ordem_categoria
            FROM (
                SELECT categoria, COUNT(*) AS n
                FROM base
                GROUP BY 1
            ) categorias_dinamicas
        """
    faixa_label = _age_stratum_label_sql("idade")
    faixa_order = _age_stratum_order_sql("idade")
    sql = f"""
        WITH raw AS (
            SELECT
                COALESCE({education_sql}, 'Sem informação/ignorado') AS categoria,
                {age_sql} AS idade
            FROM {table.ref_sql}
            {where_sql}
        ), base AS (
            SELECT
                categoria,
                {faixa_label} AS faixa_etaria,
                {faixa_order} AS faixa_ini
            FROM raw
        ), categorias AS (
            {categorias_cte}
        ), faixas AS (
            SELECT faixa_etaria, faixa_ini
            FROM base
            GROUP BY 1, 2
        ), grid AS (
            SELECT c.categoria, c.ordem_categoria, f.faixa_etaria, f.faixa_ini
            FROM categorias c
            CROSS JOIN faixas f
        ), counts AS (
            SELECT categoria, faixa_etaria, faixa_ini, COUNT(*) AS n
            FROM base
            GROUP BY 1, 2, 3
        ), totals AS (
            SELECT faixa_etaria, faixa_ini, COUNT(*) AS denominador
            FROM base
            GROUP BY 1, 2
        )
        SELECT
            grid.categoria,
            grid.faixa_etaria,
            grid.faixa_ini,
            COALESCE(counts.n, 0) AS n,
            COALESCE(totals.denominador, 0) AS denominador,
            CASE WHEN COALESCE(totals.denominador, 0) > 0
                 THEN ROUND(100.0 * COALESCE(counts.n, 0) / totals.denominador, 2)
                 ELSE NULL END AS pct,
            grid.ordem_categoria
        FROM grid
        LEFT JOIN counts
          ON counts.categoria = grid.categoria
         AND counts.faixa_etaria = grid.faixa_etaria
         AND counts.faixa_ini = grid.faixa_ini
        LEFT JOIN totals
          ON totals.faixa_etaria = grid.faixa_etaria
         AND totals.faixa_ini = grid.faixa_ini
        ORDER BY grid.faixa_ini, grid.ordem_categoria
    """
    return run_query(table, sql)


def query_sinan_education_outcomes_by_age(
    table: LoadedTable,
    education_sql: str,
    classi_sql: str,
    age_sql: str,
    where_sql: str,
) -> pd.DataFrame:
    """Escolaridade do SINAN por grupo de classificação e faixa etária quinquenal."""
    escolaridade_labels = education_category_labels("SINAN", include_missing=True)
    cats_cte = values_cte_from_labels(escolaridade_labels, "escolaridade", "ordem_escolaridade")
    group_values = ", ".join(
        f"({qstr(label)}, {idx})"
        for idx, label in enumerate(["Casos confirmados", "Casos descartados"], start=1)
    )
    faixa_label = _age_stratum_label_sql("idade")
    faixa_order = _age_stratum_order_sql("idade")
    sql = f"""
        WITH raw AS (
            SELECT
                COALESCE({education_sql}, 'Sem informação/ignorado') AS escolaridade,
                {classi_sql} AS classi,
                {age_sql} AS idade
            FROM {table.ref_sql}
            {where_sql}
        ), base AS (
            SELECT
                escolaridade,
                classi,
                {faixa_label} AS faixa_etaria,
                {faixa_order} AS faixa_ini
            FROM raw
        ), categorias AS (
            {cats_cte}
        ), grupos_ref AS (
            SELECT * FROM (VALUES {group_values}) AS t(grupo, ordem_grupo)
        ), faixas AS (
            SELECT faixa_etaria, faixa_ini
            FROM base
            GROUP BY 1, 2
        ), grid AS (
            SELECT g.grupo, g.ordem_grupo, f.faixa_etaria, f.faixa_ini, c.escolaridade, c.ordem_escolaridade
            FROM grupos_ref g
            CROSS JOIN faixas f
            CROSS JOIN categorias c
        ), grupos AS (
            SELECT 'Casos confirmados' AS grupo, faixa_etaria, faixa_ini, escolaridade
            FROM base
            WHERE classi = '1'
            UNION ALL
            SELECT 'Casos descartados' AS grupo, faixa_etaria, faixa_ini, escolaridade
            FROM base
            WHERE classi = '2'
        ), counts AS (
            SELECT grupo, faixa_etaria, faixa_ini, escolaridade, COUNT(*) AS n
            FROM grupos
            GROUP BY 1, 2, 3, 4
        ), totals AS (
            SELECT grupo, faixa_etaria, faixa_ini, COUNT(*) AS denominador
            FROM grupos
            GROUP BY 1, 2, 3
        )
        SELECT
            grid.grupo,
            grid.faixa_etaria,
            grid.faixa_ini,
            grid.escolaridade,
            COALESCE(counts.n, 0) AS n,
            COALESCE(totals.denominador, 0) AS denominador,
            CASE WHEN COALESCE(totals.denominador, 0) > 0
                 THEN ROUND(100.0 * COALESCE(counts.n, 0) / totals.denominador, 2)
                 ELSE NULL END AS pct,
            grid.ordem_escolaridade,
            grid.ordem_grupo
        FROM grid
        LEFT JOIN counts
          ON counts.grupo = grid.grupo
         AND counts.faixa_etaria = grid.faixa_etaria
         AND counts.faixa_ini = grid.faixa_ini
         AND counts.escolaridade = grid.escolaridade
        LEFT JOIN totals
          ON totals.grupo = grid.grupo
         AND totals.faixa_etaria = grid.faixa_etaria
         AND totals.faixa_ini = grid.faixa_ini
        ORDER BY grid.faixa_ini, grid.ordem_escolaridade, grid.ordem_grupo
    """
    return run_query(table, sql)


def query_yearly_category(table: LoadedTable, dt_sql: str, category_sql: str, where_sql: str) -> pd.DataFrame:
    cat_sql = category_label_expr(category_sql)
    sql = f"""
        WITH base AS (
            SELECT {dt_sql} AS dt, {cat_sql} AS categoria
            FROM {table.ref_sql}
            {where_sql}
        ), counts AS (
            SELECT EXTRACT(YEAR FROM dt) AS ano, categoria, COUNT(*) AS n
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1, 2
        ), with_totals AS (
            SELECT ano, categoria, n,
                   SUM(n) OVER (PARTITION BY ano) AS total_ano
            FROM counts
        )
        SELECT ano, categoria, n, total_ano,
               CASE WHEN total_ano > 0
                    THEN ROUND(100.0 * n / total_ano, 2)
                    ELSE NULL END AS pct
        FROM with_totals
        ORDER BY ano, categoria
    """
    return run_query(table, sql)


def collapse_sinan_evolucao_ignorado(df: pd.DataFrame) -> pd.DataFrame:
    """Agrupa EVOLUCAO=9 em Sem evolução/ignorado para evitar categorias redundantes no gráfico."""
    if df is None or df.empty or "categoria" not in df.columns:
        return df
    out = df.copy()
    out["categoria"] = out["categoria"].replace({"9 — ignorado": "Sem evolução/ignorado"})
    if not {"ano", "categoria", "n"}.issubset(out.columns):
        return out
    grouped = (
        out
        .groupby(["ano", "categoria"], dropna=False, as_index=False)
        .agg(n=("n", "sum"), total_ano=("total_ano", "max"))
    )
    grouped["pct"] = np.where(
        pd.to_numeric(grouped["total_ano"], errors="coerce").fillna(0).gt(0),
        (100.0 * pd.to_numeric(grouped["n"], errors="coerce").fillna(0) / grouped["total_ano"]).round(2),
        np.nan,
    )
    return grouped.sort_values(["ano", "categoria"]).reset_index(drop=True)


def query_age_dist(table: LoadedTable, age_sql: str, where_sql: str, sex_sql: Optional[str] = None) -> pd.DataFrame:
    faixa_label = _age_pyramid_band_label_sql("idade")
    faixa_order = _age_pyramid_band_order_sql("idade")
    if sex_sql:
        sql = f"""
            WITH base AS (
                SELECT {age_sql} AS idade, {sex_sql} AS sexo
                FROM {table.ref_sql}
                {where_sql}
            )
            SELECT sexo, {faixa_label} AS faixa, {faixa_order} AS faixa_ini, COUNT(*) AS n
            FROM base
            WHERE idade BETWEEN 0 AND 130 AND sexo IN ('Masculino', 'Feminino')
            GROUP BY 1, 2, 3
            ORDER BY 3, 1
        """
    else:
        sql = f"""
            WITH base AS (
                SELECT {age_sql} AS idade
                FROM {table.ref_sql}
                {where_sql}
            )
            SELECT {faixa_label} AS faixa, {faixa_order} AS faixa_ini, COUNT(*) AS n
            FROM base
            WHERE idade BETWEEN 0 AND 130
            GROUP BY 1, 2
            ORDER BY 2
        """
    return run_query(table, sql)


def query_cid_distribution(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    cid = exprs.get("cid")
    if not cid:
        return pd.DataFrame()
    source_expr = exprs.get("cid_source") or "NULL"
    sql = f"""
        WITH base AS (
            SELECT {cid} AS cid, {cid_group_expr(cid)} AS grupo, {cid_type_expr(cid)} AS tipo, {source_expr} AS coluna_origem
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT grupo, tipo, COUNT(*) AS n,
               COUNT(DISTINCT cid) AS cids_distintos,
               string_agg(DISTINCT cid, ', ' ORDER BY cid) FILTER (WHERE cid IS NOT NULL) AS cids_encontrados,
               string_agg(DISTINCT coluna_origem, ', ' ORDER BY coluna_origem) FILTER (WHERE coluna_origem IS NOT NULL) AS campos_origem
        FROM base
        GROUP BY 1, 2
        ORDER BY n DESC, grupo
    """
    df = run_query(table, sql)
    if not df.empty:
        df["pct"] = (df["n"] / df["n"].sum() * 100).round(2)
    return df


def query_g01_g02_cid_distribution(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    """Tabula G01/G02 em SIM/CIHA a partir do CID-10 bruto detectado.

    A verificação usa presença de G01*/G02* em qualquer campo CID detectado, não apenas
    o primeiro CID priorizado pela distribuição geral. Assim, G01/G02 não são perdidos
    quando aparecem como diagnóstico/menção associado após outro CID de meningite.
    """
    g01 = exprs.get("cid_g01_present")
    g02 = exprs.get("cid_g02_present")
    if not (g01 or g02):
        return pd.DataFrame()
    g01_sql = g01 or "FALSE"
    g02_sql = g02 or "FALSE"
    sql = f"""
        WITH base AS (
            SELECT ({g01_sql}) AS tem_g01,
                   ({g02_sql}) AS tem_g02
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT 'G01' AS grupo,
                   'G01 — meningite bacteriana em doença classificada em outra parte' AS tipo,
                   COUNT(*) FILTER (WHERE tem_g01) AS n
            FROM base
            UNION ALL
            SELECT 'G02' AS grupo,
                   'G02 — meningite em outras doenças infecciosas/parasitárias' AS tipo,
                   COUNT(*) FILTER (WHERE tem_g02) AS n
            FROM base
        ), with_totals AS (
            SELECT *, SUM(n) OVER () AS denominador
            FROM agg
        )
        SELECT *,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        WHERE n > 0
        ORDER BY grupo
    """
    return run_query(table, sql)



def query_cid10_adequacy_conversion(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    cid = exprs.get("cid")
    if not cid:
        return pd.DataFrame()
    source_expr = exprs.get("cid_source") or "NULL"
    original_display = cid10_adequacy_original_display_expr(cid)
    original_type = cid_type_expr(cid)
    converted_group = exprs.get("cid10_adequacy_group") or cid10_adequacy_group_expr(cid)
    converted_type = exprs.get("cid10_adequacy_type") or cid10_adequacy_type_expr(cid)
    status = exprs.get("cid10_adequacy_status") or cid10_adequacy_status_expr(cid)
    reason = exprs.get("cid10_adequacy_reason") or cid10_adequacy_reason_expr(cid)
    plot_label = exprs.get("cid10_adequacy_plot_label") or cid10_adequacy_plot_label_expr(cid)
    sql = f"""
        WITH base AS (
            SELECT {cid} AS cid10_detectado,
                   {original_display} AS cid10_original,
                   {original_type} AS cid10_original_classificacao,
                   {converted_group} AS cid10_adequado_grupo,
                   {converted_type} AS cid10_adequado_classificacao,
                   {status} AS status_conversao,
                   {reason} AS observacao_conversao,
                   {plot_label} AS categoria_grafico,
                   {source_expr} AS coluna_origem
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT cid10_original,
                   cid10_adequado_grupo,
                   cid10_adequado_classificacao,
                   status_conversao,
                   categoria_grafico,
                   COUNT(*) AS n,
                   COUNT(DISTINCT cid10_detectado) AS cids_distintos,
                   string_agg(DISTINCT cid10_detectado, ', ' ORDER BY cid10_detectado)
                       FILTER (WHERE cid10_detectado IS NOT NULL) AS cids_detectados,
                   string_agg(DISTINCT cid10_original_classificacao, '; ' ORDER BY cid10_original_classificacao)
                       FILTER (WHERE cid10_original_classificacao IS NOT NULL) AS classificacoes_originais,
                   string_agg(DISTINCT observacao_conversao, '; ' ORDER BY observacao_conversao)
                       FILTER (WHERE observacao_conversao IS NOT NULL) AS observacoes,
                   string_agg(DISTINCT coluna_origem, ', ' ORDER BY coluna_origem)
                       FILTER (WHERE coluna_origem IS NOT NULL) AS campos_origem
            FROM base
            WHERE cid10_detectado IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5
        ), with_totals AS (
            SELECT *, SUM(n) OVER () AS denominador
            FROM agg
        )
        SELECT *,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        ORDER BY CASE WHEN status_conversao = 'Convertido' THEN 0 ELSE 1 END,
                 n DESC, cid10_original, cid10_adequado_grupo
    """
    return run_query(table, sql)

def _join_unique_text(values: pd.Series, sep: str = ", ") -> Optional[str]:
    """Une valores textuais únicos preservando a ordem de aparecimento."""
    seen: List[str] = []
    for value in values:
        if pd.isna(value):
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.append(text)
    return sep.join(seen) if seen else None


def _format_br_int(value: object) -> str:
    if pd.isna(value):
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except Exception:
        return str(value)


def _format_br_pct(value: object) -> str:
    if pd.isna(value):
        return "—"
    try:
        return f"{float(value):.2f}%".replace(".", ",")
    except Exception:
        return str(value)


def build_cid10_adequacy_conversion_note(df: pd.DataFrame) -> str:
    """Resume, em texto curto, somente o que foi efetivamente convertido."""
    required = {"status_conversao", "cid10_original", "cid10_adequado_grupo", "n", "denominador"}
    if df.empty or not required.issubset(df.columns):
        return "Conversão efetiva no recorte: não foi possível calcular o resumo de conversões."

    denom = pd.to_numeric(df["denominador"], errors="coerce").max()
    if pd.isna(denom) or denom <= 0:
        denom = pd.to_numeric(df["n"], errors="coerce").sum()

    converted = df[df["status_conversao"].eq("Convertido")].copy()
    if converted.empty:
        return (
            "Conversão efetiva no recorte: nenhum registro foi convertido; "
            f"o gráfico mostra somente CID-10 prefixados já presentes no SIM/CIHA "
            f"({_format_br_int(denom)} registros com CID-10 detectado)."
        )

    converted["n"] = pd.to_numeric(converted["n"], errors="coerce").fillna(0)
    total_converted = converted["n"].sum()
    pct_converted = (100.0 * total_converted / denom) if denom and denom > 0 else np.nan

    agg_kwargs = {"n": ("n", "sum")}
    if "cids_detectados" in converted.columns:
        agg_kwargs["cids_detectados"] = ("cids_detectados", lambda s: _join_unique_text(s, ", "))

    detail = (
        converted
        .groupby(["cid10_original", "cid10_adequado_grupo"], dropna=False, as_index=False)
        .agg(**agg_kwargs)
        .sort_values(["cid10_adequado_grupo", "n", "cid10_original"], ascending=[True, False, True])
    )

    parts: List[str] = []
    for _, row in detail.iterrows():
        origem = row.get("cid10_original")
        destino = row.get("cid10_adequado_grupo")
        origem = "CID original não identificado" if pd.isna(origem) or not str(origem).strip() else str(origem)
        destino = "destino não identificado" if pd.isna(destino) or not str(destino).strip() else str(destino)
        piece = f"{origem} → {destino}: {_format_br_int(row.get('n'))}"
        cids = row.get("cids_detectados") if "cids_detectados" in detail.columns else None
        if isinstance(cids, str) and cids.strip():
            piece += f" (CID detectado: {cids})"
        parts.append(piece)

    return (
        f"Conversão efetiva no recorte: {_format_br_int(total_converted)} registros "
        f"({_format_br_pct(pct_converted)} do total com CID-10 detectado) foram convertidos. "
        f"Detalhe: {'; '.join(parts)}."
    )


def summarize_cid10_adequacy_plot(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega o gráfico no nível do CID-10 adequado final.

    O gráfico soma os códigos efetivamente convertidos no destino adequado e
    também mantém os CID-10 prefixados já presentes no SIM/CIHA. A tabela
    detalhada continua separando códigos originais e status de conversão.
    """
    required = {
        "categoria_grafico",
        "cid10_adequado_grupo",
        "cid10_adequado_classificacao",
        "n",
        "denominador",
    }
    if df.empty or not required.issubset(df.columns):
        return pd.DataFrame()

    plot_df = df[
        df["categoria_grafico"].notna()
        & df["cid10_adequado_grupo"].notna()
    ].copy()
    if plot_df.empty:
        return pd.DataFrame()

    agg = (
        plot_df
        .groupby(["categoria_grafico", "cid10_adequado_grupo", "cid10_adequado_classificacao"], dropna=False, as_index=False)
        .agg(
            n=("n", "sum"),
            denominador=("denominador", "max"),
            status_conversao=("status_conversao", lambda s: _join_unique_text(s, ", ") if "status_conversao" in plot_df.columns else None),
            cid10_originais=("cid10_original", lambda s: _join_unique_text(s, ", ") if "cid10_original" in plot_df.columns else None),
            cids_detectados=("cids_detectados", lambda s: _join_unique_text(s, ", ") if "cids_detectados" in plot_df.columns else None),
            classificacoes_originais=("classificacoes_originais", lambda s: _join_unique_text(s, "; ") if "classificacoes_originais" in plot_df.columns else None),
            observacoes=("observacoes", lambda s: _join_unique_text(s, "; ") if "observacoes" in plot_df.columns else None),
            campos_origem=("campos_origem", lambda s: _join_unique_text(s, ", ") if "campos_origem" in plot_df.columns else None),
        )
    )
    denom = agg["denominador"].replace({0: np.nan})
    agg["pct"] = (100.0 * agg["n"] / denom).round(2)
    return agg.sort_values(["n", "categoria_grafico"], ascending=[False, True]).reset_index(drop=True)


def query_sinan_cid10_conversion(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    """Converte CON_DIAGES do SINAN para famílias CID-10 com consulta otimizada.

    Gargalo corrigido: a consulta anterior montava texto auxiliar e executava regex em todos
    os registros confirmados, mesmo quando CON_DIAGES não era 05. Aqui o texto auxiliar só
    é avaliado para o subconjunto que realmente precisa de refinamento G01/G00. Além disso,
    a maior parte dos testes textuais usa contains(), que é mais barato que regexp_matches().

    Erro corrigido: a CTE `flags` referenciava `prepared`, uma CTE inexistente (resquício de
    refatoração), o que disparava duckdb.CatalogException e impedia o gráfico de carregar.
    A cadeia correta é src -> base -> flags -> tagged -> converted -> agg.

    Otimização adicional: CON_DIAGES e CLA_ME_BAC são normalizados uma única vez na CTE `src`
    (`_con_code`/`_bac_code`) e reutilizados, evitando reexecutar a limpeza por regex desses
    códigos várias vezes por linha.
    """
    con_code = exprs.get("con_code")
    if not con_code:
        return pd.DataFrame()

    con_label = exprs.get("con_label") or "NULL"
    con_group = exprs.get("con_group") or "NULL"
    bac_code = exprs.get("cla_me_bac_code") or "NULL"
    bac_label = exprs.get("cla_me_bac_label") or "NULL"
    ass_label = exprs.get("cla_me_ass_label") or "NULL"
    eti_label = exprs.get("cla_me_eti_label") or "NULL"
    aux_text = exprs.get("sinan_aux_text")
    g01_codes_sql = ", ".join(qstr(code) for code in sorted(SINAN_CLA_ME_BAC_G01_CODES))
    mapped_codes_sql = ", ".join(qstr(code) for code in SINAN_CID10_FROM_CON_DIAGES)

    if aux_text:
        # Restrição crítica de desempenho: não concatenar/normalizar campos textuais para
        # todas as linhas. A conversão textual só é necessária para CON_DIAGES=05 quando
        # CLA_ME_BAC não resolve o refinamento G01. Os códigos já normalizados (_con_code/
        # _bac_code, calculados uma única vez na CTE `src`) são reutilizados aqui para evitar
        # reavaliar a limpeza por regex de CON_DIAGES/CLA_ME_BAC em cada linha.
        aux_text_select = f"""
                   CASE
                       WHEN _con_code = '05'
                            AND (_bac_code IS NULL OR _bac_code NOT IN ({g01_codes_sql}))
                       THEN COALESCE({aux_text}, '')
                       ELSE ''
                   END AS aux_text_g01
        """
    else:
        aux_text_select = "'' AS aux_text_g01"

    sql = f"""
        WITH src AS (
            SELECT *,
                   {con_code} AS _con_code,
                   {bac_code} AS _bac_code
            FROM {table.ref_sql}
            {where_sql}
        ), base AS (
            SELECT _con_code AS con_code,
                   {con_label} AS conclusao_diagnostica,
                   {con_group} AS grupo_etiologico_sinan,
                   _bac_code AS bacteria_code,
                   {bac_label} AS bacteria_sinan,
                   {ass_label} AS agente_asseptica_sinan,
                   {eti_label} AS outra_etiologia_sinan,
                   {aux_text_select}
            FROM src
        ), flags AS (
            SELECT *,
                   contains(aux_text_g01, 'SALMONEL') AS txt_salmonel,
                   contains(aux_text_g01, 'LISTERI') AS txt_listeri,
                   (contains(aux_text_g01, 'NEUROSSÍFIL') OR contains(aux_text_g01, 'NEUROSSIFIL')
                    OR contains(aux_text_g01, 'NEUROSYPH') OR contains(aux_text_g01, 'SÍFIL')
                    OR contains(aux_text_g01, 'SIFIL') OR contains(aux_text_g01, 'SYPHIL')
                    OR contains(aux_text_g01, 'TREPONEMA')) AS txt_sifilis,
                   contains(aux_text_g01, 'LEPTOSPI') AS txt_leptospi,
                   (contains(aux_text_g01, 'CARBÚNCULO') OR contains(aux_text_g01, 'CARBUNCULO')
                    OR contains(aux_text_g01, 'ANTRAZ') OR contains(aux_text_g01, 'ANTHRAX')) AS txt_antraz,
                   (contains(aux_text_g01, 'LYME') OR contains(aux_text_g01, 'BORREL')) AS txt_lyme,
                   (contains(aux_text_g01, 'TIFÓIDE') OR contains(aux_text_g01, 'TIFOIDE')
                    OR contains(aux_text_g01, 'TYPHOID')) AS txt_tifoide,
                   (contains(aux_text_g01, 'GONOCOC') OR contains(aux_text_g01, 'GONOCOCO')) AS txt_gonococica
            FROM base
        ), tagged AS (
            SELECT *,
                   CASE
                       WHEN con_code <> '05' OR con_code IS NULL THEN FALSE
                       WHEN bacteria_code IN ({g01_codes_sql}) THEN TRUE
                       WHEN txt_salmonel OR txt_listeri OR txt_sifilis OR txt_leptospi
                            OR txt_antraz OR txt_lyme OR txt_tifoide OR txt_gonococica THEN TRUE
                       ELSE FALSE
                   END AS g01_match
            FROM flags
        ), converted AS (
            SELECT con_code,
                   conclusao_diagnostica,
                   grupo_etiologico_sinan,
                   bacteria_sinan,
                   agente_asseptica_sinan,
                   outra_etiologia_sinan,
                   CASE
                       WHEN con_code IN ('02', '03') THEN 'A39.0'
                       WHEN con_code = '04' THEN 'A17.0'
                       WHEN con_code = '05' AND g01_match THEN 'G01'
                       WHEN con_code = '05' THEN 'G00'
                       WHEN con_code = '06' THEN 'G03'
                       WHEN con_code = '07' THEN 'A87'
                       WHEN con_code = '08' THEN 'G02'
                       WHEN con_code IN ('09', '10') THEN 'G00'
                       WHEN con_code = '01' THEN 'Não convertido — meningococcemia isolada'
                       ELSE 'Sem conversão — CON_DIAGES ausente ou não mapeado'
                   END AS cid10_grupo,
                   CASE
                       WHEN con_code IN ('02', '03') THEN 'A39.0 — meningite meningocócica'
                       WHEN con_code = '04' THEN 'A17.0 — meningite tuberculosa'
                       WHEN con_code = '05' AND g01_match THEN 'G01 — meningite bacteriana em doença classificada em outra parte'
                       WHEN con_code = '05' THEN 'G00 — meningite bacteriana não classificada em outra parte'
                       WHEN con_code = '06' THEN 'G03 — meningite por outras causas/não especificada'
                       WHEN con_code = '07' THEN 'A87 — meningite viral'
                       WHEN con_code = '08' THEN 'G02 — meningite em outras doenças infecciosas/parasitárias'
                       WHEN con_code IN ('09', '10') THEN 'G00 — meningite bacteriana não classificada em outra parte'
                       WHEN con_code = '01' THEN 'Não convertido — meningococcemia isolada'
                       ELSE 'Sem conversão — CON_DIAGES ausente ou não mapeado'
                   END AS cid10_classificacao,
                   CASE
                       WHEN con_code IN ('02', '03') THEN 'Forma meningítica meningocócica: A39.0.'
                       WHEN con_code = '04' THEN 'Meningite tuberculosa: A17.0.'
                       WHEN con_code = '05' AND g01_match THEN 'CON_DIAGES=05 refinado como G01 por CLA_ME_BAC/texto: agente/doença bacteriana classificada em outra parte.'
                       WHEN con_code = '05' AND bacteria_code IS NULL THEN 'CON_DIAGES=05 sem CLA_ME_BAC detectado/preenchido: classificado conservadoramente como G00.'
                       WHEN con_code = '05' AND bacteria_code IN ('81') THEN 'CON_DIAGES=05 com bactéria não especificada: G00.'
                       WHEN con_code = '05' THEN 'CON_DIAGES=05 com bactéria comum/outra bactéria: G00.'
                       WHEN con_code = '06' THEN 'Meningite não especificada: G03.'
                       WHEN con_code = '07' THEN 'Meningite asséptica no SINAN: A87 como leitura operacional viral; validar etiologia quando disponível.'
                       WHEN con_code = '08' THEN 'Outra etiologia infecciosa/parasitária: G02 como família comparável; validar CLA_ME_ETI.'
                       WHEN con_code IN ('09', '10') THEN 'Haemophilus influenzae/pneumocócica: G00.'
                       WHEN con_code = '01' THEN 'Meningococcemia isolada: fora da comparação de meningite.'
                       ELSE 'CON_DIAGES ausente ou sem regra.'
                   END AS justificativa_cid10,
                   CASE WHEN con_code IN ({mapped_codes_sql}) THEN 'Sim' ELSE 'Não' END AS incluido_comparacao,
                   CASE
                       WHEN g01_match = FALSE THEN NULL
                       WHEN bacteria_code = '11' OR txt_salmonel THEN 'Salmonella sp / salmonelose invasiva'
                       WHEN bacteria_code = '21' OR txt_listeri THEN 'Listeriose / Listeria monocytogenes'
                       WHEN bacteria_code = '45' OR txt_sifilis THEN 'Sífilis / neurossífilis'
                       WHEN bacteria_code = '49' OR txt_leptospi THEN 'Leptospirose'
                       WHEN txt_antraz THEN 'Carbúnculo / antraz'
                       WHEN txt_lyme THEN 'Doença de Lyme / borreliose'
                       WHEN txt_tifoide THEN 'Febre tifóide'
                       WHEN txt_gonococica THEN 'Infecção gonocócica'
                       ELSE 'Doença bacteriana de base provável não especificada'
                   END AS doenca_base_g01_provavel
            FROM tagged
        ), agg AS (
            SELECT cid10_grupo,
                   cid10_classificacao,
                   incluido_comparacao,
                   COUNT(*) AS n,
                   COUNT(DISTINCT con_code) FILTER (WHERE con_code IS NOT NULL) AS con_diages_distintos,
                   string_agg(DISTINCT conclusao_diagnostica, '; ' ORDER BY conclusao_diagnostica)
                       FILTER (WHERE conclusao_diagnostica IS NOT NULL) AS conclusoes_sinan,
                   string_agg(DISTINCT grupo_etiologico_sinan, '; ' ORDER BY grupo_etiologico_sinan)
                       FILTER (WHERE grupo_etiologico_sinan IS NOT NULL) AS grupos_sinan,
                   string_agg(DISTINCT bacteria_sinan, '; ' ORDER BY bacteria_sinan)
                       FILTER (WHERE bacteria_sinan IS NOT NULL) AS bacterias_sinan,
                   string_agg(DISTINCT agente_asseptica_sinan, '; ' ORDER BY agente_asseptica_sinan)
                       FILTER (WHERE agente_asseptica_sinan IS NOT NULL) AS agentes_asseptica_sinan,
                   string_agg(DISTINCT outra_etiologia_sinan, '; ' ORDER BY outra_etiologia_sinan)
                       FILTER (WHERE outra_etiologia_sinan IS NOT NULL) AS outras_etiologias_sinan,
                   string_agg(DISTINCT doenca_base_g01_provavel, '; ' ORDER BY doenca_base_g01_provavel)
                       FILTER (WHERE cid10_grupo = 'G01' AND doenca_base_g01_provavel IS NOT NULL) AS doencas_base_g01_provaveis,
                   string_agg(DISTINCT justificativa_cid10, '; ' ORDER BY justificativa_cid10)
                       FILTER (WHERE justificativa_cid10 IS NOT NULL) AS justificativas
            FROM converted
            GROUP BY 1, 2, 3
        ), with_totals AS (
            SELECT *, SUM(n) OVER () AS denominador
            FROM agg
        )
        SELECT *,
               CASE WHEN denominador > 0
                    THEN ROUND(100.0 * n / denominador, 2)
                    ELSE NULL END AS pct
        FROM with_totals
        ORDER BY CASE WHEN incluido_comparacao = 'Sim' THEN 0 ELSE 1 END,
                 n DESC, cid10_grupo
    """
    return run_query(table, sql)


def query_sinan_g01_base_disease(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    con_code = exprs.get("con_code")
    cid_group = exprs.get("sinan_cid10_conversion_group")
    include = exprs.get("sinan_cid10_conversion_include")
    base_disease = exprs.get("sinan_g01_base_disease")
    con_label = exprs.get("con_label") or "NULL"
    bacteria = exprs.get("cla_me_bac_label") or "NULL"
    reason = exprs.get("sinan_cid10_conversion_reason") or "NULL"
    if not (con_code and cid_group and include and base_disease):
        return pd.DataFrame()
    sql = f"""
        WITH base AS (
            SELECT {cid_group} AS cid10_grupo,
                   {include} AS incluido_comparacao,
                   {base_disease} AS doenca_base_provavel,
                   {con_label} AS conclusao_sinan,
                   {bacteria} AS bacteria_sinan,
                   {reason} AS justificativa
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT doenca_base_provavel,
                   COUNT(*) AS n,
                   string_agg(DISTINCT conclusao_sinan, '; ' ORDER BY conclusao_sinan)
                       FILTER (WHERE conclusao_sinan IS NOT NULL) AS conclusoes_sinan,
                   string_agg(DISTINCT bacteria_sinan, '; ' ORDER BY bacteria_sinan)
                       FILTER (WHERE bacteria_sinan IS NOT NULL) AS bacterias_sinan,
                   string_agg(DISTINCT justificativa, '; ' ORDER BY justificativa)
                       FILTER (WHERE justificativa IS NOT NULL) AS justificativas
            FROM base
            WHERE cid10_grupo = 'G01' AND incluido_comparacao = 'Sim'
            GROUP BY 1
        ), with_totals AS (
            SELECT *, SUM(n) OVER () AS denominador
            FROM agg
        )
        SELECT *,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        ORDER BY n DESC, doenca_base_provavel
    """
    return run_query(table, sql)


def query_ciha_death_cid_distribution(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    morte = exprs.get("morte_code")
    cid = exprs.get("cid")
    if not (morte and cid):
        return pd.DataFrame()
    death_where = append_clause(where_sql, f"{morte} = '1'")
    return query_cid_distribution(table, exprs, death_where)


def sinan_quimio_param_exprs(exprs: Dict[str, Optional[str]]) -> List[Tuple[str, str, str]]:
    params: List[Tuple[str, str, str]] = []
    for key, info in SINAN_QUIMIO_PARAMS.items():
        expr = exprs.get(f"lab_{key}")
        if expr:
            params.append((key, str(info["label"]), expr))
    return params


def sinan_lcr_eligible_where(exprs: Dict[str, Optional[str]], base_where: str) -> str:
    """Restringe a base a quem teve punção lombar e exame quimiocitológico do LCR
    realizados (correção 2 do plano priorizado — depende da mesma cláusula de
    elegibilidade já usada corretamente na "Análise 2 — Classificação provável
    pelo LCR", ver `independent_where` mais abaixo neste arquivo).

    Usada para calcular a COMPLETUDE de preenchimento de cada parâmetro entre
    quem foi de fato puncionado — e não sobre o recorte geral de filtros da
    página (graph_where), que mistura "não indicado clinicamente" com "campo mal
    preenchido no SINAN".
    """
    where_sql = base_where
    if exprs.get("puncao_code"):
        where_sql = append_clause(where_sql, f"{exprs['puncao_code']} = '1'")
    if exprs.get("quimio_code"):
        where_sql = append_clause(where_sql, f"{exprs['quimio_code']} = '1'")
    return where_sql


def query_sinan_quimio_summary(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    """Resumo estatístico dos parâmetros quimiocitológicos do LCR.

    IMPORTANTE (correção 2): `where_sql` deve ser a base já restrita a quem teve
    punção lombar e exame quimiocitológico realizados — construída com
    `sinan_lcr_eligible_where` — para que `pct_preenchido` reflita completude de
    registro entre os elegíveis, e não uma mistura com quem nunca teve indicação
    de puncionar.

    IMPORTANTE (correções 3 e 4): cada valor passa por
    `sinan_lcr_neutralize_sentinel_expr` antes de entrar nas estatísticas (para
    não deixar códigos sentinela de "ignorado" contaminarem média/mediana/bins),
    e valores acima do teto de plausibilidade do parâmetro são contados em
    `n_acima_teto_plausibilidade` — sinalizados, não descartados. Ver a
    observação em SINAN_LCR_SENTINEL_CODES/SINAN_LCR_PLAUSIBLE_MAX: esses
    códigos e tetos ainda não foram confirmados contra o dicionário oficial do
    SINAN e devem ser tratados como provisórios.
    """
    params = sinan_quimio_param_exprs(exprs)
    if not params:
        return pd.DataFrame()
    unions = []
    for key, label, value_expr in params:
        meta = sinan_lcr_param_metadata(key)
        audit = sinan_lcr_numeric_audit_exprs(value_expr, key)
        teto = sinan_lcr_plausible_max(key)
        teto_sql = "CAST(NULL AS DOUBLE)" if teto is None else repr(float(teto))
        teto_sistema_sql = qstr(", ".join(str(int(v)) if float(v).is_integer() else str(v) for v in (meta.teto_sistema if meta else ())))
        unions.append(
            f"""
            SELECT {qstr(key)} AS parametro_id,
                   {qstr(SINAN_QUIMIO_MATERIAL)} AS material_analisado,
                   {qstr(label)} AS parametro,
                   {qstr(meta.unidade if meta else '')} AS unidade,
                   {qstr(meta.tipo_valor if meta else '')} AS tipo_valor,
                   {qstr(meta.faixa_operacional if meta else '')} AS faixa_operacional,
                   {qstr(meta.regra_sentinela if meta else '')} AS regra_sentinela,
                   {teto_sql} AS teto_plausibilidade,
                   {teto_sistema_sql} AS teto_sistema,
                   {qstr(meta.comportamento_truncamento if meta else '')} AS comportamento_truncamento,
                   {qstr(meta.uso_permitido if meta else '')} AS uso_permitido,
                   {audit['valor_bruto']} AS valor_bruto,
                   {audit['valor_limpo']} AS valor,
                   {audit['flag_sentinela']} AS flag_sentinela,
                   {audit['flag_acima_teto_plausivel']} AS flag_acima_teto_plausivel,
                   {audit['flag_teto_sistema']} AS flag_teto_sistema,
                   {audit['flag_percentual_invalido']} AS flag_percentual_invalido
            FROM {table.ref_sql}
            {where_sql}
            """
        )
    sql = f"""
        WITH valores AS (
            {' UNION ALL '.join(unions)}
        )
        SELECT parametro_id,
               material_analisado,
               parametro,
               unidade,
               tipo_valor,
               faixa_operacional,
               regra_sentinela,
               teto_plausibilidade,
               teto_sistema,
               comportamento_truncamento,
               uso_permitido,
               COUNT(*) AS registros_avaliados,
               COUNT(*) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS n_valido,
               COUNT(*) FILTER (WHERE valor IS NULL OR valor < 0) AS n_sem_valor,
               COUNT(*) FILTER (WHERE flag_sentinela) AS n_sentinela,
               COUNT(*) FILTER (WHERE flag_teto_sistema) AS n_no_teto_sistema,
               COUNT(*) FILTER (WHERE flag_percentual_invalido) AS n_percentual_invalido,
               COUNT(*) FILTER (WHERE flag_acima_teto_plausivel) AS n_acima_teto_plausibilidade,
               ROUND(100.0 * COUNT(*) FILTER (WHERE valor IS NOT NULL AND valor >= 0) / NULLIF(COUNT(*), 0), 2) AS pct_preenchido,
               MIN(valor) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS minimo,
               quantile_cont(valor, 0.25) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS q1,
               median(valor) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS mediana,
               ROUND(AVG(valor) FILTER (WHERE valor IS NOT NULL AND valor >= 0), 2) AS media,
               quantile_cont(valor, 0.75) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS q3,
               MAX(valor) FILTER (WHERE valor IS NOT NULL AND valor >= 0) AS maximo
        FROM valores
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        ORDER BY CASE parametro_id
            WHEN 'hema' THEN 1
            WHEN 'neutro' THEN 2
            WHEN 'glico' THEN 3
            WHEN 'leuco' THEN 4
            WHEN 'eosi' THEN 5
            WHEN 'prot' THEN 6
            WHEN 'mono' THEN 7
            WHEN 'linfo' THEN 8
            WHEN 'clor' THEN 9
            ELSE 99 END
    """
    return run_query(table, sql)


def query_sinan_lcr_numeric_audit_long(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    limit: int = DEFAULT_DOWNLOAD_ROW_LIMIT,
) -> pd.DataFrame:
    """Tabela longa de auditoria: valor bruto, valor limpo e flags por parâmetro.

    Esta saída preserva a rastreabilidade dos valores usados no LCR sem carregar
    automaticamente uma tabela enorme. O limite protege o navegador e pode ser
    ajustado na interface.
    """
    params = sinan_quimio_param_exprs(exprs)
    if not params:
        return pd.DataFrame()
    unions = []
    for ordem, (key, label, value_expr) in enumerate(params, start=1):
        meta = sinan_lcr_param_metadata(key)
        audit = sinan_lcr_numeric_audit_exprs(value_expr, key)
        unions.append(
            f"""
            SELECT {int(ordem)} AS ordem_parametro,
                   ROW_NUMBER() OVER () AS row_id_auditoria,
                   {qstr(key)} AS parametro_id,
                   {qstr(label)} AS parametro,
                   {qstr(meta.unidade if meta else '')} AS unidade,
                   {audit['valor_bruto']} AS valor_bruto,
                   {audit['valor_limpo']} AS valor_limpo,
                   {audit['flag_sentinela']} AS flag_sentinela,
                   {audit['flag_teto_sistema']} AS flag_teto_sistema,
                   {audit['flag_acima_teto_plausivel']} AS flag_acima_teto_plausivel,
                   {audit['flag_percentual_invalido']} AS flag_percentual_invalido,
                   {qstr(meta.comportamento_truncamento if meta else '')} AS comportamento_truncamento,
                   {qstr(meta.uso_permitido if meta else '')} AS uso_permitido
            FROM {table.ref_sql}
            {where_sql}
            """
        )
    sql = f"""
        WITH long AS (
            {' UNION ALL '.join(unions)}
        )
        SELECT *
        FROM long
        WHERE valor_bruto IS NOT NULL
           OR flag_sentinela
           OR flag_teto_sistema
           OR flag_acima_teto_plausivel
           OR flag_percentual_invalido
        ORDER BY row_id_auditoria, ordem_parametro
        LIMIT {int(max(1, limit))}
    """
    return run_query(table, sql)


def query_sinan_numeric_distribution(table: LoadedTable, value_expr: str, where_sql: str, bins: int = 30) -> pd.DataFrame:
    stats_sql = f"""
        WITH base AS (
            SELECT {value_expr} AS valor
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT COUNT(*) AS n, MIN(valor) AS minimo, MAX(valor) AS maximo
        FROM base
        WHERE valor IS NOT NULL AND valor >= 0
    """
    stats = run_query(table, stats_sql)
    if stats.empty or int(stats.iloc[0]["n"] or 0) == 0:
        return pd.DataFrame()

    n = int(stats.iloc[0]["n"])
    minimo = float(stats.iloc[0]["minimo"])
    maximo = float(stats.iloc[0]["maximo"])
    if minimo == maximo:
        return pd.DataFrame(
            {
                "faixa_inicio": [minimo],
                "faixa_fim": [maximo],
                "faixa": [f"{minimo:g}"],
                "n": [n],
                "denominador": [n],
                "pct": [100.0],
            }
        )

    bin_count = max(1, min(int(bins), n))
    width = (maximo - minimo) / bin_count
    if width <= 0:
        return pd.DataFrame()

    sql = f"""
        WITH base AS (
            SELECT {value_expr} AS valor
            FROM {table.ref_sql}
            {where_sql}
        ), validos AS (
            SELECT valor
            FROM base
            WHERE valor IS NOT NULL AND valor >= 0
        ), binned AS (
            SELECT CASE
                       WHEN valor = {maximo!r} THEN {bin_count - 1}
                       ELSE CAST(FLOOR((valor - {minimo!r}) / {width!r}) AS INTEGER)
                   END AS bin_idx
            FROM validos
        ), agg AS (
            SELECT bin_idx, COUNT(*) AS n
            FROM binned
            GROUP BY 1
        ), with_totals AS (
            SELECT bin_idx, n, SUM(n) OVER () AS denominador
            FROM agg
        )
        SELECT {minimo!r} + bin_idx * {width!r} AS faixa_inicio,
               CASE WHEN bin_idx = {bin_count - 1} THEN {maximo!r}
                    ELSE {minimo!r} + (bin_idx + 1) * {width!r} END AS faixa_fim,
               n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        ORDER BY faixa_inicio
    """
    df = run_query(table, sql)
    if df.empty:
        return df

    def fmt(value: object) -> str:
        try:
            num = float(value)
        except Exception:
            return str(value)
        if abs(num) >= 100 or abs(num - round(num)) < 1e-9:
            return f"{num:.0f}"
        return f"{num:.1f}".replace(".", ",")

    df["faixa"] = [f"{fmt(a)}–{fmt(b)}" for a, b in zip(df["faixa_inicio"], df["faixa_fim"])]
    return df


def sinan_case_classification_group_expr(classi_code_sql: Optional[str]) -> str:
    """Grupo operacional de classificação final do SINAN usado em gráficos estratificados."""
    if not classi_code_sql:
        return qstr("Sem classificação / ignorados")
    return f"""
        CASE
            WHEN {classi_code_sql} = '1' THEN 'Casos confirmados'
            WHEN {classi_code_sql} = '2' THEN 'Casos descartados'
            ELSE 'Sem classificação / ignorados'
        END
    """


def query_sinan_puncao_by_case_status(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    """Distribui a realização da punção laboratorial por total, confirmados, descartados e ignorados."""
    puncao = exprs.get("puncao_label")
    if not puncao:
        return pd.DataFrame()
    puncao_sql = category_label_expr(puncao, "Sem informação")
    grupo_sql = sinan_case_classification_group_expr(exprs.get("classi_code"))
    sql = f"""
        WITH base AS (
            SELECT {puncao_sql} AS categoria,
                   {grupo_sql} AS grupo_classificacao
            FROM {table.ref_sql}
            {where_sql}
        ), expanded AS (
            SELECT 'Casos totais' AS grupo_classificacao, categoria FROM base
            UNION ALL
            SELECT grupo_classificacao, categoria FROM base
        ), agg AS (
            SELECT grupo_classificacao, categoria, COUNT(*) AS n
            FROM expanded
            GROUP BY 1, 2
        ), totals AS (
            SELECT grupo_classificacao, SUM(n) AS denominador
            FROM agg
            GROUP BY 1
        )
        SELECT a.grupo_classificacao,
               a.categoria,
               a.n,
               t.denominador,
               ROUND(100.0 * a.n / NULLIF(t.denominador, 0), 2) AS pct
        FROM agg a
        JOIN totals t USING (grupo_classificacao)
        ORDER BY CASE a.grupo_classificacao
                    WHEN 'Casos totais' THEN 1
                    WHEN 'Casos confirmados' THEN 2
                    WHEN 'Casos descartados' THEN 3
                    WHEN 'Sem classificação / ignorados' THEN 4
                    ELSE 5
                 END,
                 CASE a.categoria
                    WHEN 'Sim' THEN 1
                    WHEN 'Não' THEN 2
                    WHEN 'Ignorado' THEN 3
                    WHEN 'Sem informação' THEN 4
                    ELSE 5
                 END,
                 a.categoria
    """

    return run_query(table, sql)


def query_sinan_quimio_by_case_status(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    """Distribui a realizacao do exame quimiocitologico do LCR por total, confirmados e descartados.

    O estrato "Casos totais" e intencionalmente sobreposto aos demais: ele contem
    todos os registros do recorte ativo, enquanto "Casos confirmados" e "Casos
    descartados" sao subconjuntos definidos por CLASSI_FIN. Isso permite comparar
    a cobertura geral do procedimento com os dois principais desfechos da
    classificacao final sem esconder a leitura global do banco.
    """
    quimio = exprs.get("quimio_label")
    if not quimio:
        return pd.DataFrame()
    quimio_sql = category_label_expr(quimio, "Sem informa\u00e7\u00e3o")
    classi_code = exprs.get("classi_code")
    if classi_code:
        grupo_sql = sinan_case_classification_group_expr(classi_code)
        expanded_sql = """
            SELECT 'Casos totais' AS grupo_classificacao, categoria FROM base
            UNION ALL
            SELECT grupo_classificacao, categoria
            FROM base
            WHERE grupo_classificacao IN ('Casos confirmados', 'Casos descartados')
        """
    else:
        grupo_sql = qstr("Sem classifica\u00e7\u00e3o / ignorados")
        expanded_sql = """
            SELECT 'Casos totais' AS grupo_classificacao, categoria FROM base
        """
    sql = f"""
        WITH base AS (
            SELECT {quimio_sql} AS categoria,
                   {grupo_sql} AS grupo_classificacao
            FROM {table.ref_sql}
            {where_sql}
        ), expanded AS (
            {expanded_sql}
        ), agg AS (
            SELECT grupo_classificacao, categoria, COUNT(*) AS n
            FROM expanded
            GROUP BY 1, 2
        ), totals AS (
            SELECT grupo_classificacao, SUM(n) AS denominador
            FROM agg
            GROUP BY 1
        )
        SELECT a.grupo_classificacao,
               a.categoria,
               a.n,
               t.denominador,
               ROUND(100.0 * a.n / NULLIF(t.denominador, 0), 2) AS pct
        FROM agg a
        JOIN totals t USING (grupo_classificacao)
        ORDER BY CASE a.grupo_classificacao
                    WHEN 'Casos totais' THEN 1
                    WHEN 'Casos confirmados' THEN 2
                    WHEN 'Casos descartados' THEN 3
                    ELSE 4
                 END,
                 CASE a.categoria
                    WHEN 'Sim' THEN 1
                    WHEN 'N\u00e3o' THEN 2
                    WHEN 'Ignorado' THEN 3
                    WHEN 'Sem informa\u00e7\u00e3o' THEN 4
                    ELSE 5
                 END,
                 a.categoria
    """
    return run_query(table, sql)



def sinan_lcr_age_stratum_expr(exprs: Dict[str, Optional[str]]) -> Optional[str]:
    """Estratos etários operacionais para interpretação do LCR."""
    age = exprs.get("age")
    if not age:
        return None
    return f"""
        CASE
            WHEN {age} IS NULL THEN 'Idade sem informação'
            WHEN {age} <= {SINAN_LCR_NEONATAL_CUTOFF_YEARS!r} THEN 'Neonatos até seis meses de idade'
            ELSE 'Crianças/adultos (>6 meses)'
        END
    """


def sinan_lcr_symptom_puncture_interval_expr(exprs: Dict[str, Optional[str]]) -> Optional[str]:
    """Estrato por intervalo entre primeiros sintomas e punção lombar."""
    dt_sin = exprs.get("dt_sin_pri")
    dt_puncao = exprs.get("dt_puncao")
    if not dt_sin or not dt_puncao:
        return None
    days = f"date_diff('day', {dt_sin}, {dt_puncao})"
    return f"""
        CASE
            WHEN {dt_sin} IS NULL OR {dt_puncao} IS NULL THEN 'Sem data de sintoma/punção'
            WHEN {days} < 0 THEN 'Punção antes dos sintomas/erro de data'
            WHEN {days} <= 1 THEN '0–1 dia entre sintoma e punção'
            WHEN {days} <= 3 THEN '2–3 dias entre sintoma e punção'
            WHEN {days} <= 7 THEN '4–7 dias entre sintoma e punção'
            ELSE '>7 dias entre sintoma e punção'
        END
    """


def combine_sinan_lcr_strata_sql(strata_sql: Sequence[str]) -> Optional[str]:
    valid = [s for s in strata_sql if s]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    return " || ' | ' || ".join(f"COALESCE(CAST(({s}) AS VARCHAR), 'Sem informação')" for s in valid)


def query_sinan_numeric_distribution_stratified(
    table: LoadedTable,
    value_expr: str,
    where_sql: str,
    stratification_sql: Optional[str] = None,
    bins: int = 30,
) -> pd.DataFrame:
    """Distribuição numérica com bins globais e denominador por estrato, quando solicitado."""
    if not stratification_sql:
        return query_sinan_numeric_distribution(table, value_expr, where_sql, bins=bins)

    stats_sql = f"""
        WITH base AS (
            SELECT {value_expr} AS valor
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT COUNT(*) AS n, MIN(valor) AS minimo, MAX(valor) AS maximo
        FROM base
        WHERE valor IS NOT NULL AND valor >= 0
    """
    stats = run_query(table, stats_sql)
    if stats.empty or int(stats.iloc[0]["n"] or 0) == 0:
        return pd.DataFrame()

    n = int(stats.iloc[0]["n"])
    minimo = float(stats.iloc[0]["minimo"])
    maximo = float(stats.iloc[0]["maximo"])
    estrato_sql = category_label_expr(stratification_sql, "Sem estrato")

    if minimo == maximo:
        sql_single = f"""
            WITH base AS (
                SELECT {value_expr} AS valor,
                       {estrato_sql} AS estrato
                FROM {table.ref_sql}
                {where_sql}
            ), validos AS (
                SELECT valor, estrato
                FROM base
                WHERE valor IS NOT NULL AND valor >= 0
            ), agg AS (
                SELECT estrato, COUNT(*) AS n
                FROM validos
                GROUP BY 1
            ), totals AS (
                SELECT estrato, n, SUM(n) OVER (PARTITION BY estrato) AS denominador
                FROM agg
            )
            SELECT {minimo!r} AS faixa_inicio,
                   {maximo!r} AS faixa_fim,
                   CAST({qstr(f'{minimo:g}')} AS VARCHAR) AS faixa,
                   estrato,
                   n,
                   denominador,
                   CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
            FROM totals
            ORDER BY estrato
        """
        return run_query(table, sql_single)

    bin_count = max(1, min(int(bins), n))
    width = (maximo - minimo) / bin_count
    if width <= 0:
        return pd.DataFrame()

    sql = f"""
        WITH base AS (
            SELECT {value_expr} AS valor,
                   {estrato_sql} AS estrato
            FROM {table.ref_sql}
            {where_sql}
        ), validos AS (
            SELECT valor, estrato
            FROM base
            WHERE valor IS NOT NULL AND valor >= 0
        ), binned AS (
            SELECT estrato,
                   CASE
                       WHEN valor = {maximo!r} THEN {bin_count - 1}
                       ELSE CAST(FLOOR((valor - {minimo!r}) / {width!r}) AS INTEGER)
                   END AS bin_idx
            FROM validos
        ), agg AS (
            SELECT estrato, bin_idx, COUNT(*) AS n
            FROM binned
            GROUP BY 1, 2
        ), with_totals AS (
            SELECT estrato, bin_idx, n, SUM(n) OVER (PARTITION BY estrato) AS denominador
            FROM agg
        )
        SELECT {minimo!r} + bin_idx * {width!r} AS faixa_inicio,
               CASE WHEN bin_idx = {bin_count - 1} THEN {maximo!r}
                    ELSE {minimo!r} + (bin_idx + 1) * {width!r} END AS faixa_fim,
               estrato,
               n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        ORDER BY faixa_inicio, estrato
    """
    df = run_query(table, sql)
    if df.empty:
        return df

    def fmt(value: object) -> str:
        try:
            num = float(value)
        except Exception:
            return str(value)
        if abs(num) >= 100 or abs(num - round(num)) < 1e-9:
            return f"{num:.0f}"
        return f"{num:.1f}".replace(".", ",")

    df["faixa"] = [f"{fmt(a)}–{fmt(b)}" for a, b in zip(df["faixa_inicio"], df["faixa_fim"])]
    return df



def query_sinan_numeric_distribution_stratified_by_reference_bins(
    table: LoadedTable,
    value_expr: str,
    where_sql: str,
    param_key: str,
    stratification_sql: Optional[str] = None,
) -> pd.DataFrame:
    """Distribuição numérica usando classes clínicas fixas, não bins automáticos.

    Os intervalos vêm de SINAN_LCR_DISTRIBUTION_BIN_SPECS e são alinhados às
    faixas da tabela-resumo do LCR. Quando não houver especificação para o
    parâmetro, a função recorre à distribuição automática anterior.
    """
    specs = sinan_lcr_distribution_bin_specs(param_key)
    if not specs:
        return query_sinan_numeric_distribution_stratified(table, value_expr, where_sql, stratification_sql)

    # Correção 3: neutraliza códigos sentinela de "ignorado" (ex.: 999/9999/99999)
    # antes de classificar o valor nas faixas clínicas fixas, para que eles não
    # sejam contados como dado real em nenhum bin. Ver observação sobre a
    # natureza provisória desses códigos em SINAN_LCR_SENTINEL_CODES.
    value_expr = sinan_lcr_clean_value_expr(value_expr, param_key)

    def sql_literal_or_null(value: object) -> str:
        if value is None:
            return "NULL"
        return repr(float(value))

    label_case = "CASE " + " ".join(f"WHEN {spec['condition']} THEN {qstr(spec['label'])}" for spec in specs) + " ELSE NULL END"
    order_case = "CASE " + " ".join(f"WHEN {spec['condition']} THEN {idx}" for idx, spec in enumerate(specs, start=1)) + " ELSE 999 END"
    start_case = "CASE " + " ".join(f"WHEN {spec['condition']} THEN {sql_literal_or_null(spec.get('start'))}" for spec in specs) + " ELSE NULL END"
    end_case = "CASE " + " ".join(f"WHEN {spec['condition']} THEN {sql_literal_or_null(spec.get('end'))}" for spec in specs) + " ELSE NULL END"
    leitura_case = "CASE " + " ".join(f"WHEN {spec['condition']} THEN {qstr(spec.get('leitura', ''))}" for spec in specs) + " ELSE NULL END"

    estrato_sql = category_label_expr(stratification_sql, "Sem estrato") if stratification_sql else qstr("Todos")

    sql = f"""
        WITH base AS (
            SELECT {value_expr} AS valor,
                   {estrato_sql} AS estrato
            FROM {table.ref_sql}
            {where_sql}
        ), validos AS (
            SELECT valor, estrato
            FROM base
            WHERE valor IS NOT NULL AND valor >= 0
        ), binned AS (
            SELECT estrato,
                   {label_case} AS faixa,
                   {order_case} AS ordem,
                   {start_case} AS faixa_inicio,
                   {end_case} AS faixa_fim,
                   {leitura_case} AS leitura
            FROM validos
        ), agg AS (
            SELECT estrato, faixa, ordem, faixa_inicio, faixa_fim, leitura, COUNT(*) AS n
            FROM binned
            WHERE faixa IS NOT NULL
            GROUP BY 1, 2, 3, 4, 5, 6
        ), with_totals AS (
            SELECT *, SUM(n) OVER (PARTITION BY estrato) AS denominador
            FROM agg
        )
        SELECT faixa_inicio,
               faixa_fim,
               faixa,
               estrato,
               n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct,
               ordem,
               leitura
        FROM with_totals
        ORDER BY ordem, estrato
    """
    df = run_query(table, sql)
    if df.empty:
        return df
    # Correção (revisão v49): preserva as faixas de referência com contagem zero.
    # A agregação SQL só devolve as faixas efetivamente presentes; em recortes
    # pequenos ou estratificados, faixas com n=0 desapareciam do eixo x, o que
    # quebrava a comparabilidade visual entre filtros, anos e estratos (mesma
    # gramática visual). Aqui reconstruímos a grade completa (faixa × estrato)
    # a partir das classes clínicas fixas, com n=0 explícito e pct=0.
    df = _sinan_lcr_fill_zero_reference_bins(df, param_key)
    meta = sinan_lcr_param_metadata(param_key)
    if meta:
        df["unidade"] = meta.unidade
        df["tipo_valor"] = meta.tipo_valor
        df["faixa_operacional"] = meta.faixa_operacional
        df["uso_permitido"] = meta.uso_permitido
        df["comportamento_truncamento"] = meta.comportamento_truncamento
    if not stratification_sql and "estrato" in df.columns:
        df = df.drop(columns=["estrato"])
    return df


def _sinan_lcr_fill_zero_reference_bins(df: pd.DataFrame, param_key: str) -> pd.DataFrame:
    """Completa a distribuição por faixas fixas mantendo bins com n=0 explícitos.

    A query agrega apenas as faixas com ocorrência; esta função reconstrói a
    grade completa de faixas clínicas (na ordem fixa das especificações) para
    cada estrato presente, atribuindo n=0/pct=0 às faixas ausentes. O
    denominador por estrato é preservado (soma dos n do estrato, idêntica ao
    SUM() OVER da própria query), de modo que os percentuais não mudam para as
    faixas que já existiam.
    """
    specs = sinan_lcr_distribution_bin_specs(param_key)
    if df.empty or not specs or "faixa" not in df.columns or "estrato" not in df.columns:
        return df
    bins = [
        {
            "faixa": str(spec["label"]),
            "ordem": idx,
            "faixa_inicio": (float(spec["start"]) if spec.get("start") is not None else np.nan),
            "faixa_fim": (float(spec["end"]) if spec.get("end") is not None else np.nan),
            "leitura": spec.get("leitura"),
        }
        for idx, spec in enumerate(specs, start=1)
    ]
    estratos = list(dict.fromkeys(df["estrato"].tolist()))
    denom_by_estrato = df.groupby("estrato")["n"].sum().to_dict()
    grid_rows = []
    for est in estratos:
        for b in bins:
            grid_rows.append({"estrato": est, **b})
    full = pd.DataFrame(grid_rows)
    existing = df[["estrato", "faixa", "n"]].copy()
    merged = full.merge(existing, on=["estrato", "faixa"], how="left")
    merged["n"] = pd.to_numeric(merged["n"], errors="coerce").fillna(0).astype(int)
    merged["denominador"] = merged["estrato"].map(denom_by_estrato).fillna(0).astype(int)
    merged["pct"] = np.where(
        merged["denominador"] > 0,
        (100.0 * merged["n"] / merged["denominador"]).round(2),
        np.nan,
    )
    ordered_cols = ["faixa_inicio", "faixa_fim", "faixa", "estrato", "n", "denominador", "pct", "ordem", "leitura"]
    merged = merged[ordered_cols].sort_values(["ordem", "estrato"]).reset_index(drop=True)
    return merged


# =============================================================================
# Classificação por faixas de LCR — comparação confirmados x faixa esperada
# e rastreio de possível meningite entre descartados (ver constantes acima).
# =============================================================================

def _lcr_predominio_expr(exprs: Dict[str, Optional[str]]) -> Optional[str]:
    """Predomínio celular (Neutrófilos x Linfócitos), ambos em % do total de leucócitos.

    Usa o wrapper de LCR para neutralizar sentinelas e descartar, apenas para o
    cálculo de predomínio, valores percentuais fora de 0-100.
    """
    neutro, linfo = exprs.get("lab_neutro"), exprs.get("lab_linfo")
    if not neutro or not linfo:
        return None
    neutro_limpo = sinan_lcr_analysis_value_expr(neutro, "neutro", enforce_percent_range=True)
    linfo_limpo = sinan_lcr_analysis_value_expr(linfo, "linfo", enforce_percent_range=True)
    return f"""
        CASE
            WHEN ({neutro_limpo}) IS NULL OR ({linfo_limpo}) IS NULL THEN NULL
            WHEN ({neutro_limpo}) > ({linfo_limpo}) THEN 'Neutrófilos'
            WHEN ({linfo_limpo}) > ({neutro_limpo}) THEN 'Linfócitos'
            ELSE 'Empate/indefinido'
        END
    """


def query_sinan_confirmed_param_vs_range(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    param: str,
) -> pd.DataFrame:
    """Para casos confirmados com grupo etiológico esperado definido, compara um
    parâmetro numérico (leuco/prot) contra a faixa de referência do próprio grupo
    e retorna, por grupo, quantos casos ficaram dentro, abaixo ou acima da faixa.
    """
    expected = exprs.get("expected_etiology_group")
    value_expr = exprs.get(f"lab_{param}")
    if not expected or not value_expr:
        return pd.DataFrame()
    value_expr = sinan_lcr_clean_value_expr(value_expr, param)

    case_rows = []
    for grupo, faixas in SINAN_LCR_ETIOLOGY_RANGES.items():
        rng = faixas.get(param)
        if not rng:
            continue
        lo, hi = rng
        if param == "prot" and grupo == "Fúngica" and float(hi) >= 10000:
            case_rows.append(
                f"WHEN grupo = {qstr(grupo)} THEN "
                f"CASE WHEN valor < {lo} THEN 'Abaixo da faixa típica' "
                f"ELSE 'Dentro da faixa típica' END"
            )
        else:
            case_rows.append(
                f"WHEN grupo = {qstr(grupo)} THEN "
                f"CASE WHEN valor < {lo} THEN 'Abaixo da faixa típica' "
                f"WHEN valor > {hi} THEN 'Acima da faixa típica' "
                f"ELSE 'Dentro da faixa típica' END"
            )
    if not case_rows:
        return pd.DataFrame()
    classificacao_sql = "CASE " + " ".join(case_rows) + " ELSE NULL END"

    sql = f"""
        WITH base AS (
            SELECT {expected} AS grupo, {value_expr} AS valor
            FROM {table.ref_sql}
            {where_sql}
        ), valid AS (
            SELECT grupo, valor, {classificacao_sql} AS posicao
            FROM base
            WHERE grupo IS NOT NULL AND valor IS NOT NULL AND valor >= 0
        ), totals AS (
            SELECT grupo, COUNT(*) AS total FROM valid GROUP BY 1
        )
        SELECT v.grupo AS grupo_etiologico,
               v.posicao,
               COUNT(*) AS n,
               t.total AS denominador,
               ROUND(100.0 * COUNT(*) / NULLIF(t.total, 0), 1) AS pct
        FROM valid v
        JOIN totals t USING (grupo)
        GROUP BY 1, 2, 4
        ORDER BY 1,
                 CASE v.posicao WHEN 'Abaixo da faixa típica' THEN 1 WHEN 'Dentro da faixa típica' THEN 2 WHEN 'Acima da faixa típica' THEN 3 ELSE 4 END
    """
    return run_query(table, sql)


def query_sinan_confirmed_predominio_vs_expected(
    table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str
) -> pd.DataFrame:
    """Compara o predomínio celular observado (neutrófilos x linfócitos) com o
    predomínio esperado para o grupo etiológico do caso confirmado."""
    expected = exprs.get("expected_etiology_group")
    predominio_expr = _lcr_predominio_expr(exprs)
    if not expected or not predominio_expr:
        return pd.DataFrame()

    expected_predominio_case = "CASE " + " ".join(
        f"WHEN {expected} = {qstr(grupo)} THEN {qstr(faixas['predominio'])}"
        for grupo, faixas in SINAN_LCR_ETIOLOGY_RANGES.items()
    ) + " ELSE NULL END"

    sql = f"""
        WITH base AS (
            SELECT {expected} AS grupo,
                   {predominio_expr} AS predominio_observado,
                   {expected_predominio_case} AS predominio_esperado
            FROM {table.ref_sql}
            {where_sql}
        ), valid AS (
            SELECT grupo, predominio_observado, predominio_esperado,
                   CASE
                       WHEN predominio_observado = predominio_esperado THEN 'Compatível com o esperado'
                       WHEN predominio_observado = 'Empate/indefinido' THEN 'Empate/indefinido'
                       ELSE 'Discordante do esperado'
                   END AS situacao
            FROM base
            WHERE grupo IS NOT NULL AND predominio_observado IS NOT NULL AND predominio_esperado IS NOT NULL
        ), totals AS (
            SELECT grupo, COUNT(*) AS total FROM valid GROUP BY 1
        )
        SELECT v.grupo AS grupo_etiologico,
               v.situacao,
               COUNT(*) AS n,
               t.total AS denominador,
               ROUND(100.0 * COUNT(*) / NULLIF(t.total, 0), 1) AS pct
        FROM valid v
        JOIN totals t USING (grupo)
        GROUP BY 1, 2, 4
        ORDER BY 1,
                 CASE v.situacao WHEN 'Compatível com o esperado' THEN 1 WHEN 'Discordante do esperado' THEN 2 ELSE 3 END
    """
    return run_query(table, sql)


def query_sinan_confirmed_glucose_vs_expected(
    table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str
) -> pd.DataFrame:
    """Compara a glicose absoluta do LCR (mg/dL — ver nota de limitação) com a
    direção esperada para o grupo etiológico: reduzida (<40) nas bacteriana/TB/
    fúngica; usualmente preservada (>=45) na viral."""
    expected = exprs.get("expected_etiology_group")
    glico = exprs.get("lab_glico")
    if not expected or not glico:
        return pd.DataFrame()
    glico = sinan_lcr_clean_value_expr(glico, "glico")

    case_rows = []
    for grupo, faixas in SINAN_LCR_ETIOLOGY_RANGES.items():
        if "glico_min" in faixas:
            case_rows.append(
                f"WHEN {expected} = {qstr(grupo)} THEN "
                f"CASE WHEN {glico} < {faixas['glico_min']} THEN 'Reduzida (atípico p/ viral)' ELSE 'Preservada (esperado)' END"
            )
        elif "glico_max" in faixas:
            case_rows.append(
                f"WHEN {expected} = {qstr(grupo)} THEN "
                f"CASE WHEN {glico} < {faixas['glico_max']} THEN 'Reduzida (esperado)' ELSE 'Preservada (atípico)' END"
            )
    if not case_rows:
        return pd.DataFrame()
    classificacao_sql = "CASE " + " ".join(case_rows) + " ELSE NULL END"

    sql = f"""
        WITH base AS (
            SELECT {expected} AS grupo, {glico} AS valor, {classificacao_sql} AS posicao
            FROM {table.ref_sql}
            {where_sql}
        ), valid AS (
            SELECT grupo, posicao FROM base
            WHERE grupo IS NOT NULL AND valor IS NOT NULL AND valor >= 0 AND posicao IS NOT NULL
        ), totals AS (
            SELECT grupo, COUNT(*) AS total FROM valid GROUP BY 1
        )
        SELECT v.grupo AS grupo_etiologico,
               v.posicao,
               COUNT(*) AS n,
               t.total AS denominador,
               ROUND(100.0 * COUNT(*) / NULLIF(t.total, 0), 1) AS pct
        FROM valid v
        JOIN totals t USING (grupo)
        GROUP BY 1, 2, 4
        ORDER BY 1, 2
    """
    return run_query(table, sql)



def query_sinan_confirmed_aspect_vs_expected(
    table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str
) -> pd.DataFrame:
    """Compara o aspecto do líquor observado no campo LAB_ASPECT com o aspecto
    esperado para o grupo etiológico registrado no SINAN.

    A comparação é propositalmente descritiva: aspecto límpido, turvo ou
    purulento ajuda a contextualizar a etiologia, mas não substitui cultura,
    PCR, antígeno, bacterioscopia ou a interpretação clínico-laboratorial.
    """
    expected = exprs.get("expected_etiology_group")
    aspect_code = exprs.get("lab_aspect_code")
    aspect_label = exprs.get("lab_aspect_label")
    if not expected or not aspect_code or not aspect_label:
        return pd.DataFrame()

    expected_aspect_case = "CASE " + " ".join(
        f"WHEN grupo = {qstr(grupo)} THEN {qstr(info['descricao'])}"
        for grupo, info in SINAN_LCR_EXPECTED_ASPECT.items()
    ) + " ELSE NULL END"

    compatibility_rows = []
    for grupo, info in SINAN_LCR_EXPECTED_ASPECT.items():
        codes = ", ".join(qstr(code) for code in sorted(info["compatible_codes"]))
        compatibility_rows.append(
            f"WHEN grupo = {qstr(grupo)} AND aspecto_code IN ({codes}) THEN 'Compatível com o esperado'"
        )
    compatibility_case = "CASE " + " ".join(compatibility_rows) + """
        WHEN aspecto_code IS NULL OR aspecto_code = '9' THEN 'Ignorado/sem informação'
        WHEN aspecto_code IN ('3', '5', '6') THEN 'Outro aspecto/atípico'
        ELSE 'Discordante do esperado'
    END"""

    sql = f"""
        WITH base AS (
            SELECT {expected} AS grupo,
                   {aspect_code} AS aspecto_code,
                   {aspect_label} AS aspecto_observado
            FROM {table.ref_sql}
            {where_sql}
        ), classified AS (
            SELECT grupo,
                   COALESCE(aspecto_observado, 'Sem informação/ignorado') AS aspecto_observado,
                   {expected_aspect_case} AS aspecto_esperado,
                   {compatibility_case} AS situacao
            FROM base
            WHERE grupo IS NOT NULL
        ), totals AS (
            SELECT grupo, COUNT(*) AS total FROM classified GROUP BY 1
        )
        SELECT c.grupo AS grupo_etiologico,
               c.aspecto_esperado,
               c.situacao,
               STRING_AGG(DISTINCT c.aspecto_observado, ', ') AS aspectos_observados,
               COUNT(*) AS n,
               t.total AS denominador,
               ROUND(100.0 * COUNT(*) / NULLIF(t.total, 0), 1) AS pct
        FROM classified c
        JOIN totals t USING (grupo)
        GROUP BY c.grupo, c.aspecto_esperado, c.situacao, t.total
        ORDER BY 1,
                 CASE c.situacao
                    WHEN 'Compatível com o esperado' THEN 1
                    WHEN 'Discordante do esperado' THEN 2
                    WHEN 'Outro aspecto/atípico' THEN 3
                    WHEN 'Ignorado/sem informação' THEN 4
                    ELSE 5
                 END
    """
    return run_query(table, sql)



def _sinan_lcr_independent_classification_with_sql(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
) -> Optional[str]:
    """Monta CTE com classificação provável pelo LCR sem usar CON_DIAGES.

    A classificação é deliberadamente exploratória: cada etiologia recebe um
    ponto quando o parâmetro disponível combina com seu padrão típico
    (leucócitos, proteína, glicose absoluta, predomínio celular e aspecto).
    A etiologia com maior pontuação é apresentada como "provável pelo LCR";
    empate, ausência de dados ou pontuação zero são tratados como
    indeterminados.
    """
    leuco = sinan_lcr_clean_value_expr(exprs["lab_leuco"], "leuco") if exprs.get("lab_leuco") else "NULL"
    prot = sinan_lcr_clean_value_expr(exprs["lab_prot"], "prot") if exprs.get("lab_prot") else "NULL"
    glico = sinan_lcr_clean_value_expr(exprs["lab_glico"], "glico") if exprs.get("lab_glico") else "NULL"
    predominio = _lcr_predominio_expr(exprs) or "NULL"
    aspecto_code = exprs.get("lab_aspect_code") or "NULL"
    expected = exprs.get("expected_etiology_group") or "NULL"
    classi_code = exprs.get("classi_code")
    if classi_code:
        case_status = f"""
            CASE
                WHEN {classi_code} = '1' THEN 'Casos confirmados'
                WHEN {classi_code} = '2' THEN 'Casos descartados'
                ELSE 'Sem classificação / ignorados'
            END
        """
    else:
        case_status = qstr("Casos avaliados")

    valid_count = " + ".join([
        "CASE WHEN leuco IS NOT NULL AND leuco >= 0 THEN 1 ELSE 0 END",
        "CASE WHEN prot IS NOT NULL AND prot >= 0 THEN 1 ELSE 0 END",
        "CASE WHEN glico IS NOT NULL AND glico >= 0 THEN 1 ELSE 0 END",
        "CASE WHEN predominio_observado IS NOT NULL AND predominio_observado <> 'Empate/indefinido' THEN 1 ELSE 0 END",
        "CASE WHEN aspecto_code IS NOT NULL AND aspecto_code <> '9' THEN 1 ELSE 0 END",
    ])

    score_selects: List[str] = []
    for grupo, faixas in SINAN_LCR_ETIOLOGY_RANGES.items():
        score_parts: List[str] = []
        leuco_rng = faixas.get("leuco")
        if leuco_rng:
            lo, hi = leuco_rng
            score_parts.append(f"CASE WHEN leuco IS NOT NULL AND leuco >= {lo} AND leuco <= {hi} THEN 1 ELSE 0 END")
        prot_rng = faixas.get("prot")
        if prot_rng:
            lo, hi = prot_rng
            if grupo == "Fúngica" and float(hi) >= 10000:
                score_parts.append(f"CASE WHEN prot IS NOT NULL AND prot >= {lo} THEN 1 ELSE 0 END")
            else:
                score_parts.append(f"CASE WHEN prot IS NOT NULL AND prot >= {lo} AND prot <= {hi} THEN 1 ELSE 0 END")
        if "glico_min" in faixas:
            score_parts.append(f"CASE WHEN glico IS NOT NULL AND glico >= {faixas['glico_min']} THEN 1 ELSE 0 END")
        elif "glico_max" in faixas:
            score_parts.append(f"CASE WHEN glico IS NOT NULL AND glico < {faixas['glico_max']} THEN 1 ELSE 0 END")
        score_parts.append(
            f"CASE WHEN predominio_observado IS NOT NULL AND predominio_observado = {qstr(faixas['predominio'])} THEN 1 ELSE 0 END"
        )
        aspect_info = SINAN_LCR_EXPECTED_ASPECT.get(grupo)
        if aspect_info:
            codes = ", ".join(qstr(code) for code in sorted(aspect_info["compatible_codes"]))
            score_parts.append(f"CASE WHEN aspecto_code IS NOT NULL AND aspecto_code IN ({codes}) THEN 1 ELSE 0 END")
        score_sql = " + ".join(score_parts) if score_parts else "0"
        score_selects.append(
            f"""
            SELECT row_id, grupo_caso, grupo_sinan, {qstr(grupo)} AS grupo_lcr,
                   ({score_sql}) AS pontos,
                   criterios_validos
            FROM base
            """
        )

    scored_union = "\nUNION ALL\n".join(score_selects)
    groups_list = ", ".join(qstr(grupo) for grupo in SINAN_ETIOLOGY_GROUPS)
    return f"""
        WITH base_raw AS (
            SELECT
                ROW_NUMBER() OVER () AS row_id,
                {case_status} AS grupo_caso,
                {expected} AS grupo_sinan,
                {leuco} AS leuco,
                {prot} AS prot,
                {glico} AS glico,
                {predominio} AS predominio_observado,
                {aspecto_code} AS aspecto_code
            FROM {table.ref_sql}
            {where_sql}
        ), base AS (
            SELECT *, ({valid_count}) AS criterios_validos
            FROM base_raw
        ), scored AS (
            {scored_union}
        ), ranked AS (
            SELECT *, MAX(pontos) OVER (PARTITION BY row_id) AS pontos_melhor
            FROM scored
        ), best_rows AS (
            SELECT * FROM ranked WHERE pontos = pontos_melhor
        ), resolved AS (
            SELECT
                row_id,
                ANY_VALUE(grupo_caso) AS grupo_caso,
                ANY_VALUE(grupo_sinan) AS grupo_sinan,
                MAX(criterios_validos) AS criterios_validos,
                MAX(pontos_melhor) AS pontos_melhor,
                COUNT(*) AS empates_melhor,
                MIN(grupo_lcr) AS grupo_lcr_melhor,
                CASE
                    WHEN MAX(criterios_validos) = 0 THEN 'Sem dados suficientes'
                    WHEN MAX(pontos_melhor) = 0 THEN 'Indeterminado/baixo suporte'
                    WHEN COUNT(*) > 1 THEN 'Indeterminado/empate'
                    ELSE MIN(grupo_lcr)
                END AS classificacao_lcr,
                CASE
                    WHEN MAX(criterios_validos) > 0 THEN ROUND(100.0 * MAX(pontos_melhor) / MAX(criterios_validos), 1)
                    ELSE NULL
                END AS suporte_pct
            FROM best_rows
            GROUP BY row_id
        ), resolved_labeled AS (
            SELECT *,
                   CASE
                       WHEN grupo_sinan IN ({groups_list}) AND classificacao_lcr = grupo_sinan THEN 'Concordante com SINAN'
                       WHEN classificacao_lcr IN ({groups_list}) AND grupo_sinan IN ({groups_list}) THEN 'Discordante do SINAN'
                       WHEN grupo_sinan IN ({groups_list}) THEN 'Indeterminado pelo LCR'
                       ELSE 'Sem grupo SINAN comparável'
                   END AS situacao_vs_sinan
            FROM resolved
        )
    """


def query_sinan_lcr_independent_distribution(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
) -> pd.DataFrame:
    """Distribuição da classificação provável pelo LCR, sem usar CON_DIAGES."""
    with_sql = _sinan_lcr_independent_classification_with_sql(table, exprs, where_sql)
    if not with_sql:
        return pd.DataFrame()
    case_order = {
        "Casos confirmados": 1,
        "Casos descartados": 2,
        "Sem classificação / ignorados": 3,
        "Casos avaliados": 4,
    }
    class_order = {grupo: idx for idx, grupo in enumerate(SINAN_ETIOLOGY_GROUPS, start=1)}
    class_order.update({"Indeterminado/empate": 90, "Indeterminado/baixo suporte": 91, "Sem dados suficientes": 92})
    case_order_sql = "CASE " + " ".join(f"WHEN grupo_caso = {qstr(k)} THEN {v}" for k, v in case_order.items()) + " ELSE 99 END"
    class_order_sql = "CASE " + " ".join(f"WHEN classificacao_lcr = {qstr(k)} THEN {v}" for k, v in class_order.items()) + " ELSE 99 END"
    sql = f"""
        {with_sql}, agg AS (
            SELECT grupo_caso,
                   classificacao_lcr,
                   COUNT(*) AS n,
                   ROUND(AVG(suporte_pct), 1) AS suporte_medio_pct
            FROM resolved_labeled
            GROUP BY grupo_caso, classificacao_lcr
        ), totals AS (
            SELECT grupo_caso, SUM(n) AS denominador
            FROM agg
            GROUP BY grupo_caso
        )
        SELECT a.grupo_caso,
               a.classificacao_lcr,
               a.n,
               t.denominador,
               ROUND(100.0 * a.n / NULLIF(t.denominador, 0), 1) AS pct,
               a.suporte_medio_pct,
               {case_order_sql} AS ordem_caso,
               {class_order_sql} AS ordem_lcr
        FROM agg a
        JOIN totals t USING (grupo_caso)
        ORDER BY ordem_caso, ordem_lcr, a.classificacao_lcr
    """
    return run_query(table, sql)


def query_sinan_lcr_independent_vs_sinan(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
) -> pd.DataFrame:
    """Compara a classificação provável pelo LCR com a etiologia oficial do SINAN nos confirmados."""
    with_sql = _sinan_lcr_independent_classification_with_sql(table, exprs, where_sql)
    if not with_sql:
        return pd.DataFrame()
    groups_list = ", ".join(qstr(grupo) for grupo in SINAN_ETIOLOGY_GROUPS)
    situation_order = {
        "Concordante com SINAN": 1,
        "Discordante do SINAN": 2,
        "Indeterminado pelo LCR": 3,
        "Sem grupo SINAN comparável": 4,
    }
    situation_order_sql = "CASE " + " ".join(f"WHEN situacao_vs_sinan = {qstr(k)} THEN {v}" for k, v in situation_order.items()) + " ELSE 99 END"
    sql = f"""
        {with_sql}, agg AS (
            SELECT grupo_sinan AS grupo_etiologico_sinan,
                   classificacao_lcr,
                   situacao_vs_sinan,
                   COUNT(*) AS n,
                   ROUND(AVG(suporte_pct), 1) AS suporte_medio_pct
            FROM resolved_labeled
            WHERE grupo_caso = 'Casos confirmados'
              AND grupo_sinan IN ({groups_list})
            GROUP BY grupo_sinan, classificacao_lcr, situacao_vs_sinan
        ), totals AS (
            SELECT grupo_etiologico_sinan, SUM(n) AS denominador
            FROM agg
            GROUP BY grupo_etiologico_sinan
        )
        SELECT a.grupo_etiologico_sinan,
               a.classificacao_lcr,
               a.situacao_vs_sinan,
               a.n,
               t.denominador,
               ROUND(100.0 * a.n / NULLIF(t.denominador, 0), 1) AS pct,
               a.suporte_medio_pct,
               {situation_order_sql} AS ordem_situacao
        FROM agg a
        JOIN totals t USING (grupo_etiologico_sinan)
        ORDER BY a.grupo_etiologico_sinan, ordem_situacao, a.classificacao_lcr
    """
    return run_query(table, sql)


def query_sinan_lcr_aspect_distribution(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    stratification_sql: Optional[str] = None,
) -> pd.DataFrame:
    """Distribuição do campo Aspecto do Líquor conforme categorias oficiais
    da ficha do SINAN: límpido, purulento, hemorrágico, turvo, xantocrômico,
    outro e ignorado.
    """
    aspect_label = exprs.get("lab_aspect_label")
    if not aspect_label:
        return pd.DataFrame()
    estrato_sql = category_label_expr(stratification_sql, "Sem estrato") if stratification_sql else qstr("Todos")
    order_case = "CASE " + " ".join(
        f"WHEN categoria = {qstr(label)} THEN {idx}"
        for idx, label in enumerate(SINAN_LAB_ASPECT_ORDER, start=1)
    ) + " ELSE 999 END"
    sql = f"""
        WITH base AS (
            SELECT COALESCE({aspect_label}, 'Sem informação/ignorado') AS categoria,
                   {estrato_sql} AS estrato
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT categoria, estrato, COUNT(*) AS n
            FROM base
            GROUP BY 1, 2
        ), with_totals AS (
            SELECT *, SUM(n) OVER (PARTITION BY estrato) AS denominador
            FROM agg
        )
        SELECT categoria,
               estrato,
               n,
               denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct,
               {order_case} AS ordem
        FROM with_totals
        ORDER BY ordem, estrato
    """
    df = run_query(table, sql)
    if df.empty:
        return df
    if not stratification_sql and "estrato" in df.columns:
        df = df.drop(columns=["estrato"])
    return df

def query_sinan_discarded_meningitis_risk(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    stratification_sql: Optional[str] = None,
) -> pd.DataFrame:
    """Para casos descartados com LCR coletado, calcula quantos seriam
    classificáveis como possível meningite considerando isoladamente:
    - pleocitose (leucócitos dentro de qualquer faixa etiológica, ou seja >=20);
    - glicose reduzida (<40 mg/dL absoluto no LCR);
    - proteína elevada (>=45 mg/dL, menor limiar entre as faixas usadas).
    Cada critério é avaliado de forma independente (não é uma combinação AND),
    propositalmente, para mostrar o efeito de cada marcador isolado, como pedido.
    Pode ser estratificado por idade operacional (neonatos até 6 meses x crianças/adultos).
    """
    leuco, glico, prot = exprs.get("lab_leuco"), exprs.get("lab_glico"), exprs.get("lab_prot")
    rows = []
    estrato_sql = category_label_expr(stratification_sql, "Sem estrato") if stratification_sql else qstr("Todos")

    def _flag_query(label: str, value_expr: Optional[str], threshold_sql: str) -> Optional[pd.DataFrame]:
        if not value_expr:
            return None
        valor_limpo = sinan_lcr_neutralize_sentinel_expr(value_expr)
        sql = f"""
            WITH base AS (
                SELECT {valor_limpo} AS valor,
                       {estrato_sql} AS estrato
                FROM {table.ref_sql}
                {where_sql}
            ), valid AS (
                SELECT valor, estrato FROM base WHERE valor IS NOT NULL AND valor >= 0
            )
            SELECT {qstr(label)} AS criterio,
                   estrato,
                   COUNT(*) FILTER (WHERE {threshold_sql}) AS n_sugestivo,
                   COUNT(*) AS denominador,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE {threshold_sql}) / NULLIF(COUNT(*), 0), 1) AS pct_sugestivo
            FROM valid
            GROUP BY estrato
        """
        return run_query(table, sql)

    pleo = _flag_query("Pleocitose isolada (leucócitos ≥ 20 céls/mm³)", leuco, "valor >= 20")
    if pleo is not None and not pleo.empty:
        rows.append(pleo)
    gli = _flag_query("Glicose reduzida isolada (< 40 mg/dL no LCR)", glico, "valor < 40")
    if gli is not None and not gli.empty:
        rows.append(gli)
    pr = _flag_query("Proteína elevada isolada (≥ 45 mg/dL)", prot, "valor >= 45")
    if pr is not None and not pr.empty:
        rows.append(pr)

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    if not stratification_sql and "estrato" in out.columns:
        out = out.drop(columns=["estrato"])
    return out


def query_sinan_confirmed_etiology_counts(
    table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str
) -> pd.DataFrame:
    """Conta quantos casos confirmados (com LCR) caem em cada grupo etiológico
    esperado — usado como denominador/contexto para os gráficos de comparação."""
    expected = exprs.get("expected_etiology_group")
    if not expected:
        return pd.DataFrame()
    sql = f"""
        SELECT {expected} AS grupo_etiologico, COUNT(*) AS n
        FROM {table.ref_sql}
        {where_sql}
        GROUP BY 1
        ORDER BY 2 DESC
    """
    df = run_query(table, sql)
    return df[df["grupo_etiologico"].notna()] if not df.empty else df


def query_sinan_numeric_field_with_class_filter(
    table: LoadedTable, value_expr: str, where_sql: str
) -> int:
    """Conta quantos registros têm valor numérico válido (>=0) para o expr dado,
    dentro do where_sql informado. Usado para denominadores de cobertura."""
    sql = f"""
        WITH base AS (SELECT {value_expr} AS valor FROM {table.ref_sql} {where_sql})
        SELECT COUNT(*) AS n FROM base WHERE valor IS NOT NULL AND valor >= 0
    """
    df = run_query(table, sql)
    return int(df.iloc[0]["n"]) if not df.empty else 0


def query_sinan_indicators(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    dt, classi, evol = exprs.get("dt"), exprs.get("classi_code"), exprs.get("evol_code")
    con_group = exprs.get("con_group")
    if not (dt and classi and evol):
        return pd.DataFrame()
    extra = f", {con_group} AS etiologia" if con_group else ", NULL AS etiologia"
    encerr = exprs.get("dt_encerramento")
    notif = exprs.get("dt_notificacao") or dt
    dias_encerr = f"DATEDIFF('day', {notif}, {encerr})" if encerr and notif else "NULL"
    sql = f"""
        WITH base AS (
            SELECT {dt} AS dt,
                   {classi} AS classi,
                   {evol} AS evol,
                   {dias_encerr} AS dias_encerramento
                   {extra}
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT EXTRACT(YEAR FROM dt) AS ano,
                   COUNT(*) AS notificacoes,
                   COUNT(*) FILTER (WHERE classi = '1') AS confirmados,
                   COUNT(*) FILTER (WHERE classi = '2') AS descartados,
                   COUNT(*) FILTER (WHERE classi IS NULL OR classi NOT IN ('1','2')) AS sem_classificacao,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol = '1') AS altas_confirmados,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol = '2') AS obitos_meningite_confirmados,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol = '3') AS obitos_outra_causa_confirmados,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol IN ('1','2','3')) AS confirmados_evolucao_conhecida,
                   COUNT(*) FILTER (WHERE classi = '1' AND (evol IS NULL OR evol = '9')) AS confirmados_evolucao_ignorada,
                   median(dias_encerramento) FILTER (WHERE dias_encerramento BETWEEN 0 AND 3650) AS mediana_dias_encerramento
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1
        )
        SELECT *,
               {pct_expr('confirmados', 'notificacoes')} AS pct_confirmacao,
               {pct_expr('descartados', 'notificacoes')} AS pct_descarte,
               {pct_expr('sem_classificacao', 'notificacoes')} AS pct_sem_classificacao,
               {pct_expr('obitos_meningite_confirmados', 'notificacoes')} AS pct_obitos_meningite_confirmados_notificacoes,
               {pct_expr('obitos_meningite_confirmados', 'confirmados')} AS letalidade_confirmados,
               {pct_expr('obitos_meningite_confirmados', 'confirmados_evolucao_conhecida')} AS letalidade_confirmados_evolucao_conhecida
        FROM agg
        ORDER BY ano
    """
    return run_query(table, sql)



def _available_column_specs(columns: Sequence[str], specs: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    available: List[Tuple[str, str]] = []
    for default_col, label in specs:
        col = choose_candidate(columns, [default_col])
        if col:
            available.append((col, label))
    return available


def query_sinan_symptom_prevalence(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str, symptom_specs: Sequence[Tuple[str, str]]) -> pd.DataFrame:
    dt = exprs.get("dt")
    classi = exprs.get("classi_code")
    if not (dt and classi and symptom_specs):
        return pd.DataFrame()

    unions = []
    for col, label in symptom_specs:
        unions.append(
            f"""
            SELECT {dt} AS dt,
                   {classi} AS classi,
                   {qstr(label)} AS sintoma,
                   {clean_code_expr(col)} AS sintoma_codigo
            FROM {table.ref_sql}
            {where_sql}
            """
        )

    sql = f"""
        WITH long AS (
            {' UNION ALL '.join(unions)}
        ), base AS (
            SELECT EXTRACT(YEAR FROM dt) AS ano,
                   sintoma,
                   sintoma_codigo
            FROM long
            WHERE dt IS NOT NULL
              AND classi = '1'
        ), agg AS (
            SELECT ano,
                   sintoma,
                   COUNT(*) AS confirmados,
                   COUNT(*) FILTER (WHERE sintoma_codigo = '1') AS sintoma_sim,
                   COUNT(*) FILTER (WHERE sintoma_codigo = '2') AS sintoma_nao,
                   COUNT(*) FILTER (WHERE sintoma_codigo IS NULL OR sintoma_codigo NOT IN ('1','2')) AS sintoma_ignorado
            FROM base
            GROUP BY 1, 2
        )
        SELECT *,
               {pct_expr('sintoma_sim', 'confirmados')} AS pct_sintoma_confirmados
        FROM agg
        ORDER BY ano, sintoma
    """
    return run_query(table, sql)


def query_sinan_hospitalization_internment(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    hospital_col: Optional[str],
    internment_col: Optional[str] = None,
) -> pd.DataFrame:
    dt = exprs.get("dt")
    classi = exprs.get("classi_code")
    if not (dt and classi and hospital_col):
        return pd.DataFrame()

    hospital_code = clean_code_expr(hospital_col)
    sql = f"""
        WITH base AS (
            SELECT EXTRACT(YEAR FROM {dt}) AS ano,
                   {classi} AS classi,
                   {hospital_code} AS hospitalizacao
            FROM {table.ref_sql}
            {where_sql}
        ), scoped AS (
            SELECT ano,
                   'Total de notificações' AS grupo_caso,
                   1 AS ordem_grupo,
                   COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE hospitalizacao = '1') AS n
            FROM base
            WHERE ano IS NOT NULL
            GROUP BY 1
            UNION ALL
            SELECT ano,
                   'Confirmados' AS grupo_caso,
                   2 AS ordem_grupo,
                   COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE hospitalizacao = '1') AS n
            FROM base
            WHERE ano IS NOT NULL
              AND classi = '1'
            GROUP BY 1
            UNION ALL
            SELECT ano,
                   'Descartados' AS grupo_caso,
                   3 AS ordem_grupo,
                   COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE hospitalizacao = '1') AS n
            FROM base
            WHERE ano IS NOT NULL
              AND classi = '2'
            GROUP BY 1
            UNION ALL
            SELECT ano,
                   'Sem confirmação / ignorados' AS grupo_caso,
                   4 AS ordem_grupo,
                   COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE hospitalizacao = '1') AS n
            FROM base
            WHERE ano IS NOT NULL
              AND (classi IS NULL OR classi NOT IN ('1', '2'))
            GROUP BY 1
        )
        SELECT ano,
               grupo_caso,
               'Hospitalização informada (ATE_HOSPIT = Sim)' AS indicador,
               n,
               denominador,
               {pct_expr('n', 'denominador')} AS pct,
               ordem_grupo
        FROM scoped
        ORDER BY ano, ordem_grupo
    """
    return run_query(table, sql)

# Correção "Denominador Não Específico para Quimioprofilaxia":
# O indicador agora exibe duas visões quando CON_DIAGES está disponível:
# (1) todos os registros do recorte, para auditoria histórica; e
# (2) elegíveis operacionais para quimioprofilaxia de contatos
#     (CON_DIAGES 02/03/09: formas meningocócicas e Haemophilus influenzae).
# Assim, o usuário enxerga o efeito do denominador amplo sem confundir cobertura
# de intervenção com casos em que a variável não deveria ser preenchida.
def query_sinan_communicants_prophylaxis(
    table: LoadedTable,
    exprs: Dict[str, Optional[str]],
    where_sql: str,
    communicants_col: Optional[str],
    prophylaxis_col: Optional[str],
) -> pd.DataFrame:
    dt = exprs.get("dt")
    if not (dt and (communicants_col or prophylaxis_col)):
        return pd.DataFrame()

    con_code = exprs.get("con_code")
    communicants = numeric_expr(communicants_col) if communicants_col else "CAST(NULL AS DOUBLE)"
    prophylaxis = case_from_mapping(clean_code_expr(prophylaxis_col), YES_NO_IGN, "Sem informação") if prophylaxis_col else qstr("Sem informação")
    con_select = con_code if con_code else "CAST(NULL AS VARCHAR)"
    recorte_case = """
        CASE
            WHEN con_code IN ('02', '03', '09') THEN 'Elegíveis operacionais (CON_DIAGES 02/03/09)'
            ELSE NULL
        END
    """ if con_code else "CAST(NULL AS VARCHAR)"
    sql = f"""
        WITH base AS (
            SELECT EXTRACT(YEAR FROM {dt}) AS ano,
                   {con_select} AS con_code,
                   {communicants} AS comunicantes,
                   {prophylaxis} AS quimioprofilaxia
            FROM {table.ref_sql}
            {where_sql}
        ), recortes AS (
            SELECT 'Todos os registros do recorte' AS recorte_quimioprofilaxia,
                   ano, comunicantes, quimioprofilaxia
            FROM base
            UNION ALL
            SELECT {recorte_case} AS recorte_quimioprofilaxia,
                   ano, comunicantes, quimioprofilaxia
            FROM base
            WHERE con_code IN ('02', '03', '09')
        ), agg AS (
            SELECT recorte_quimioprofilaxia,
                   ano,
                   quimioprofilaxia,
                   COUNT(*) AS registros,
                   COUNT(*) FILTER (WHERE comunicantes IS NOT NULL AND comunicantes >= 0) AS registros_com_comunicantes,
                   SUM(CASE WHEN comunicantes IS NOT NULL AND comunicantes >= 0 THEN comunicantes ELSE 0 END) AS comunicantes_total,
                   ROUND(AVG(comunicantes) FILTER (WHERE comunicantes IS NOT NULL AND comunicantes >= 0), 2) AS media_comunicantes
            FROM recortes
            WHERE ano IS NOT NULL
              AND recorte_quimioprofilaxia IS NOT NULL
              AND (comunicantes IS NOT NULL OR quimioprofilaxia <> 'Sem informação')
            GROUP BY 1, 2, 3
        ), with_totals AS (
            SELECT *,
                   SUM(registros) OVER (PARTITION BY recorte_quimioprofilaxia, ano) AS total_registros_ano,
                   SUM(comunicantes_total) OVER (PARTITION BY recorte_quimioprofilaxia, ano) AS total_comunicantes_ano
            FROM agg
        )
        SELECT *,
               {pct_expr('registros', 'total_registros_ano')} AS pct_registros_ano,
               {pct_expr('comunicantes_total', 'total_comunicantes_ano')} AS pct_comunicantes_ano
        FROM with_totals
        ORDER BY recorte_quimioprofilaxia, ano, quimioprofilaxia
    """
    return run_query(table, sql)


def query_sinan_vaccination_by_classification(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str, vaccine_specs: Sequence[Tuple[str, str]]) -> pd.DataFrame:
    classi = exprs.get("classi_code")
    if not (classi and vaccine_specs):
        return pd.DataFrame()

    unions = []
    for col, label in vaccine_specs:
        unions.append(
            f"""
            SELECT {qstr(label)} AS vacina,
                   {classi} AS classi,
                   {clean_code_expr(col)} AS vacina_codigo
            FROM {table.ref_sql}
            {where_sql}
            """
        )

    sql = f"""
        WITH long AS (
            {' UNION ALL '.join(unions)}
        ), base AS (
            SELECT vacina,
                   CASE
                       WHEN classi = '1' THEN 'Confirmados'
                       WHEN classi = '2' THEN 'Descartados'
                       ELSE 'Sem classificação / ignorados'
                   END AS grupo_classificacao,
                   vacina_codigo
            FROM long
        ), agg AS (
            SELECT vacina,
                   grupo_classificacao,
                   COUNT(*) AS denominador,
                   COUNT(*) FILTER (WHERE vacina_codigo = '1') AS vacinados_sim,
                   COUNT(*) FILTER (WHERE vacina_codigo = '2') AS vacinados_nao,
                   COUNT(*) FILTER (WHERE vacina_codigo IS NULL OR vacina_codigo NOT IN ('1','2')) AS vacinacao_ignorada
            FROM base
            WHERE grupo_classificacao IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT *,
               {pct_expr('vacinados_sim', 'denominador')} AS pct_vacinados_sim
        FROM agg
        ORDER BY vacina,
                 CASE
                    WHEN grupo_classificacao = 'Confirmados' THEN 0
                    WHEN grupo_classificacao = 'Descartados' THEN 1
                    ELSE 2
                 END
    """
    return run_query(table, sql)


def query_sinan_etiology_lethality(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    classi, evol, con_group, con_label = exprs.get("classi_code"), exprs.get("evol_code"), exprs.get("con_group"), exprs.get("con_label")
    if not (classi and evol and con_group):
        return pd.DataFrame()
    sql = f"""
        WITH base AS (
            SELECT {classi} AS classi,
                   {evol} AS evol,
                   {con_group} AS grupo_etiologico,
                   {con_label or con_group} AS conclusao_diagnostica
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT grupo_etiologico,
                   COUNT(*) FILTER (WHERE classi = '1') AS confirmados,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol = '2') AS obitos_meningite,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol = '3') AS obitos_outra_causa,
                   COUNT(*) FILTER (WHERE classi = '1' AND evol IN ('1','2','3')) AS confirmados_evolucao_conhecida,
                   COUNT(*) FILTER (WHERE classi = '1' AND (evol IS NULL OR evol = '9')) AS confirmados_evolucao_ignorada
            FROM base
            GROUP BY 1
        )
        SELECT *,
               {pct_expr('obitos_meningite', 'confirmados')} AS letalidade_pct,
               {pct_expr('obitos_meningite', 'confirmados_evolucao_conhecida')} AS letalidade_evolucao_conhecida_pct
        FROM agg
        WHERE confirmados > 0
        ORDER BY confirmados DESC, grupo_etiologico
    """
    return run_query(table, sql)


def query_sinan_diagnostics_by_year(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    dt = exprs.get("dt")
    classi = exprs.get("classi_code")
    cid_group = exprs.get("sinan_cid10_conversion_group")
    cid_type = exprs.get("sinan_cid10_conversion_type")
    include = exprs.get("sinan_cid10_conversion_include")
    if not (dt and classi and cid_group and cid_type and include):
        return pd.DataFrame()
    ordem = """
        CASE cid10_grupo
            WHEN 'A17.0' THEN 1
            WHEN 'A39.0' THEN 2
            WHEN 'A87' THEN 3
            WHEN 'G00' THEN 4
            WHEN 'G01' THEN 5
            WHEN 'G02' THEN 6
            WHEN 'G03' THEN 7
            WHEN 'G04' THEN 8
            WHEN 'G05' THEN 9
            ELSE 99
        END
    """
    sql = f"""
        WITH base AS (
            SELECT CAST(EXTRACT(YEAR FROM {dt}) AS INTEGER) AS ano,
                   {classi} AS classi,
                   {cid_group} AS cid10_grupo,
                   {cid_type} AS grupo_etiologico
            FROM {table.ref_sql}
            {append_clause(where_sql, f"{classi} = '1' AND {include} = 'Sim'")}
        ), agg AS (
            SELECT ano,
                   cid10_grupo,
                   grupo_etiologico,
                   COUNT(*) AS confirmados
            FROM base
            WHERE ano IS NOT NULL
              AND cid10_grupo IS NOT NULL
              AND grupo_etiologico IS NOT NULL
            GROUP BY 1, 2, 3
        ), totais AS (
            SELECT ano, SUM(confirmados) AS total_ano
            FROM agg
            GROUP BY 1
        )
        SELECT agg.ano,
               agg.cid10_grupo,
               agg.grupo_etiologico,
               agg.confirmados,
               totais.total_ano,
               CASE WHEN totais.total_ano > 0 THEN ROUND(100.0 * agg.confirmados / totais.total_ano, 2) ELSE NULL END AS pct_ano
        FROM agg
        JOIN totais USING (ano)
        WHERE agg.confirmados > 0
        ORDER BY agg.ano, {ordem}, agg.grupo_etiologico
    """
    return run_query(table, sql)


def query_sim_indicators(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    dt = exprs.get("dt")
    cid_any = exprs.get("cid")
    causabas = exprs.get("causabas_cid")
    if not (dt and cid_any):
        return pd.DataFrame()
    causabas_sql = causabas or "NULL"
    sql = f"""
        WITH base AS (
            SELECT {dt} AS dt, {cid_any} AS cid_mencao, {causabas_sql} AS cid_causa_basica
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT EXTRACT(YEAR FROM dt) AS ano,
                   COUNT(*) AS obitos_registros,
                   COUNT(*) FILTER (WHERE cid_causa_basica IS NOT NULL) AS obitos_causa_basica_meningite,
                   COUNT(*) FILTER (WHERE cid_mencao IS NOT NULL) AS obitos_com_mencao_meningite
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1
        )
        SELECT *,
               {pct_expr('obitos_causa_basica_meningite', 'obitos_registros')} AS pct_causa_basica_meningite,
               {pct_expr('obitos_com_mencao_meningite', 'obitos_registros')} AS pct_mencao_meningite
        FROM agg
        ORDER BY ano
    """
    return run_query(table, sql)


def query_ciha_indicators(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    dt = exprs.get("dt")
    cid_any = exprs.get("cid")
    diag_princ = exprs.get("diag_princ_cid")
    morte = exprs.get("morte_code")
    dias = exprs.get("dias_perm")
    if not dt:
        return pd.DataFrame()
    cid_any_sql = cid_any or "NULL"
    diag_princ_sql = diag_princ or "NULL"
    morte_sql = morte or "NULL"
    dias_sql = dias or "NULL"
    sql = f"""
        WITH base AS (
            SELECT {dt} AS dt,
                   {cid_any_sql} AS cid_mencao,
                   {diag_princ_sql} AS cid_principal,
                   {morte_sql} AS morte,
                   {dias_sql} AS dias_perm
            FROM {table.ref_sql}
            {where_sql}
        ), agg AS (
            SELECT EXTRACT(YEAR FROM dt) AS ano,
                   COUNT(*) AS atendimentos,
                   COUNT(*) FILTER (WHERE cid_principal IS NOT NULL) AS atendimentos_diag_principal_meningite,
                   COUNT(*) FILTER (WHERE cid_mencao IS NOT NULL) AS atendimentos_qualquer_cid_meningite,
                   COUNT(*) FILTER (WHERE morte = '1') AS mortes_administrativas,
                   COUNT(*) FILTER (WHERE dias_perm = 0) AS permanencia_zero,
                   median(dias_perm) FILTER (WHERE dias_perm BETWEEN 0 AND 365) AS mediana_dias_perm
            FROM base
            WHERE dt IS NOT NULL
            GROUP BY 1
        )
        SELECT *,
               {pct_expr('atendimentos_diag_principal_meningite', 'atendimentos')} AS pct_atendimentos_diag_principal_meningite,
               {pct_expr('atendimentos_qualquer_cid_meningite', 'atendimentos')} AS pct_atendimentos_qualquer_cid_meningite,
               {pct_expr('mortes_administrativas', 'atendimentos')} AS pct_morte_administrativa,
               {pct_expr('permanencia_zero', 'atendimentos')} AS pct_permanencia_zero
        FROM agg
        ORDER BY ano
    """
    return run_query(table, sql)


def query_ciha_dias_perm_distribution(table: LoadedTable, exprs: Dict[str, Optional[str]], where_sql: str) -> pd.DataFrame:
    dias = exprs.get("dias_perm")
    if not dias:
        return pd.DataFrame()
    sql = f"""
        WITH base AS (
            SELECT {dias} AS dias_perm
            FROM {table.ref_sql}
            {where_sql}
        ), bucketed AS (
            SELECT
                CASE
                    WHEN dias_perm IS NULL THEN 'Sem informação'
                    WHEN dias_perm < 0 THEN 'Valor negativo/inválido'
                    WHEN dias_perm BETWEEN 0 AND 30 THEN CAST(CAST(dias_perm AS BIGINT) AS VARCHAR)
                    WHEN dias_perm BETWEEN 31 AND 60 THEN '31–60'
                    WHEN dias_perm BETWEEN 61 AND 90 THEN '61–90'
                    WHEN dias_perm > 90 THEN '91+'
                    ELSE 'Sem informação'
                END AS faixa_dias_perm,
                CASE
                    WHEN dias_perm IS NULL THEN 9998
                    WHEN dias_perm < 0 THEN 9999
                    WHEN dias_perm BETWEEN 0 AND 30 THEN CAST(dias_perm AS BIGINT)
                    WHEN dias_perm BETWEEN 31 AND 60 THEN 31
                    WHEN dias_perm BETWEEN 61 AND 90 THEN 61
                    WHEN dias_perm > 90 THEN 91
                    ELSE 9998
                END AS ordem
            FROM base
        ), counts AS (
            SELECT faixa_dias_perm, ordem, COUNT(*) AS n
            FROM bucketed
            GROUP BY 1, 2
        ), with_totals AS (
            SELECT faixa_dias_perm, ordem, n, SUM(n) OVER () AS denominador
            FROM counts
        )
        SELECT faixa_dias_perm, ordem, n, denominador,
               CASE WHEN denominador > 0 THEN ROUND(100.0 * n / denominador, 2) ELSE NULL END AS pct
        FROM with_totals
        ORDER BY ordem, faixa_dias_perm
    """
    return run_query(table, sql)


def query_missingness(table: LoadedTable, fields: Dict[str, Optional[str]], dt_sql: Optional[str], where_sql: str) -> pd.DataFrame:
    checks = [(label, expr) for label, expr in fields.items() if expr]
    if not checks:
        return pd.DataFrame()
    select_parts = []
    for label, expr in checks:
        select_parts.append(
            f"SELECT {qstr(label)} AS campo, COUNT(*) FILTER (WHERE {expr} IS NULL) AS faltantes, COUNT(*) AS total FROM base"
        )
    sql = f"""
        WITH base AS (
            SELECT {', '.join(f'{expr} AS f_{i}' for i, (_, expr) in enumerate(checks))}
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT campo, faltantes, total, CASE WHEN total > 0 THEN ROUND(100.0 * faltantes / total, 2) ELSE NULL END AS pct_faltante
        FROM (
            {' UNION ALL '.join(
                f"SELECT {qstr(label)} AS campo, COUNT(*) FILTER (WHERE f_{i} IS NULL) AS faltantes, COUNT(*) AS total FROM base"
                for i, (label, _) in enumerate(checks)
            )}
        )
        ORDER BY pct_faltante DESC, campo
    """
    return run_query(table, sql)


def query_missingness_by_year(table: LoadedTable, fields: Dict[str, Optional[str]], dt_sql: Optional[str], where_sql: str) -> pd.DataFrame:
    if not dt_sql:
        return pd.DataFrame()
    checks = [(label, expr) for label, expr in fields.items() if expr]
    if not checks:
        return pd.DataFrame()
    field_select = ", ".join(f"{expr} AS f_{i}" for i, (_, expr) in enumerate(checks))
    union = []
    for i, (label, _) in enumerate(checks):
        union.append(
            f"""
            SELECT ano, {qstr(label)} AS campo,
                   COUNT(*) FILTER (WHERE f_{i} IS NULL) AS faltantes,
                   COUNT(*) AS total
            FROM base
            GROUP BY 1
            """
        )
    sql = f"""
        WITH base AS (
            SELECT EXTRACT(YEAR FROM {dt_sql}) AS ano, {field_select}
            FROM {table.ref_sql}
            {where_sql}
        )
        SELECT ano, campo, faltantes, total,
               CASE WHEN total > 0 THEN ROUND(100.0 * faltantes / total, 2) ELSE NULL END AS pct_faltante
        FROM ({' UNION ALL '.join(union)})
        WHERE ano IS NOT NULL
        ORDER BY ano, campo
    """
    return run_query(table, sql)


def query_enriched_preview(table: LoadedTable, sel: ColumnSelection, exprs: Dict[str, Optional[str]], where_sql: str, limit: Optional[int] = 200, offset: int = 0) -> pd.DataFrame:
    items = []
    mapping = [
        ("data_analise", exprs.get("dt")),
        ("sexo", exprs.get("sex")),
        ("idade_anos", exprs.get("age")),
        ("raca_cor", exprs.get("race")),
        ("municipio_residencia", exprs.get("mun_res_label") or exprs.get("mun_res")),
        ("municipio_evento_atendimento", exprs.get("mun_event_label") or exprs.get("mun_event")),
        ("cid_meningite_encefalite_detectado", exprs.get("cid")),
        ("tipo_cid10", exprs.get("cid_type")),
        ("cid10_adequado_grupo", exprs.get("cid10_adequacy_group")),
        ("cid10_adequado_tipo", exprs.get("cid10_adequacy_type")),
        ("cid10_status_conversao", exprs.get("cid10_adequacy_status")),
        ("cid10_observacao_conversao", exprs.get("cid10_adequacy_reason")),
        ("campo_origem_cid", exprs.get("cid_source")),
        ("sinan_classificacao_final", exprs.get("classi_label")),
        ("sinan_conclusao_diagnostica", exprs.get("con_label")),
        ("sinan_grupo_etiologico", exprs.get("con_group")),
        ("sinan_cla_me_bac", exprs.get("cla_me_bac_label")),
        ("sinan_cla_me_ass", exprs.get("cla_me_ass_label")),
        ("sinan_cla_me_eti", exprs.get("cla_me_eti_label")),
        ("sinan_cid10_convertido_grupo", exprs.get("sinan_cid10_conversion_group")),
        ("sinan_cid10_convertido_tipo", exprs.get("sinan_cid10_conversion_type")),
        ("sinan_cid10_justificativa", exprs.get("sinan_cid10_conversion_reason")),
        ("sinan_cid10_inclui_comparacao", exprs.get("sinan_cid10_conversion_include")),
        ("sinan_g01_doenca_base_provavel", exprs.get("sinan_g01_base_disease")),
        ("sinan_evolucao", exprs.get("evol_label")),
        ("sinan_criterio", exprs.get("criterio_label")),
        ("sinan_puncao_laboratorial", exprs.get("puncao_label")),
        ("sinan_exame_quimiocitologico_liquor_lcr", exprs.get("quimio_label")),
        ("sinan_lab_hemacias", exprs.get("lab_hema")),
        ("sinan_lab_neutrofilos", exprs.get("lab_neutro")),
        ("sinan_lab_glicose", exprs.get("lab_glico")),
        ("sinan_lab_leucocitos", exprs.get("lab_leuco")),
        ("sinan_lab_eosinofilos", exprs.get("lab_eosi")),
        ("sinan_lab_proteinas", exprs.get("lab_prot")),
        ("sinan_lab_monocitos", exprs.get("lab_mono")),
        ("sinan_lab_linfocitos", exprs.get("lab_linfo")),
        ("sinan_lab_cloreto", exprs.get("lab_clor")),
        ("sim_obito_gravidez", exprs.get("obitograv_label")),
        ("sim_obito_puerperio", exprs.get("obitopuerp_label")),
        ("ciha_morte", exprs.get("morte_code")),
        ("ciha_dias_perm", exprs.get("dias_perm")),
    ]
    for alias, expr in mapping:
        if expr:
            items.append(f"{expr} AS {qident(alias)}")

    raw_cols: List[str] = []
    for col in [
        sel.date_col,
        sel.sex_col,
        sel.age_col,
        sel.age_unit_col,
        sel.race_col,
        sel.municipality_res_col,
        sel.municipality_event_col,
        *sel.cid_cols,
        sel.classi_fin_col,
        sel.con_diages_col,
        sel.cla_me_bac_col,
        sel.cla_me_ass_col,
        sel.cla_me_eti_col,
        *(sel.sinan_auxiliary_cid10_cols or []),
        sel.evolucao_col,
        sel.criterio_col,
        sel.lab_puncao_col,
        sel.lab_liquor_col,
        sel.lab_aspect_col,
        sel.lab_hema_col,
        sel.lab_neutro_col,
        sel.lab_glico_col,
        sel.lab_leuco_col,
        sel.lab_eosi_col,
        sel.lab_prot_col,
        sel.lab_mono_col,
        sel.lab_linfo_col,
        sel.lab_clor_col,
        sel.causabas_col,
        sel.causabas_o_col,
        sel.obitograv_col,
        sel.obitopuerp_col,
        sel.diag_princ_col,
        sel.diag_secun_col,
        sel.morte_col,
        sel.dias_perm_col,
        sel.modalidade_col,
        sel.procedimento_col,
    ]:
        if col and col not in raw_cols:
            raw_cols.append(col)
    for col in raw_cols:
        items.append(f"{qident(col)} AS {qident('raw_' + col[:45])}")
    if not items:
        items = ["*"]
    limit_sql = "" if limit is None else f" LIMIT {int(limit)} OFFSET {int(max(offset, 0))}"
    sql = f"SELECT {', '.join(items)} FROM {table.ref_sql} {where_sql}{limit_sql}"
    return run_query(table, sql, cache=False)


# =============================================================================
# Visualização e UI
# =============================================================================


def download_button(df: pd.DataFrame, filename: str, label: str = "Baixar CSV", max_rows: Optional[int] = None) -> None:
    if df is None or df.empty:
        return
    row_limit = perf_int("perf_download_row_limit", DEFAULT_DOWNLOAD_ROW_LIMIT) if max_rows is None else int(max_rows)
    out = df
    if row_limit > 0 and len(df) > row_limit:
        out = df.head(row_limit).copy()
        st.caption(
            f"Download limitado às primeiras {row_limit:,} linhas de {len(df):,} para evitar excesso de memória."
            .replace(",", ".")
        )
    st.download_button(
        label=label,
        data=out.to_csv(index=False).encode("utf-8-sig"),
        file_name=filename,
        mime="text/csv",
        width="content",
    )


def _copyable_table_payload(df: pd.DataFrame) -> Tuple[str, str]:
    """Gera versões HTML e TSV para colagem em Google Docs/editores."""
    if df is None or df.empty:
        return "", ""
    out = df.copy()
    out = out.where(pd.notna(out), "")
    html_table = out.to_html(index=False, escape=True, border=1)
    tsv_table = out.to_csv(index=False, sep="\t", lineterminator="\n")
    return html_table, tsv_table


def copy_table_button(df: pd.DataFrame, label: str = "Copiar tabela para Google Docs/editores") -> None:
    """Renderiza botão de cópia com HTML de tabela e fallback em TSV."""
    if df is None or df.empty:
        return
    html_table, tsv_table = _copyable_table_payload(df)
    if not html_table:
        return
    uid = hashlib.sha1((html_table[:4000] + str(df.shape)).encode("utf-8", errors="ignore")).hexdigest()[:12]
    button_id = f"copy-table-{uid}"
    status_id = f"copy-status-{uid}"
    html_component = f"""
    <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
      <button id="{button_id}"
              style="border: 1px solid #d0d7de; border-radius: 8px; background: #f6f8fa; padding: 6px 10px; cursor: pointer; font-size: 0.9rem;">
        {html_lib.escape(label)}
      </button>
      <span id="{status_id}" style="margin-left: 8px; color: #57606a; font-size: 0.85rem;"></span>
    </div>
    <script>
    const htmlTable_{uid} = {json.dumps(html_table, ensure_ascii=False)};
    const plainText_{uid} = {json.dumps(tsv_table, ensure_ascii=False)};
    const button_{uid} = document.getElementById({json.dumps(button_id)});
    const status_{uid} = document.getElementById({json.dumps(status_id)});

    async function copyRichTable_{uid}() {{
      try {{
        if (navigator.clipboard && window.ClipboardItem) {{
          const item = new ClipboardItem({{
            'text/html': new Blob([htmlTable_{uid}], {{type: 'text/html'}}),
            'text/plain': new Blob([plainText_{uid}], {{type: 'text/plain'}})
          }});
          await navigator.clipboard.write([item]);
        }} else if (navigator.clipboard) {{
          await navigator.clipboard.writeText(plainText_{uid});
        }} else {{
          const textarea = document.createElement('textarea');
          textarea.value = plainText_{uid};
          textarea.style.position = 'fixed';
          textarea.style.left = '-9999px';
          document.body.appendChild(textarea);
          textarea.focus();
          textarea.select();
          document.execCommand('copy');
          textarea.remove();
        }}
        status_{uid}.textContent = 'Tabela copiada.';
      }} catch (err) {{
        const textarea = document.createElement('textarea');
        textarea.value = plainText_{uid};
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        const ok = document.execCommand('copy');
        textarea.remove();
        status_{uid}.textContent = ok ? 'Tabela copiada em texto tabulado.' : 'Copie manualmente pela tabela acima.';
      }}
    }}
    button_{uid}.addEventListener('click', copyRichTable_{uid});
    </script>
    """
    st.iframe(html_component, height=42, width="stretch")


def copyable_dataframe(df: pd.DataFrame, *args, **kwargs) -> None:
    if df is None:
        return

    display_limit = perf_int("perf_display_row_limit", DEFAULT_DISPLAY_ROW_LIMIT)
    copy_limit = perf_int("perf_copy_row_limit", DEFAULT_COPY_ROW_LIMIT)

    display_df = df
    if display_limit > 0 and len(df) > display_limit:
        display_df = df.head(display_limit).copy()
        st.caption(
            f"Tabela renderizada com {display_limit:,} de {len(df):,} linhas. "
            "Use filtros, paginação ou download para volumes maiores."
            .replace(",", ".")
        )

    st.dataframe(display_df, *args, **kwargs)

    copy_df = df
    if copy_limit > 0 and len(df) > copy_limit:
        copy_df = df.head(copy_limit).copy()
        st.caption(
            f"Botão de cópia limitado às primeiras {copy_limit:,} linhas para não sobrecarregar o navegador."
            .replace(",", ".")
        )
    copy_table_button(copy_df)




def format_int_br(value: object) -> str:
    """Formata inteiros em padrão brasileiro para captions de gráficos."""
    if pd.isna(value):
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", ".")
    except Exception:
        return str(value)


def format_pct_br(value: object) -> str:
    """Formata percentuais em padrão brasileiro para captions de gráficos."""
    if pd.isna(value):
        return "—"
    try:
        return f"{float(value):.2f}%".replace(".", ",")
    except Exception:
        return str(value)


def render_interval_total(
    df: pd.DataFrame,
    value_col: str = "n",
    by_col: Optional[str] = None,
    denominator_col: Optional[str] = None,
    value_label: str = "registros",
    denominator_label: str = "denominador",
    prefix: str = "Somatória no intervalo filtrado",
    max_items: int = 8,
) -> None:
    """Exibe a somatória representada pelo gráfico no recorte de tempo/filtros atual.

    O recorte é o que chegou ao dataframe do gráfico: filtros-base, definição exploratória
    quando aplicável e os anos/parquets efetivamente carregados pelo usuário.
    """
    if df is None or df.empty or value_col not in df.columns:
        return
    tmp = df.copy()
    tmp[value_col] = pd.to_numeric(tmp[value_col], errors="coerce").fillna(0)

    def build_piece(label: object, part: pd.DataFrame) -> str:
        total = part[value_col].sum()
        piece = f"{label}: {format_int_br(total)} {value_label}"
        if denominator_col and denominator_col in part.columns:
            denom = pd.to_numeric(part[denominator_col], errors="coerce").fillna(0).sum()
            if denom > 0:
                pct = 100.0 * total / denom
                piece += f" de {format_int_br(denom)} {denominator_label} ({format_pct_br(pct)})"
        return piece

    if by_col and by_col in tmp.columns:
        grouped = (
            tmp.groupby(by_col, dropna=False, as_index=False)[value_col]
            .sum()
            .sort_values(value_col, ascending=False)
        )
        labels = grouped[by_col].tolist()
        pieces = []
        for label in labels[:max_items]:
            part = tmp[tmp[by_col].isna()] if pd.isna(label) else tmp[tmp[by_col].eq(label)]
            pieces.append(build_piece(label if pd.notna(label) else "Sem informação", part))
        if len(labels) > max_items:
            pieces.append(f"+{len(labels) - max_items} categorias")
        st.caption(f"{prefix}: " + "; ".join(pieces) + ".")
        return

    total = tmp[value_col].sum()
    text = f"{prefix}: {format_int_br(total)} {value_label}"
    if denominator_col and denominator_col in tmp.columns:
        denom = pd.to_numeric(tmp[denominator_col], errors="coerce").fillna(0).sum()
        if denom > 0:
            pct = 100.0 * total / denom
            text += f" de {format_int_br(denom)} {denominator_label} ({format_pct_br(pct)})"
    st.caption(text + ".")

def render_field_guide(source: str) -> None:
    copyable_dataframe(
        pd.DataFrame(FIELD_GUIDE[source], columns=["Campo", "Uso", "Leitura epidemiológica"]),
        width="stretch",
        hide_index=True,
    )
    for note in SOURCE_CONFIG[source].field_notes:
        st.caption("• " + note)


def render_cid_reference() -> None:
    copyable_dataframe(pd.DataFrame(CID_RULES)[["grupo", "padrao", "rotulo"]], width="stretch", hide_index=True)
    st.caption(
        "O app procura os padrões CID-10 listados acima nos campos de diagnóstico/causa. "
        "G04* e G05* são tratados como prefixos. A22.8, A32.1, A83*, A84*, A85*, A86*, B00.3, B00.4, B01.0, B01.1, B02.0, B02.1, B05.0, B05.1, B06*, B26.1, B26.2, B37.5, B38.4, B45.1, B57.4, B58.2 e B60.2 foram adicionados ao recorte de meningite/encefalite/meningoencefalite. "
        "No SINAN, a etiologia específica continua derivada de CON_DIAGES e campos complementares; CON_DIAGES=05 não é convertido para G04.2."
    )


def render_quimio_interpretation() -> None:
    st.markdown("### 📌 Tabela-resumo — Como os parâmetros do LCR costumam se comportar por etiologia")
    st.markdown(
        """
        <div style="border-left: 0.45rem solid #1f77b4; background: rgba(31, 119, 180, 0.08);
                    padding: 0.8rem 1rem; border-radius: 0.5rem; margin: 0.35rem 0 0.9rem 0;">
            <strong>Use esta tabela como referência visual rápida.</strong><br>
            Ela resume padrões típicos de LCR e, logo abaixo, traz as exceções que mais interferem na leitura
            dos gráficos do painel.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "Padrões de LCR ajudam a levantar hipóteses, mas não substituem cultura, PCR, Gram, tinta nanquim, sorologia, "
        "epidemiologia e avaliação clínica. Os limites variam com idade, coleta traumática, antibiótico prévio, "
        "imunossupressão e laboratório."
    )
    copyable_dataframe(pd.DataFrame(SINAN_QUIMIO_INTERPRETATION_ROWS), width="stretch", hide_index=True)

    st.markdown("**Observações da tabela — exceções que interferem na leitura dos gráficos:**")
    for row in SINAN_QUIMIO_NOTE_ROWS:
        st.markdown(f"{row['Índice']} {row['Texto']}")

    refs_text = "\n".join(
        f"- {row['Referência']} — {row['Uso no painel']}"
        for row in SINAN_QUIMIO_REFERENCES
    )
    st.markdown("**Referências bibliográficas usadas para esta síntese:**\n" + refs_text)


def render_quimio_classification_tab(
    table: LoadedTable, exprs: Dict[str, Optional[str]], base_where: str
) -> None:
    """Classificação etiológica do LCR por faixas de referência (independente do
    CLASSI_FIN/CON_DIAGES oficial) — comparação para casos confirmados e rastreio
    de possível meningite entre casos descartados, conforme solicitado.
    """
    st.markdown("### Classificação etiológica do líquor por faixas de referência")
    st.caption(
        "Este bloco agora separa duas perguntas diferentes: (1) aderência do LCR à etiologia oficial registrada "
        "no SINAN, restrita aos casos confirmados; e (2) classificação provável pelo LCR, feita sem usar CON_DIAGES, "
        "para mostrar o que o padrão quimiocitológico e o aspecto do líquor sugeririam isoladamente. Essa segunda "
        "análise é exploratória e não substitui cultura, PCR, bacterioscopia, antígenos, clínica ou epidemiologia."
    )

    expected = exprs.get("expected_etiology_group")
    classi_code = exprs.get("classi_code")
    leuco, prot, glico = exprs.get("lab_leuco"), exprs.get("lab_prot"), exprs.get("lab_glico")
    neutro, linfo = exprs.get("lab_neutro"), exprs.get("lab_linfo")

    missing = []
    if not classi_code:
        missing.append("CLASSI_FIN")
    if not exprs.get("con_code"):
        missing.append("CON_DIAGES")
    if not leuco:
        missing.append("LAB_LEUCO")
    if not prot:
        missing.append("LAB_PROT")
    if not glico:
        missing.append("LAB_GLICO")
    if not exprs.get("lab_aspect_code"):
        missing.append("LAB_ASPECT")
    if missing:
        st.warning(
            "Para esta análise funcionar plenamente, preciso detectar: " + ", ".join(missing) +
            ". Verifique a configuração de colunas do SINAN."
        )
        if not classi_code or not exprs.get("con_code") or not leuco:
            return

    with st.expander("Faixas de referência usadas e limitações reconhecidas", expanded=False):
        ranges_rows = []
        for grupo, faixas in SINAN_LCR_ETIOLOGY_RANGES.items():
            leuco_rng = faixas.get("leuco")
            prot_rng = faixas.get("prot")
            if "glico_desc" in faixas:
                glico_desc = faixas["glico_desc"]
            else:
                glico_desc = (
                    f"< {faixas['glico_max']} mg/dL (proxy operacional de redução)"
                    if "glico_max" in faixas
                    else f"≥ {faixas['glico_min']} mg/dL (proxy operacional de preservação)"
                )
            ranges_rows.append(
                {
                    "Grupo etiológico": grupo,
                    "Leucócitos (céls/mm³)": f"{leuco_rng[0]:g}–{leuco_rng[1]:g}" if leuco_rng else "—",
                    "Proteínas (mg/dL)": (f"≥{prot_rng[0]:g}" if prot_rng and grupo == "Fúngica" and float(prot_rng[1]) >= 10000 else (f"{prot_rng[0]:g}–{prot_rng[1]:g}" if prot_rng else "—")),
                    "Glicose": glico_desc,
                    "Predomínio celular esperado": faixas["predominio"],
                    "Aspecto habitual": faixas.get("aspecto", "—"),
                    "Observação": faixas.get("nota", "—"),
                }
            )
        copyable_dataframe(pd.DataFrame(ranges_rows), width="stretch", hide_index=True)
        st.caption(SINAN_LCR_RANGES_SOURCE_NOTE)
        st.caption(
            "Glicose: o SINAN só registra a glicose do LCR (LAB_GLICO), sem glicemia sérica pareada. "
            "A literatura recomenda a razão LCR/soro como parâmetro mais acurado; na ausência do soro, "
            "este painel usa o valor absoluto no LCR como proxy (corte de 40 mg/dL), o que é uma limitação "
            "deste banco de dados, não da metodologia clínica em si."
        )

    def br_int(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{int(value):,}".replace(",", ".")

    def br_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.1f}%".replace(".", ",")

    def add_text(df: pd.DataFrame, n_col: str = "n", pct_col: str = "pct") -> pd.DataFrame:
        out = df.copy()
        out["texto"] = [f"{br_int(n)} ({br_pct(p)})" for n, p in zip(out[n_col], out[pct_col])]
        return out

    def improve_lcr_stacked_bar_readability(fig: go.Figure) -> go.Figure:
        """Normaliza barras empilhadas para comparar proporções e evita texto ilegível em segmentos pequenos."""
        if fig is None:
            return fig
        fig.update_traces(textposition="auto", cliponaxis=False)
        fig.update_layout(barnorm="percent", uniformtext_minsize=9, uniformtext_mode="hide")
        fig.update_yaxes(title_text="Percentual dentro do grupo (%)", ticksuffix="%")
        return fig

    def warn_small_denominators(df: pd.DataFrame, group_col: str, denom_col: str = "denominador") -> None:
        """Sinaliza quando percentuais foram calculados sobre denominadores pequenos."""
        if df.empty or group_col not in df.columns or denom_col not in df.columns:
            return
        denom = df[[group_col, denom_col]].dropna().drop_duplicates().copy()
        denom[denom_col] = pd.to_numeric(denom[denom_col], errors="coerce")
        small = denom[(denom[denom_col] > 0) & (denom[denom_col] < SINAN_LCR_SMALL_DENOMINATOR_WARNING_N)]
        if small.empty:
            return
        small = small.sort_values([denom_col, group_col])
        examples = "; ".join(
            f"{row[group_col]}: N={format_int_br(int(row[denom_col]))}"
            for _, row in small.iterrows()
        )
        st.caption(
            f"Atenção: percentuais com denominador pequeno podem ser instáveis "
            f"(<{SINAN_LCR_SMALL_DENOMINATOR_WARNING_N} registros/casos): {examples}."
        )

    # -------------------------------------------------------------------
    # Análise 1 — Aderência do LCR à etiologia oficial do SINAN
    # -------------------------------------------------------------------
    st.divider()
    st.markdown("#### Análise 1 — Aderência do LCR à etiologia oficial do SINAN")
    st.markdown("##### Casos confirmados — aderência às faixas esperadas por grupo etiológico")
    st.caption(
        "Esta análise usa a etiologia oficial registrada no SINAN como referência. Considera apenas casos com "
        "CLASSI_FIN = confirmado, punção lombar realizada e grupo etiológico identificável a partir de CON_DIAGES "
        "(e CLA_ME_ETI para distinguir fungo dentro de 'outra etiologia'). "
        "Meningococcemia isolada (CON_DIAGES = 01) é excluída por não representar, isoladamente, meningite "
        "confirmada por LCR. Os intervalos usados nestes gráficos são lidos diretamente de "
        "SINAN_LCR_ETIOLOGY_RANGES, a mesma estrutura exibida em 'Faixas de referência usadas e limitações reconhecidas'."
    )

    confirmed_where = append_clause(base_where, f"{classi_code} = '1'")
    if exprs.get("puncao_code"):
        confirmed_where = append_clause(confirmed_where, f"{exprs['puncao_code']} = '1'")
    if exprs.get("quimio_code"):
        confirmed_where = append_clause(confirmed_where, f"{exprs['quimio_code']} = '1'")

    etio_counts = query_sinan_confirmed_etiology_counts(table, exprs, confirmed_where)
    if etio_counts.empty:
        st.info(
            "Não há casos confirmados com grupo etiológico identificável (Viral/Bacteriana/Tuberculosa/Fúngica) "
            "e punção lombar realizada nos filtros atuais."
        )
    else:
        etio_counts_display = etio_counts[etio_counts["grupo_etiologico"].isin(SINAN_ETIOLOGY_GROUPS)]
        total_considerado = int(etio_counts_display["n"].sum())
        st.caption(
            f"Casos confirmados com grupo etiológico nos 4 grupos comparados (Viral/Bacteriana/Tuberculosa/"
            f"Fúngica): {format_int_br(total_considerado)}."
        )
        fig_counts = px.bar(
            etio_counts_display,
            x="grupo_etiologico",
            y="n",
            title="Casos confirmados por grupo etiológico (com LCR)",
            labels={"grupo_etiologico": "Grupo etiológico", "n": "Casos"},
            color="grupo_etiologico",
            category_orders={"grupo_etiologico": SINAN_ETIOLOGY_GROUPS},
            color_discrete_map=SINAN_ETIOLOGY_COLOR_MAP,
        )
        render_plotly_chart(preserve_trace_colors(fig_counts))

        param_labels = {"leuco": "Leucócitos", "prot": "Proteínas"}
        for param, label in param_labels.items():
            df_param = query_sinan_confirmed_param_vs_range(table, exprs, confirmed_where, param)
            if df_param.empty:
                st.info(f"Sem dados suficientes de {label.lower()} para comparar com as faixas esperadas.")
                continue
            df_param = add_text(df_param)
            fig = px.bar(
                df_param,
                x="grupo_etiologico",
                y="n",
                color="posicao",
                text="texto",
                barmode="stack",
                title=f"{label}: posição em relação à faixa esperada, por grupo etiológico confirmado",
                labels={"grupo_etiologico": "Grupo etiológico", "n": "Casos", "posicao": "Posição"},
                category_orders={
                    "grupo_etiologico": SINAN_ETIOLOGY_GROUPS,
                    "posicao": SINAN_LCR_RANGE_POSITION_ORDER,
                },
                color_discrete_map=SINAN_LCR_RANGE_POSITION_COLOR_MAP,
            )
            improve_lcr_stacked_bar_readability(fig)
            render_plotly_chart(preserve_trace_colors(fig))
            warn_small_denominators(df_param, "grupo_etiologico")
            copyable_dataframe(
                df_param[["grupo_etiologico", "posicao", "n", "denominador", "pct"]],
                width="stretch",
                hide_index=True,
            )
            download_button(df_param, f"sinan_confirmados_{param}_vs_faixa.csv")

        if neutro and linfo:
            df_pred = query_sinan_confirmed_predominio_vs_expected(table, exprs, confirmed_where)
            if not df_pred.empty:
                df_pred = add_text(df_pred)
                fig_pred = px.bar(
                    df_pred,
                    x="grupo_etiologico",
                    y="n",
                    color="situacao",
                    text="texto",
                    barmode="stack",
                    title="Predomínio celular (neutrófilos x linfócitos) observado vs. esperado",
                    labels={"grupo_etiologico": "Grupo etiológico", "n": "Casos", "situacao": "Situação"},
                    category_orders={
                        "grupo_etiologico": SINAN_ETIOLOGY_GROUPS,
                        "situacao": SINAN_LCR_PREDOMINIO_STATUS_ORDER,
                    },
                    color_discrete_map=SINAN_LCR_PREDOMINIO_STATUS_COLOR_MAP,
                )
                improve_lcr_stacked_bar_readability(fig_pred)
                render_plotly_chart(preserve_trace_colors(fig_pred))
                warn_small_denominators(df_pred, "grupo_etiologico")
                copyable_dataframe(
                    df_pred[["grupo_etiologico", "situacao", "n", "denominador", "pct"]],
                    width="stretch",
                    hide_index=True,
                )
                download_button(df_pred, "sinan_confirmados_predominio_vs_esperado.csv")
                st.caption(
                    "Lembre-se: o Mandell/MSD e a literatura clínica descrevem predomínio linfocitário em parte das bacterianas "
                    "(sobretudo no início do quadro ou após tratamento) e predomínio neutrofílico nas primeiras 24–48h da viral. "
                    "'Discordante' aqui não significa necessariamente caso mal classificado."
                )
        else:
            st.info("LAB_NEUTRO e/ou LAB_LINFO não detectados; gráfico de predomínio celular não pode ser gerado.")

        df_glico = query_sinan_confirmed_glucose_vs_expected(table, exprs, confirmed_where)
        if not df_glico.empty:
            df_glico = add_text(df_glico)
            fig_glico = px.bar(
                df_glico,
                x="grupo_etiologico",
                y="n",
                color="posicao",
                text="texto",
                barmode="stack",
                title="Glicose absoluta no LCR: reduzida x preservada, por grupo etiológico confirmado",
                labels={"grupo_etiologico": "Grupo etiológico", "n": "Casos", "posicao": "Posição"},
                category_orders={
                    "grupo_etiologico": SINAN_ETIOLOGY_GROUPS,
                    "posicao": SINAN_LCR_GLUCOSE_POSITION_ORDER,
                },
                color_discrete_map=SINAN_LCR_GLUCOSE_POSITION_COLOR_MAP,
            )
            improve_lcr_stacked_bar_readability(fig_glico)
            render_plotly_chart(preserve_trace_colors(fig_glico))
            warn_small_denominators(df_glico, "grupo_etiologico")
            copyable_dataframe(
                df_glico[["grupo_etiologico", "posicao", "n", "denominador", "pct"]],
                width="stretch",
                hide_index=True,
            )
            download_button(df_glico, "sinan_confirmados_glicose_vs_esperado.csv")
            st.caption(
                "Critério de corte aproximado: viral é tratado como glicose preservada quando ≥45 mg/dL; "
                "bacteriana, tuberculosa e fúngica são tratadas como reduzidas quando <40 mg/dL. Use com cautela: "
                "a razão LCR/soro é mais adequada que a glicose absoluta isolada."
            )

        df_aspecto = query_sinan_confirmed_aspect_vs_expected(table, exprs, confirmed_where)
        if not df_aspecto.empty:
            df_aspecto = add_text(df_aspecto)
            fig_aspecto = px.bar(
                df_aspecto,
                x="grupo_etiologico",
                y="n",
                color="situacao",
                text="texto",
                barmode="stack",
                title="Aspecto do líquor observado vs. esperado, por grupo etiológico confirmado",
                labels={"grupo_etiologico": "Grupo etiológico", "n": "Casos", "situacao": "Situação", "aspecto_esperado": "Aspecto esperado"},
                category_orders={
                    "grupo_etiologico": SINAN_ETIOLOGY_GROUPS,
                    "situacao": SINAN_LCR_ASPECT_STATUS_ORDER,
                },
                color_discrete_map=SINAN_LCR_ASPECT_STATUS_COLOR_MAP,
                hover_data={"texto": False, "pct": ":.1f", "denominador": True, "aspecto_esperado": True, "aspectos_observados": True},
            )
            improve_lcr_stacked_bar_readability(fig_aspecto)
            render_plotly_chart(preserve_trace_colors(fig_aspecto))
            warn_small_denominators(df_aspecto, "grupo_etiologico")
            copyable_dataframe(
                df_aspecto[["grupo_etiologico", "aspecto_esperado", "situacao", "aspectos_observados", "n", "denominador", "pct"]],
                width="stretch",
                hide_index=True,
            )
            download_button(df_aspecto, "sinan_confirmados_aspecto_liquor_vs_esperado.csv")
            st.caption(
                "Leitura operacional do aspecto: viral e fúngica costumam ser límpidas; bacteriana costuma ser turva ou purulenta; "
                "tuberculosa pode ser límpida ou turva. Hemorrágico, xantocrômico e 'outro' foram mantidos como categoria atípica/não específica, "
                "pois podem refletir coleta traumática, sangue, degradação de hemácias ou outras condições."
            )
        elif exprs.get("lab_aspect_code"):
            st.info("Sem dados suficientes de aspecto do líquor para comparar com o padrão esperado por etiologia.")
        else:
            st.info("LAB_ASPECT não foi detectado; a comparação do aspecto do líquor não pode ser gerada.")

    # -------------------------------------------------------------------
    # Análise 2 — Classificação provável pelo LCR, sem usar CON_DIAGES
    # -------------------------------------------------------------------
    st.divider()
    st.markdown("#### Análise 2 — Classificação provável pelo LCR, independente do SINAN")
    st.caption(
        "Nesta análise, CON_DIAGES e a etiologia oficial não entram no cálculo. O app atribui pontos para cada "
        "etiologia quando os dados disponíveis do LCR combinam com seu padrão típico: leucócitos, proteínas, "
        "glicose absoluta, predomínio celular e aspecto. A etiologia com maior pontuação é exibida como sugestão "
        "do LCR; empates, ausência de dados e pontuação zero são tratados como indeterminados."
    )

    independent_where = base_where
    if exprs.get("puncao_code"):
        independent_where = append_clause(independent_where, f"{exprs['puncao_code']} = '1'")
    if exprs.get("quimio_code"):
        independent_where = append_clause(independent_where, f"{exprs['quimio_code']} = '1'")

    n_independent_lcr = count_rows(table, independent_where)
    if n_independent_lcr == 0:
        st.info("Não há registros com punção lombar e exame quimiocitológico do LCR realizados nos filtros atuais.")
    else:
        st.caption(
            f"Registros avaliados com punção lombar" +
            (" e exame quimiocitológico" if exprs.get("quimio_code") else "") +
            f" nos filtros atuais: {format_int_br(n_independent_lcr)}."
        )
        df_independent = query_sinan_lcr_independent_distribution(table, exprs, independent_where)
        if df_independent.empty:
            st.info("Sem dados suficientes para gerar a classificação provável pelo LCR.")
        else:
            df_independent = add_text(df_independent)
            case_order = ["Casos confirmados", "Casos descartados", "Sem classificação / ignorados", "Casos avaliados"]
            class_order = SINAN_LCR_INDEPENDENT_CLASS_ORDER
            fig_independent = px.bar(
                df_independent,
                x="grupo_caso",
                y="n",
                color="classificacao_lcr",
                text="texto",
                barmode="stack",
                title="Classificação provável pelo LCR — distribuição por definição de caso",
                labels={"grupo_caso": "Definição de caso", "n": "Registros", "classificacao_lcr": "Classificação pelo LCR"},
                category_orders={"grupo_caso": case_order, "classificacao_lcr": class_order},
                color_discrete_map=SINAN_LCR_INDEPENDENT_CLASS_COLOR_MAP,
                hover_data={"texto": False, "pct": ":.1f", "denominador": True, "suporte_medio_pct": ":.1f"},
            )
            improve_lcr_stacked_bar_readability(fig_independent)
            render_plotly_chart(preserve_trace_colors(fig_independent))
            warn_small_denominators(df_independent, "grupo_caso")
            copyable_dataframe(
                df_independent[["grupo_caso", "classificacao_lcr", "n", "denominador", "pct", "suporte_medio_pct"]],
                width="stretch",
                hide_index=True,
            )
            download_button(df_independent, "sinan_classificacao_lcr_independente_distribuicao.csv")
            st.caption(
                "Interpretação: esta é uma classificação por compatibilidade laboratorial, não um diagnóstico. "
                "Ela tende a ficar indeterminada quando poucos parâmetros estão preenchidos ou quando diferentes "
                "etiologias compartilham o mesmo padrão de LCR."
            )

        if expected:
            df_vs_sinan = query_sinan_lcr_independent_vs_sinan(table, exprs, independent_where)
            if not df_vs_sinan.empty:
                df_vs_sinan = add_text(df_vs_sinan)
                fig_vs_sinan = px.bar(
                    df_vs_sinan,
                    x="grupo_etiologico_sinan",
                    y="n",
                    color="situacao_vs_sinan",
                    text="texto",
                    barmode="stack",
                    title="Casos confirmados — classificação provável pelo LCR vs etiologia oficial do SINAN",
                    labels={
                        "grupo_etiologico_sinan": "Etiologia oficial no SINAN",
                        "n": "Casos confirmados",
                        "situacao_vs_sinan": "Comparação",
                        "classificacao_lcr": "Classificação pelo LCR",
                    },
                    category_orders={
                        "grupo_etiologico_sinan": SINAN_ETIOLOGY_GROUPS,
                        "situacao_vs_sinan": SINAN_LCR_VS_SINAN_STATUS_ORDER,
                    },
                    color_discrete_map=SINAN_LCR_VS_SINAN_STATUS_COLOR_MAP,
                    hover_data={"texto": False, "classificacao_lcr": True, "pct": ":.1f", "denominador": True, "suporte_medio_pct": ":.1f"},
                )
                improve_lcr_stacked_bar_readability(fig_vs_sinan)
                render_plotly_chart(preserve_trace_colors(fig_vs_sinan))
                warn_small_denominators(df_vs_sinan, "grupo_etiologico_sinan")
                copyable_dataframe(
                    df_vs_sinan[["grupo_etiologico_sinan", "classificacao_lcr", "situacao_vs_sinan", "n", "denominador", "pct", "suporte_medio_pct"]],
                    width="stretch",
                    hide_index=True,
                )
                download_button(df_vs_sinan, "sinan_classificacao_lcr_independente_vs_sinan.csv")
                st.caption(
                    "Aqui a etiologia oficial do SINAN é usada apenas depois da classificação pelo LCR, para comparar "
                    "concordância ou discordância. Diferente da Análise 1, ela não define previamente a faixa esperada."
                )

    # -------------------------------------------------------------------
    # Análise 2 — Casos descartados: quanto o LCR isoladamente sugere meningite
    # -------------------------------------------------------------------
    st.divider()
    st.markdown("##### Casos descartados — quanto o perfil do LCR isoladamente sugeriria meningite")
    st.caption(
        "Considera casos com CLASSI_FIN = descartado, punção lombar realizada"
        + (" e exame quimiocitológico do LCR realizado" if exprs.get("quimio_code") else "")
        + ". Cada critério (pleocitose, "
        "glicose reduzida, proteína elevada) é avaliado isoladamente — não combinado — para mostrar o efeito "
        "de cada marcador isolado, como a pleocitose costuma ser o sinal mais sensível, porém pouco específico."
    )

    if not classi_code:
        st.info("CLASSI_FIN não detectado; não é possível isolar os casos descartados.")
        return

    discarded_where = append_clause(base_where, f"{classi_code} = '2'")
    if exprs.get("puncao_code"):
        discarded_where = append_clause(discarded_where, f"{exprs['puncao_code']} = '1'")
    if exprs.get("quimio_code"):
        discarded_where = append_clause(discarded_where, f"{exprs['quimio_code']} = '1'")

    n_discarded_lcr = count_rows(table, discarded_where)
    if n_discarded_lcr == 0:
        st.info("Não há casos descartados com punção lombar realizada nos filtros atuais.")
        return
    st.caption(f"Casos descartados com punção lombar realizada nos filtros atuais: {format_int_br(n_discarded_lcr)}.")

    discarded_age_strat = sinan_lcr_age_stratum_expr(exprs)
    discarded_strat_options = {"Sem estratificação por idade": None}
    if discarded_age_strat:
        discarded_strat_options[SINAN_LCR_AGE_STRATIFICATION_LABEL] = discarded_age_strat
    discarded_strat_choice = st.selectbox(
        "Estratificar este gráfico por idade",
        list(discarded_strat_options.keys()),
        key="sinan_descartados_lcr_risco_stratificacao_idade",
        help="Permite comparar o perfil dos casos descartados entre neonatos até seis meses de idade e crianças/adultos (>6 meses).",
    )
    discarded_strat_sql = discarded_strat_options[discarded_strat_choice]
    if discarded_age_strat:
        st.caption(SINAN_LCR_AGE_STRATIFICATION_NOTE)
    else:
        st.caption("Estratificação etária não disponível porque a coluna de idade não foi detectada.")

    df_risk = query_sinan_discarded_meningitis_risk(table, exprs, discarded_where, discarded_strat_sql)
    if df_risk.empty:
        st.info("Sem dados numéricos suficientes (LAB_LEUCO/LAB_GLICO/LAB_PROT) para esta análise.")
    else:
        df_risk = df_risk.rename(columns={"n_sugestivo": "n", "pct_sugestivo": "pct"})
        df_risk = add_text(df_risk)
        labels_risk = {"criterio": "Critério avaliado isoladamente", "n": "Casos descartados", "pct": "%", "estrato": "Estrato etário"}
        hover_risk = {"texto": False, "pct": ":.1f", "denominador": True}
        if discarded_strat_sql and "estrato" in df_risk.columns:
            fig_risk = px.bar(
                df_risk,
                x="criterio",
                y="n",
                color="estrato",
                barmode="group",
                text="texto",
                title="Casos descartados cujo LCR seria sugestivo de meningite, por critério isolado — estratificado por idade",
                labels=labels_risk,
                hover_data=hover_risk,
                category_orders={"estrato": SINAN_LCR_AGE_STRATA_ORDER},
            )
        else:
            fig_risk = px.bar(
                df_risk,
                x="criterio",
                y="n",
                text="texto",
                title="Casos descartados cujo LCR seria sugestivo de meningite, por critério isolado",
                labels=labels_risk,
                hover_data=hover_risk,
            )
        fig_risk.update_traces(textposition="outside", cliponaxis=False)
        fig_risk.update_layout(xaxis_tickangle=-15)
        render_plotly_chart(fig_risk)
        display_cols = ["criterio"] + (["estrato"] if "estrato" in df_risk.columns else []) + ["n", "denominador", "pct"]
        copyable_dataframe(df_risk[display_cols], width="stretch", hide_index=True)
        download_button(df_risk, "sinan_descartados_risco_isolado.csv")
        st.caption(
            "Pleocitose isolada (leucócitos ≥ 20 céls/mm³) é o critério mais sensível e menos específico: muitas "
            "condições não infecciosas elevam levemente a celularidade do LCR. Glicose reduzida e proteína "
            "elevada têm maior especificidade para infecção, mas também ocorrem em outras doenças do SNC."
        )


def render_loader(source: str) -> Optional[LoadedTable]:
    cfg = SOURCE_CONFIG[source]
    st.markdown(f"### {source} — {cfg.title}")
    st.caption(f"Período esperado no arquivo enviado: {cfg.expected_period}")

    load_modes = [GITHUB_HOSTED_PARQUETS_LABEL, "Upload DuckDB", "Upload Parquet", "Upload CSV", "Upload DBF"]
    load_mode_key = f"load_mode_{source}"
    if st.session_state.get(load_mode_key) not in (None, *load_modes):
        st.session_state.pop(load_mode_key, None)
    mode = st.radio(
        "Fonte de dados",
        load_modes,
        horizontal=True,
        key=load_mode_key,
    )

    if mode == GITHUB_HOSTED_PARQUETS_LABEL:
        st.caption(f"Fonte padrão: {GITHUB_RELEASE_PAGE_URL}")
        st.info(
            "Nenhum banco hospedado no github é carregado automaticamente. "
            "Marque manualmente os Parquets desejados e clique em **Carregar/atualizar seleção**."
        )
        try:
            release_assets = list_github_release_parquets()
        except Exception as exc:
            st.error(f"Não consegui listar os assets da release do GitHub: {exc}")
            return None

        source_assets = [asset for asset in release_assets if asset.get("source") == source]
        if not source_assets:
            st.error(f"Não encontrei Parquets da base {source} nos bancos hospedados no github.")
            return None

        max_files = perf_int("perf_max_parquet_files", DEFAULT_MAX_PARQUET_FILES_PER_LOAD)
        visible_assets = source_assets

        label_to_asset = {github_asset_label(asset): asset for asset in visible_assets}
        name_to_asset = {str(asset.get("name") or ""): asset for asset in source_assets}
        labels = list(label_to_asset.keys())
        selected_labels = st.multiselect(
            "Escolha manualmente os Parquets da release para carregar",
            options=labels,
            default=[],
            key=f"github_release_assets_{source}",
            help=f"Nada é pré-selecionado. Limite defensivo atual: {max_files} arquivo(s) por carregamento.",
        )
        selected_names = [str(label_to_asset[label].get("name") or "") for label in selected_labels]
        loaded_key = f"github_release_loaded_asset_names_{source}"

        c_load, c_clear = st.columns([2, 1])
        with c_load:
            load_clicked = st.button(
                "Carregar/atualizar seleção",
                key=f"github_release_load_selected_{source}",
                type="primary",
                disabled=not selected_names,
                width="stretch",
            )
        with c_clear:
            clear_clicked = st.button(
                "Descarregar base",
                key=f"github_release_clear_selected_{source}",
                disabled=not st.session_state.get(loaded_key),
                width="stretch",
            )

        if load_clicked:
            if len(selected_names) > max_files:
                st.error(
                    f"Seleção bloqueada: {len(selected_names)} Parquets excedem o limite atual de {max_files}. "
                    "Reduza os anos/arquivos ou aumente o limite em Desempenho e memória."
                )
            else:
                st.session_state[loaded_key] = selected_names
        if clear_clicked:
            st.session_state.pop(loaded_key, None)

        loaded_names = list(st.session_state.get(loaded_key, []))
        if not loaded_names:
            st.info("Selecione um ou mais Parquets e clique em **Carregar/atualizar seleção** para iniciar a análise.")
            return None
        if len(loaded_names) > max_files:
            st.error(
                f"A seleção carregada contém {len(loaded_names)} Parquets, acima do limite atual de {max_files}. "
                "Clique em **Descarregar base**, reduza a seleção ou aumente o limite em Desempenho e memória."
            )
            return None

        selected_assets = [name_to_asset[name] for name in loaded_names if name in name_to_asset]
        missing_assets = [name for name in loaded_names if name not in name_to_asset]
        if missing_assets:
            st.warning(
                "Alguns arquivos carregados anteriormente não aparecem mais na release e foram ignorados: "
                + ", ".join(missing_assets)
            )
        if not selected_assets:
            st.info("A seleção carregada não contém Parquets válidos. Escolha novamente os arquivos da release.")
            return None

        if set(selected_names) != set(loaded_names):
            st.warning(
                "A lista marcada na tela é diferente da seleção atualmente carregada. "
                "Clique em **Carregar/atualizar seleção** para aplicar a nova escolha."
            )

        with st.expander("Parquets atualmente carregados", expanded=False):
            st.write("\n".join(f"- {name}" for name in loaded_names))

        try:
            with st.spinner(f"Preparando {len(selected_assets)} parquet(s) selecionado(s) dos bancos hospedados no github..."):
                paths = [materialize_github_release_asset(asset) for asset in selected_assets]
        except Exception as exc:
            st.error(f"Não consegui baixar os Parquets selecionados dos bancos hospedados no github: {exc}")
            st.info("Como alternativa, use Upload Parquet.")
            return None

        st.success(f"{github_selection_summary(selected_assets)} carregado(s) dos bancos hospedados no github.")
        return LoadedTable(
            source=source,
            kind="parquet",
            parquet_paths=paths,
            ref_sql=parquet_object_name(source, paths),
            label=f"Bancos hospedados no github: {github_selection_summary(selected_assets)}",
        )

    if mode == "Upload DuckDB":
        upload = st.file_uploader("Envie um arquivo .duckdb", type=["duckdb"], key=f"upload_duckdb_{source}")
        if not upload:
            st.info("Envie o DuckDB para continuar.")
            return None
        path = materialize_upload(upload, f"{source.lower()}_duckdb")
        try:
            tables = list_duckdb_tables(path)
        except Exception as exc:
            st.error(f"Não consegui abrir o DuckDB enviado: {exc}")
            return None
        default_idx = tables.index(cfg.default_table) if cfg.default_table in tables else 0
        table_name = st.selectbox("Tabela", options=tables, index=default_idx, key=f"upload_duckdb_table_{source}")
        return LoadedTable(source=source, kind="duckdb", db_path=path, table_name=table_name, ref_sql=qident(table_name), label=f"upload:{upload.name}:{table_name}")

    if mode == "Upload CSV":
        st.caption(
            "Todas as colunas do CSV são lidas como texto, preservando zeros à esquerda em campos como "
            "NU_NOTIFIC. Encoding (UTF-8/Latin-1/cp1252) e delimitador (`;`, `,`, tab ou `|`) são detectados "
            "automaticamente a partir do próprio arquivo."
        )
        uploads = st.file_uploader(
            "Envie um ou mais arquivos .csv", type=["csv"], accept_multiple_files=True, key=f"upload_csv_{source}"
        )
        if not uploads:
            st.info("Envie CSV(s) para continuar.")
            return None
        max_files = perf_int("perf_max_parquet_files", DEFAULT_MAX_PARQUET_FILES_PER_LOAD)
        if len(uploads) > max_files:
            st.error(
                f"Foram enviados {len(uploads)} CSVs, acima do limite atual de {max_files}. "
                "Reduza a seleção ou aumente o limite em Desempenho e memória."
            )
            return None
        paths = []
        try:
            with st.spinner(f"Convertendo {len(uploads)} CSV(s) para Parquet..."):
                for up in uploads:
                    raw_path = materialize_upload(up, f"{source.lower()}_csv_raw")
                    paths.append(convert_csv_to_parquet(raw_path, f"{source.lower()}_csv"))
        except Exception as exc:
            st.error(f"Não consegui converter o(s) CSV(s) enviado(s): {exc}")
            return None
        return LoadedTable(
            source=source,
            kind="parquet",
            parquet_paths=paths,
            ref_sql=parquet_object_name(source, paths),
            label=f"{len(paths)} CSV(s) enviados (convertidos para Parquet)",
        )

    if mode == "Upload DBF":
        st.caption(
            "Formato típico de exportação do SINAN/DATASUS. Todas as colunas são lidas como texto, preservando "
            "zeros à esquerda em campos como NU_NOTIFIC. O encoding (geralmente cp850 nos arquivos do DATASUS) "
            "é detectado automaticamente."
        )
        if _DBFReader is None:
            st.error(
                "O suporte a DBF requer o pacote `dbfread`, que não está instalado neste ambiente. "
                "Instale com `pip install dbfread` e reinicie o app, ou converta o DBF para CSV/Parquet antes do upload."
            )
            return None
        uploads = st.file_uploader(
            "Envie um ou mais arquivos .dbf", type=["dbf"], accept_multiple_files=True, key=f"upload_dbf_{source}"
        )
        if not uploads:
            st.info("Envie DBF(s) para continuar.")
            return None
        max_files = perf_int("perf_max_parquet_files", DEFAULT_MAX_PARQUET_FILES_PER_LOAD)
        if len(uploads) > max_files:
            st.error(
                f"Foram enviados {len(uploads)} DBFs, acima do limite atual de {max_files}. "
                "Reduza a seleção ou aumente o limite em Desempenho e memória."
            )
            return None
        paths = []
        try:
            with st.spinner(f"Convertendo {len(uploads)} DBF(s) para Parquet..."):
                for up in uploads:
                    raw_path = materialize_upload(up, f"{source.lower()}_dbf_raw")
                    paths.append(convert_dbf_to_parquet(raw_path, f"{source.lower()}_dbf"))
        except Exception as exc:
            st.error(f"Não consegui converter o(s) DBF(s) enviado(s): {exc}")
            return None
        return LoadedTable(
            source=source,
            kind="parquet",
            parquet_paths=paths,
            ref_sql=parquet_object_name(source, paths),
            label=f"{len(paths)} DBF(s) enviados (convertidos para Parquet)",
        )

    uploads = st.file_uploader("Envie um ou mais Parquets", type=["parquet"], accept_multiple_files=True, key=f"upload_parquet_{source}")
    if not uploads:
        st.info("Envie Parquet(s) para continuar.")
        return None
    max_files = perf_int("perf_max_parquet_files", DEFAULT_MAX_PARQUET_FILES_PER_LOAD)
    if len(uploads) > max_files:
        st.error(
            f"Foram enviados {len(uploads)} Parquets, acima do limite atual de {max_files}. "
            "Reduza a seleção ou aumente o limite em Desempenho e memória."
        )
        return None
    paths = [materialize_upload(up, f"{source.lower()}_parquet") for up in uploads]
    return LoadedTable(source=source, kind="parquet", parquet_paths=paths, ref_sql=parquet_object_name(source, paths), label=f"{len(paths)} parquet(s) enviados")


def render_column_config(source: str, columns: Sequence[str]) -> ColumnSelection:
    """Detecta automaticamente as colunas esperadas para cada base.

    A interface manual de seleção de colunas foi omitida para manter o painel mais limpo.
    Quando uma coluna não é encontrada, as abas correspondentes exibem avisos operacionais.
    """
    return default_selections(source, columns)


def case_definition_clause(source: str, definition: str, exprs: Dict[str, Optional[str]]) -> Optional[str]:
    if source == "SINAN":
        classi = exprs.get("classi_code")
        evol = exprs.get("evol_code")
        if definition == "Todos os registros/notificações" or not classi:
            return None
        if definition == "Somente confirmados":
            return f"{classi} = '1'"
        if definition == "Somente descartados":
            return f"{classi} = '2'"
        if definition == "Confirmados com evolução conhecida" and evol:
            return f"{classi} = '1' AND {evol} IN ('1','2','3')"
        if definition == "Óbito por meningite entre confirmados" and evol:
            return f"{classi} = '1' AND {evol} = '2'"
        return None
    if source == "SIM":
        cid = exprs.get("cid")
        cb = exprs.get("causabas_cid")
        if definition == "Todos os registros do recorte":
            return None
        if definition == "Causa básica com CID de meningite" and cb:
            return f"{cb} IS NOT NULL"
        if definition == "Menção de CID de meningite em qualquer campo" and cid:
            return f"{cid} IS NOT NULL"
        return None
    if source == "CIHA":
        cid = exprs.get("cid")
        dp = exprs.get("diag_princ_cid")
        morte = exprs.get("morte_code")
        if definition == "Todos os atendimentos do recorte":
            return None
        if definition == "Diagnóstico principal com CID de meningite" and dp:
            return f"{dp} IS NOT NULL"
        if definition == "Diagnóstico principal ou secundário com CID de meningite" and cid:
            return f"{cid} IS NOT NULL"
        if definition == "Somente registros com morte" and morte:
            return f"{morte} = '1'"
        return None
    return None


def render_filters(source: str, table: LoadedTable, exprs: Dict[str, Optional[str]]) -> Tuple[str, str, str]:
    clauses: List[str] = []
    definition_clause: Optional[str] = None

    with st.expander("2) Filtros e definição de série", expanded=True):
        if source == "SINAN":
            definitions = [
                "Todos os registros/notificações",
                "Somente confirmados",
                "Somente descartados",
                "Confirmados com evolução conhecida",
                "Óbito por meningite entre confirmados",
            ]
        elif source == "SIM":
            definitions = [
                "Todos os registros do recorte",
                "Causa básica com CID de meningite",
                "Menção de CID de meningite em qualquer campo",
            ]
        else:
            definitions = [
                "Todos os atendimentos do recorte",
                "Diagnóstico principal com CID de meningite",
                "Diagnóstico principal ou secundário com CID de meningite",
                "Somente registros com morte",
            ]
        definition = st.selectbox("Definição aplicada aos gráficos exploratórios", definitions, key=f"definition_{source}")
        definition_clause = case_definition_clause(source, definition, exprs)

        c1, c2, c3, c4 = st.columns(4)
        dt = exprs.get("dt")
        if dt:
            bounds = minmax_date(table, dt)
            if bounds:
                min_year, max_year = int(bounds[0].year), int(bounds[1].year)
                expected_years = [int(x) for x in __import__('re').findall(r'\d{4}', SOURCE_CONFIG[source].expected_period)]
                if len(expected_years) >= 2:
                    default_start = max(min_year, expected_years[0])
                    default_end = min(max_year, expected_years[-1])
                else:
                    default_start, default_end = min_year, max_year
                if default_start > default_end:
                    default_start, default_end = min_year, max_year
                with c1:
                    if min_year >= max_year:
                        st.markdown(f"**Ano:** {min_year}")
                        year_range = (min_year, max_year)
                    else:
                        year_range = st.slider("Ano", min_year, max_year, (default_start, default_end), key=f"year_{source}")
                        if min_year < default_start or max_year > default_end:
                            st.caption("Há datas fora do período esperado; o intervalo padrão usa o período operacional da base.")
                clauses.append(f"EXTRACT(YEAR FROM {dt}) BETWEEN {int(year_range[0])} AND {int(year_range[1])}")
        age = exprs.get("age")
        if age:
            with c2:
                age_range = st.slider("Idade em anos", 0, 120, (0, 120), key=f"age_filter_{source}")
            clauses.append(f"{age} BETWEEN {int(age_range[0])} AND {int(age_range[1])}")
        sex = exprs.get("sex")
        if sex:
            with c3:
                sex_opts = top_values(table, sex, limit=10)
                selected = st.multiselect("Sexo", sex_opts, default=[], key=f"sex_filter_{source}")
            if selected:
                clauses.append(f"{sex} IN ({', '.join(qstr(x) for x in selected)})")
        mun = exprs.get("mun_res_label") or exprs.get("mun_res")
        if mun:
            with c4:
                mun_opts = top_values(table, mun, limit=50)
                selected_mun = st.multiselect("Município de residência", mun_opts, default=[], key=f"mun_filter_{source}")
            if selected_mun:
                clauses.append(f"{mun} IN ({', '.join(qstr(x) for x in selected_mun)})")

        c5, c6, c7 = st.columns(3)
        if source == "SINAN":
            sinan_cid_type = exprs.get("sinan_cid10_conversion_type")
            sinan_include = exprs.get("sinan_cid10_conversion_include")
            if sinan_cid_type:
                with c5:
                    opt_where = append_clause("", f"{sinan_include} = 'Sim'") if sinan_include else ""
                    cid_opts = top_values(table, sinan_cid_type, opt_where, limit=20)
                    selected_cid = st.multiselect(
                        "CID-10 convertido (SINAN)",
                        cid_opts,
                        default=[],
                        key=f"sinan_cid10_convertido_filter_{source}",
                    )
                    st.caption("Filtro baseado em CON_DIAGES/CLA_ME_BAC/campos complementares, não no ID_AGRAVO bruto.")
                if selected_cid:
                    clauses.append(f"{sinan_cid_type} IN ({', '.join(qstr(x) for x in selected_cid)})")
            if exprs.get("classi_label"):
                with c6:
                    opts = top_values(table, exprs["classi_label"], limit=10)
                    selected_classi = st.multiselect("CLASSI_FIN", opts, default=[], key=f"classi_filter_{source}")
                if selected_classi:
                    clauses.append(f"{exprs['classi_label']} IN ({', '.join(qstr(x) for x in selected_classi)})")
            if exprs.get("con_group"):
                with c7:
                    opts = top_values(table, exprs["con_group"], limit=20)
                    selected_con = st.multiselect("Classificação etiológica conforme o SINAN", opts, default=[], key=f"con_filter_{source}")
                if selected_con:
                    clauses.append(f"{exprs['con_group']} IN ({', '.join(qstr(x) for x in selected_con)})")
        else:
            cid_type = exprs.get("cid10_adequacy_type") or exprs.get("cid_type")
            if cid_type:
                with c5:
                    cid_opts = top_values(table, cid_type, limit=25)
                    selected_cid = st.multiselect("CID-10 adequado/conversão", cid_opts, default=[], key=f"cidtype_filter_{source}")
                    st.caption("Filtro baseado na conversão de adequação quando aplicável; os CID-10 fora da conversão permanecem como categoria original.")
                if selected_cid:
                    clauses.append(f"{cid_type} IN ({', '.join(qstr(x) for x in selected_cid)})")
            if source == "CIHA" and exprs.get("modalidade_label"):
                with c6:
                    opts = top_values(table, exprs["modalidade_label"], limit=10)
                    selected_mod = st.multiselect("Modalidade", opts, default=[], key=f"modalidade_filter_{source}")
                if selected_mod:
                    clauses.append(f"{exprs['modalidade_label']} IN ({', '.join(qstr(x) for x in selected_mod)})")

    base_where = sql_where(clauses)
    graph_where = append_clause(base_where, definition_clause)
    return base_where, graph_where, definition


def render_kpis(table: LoadedTable, source: str, base_where: str, graph_where: str, exprs: Dict[str, Optional[str]]) -> None:
    total_base = count_rows(table, base_where)
    total_graph = count_rows(table, graph_where)
    bounds = minmax_date(table, exprs.get("dt"), graph_where) if exprs.get("dt") else None
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Registros após filtros-base", f"{total_base:,}".replace(",", "."))
    k2.metric("Registros nos gráficos", f"{total_graph:,}".replace(",", "."))
    if bounds:
        k3.metric("Data mínima", str(bounds[0].date()))
        k4.metric("Data máxima", str(bounds[1].date()))
    else:
        k3.metric("Data mínima", "—")
        k4.metric("Data máxima", "—")
    if source == "SINAN" and exprs.get("classi_code"):
        confirmed = count_rows(table, append_clause(base_where, f"{exprs['classi_code']} = '1'"))
        k5.metric("Confirmados", f"{confirmed:,}".replace(",", "."), f"{confirmed / total_base * 100:.2f}%" if total_base else None)
    elif source == "CIHA" and exprs.get("morte_code"):
        deaths = count_rows(table, append_clause(base_where, f"{exprs['morte_code']} = '1'"))
        k5.metric("Mortes CIHA", f"{deaths:,}".replace(",", "."), f"{deaths / total_base * 100:.2f}%" if total_base else None)
    elif source == "SIM" and exprs.get("causabas_cid"):
        cause = count_rows(table, append_clause(base_where, f"{exprs['causabas_cid']} IS NOT NULL"))
        k5.metric("Causa básica meningite", f"{cause:,}".replace(",", "."), f"{cause / total_base * 100:.2f}%" if total_base else None)
    else:
        k5.metric("Tipo CID-10", "detectado" if exprs.get("cid") else "não detectado")


def add_covid_context_annotation(fig: go.Figure, enabled: bool = True) -> go.Figure:
    if not enabled or fig is None:
        return fig
    try:
        fig.add_vrect(
            x0="2020-01-01",
            x1="2021-12-31",
            fillcolor="LightGray",
            opacity=0.20,
            layer="below",
            line_width=0,
            annotation_text="2020-2021: contexto COVID-19",
            annotation_position="top left",
        )
    except Exception:
        pass
    return fig


def render_temporal_tab(table: LoadedTable, source: str, graph_where: str, exprs: Dict[str, Optional[str]]) -> None:
    dt = exprs.get("dt")
    if not dt:
        st.warning("Configure uma coluna de data para gerar a série temporal.")
        return
    c1, c2 = st.columns([1, 2])
    with c1:
        freq_label = st.selectbox("Agregação", ["Ano", "Mês", "Semana"], index=1, key=f"freq_{source}")
    freq = {"Ano": "year", "Mês": "month", "Semana": "week"}[freq_label]
    cat_options = {"Nenhuma": None}
    if source == "SINAN":
        if exprs.get("sinan_cid10_conversion_type"):
            cat_options["CID-10 convertido SINAN"] = exprs["sinan_cid10_conversion_type"]
        if exprs.get("con_group"):
            cat_options["Classificação etiológica conforme o SINAN"] = exprs["con_group"]
        if exprs.get("classi_label"):
            cat_options["CLASSI_FIN"] = exprs["classi_label"]
    elif exprs.get("cid_type"):
        cat_options["Tipo CID-10"] = exprs["cid_type"]
    if exprs.get("sex"):
        cat_options["Sexo"] = exprs["sex"]
    with c2:
        cat_label = st.selectbox("Estratificar por", list(cat_options.keys()), key=f"ts_cat_{source}")
    show_covid_context = st.checkbox(
        "Mostrar anotação de contexto COVID-19 (2020-2021)",
        value=True,
        key=f"show_covid_context_{source}",
        help="A anotação é contextual e não atribui causalidade às variações da série.",
    )
    ts = query_timeseries(table, dt, graph_where, freq, cat_options[cat_label])
    if ts.empty:
        st.info("Sem dados para a série temporal com os filtros atuais.")
    elif cat_options[cat_label]:
        fig = px.line(ts, x="periodo", y="n", color="categoria", markers=True, title="Série temporal estratificada", labels={"periodo": "Período", "n": "Registros", "categoria": cat_label})
        add_covid_context_annotation(fig, show_covid_context)
        render_plotly_chart(fig)
        if show_covid_context:
            st.caption(COVID_CONTEXT_NOTE)
        render_interval_total(ts, value_col="n", by_col="categoria")
        download_button(ts, f"{source.lower()}_serie_temporal_estratificada.csv")
    else:
        fig = px.line(ts, x="periodo", y="n", markers=True, title="Série temporal", labels={"periodo": "Período", "n": "Registros"})
        add_covid_context_annotation(fig, show_covid_context)
        render_plotly_chart(fig)
        if show_covid_context:
            st.caption(COVID_CONTEXT_NOTE)
        render_interval_total(ts, value_col="n")
        download_button(ts, f"{source.lower()}_serie_temporal.csv")

    st.markdown("**Sazonalidade**")
    heat_freq_label = st.selectbox(
        "Granularidade da sazonalidade",
        ["Mês", "Semana"],
        index=0,
        key=f"heatmap_freq_{source}",
    )
    heat_freq = {"Mês": "month", "Semana": "week"}[heat_freq_label]
    heat = query_heatmap(table, dt, graph_where, heat_freq)
    if not heat.empty:
        if heat_freq == "week":
            period_col = "semana"
            columns_range = list(range(1, 54))
            col_labels = [str(w) for w in columns_range]
            # Correção (revisão v49): rótulo honesto. A semana é derivada da data
            # (EXTRACT(WEEK), padrão ISO), que se aproxima — mas não é idêntica —
            # à semana epidemiológica oficial (SE), definida a partir de SEM_NOT
            # (formato AAAASS). Evita-se prometer precisão que o cálculo não tem.
            x_title = "Semana do ano (derivada da data, ~ISO)"
        else:
            period_col = "mes"
            columns_range = list(range(1, 13))
            col_labels = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
            x_title = "Mês"
        pivot = heat.pivot(index="ano", columns=period_col, values="n").fillna(0)
        pivot = pivot.reindex(sorted(pivot.index))
        pivot = pivot.reindex(columns=columns_range, fill_value=0)
        fig = go.Figure(
            data=go.Heatmap(
                z=pivot.values,
                x=col_labels,
                y=[str(int(x)) for x in pivot.index],
                hovertemplate="Ano %{y}<br>" + x_title + " %{x}<br>Registros %{z}<extra></extra>",
            )
        )
        fig.update_layout(title=f"Sazonalidade — ano × {heat_freq_label.lower()}", xaxis_title=x_title, yaxis_title="Ano")
        render_plotly_chart(fig)
        if heat_freq == "week":
            st.caption(
                "A semana aqui é calculada a partir da data (padrão ISO, semanas 1–53). "
                "Para a semana epidemiológica oficial (SE), o campo `SEM_NOT` (AAAASS) é a referência "
                "correta e evita a variação mecânica de meses com 4 ou 5 semanas; considere-o para "
                "análises sazonais estritas."
            )
        if show_covid_context:
            st.caption(COVID_CONTEXT_NOTE)
        render_interval_total(heat, value_col="n")
        download_button(heat, f"{source.lower()}_heatmap_ano_{period_col}.csv", "Baixar dados do heatmap")



# Limiar mínimo de célula para os gráficos estratificados do LCR (correção 5).
# Abaixo deste n, a barra é exibida com opacidade reduzida em vez de ser tratada
# como um padrão robusto — célula com n=1 ou n=2 pode virar uma barra de 100%
# dentro do estrato e induzir leitura equivocada. Valor ajustável pelo usuário
# na interface (ver `st.number_input` em render_sinan_lcr_indicators).
SINAN_LCR_MIN_CELL_SIZE_DEFAULT = 5


def sinan_lcr_apply_small_cell_opacity(
    fig: go.Figure,
    df: pd.DataFrame,
    min_cell: int,
    x_col: str = "faixa",
    color_col: Optional[str] = None,
) -> bool:
    """Reduz a opacidade das barras cuja célula (n) fica abaixo de `min_cell`.

    Funciona tanto para gráficos simples quanto para gráficos agrupados por
    `color_col` (ex.: estrato), casando cada traço do Plotly com a linha
    correspondente de `df` pelo valor de `x_col` (e de `color_col`, quando
    aplicável). Retorna True se algum traço foi marcado como amostra pequena,
    para permitir exibir um aviso condicional na interface.
    """
    if df.empty or "n" not in df.columns or x_col not in df.columns:
        return False
    has_small_cell = False
    for trace in fig.data:
        if color_col and color_col in df.columns:
            subset = df[df[color_col] == trace.name]
        else:
            subset = df
        if subset.empty:
            continue
        lookup = subset.drop_duplicates(subset=[x_col]).set_index(x_col)["n"]
        opacities = []
        for x_val in trace.x:
            n_val = lookup.get(x_val)
            # n=0 é faixa vazia (bin de referência sem ocorrência), não amostra
            # pequena: mantém opacidade cheia e não dispara o aviso, que é
            # reservado a células com 0 < n < min_cell (poucos casos reais).
            if n_val is not None and 0 < n_val < min_cell:
                opacities.append(0.35)
                has_small_cell = True
            else:
                opacities.append(1.0)
        trace.marker.opacity = opacities
    return has_small_cell


def render_sinan_lcr_indicators(table: LoadedTable, exprs: Dict[str, Optional[str]], base_where: str, graph_where: str) -> None:
    """Renderiza punção e parâmetros do LCR no bloco de principais indicadores."""
    def br_int(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{int(value):,}".replace(",", ".")

    def br_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}%".replace(".", ",")

    def add_text(df: pd.DataFrame, pct_col: str = "pct") -> pd.DataFrame:
        out = df.copy()
        out["texto"] = [f"{br_int(n)} ({br_pct(pct)})" for n, pct in zip(out["n"], out[pct_col])]
        return out

    st.markdown("### Punção laboratorial e exame quimiocitológico do líquor")
    st.caption(
        "Estes gráficos usam o recorte exploratório atual determinado pelos filtros do usuário. "
        f"Material analisado no bloco quimiocitológico: {SINAN_QUIMIO_MATERIAL}."
    )

    puncao_expr = exprs.get("puncao_label")
    if puncao_expr:
        df_puncao = query_sinan_puncao_by_case_status(table, exprs, base_where)
        if not df_puncao.empty:
            df_puncao = add_text(df_puncao)
            st.markdown("**Realização da Punção Laboratorial**")
            st.caption(
                "A estratificação por classificação final faz sentido como indicador de completude da investigação: "
                "mostra se confirmados, descartados e registros sem classificação tiveram punção registrada. "
                "Não deve ser lida como desempenho diagnóstico, porque a classificação final é posterior e pode depender do próprio LCR."
            )
            fig_puncao = px.bar(
                df_puncao,
                x="grupo_classificacao",
                y="pct",
                color="categoria",
                text="texto",
                barmode="stack",
                title="Realização da Punção Laboratorial",
                labels={
                    "grupo_classificacao": "Estrato da classificação final",
                    "categoria": "Realização da punção laboratorial",
                    "n": "Registros",
                    "pct": "% dentro do estrato",
                },
                hover_data={"texto": False, "n": True, "pct": ":.2f", "denominador": True},
                category_orders={
                    "grupo_classificacao": ["Casos totais", "Casos confirmados", "Casos descartados", "Sem classificação / ignorados"],
                    "categoria": ["Sim", "Não", "Ignorado", "Sem informação"],
                },
            )
            fig_puncao.update_traces(textposition="inside")
            fig_puncao.update_yaxes(range=[0, 100])
            render_plotly_chart(fig_puncao)
            render_interval_total(df_puncao, value_col="n", by_col="grupo_classificacao")
            copyable_dataframe(df_puncao, width="stretch", hide_index=True)
            download_button(df_puncao, "sinan_realizacao_puncao_laboratorial_por_classificacao.csv")
    else:
        st.info("LAB_PUNCAO não foi detectado; não é possível gerar o gráfico de realização da punção laboratorial.")

    quimio_expr = exprs.get("quimio_label")
    if quimio_expr:
        df_quimio = query_sinan_quimio_by_case_status(table, exprs, graph_where)
        if not df_quimio.empty:
            df_quimio = add_text(df_quimio)
            st.markdown("**Exame Quimiocitol\u00f3gico do l\u00edquor (LCR)**")
            st.caption(
                "Este gr\u00e1fico agora separa a realiza\u00e7\u00e3o/cobertura do exame em tr\u00eas estratos: "
                "casos totais, casos confirmados e casos descartados. Isso faz sentido metodol\u00f3gico porque "
                "v\u00e1rias an\u00e1lises do painel comparam confirmados e descartados; aqui a leitura \u00e9 de cobertura do "
                "procedimento, n\u00e3o de desempenho diagn\u00f3stico. O estrato 'Casos totais' inclui os demais e tamb\u00e9m "
                "registros sem classifica\u00e7\u00e3o final, portanto os tr\u00eas denominadores n\u00e3o s\u00e3o mutuamente exclusivos."
            )
            if exprs.get("quimio_inferred_from_params"):
                st.caption(
                    "LAB_LIQUOR n\u00e3o foi encontrado no banco; a realiza\u00e7\u00e3o do exame quimiocitol\u00f3gico foi inferida pela presen\u00e7a "
                    "de pelo menos um par\u00e2metro do LCR preenchido (hem\u00e1cias, neutr\u00f3filos, glicose, leuc\u00f3citos, eosin\u00f3filos, "
                    "prote\u00ednas, mon\u00f3citos, linf\u00f3citos ou cloreto)."
                )
            fig_quimio_lcr = px.bar(
                df_quimio,
                x="grupo_classificacao",
                y="pct",
                color="categoria",
                text="texto",
                barmode="stack",
                title="Exame Quimiocitol\u00f3gico do l\u00edquor (LCR) por classifica\u00e7\u00e3o final",
                labels={
                    "grupo_classificacao": "Estrato da classifica\u00e7\u00e3o final",
                    "categoria": "Exame quimiocitol\u00f3gico do LCR",
                    "n": "Registros",
                    "pct": "% dentro do estrato",
                    "denominador": "Denominador do estrato",
                },
                hover_data={"texto": False, "n": True, "pct": ":.2f", "denominador": True},
                category_orders={
                    "grupo_classificacao": ["Casos totais", "Casos confirmados", "Casos descartados"],
                    "categoria": ["Sim", "N\u00e3o", "Ignorado", "Sem informa\u00e7\u00e3o"],
                },
            )
            fig_quimio_lcr.update_traces(textposition="inside")
            fig_quimio_lcr.update_yaxes(range=[0, 100])
            render_plotly_chart(fig_quimio_lcr)
            render_interval_total(df_quimio, value_col="n", by_col="grupo_classificacao")
            copyable_dataframe(df_quimio, width="stretch", hide_index=True)
            download_button(df_quimio, "sinan_exame_quimiocitologico_lcr_por_classificacao.csv")
    else:
        st.info(
            "N\u00e3o foi detectado LAB_LIQUOR nem par\u00e2metros quimiocitol\u00f3gicos do LCR suficientes "
            "para inferir a realiza\u00e7\u00e3o do exame."
        )

    with st.expander("📌 Tabela-resumo: como os parâmetros do LCR costumam se comportar por etiologia", expanded=True):
        render_quimio_interpretation()

    # Correção 2 (depende da cláusula de elegibilidade também usada na Análise 2):
    # o resumo estatístico e sua "% preenchido" passam a usar como base apenas
    # quem teve punção lombar e exame quimiocitológico realizados — não mais o
    # recorte geral de filtros da página (graph_where), que misturava "sem
    # indicação clínica de puncionar" com "campo mal preenchido no SINAN".
    lcr_eligible_where = sinan_lcr_eligible_where(exprs, graph_where)
    n_eligible_lcr = count_rows(table, lcr_eligible_where)
    quimio_summary = query_sinan_quimio_summary(table, exprs, lcr_eligible_where)
    if quimio_summary.empty:
        st.info(
            "Para gerar o resumo do Exame Quimiocitológico do líquor (LCR), os campos laboratoriais do SINAN precisam existir "
            "e ser detectados automaticamente, como LAB_GLICO, LAB_LEUCO, LAB_NEUTRO e LAB_PROT."
        )
        render_quimio_classification_tab(table, exprs, base_where)
        return

    st.caption(
        f"Completude calculada sobre os {format_int_br(n_eligible_lcr)} registros do recorte atual com punção lombar "
        "**e** exame quimiocitológico do LCR realizados — não sobre o total de casos filtrados na página. "
        "Isso separa dois indicadores que antes apareciam misturados: a **cobertura da punção** sobre o total de "
        "casos (gráfico 'Realização da Punção Laboratorial', acima) e a **completude de preenchimento** de cada "
        "parâmetro entre quem já foi puncionado (tabela abaixo). Uma completude baixa aqui reflete falha de "
        "registro, não falta de indicação clínica para puncionar."
    )

    st.markdown("**Distribuição dos parâmetros quimiocitológicos do LCR**")
    st.caption(
        "Os histogramas foram substituídos por classes clínicas fixas. As faixas do eixo x seguem os intervalos da tabela-resumo "
        "e destacam zonas de sobreposição entre etiologias, em vez de usar bins automáticos que mudam conforme o recorte filtrado. "
        "O denominador destes gráficos agora é o mesmo da tabela-resumo: registros com punção lombar e exame quimiocitológico realizados."
    )
    st.caption(
        "⚠️ Antes de entrar nas estatísticas e nas faixas clínicas, os valores passam por uma neutralização de "
        "códigos sentinela de 'ignorado' (999/9999/99999) e, para os parâmetros absolutos, são comparados a um "
        "teto de plausibilidade clínica (sinalizado em 'n_acima_teto_plausibilidade' na tabela-resumo, sem "
        "descarte). **Esses códigos e tetos ainda não foram confirmados junto ao dicionário de dados oficial do "
        "SINAN NET e podem variar entre versões da ficha de investigação — tratar como provisórios até essa "
        "confirmação.**"
    )
    age_strat = sinan_lcr_age_stratum_expr(exprs)
    interval_strat = sinan_lcr_symptom_puncture_interval_expr(exprs)
    strat_options = {"Sem estratificação": None}
    if age_strat:
        strat_options[SINAN_LCR_AGE_STRATIFICATION_LABEL] = age_strat
    if interval_strat:
        strat_options["Tempo entre primeiros sintomas e punção lombar"] = interval_strat
    if age_strat and interval_strat:
        strat_options[SINAN_LCR_AGE_STRATIFICATION_WITH_INTERVAL_LABEL] = combine_sinan_lcr_strata_sql([age_strat, interval_strat])
    strat_choice = st.selectbox(
        "Estratificar gráficos de distribuição por",
        list(strat_options.keys()),
        key="sinan_lcr_distribution_stratification",
        help="Permite comparar padrões do LCR sem estratificação por idade, por neonatos até seis meses versus crianças/adultos (>6 meses), e/ou pelo intervalo entre início dos sintomas e punção lombar.",
    )
    strat_sql = strat_options[strat_choice]
    if age_strat:
        st.caption(SINAN_LCR_AGE_STRATIFICATION_NOTE)
    if not age_strat:
        st.caption("Estratificação etária do LCR não disponível porque a coluna de idade não foi detectada.")
    if not interval_strat:
        st.caption("Estratificação por tempo sintoma-punção não disponível porque DT_SIN_PRI e/ou a data da punção não foram detectadas.")

    # Correção 5: limiar mínimo de célula, ajustável pelo usuário. Cruzar as
    # faixas clínicas com estratos gera subgrupos pequenos (n=1, n=2) que, sem
    # aviso, podem virar uma barra de 100% e ser lida como padrão robusto.
    min_cell_size = st.number_input(
        "Alertar (opacidade reduzida) células estratificadas com menos de N registros",
        min_value=1,
        max_value=50,
        value=SINAN_LCR_MIN_CELL_SIZE_DEFAULT,
        step=1,
        key="sinan_lcr_min_cell_size",
        help="Só se aplica quando há estratificação ativa: células com n abaixo deste valor não são amostra "
        "suficiente para leitura robusta e aparecem com opacidade reduzida nos gráficos abaixo.",
    )

    def render_param_distribution(key: str, titulo: str, eixo_x: Optional[str] = None) -> None:
        expr = exprs.get(f"lab_{key}")
        if not expr:
            st.info(f"Para gerar a distribuição de {titulo}, o campo correspondente precisa existir no SINAN e ser detectado automaticamente.")
            return
        dist = query_sinan_numeric_distribution_stratified_by_reference_bins(table, expr, lcr_eligible_where, key, strat_sql)
        if dist.empty:
            st.info(f"Não há valores numéricos válidos para {titulo} no recorte atual.")
            return
        if "ordem" in dist.columns:
            dist = dist.sort_values(["ordem"] + (["estrato"] if "estrato" in dist.columns else [])).reset_index(drop=True)
        dist = add_text(dist)
        st.markdown(f"**Distribuição — {titulo}**")
        labels = {"faixa": eixo_x or titulo, "n": "Registros", "pct": "%", "estrato": "Estrato", "leitura": "Leitura clínica da faixa"}
        hover_data = {"texto": False, "pct": ":.2f", "denominador": True, "faixa_inicio": ":.2f", "faixa_fim": ":.2f"}
        if "leitura" in dist.columns:
            hover_data["leitura"] = True
        for meta_col in ["unidade", "tipo_valor", "faixa_operacional", "uso_permitido", "comportamento_truncamento"]:
            if meta_col in dist.columns:
                hover_data[meta_col] = True
        category_orders = {"faixa": sinan_lcr_distribution_bin_order(key)} if sinan_lcr_distribution_bin_order(key) else {}
        if strat_choice == SINAN_LCR_AGE_STRATIFICATION_LABEL:
            category_orders["estrato"] = SINAN_LCR_AGE_STRATA_ORDER
        if not category_orders:
            category_orders = None
        if strat_sql and "estrato" in dist.columns:
            fig_dist = px.bar(
                dist,
                x="faixa",
                y="n",
                color="estrato",
                text="texto",
                barmode="group",
                title=f"Distribuição de {titulo} — {strat_choice}",
                labels=labels,
                hover_data=hover_data,
                category_orders=category_orders,
            )
        else:
            fig_dist = px.bar(
                dist,
                x="faixa",
                y="n",
                text="texto",
                title=f"Distribuição de {titulo}",
                labels=labels,
                hover_data=hover_data,
                category_orders=category_orders,
            )
        fig_dist.update_xaxes(tickangle=-30)
        # Correção 5: opacidade reduzida para células (faixa, ou faixa x estrato
        # quando estratificado) com amostra abaixo do limiar definido pelo
        # usuário, evitando que um n=1 ou n=2 seja lido como padrão robusto —
        # mais crítico quando há estratificação, que multiplica o número de
        # subgrupos e reduz o n de cada um.
        is_stratified = bool(strat_sql and "estrato" in dist.columns)
        has_small_cell = sinan_lcr_apply_small_cell_opacity(
            fig_dist, dist, int(min_cell_size), x_col="faixa", color_col="estrato" if is_stratified else None,
        )
        render_plotly_chart(fig_dist)
        if has_small_cell:
            st.caption(
                f"⚠️ Barras com opacidade reduzida representam células com menos de {int(min_cell_size)} registros"
                + (" no cruzamento faixa × estrato" if is_stratified else "")
                + "; leitura pouco robusta, evite interpretar como padrão."
            )
        # Correção (revisão v49): leitura clínica das faixas como texto estático,
        # não apenas no hover do Plotly. O hover se perde em captura de tela,
        # impressão ou PDF — justamente onde este painel costuma ser usado para
        # decisão/relatório. Mostramos aqui as faixas presentes (n>0) no recorte.
        if "leitura" in dist.columns and "faixa" in dist.columns:
            leitura_base = dist[dist["n"] > 0] if "n" in dist.columns else dist
            sort_cols = ["ordem"] if "ordem" in leitura_base.columns else ["faixa"]
            leitura_rows = (
                leitura_base[sort_cols + ["faixa", "leitura"]]
                .dropna(subset=["leitura"])
                .drop_duplicates(subset=["faixa"])
                .sort_values(sort_cols)
            )
            linhas_leitura = [
                f"- **{row.faixa}** — {row.leitura}"
                for row in leitura_rows.itertuples()
                if str(getattr(row, "leitura", "")).strip()
            ]
            if linhas_leitura:
                st.markdown("**Leitura clínica das faixas presentes neste gráfico:**")
                st.markdown("\n".join(linhas_leitura))
        if strat_sql and "estrato" in dist.columns:
            render_interval_total(dist, value_col="n", by_col="estrato")
        else:
            render_interval_total(dist, value_col="n")
        copyable_dataframe(dist, width="stretch", hide_index=True)
        download_button(dist, f"sinan_quimiocitologico_distribuicao_{safe_filename(titulo)}.csv")
        if key == "glico":
            st.caption(
                "Observação: a glicose liquórica deve ser comparada com a glicose sérica; idealmente, a glicemia sérica é colhida "
                "e aferida junto da punção lombar. O gráfico usa LAB_GLICO absoluto porque o SINAN não traz, em geral, a glicemia pareada."
            )

    render_param_distribution("glico", "Glicose", "Glicose do LCR (mg/dL)")
    render_param_distribution("prot", "Proteínas", "Proteínas do LCR (mg/dL)")

    st.markdown("**Distribuição dos glóbulos brancos no LCR**")
    st.caption(
        "Leucócitos são registrados como contagem absoluta (céls/mm³). Neutrófilos, linfócitos e eosinófilos são lidos como percentuais "
        "em relação ao total de leucócitos. LAB_MONO é tratado com cautela: quando fica entre 0 e 100, pode ser lido como composição celular; "
        "valores >100 são sinalizados como incompatíveis/ambíguos, pois o dicionário operacional apontou máximo 994."
    )
    render_param_distribution("leuco", "Leucócitos", "Leucócitos (céls/mm³)")
    render_param_distribution("neutro", "Neutrófilos", "Neutrófilos (% dos leucócitos)")
    render_param_distribution("linfo", "Linfócitos", "Linfócitos (% dos leucócitos)")
    render_param_distribution("mono", "Monócitos", "Monócitos (LAB_MONO; % apenas quando 0-100)")
    render_param_distribution("eosi", "Eosinófilos", "Eosinófilos (% dos leucócitos)")

    st.markdown("**Distribuição — aspecto do líquor (Ficha SINAN)**")
    st.caption(
        "Campo 48 da ficha de investigação: 1 — Límpido; 2 — Purulento; 3 — Hemorrágico; "
        "4 — Turvo; 5 — Xantocrômico; 6 — Outro; 9 — Ignorado. O gráfico abaixo usa essas categorias oficiais, "
        "mantendo ignorados/sem informação para avaliar também completude de preenchimento entre os elegíveis para interpretação do exame (punção + quimiocitológico)."
    )
    if exprs.get("lab_aspect_label"):
        aspect_dist = query_sinan_lcr_aspect_distribution(table, exprs, lcr_eligible_where, strat_sql)
        if aspect_dist.empty:
            st.info("Não há registros com aspecto do líquor preenchido no recorte atual.")
        else:
            if "ordem" in aspect_dist.columns:
                aspect_dist = aspect_dist.sort_values(["ordem"] + (["estrato"] if "estrato" in aspect_dist.columns else [])).reset_index(drop=True)
            aspect_dist = add_text(aspect_dist)
            labels = {"categoria": "Aspecto do líquor", "n": "Registros", "pct": "%", "estrato": "Estrato"}
            if strat_sql and "estrato" in aspect_dist.columns:
                fig_aspect = px.bar(
                    aspect_dist,
                    x="categoria",
                    y="n",
                    color="estrato",
                    text="texto",
                    barmode="group",
                    title=f"Distribuição do aspecto do líquor — {strat_choice}",
                    labels=labels,
                    hover_data={"texto": False, "pct": ":.2f", "denominador": True},
                    category_orders={
                        "categoria": SINAN_LAB_ASPECT_ORDER,
                        **({"estrato": SINAN_LCR_AGE_STRATA_ORDER} if strat_choice == SINAN_LCR_AGE_STRATIFICATION_LABEL else {}),
                    },
                )
            else:
                fig_aspect = px.bar(
                    aspect_dist,
                    x="categoria",
                    y="n",
                    text="texto",
                    title="Distribuição do aspecto do líquor",
                    labels=labels,
                    hover_data={"texto": False, "pct": ":.2f", "denominador": True},
                    category_orders={"categoria": SINAN_LAB_ASPECT_ORDER},
                )
            fig_aspect.update_xaxes(tickangle=-30)
            # Correção 5: mesmo aviso de célula pequena aplicado ao gráfico de
            # aspecto do líquor, que o diagnóstico apontou como igualmente
            # exposto a barras de 100% com n=1 ou n=2 quando estratificado.
            is_stratified_aspect = bool(strat_sql and "estrato" in aspect_dist.columns)
            has_small_cell_aspect = sinan_lcr_apply_small_cell_opacity(
                fig_aspect, aspect_dist, int(min_cell_size), x_col="categoria",
                color_col="estrato" if is_stratified_aspect else None,
            )
            render_plotly_chart(fig_aspect)
            if has_small_cell_aspect:
                st.caption(
                    f"⚠️ Barras com opacidade reduzida representam células com menos de {int(min_cell_size)} registros"
                    + (" no cruzamento categoria × estrato" if is_stratified_aspect else "")
                    + "; leitura pouco robusta, evite interpretar como padrão."
                )
            if strat_sql and "estrato" in aspect_dist.columns:
                render_interval_total(aspect_dist, value_col="n", by_col="estrato")
            else:
                render_interval_total(aspect_dist, value_col="n")
            copyable_dataframe(aspect_dist, width="stretch", hide_index=True)
            download_button(aspect_dist, "sinan_distribuicao_aspecto_liquor.csv")
    else:
        st.info("LAB_ASPECT não foi detectado; não é possível gerar a distribuição do aspecto do líquor.")

    st.markdown("**Resumo estatístico dos parâmetros quimiocitológicos do LCR**")
    st.caption(
        "O gráfico de valores médios foi removido. A tabela abaixo fica apenas como apoio para auditoria de preenchimento, "
        "mediana, quartis, mínimos e máximos; para interpretação visual, prefira as distribuições por faixas clínicas acima. "
        "'registros_avaliados' e 'pct_preenchido' agora usam como base apenas quem teve punção lombar e exame "
        "quimiocitológico realizados (ver observação acima). A coluna 'n_acima_teto_plausibilidade' sinaliza — sem "
        "descartar — valores acima de um teto clínico provisório por parâmetro; **os códigos sentinela e os tetos "
        "de plausibilidade usados aqui ainda não foram confirmados junto ao dicionário de dados oficial do SINAN "
        "e devem ser tratados como estimativas, não como valores validados.**"
    )
    copyable_dataframe(quimio_summary, width="stretch", hide_index=True)
    download_button(quimio_summary, "sinan_quimiocitologico_liquor_resumo_parametros.csv")

    with st.expander("Metadados e auditoria dos valores LAB_*", expanded=False):
        metadata_df = sinan_lcr_metadata_dataframe([key for key, _, _ in sinan_quimio_param_exprs(exprs)])
        if not metadata_df.empty:
            st.markdown("**Cadastro operacional por parâmetro**")
            st.caption("Este cadastro alimenta a tabela-resumo e os hovers dos gráficos de distribuição: unidade, tipo, faixa operacional, sentinelas, teto plausível, teto de sistema/truncamento e uso permitido.")
            copyable_dataframe(metadata_df, width="stretch", hide_index=True)
            download_button(metadata_df, "sinan_lcr_metadados_parametros.csv")
        st.markdown("**Tabela longa de auditoria: valor bruto, valor limpo e flags**")
        st.caption("Preserva valor bruto, valor limpo e flags separadas: sentinela/ausente, teto do sistema, acima do teto de plausibilidade e percentual incompatível. A prévia é limitada para proteger memória e navegador.")
        audit_limit = st.number_input(
            "Máximo de linhas da auditoria LCR",
            min_value=100,
            max_value=max(100, perf_int("perf_download_row_limit", DEFAULT_DOWNLOAD_ROW_LIMIT)),
            value=min(5000, max(100, perf_int("perf_download_row_limit", DEFAULT_DOWNLOAD_ROW_LIMIT))),
            step=100,
            key="sinan_lcr_audit_long_limit",
        )
        if st.checkbox("Carregar prévia da auditoria LCR", value=False, key="sinan_lcr_load_audit_long"):
            audit_long = query_sinan_lcr_numeric_audit_long(table, exprs, lcr_eligible_where, int(audit_limit))
            if audit_long.empty:
                st.info("Não há valores laboratoriais para auditar no recorte elegível atual.")
            else:
                copyable_dataframe(audit_long, width="stretch", hide_index=True)
                download_button(audit_long, "sinan_lcr_auditoria_valores_brutos_limpos_flags.csv")

    render_quimio_classification_tab(table, exprs, base_where)


def query_sinan_overlap_summary(
    table: LoadedTable,
    target_col: str,
    where_sql: str,
) -> pd.DataFrame:
    """Resumo de sobreposição (valores repetidos) para uma coluna genérica (ex.: NU_NOTIFIC, NM_PACIENT)."""
    target_expr = clean_str_expr(target_col)
    sql = f"""
        WITH base AS (
            SELECT {target_expr} AS valor_alvo
            FROM {table.ref_sql}
            {where_sql}
        ), counts AS (
            SELECT valor_alvo, COUNT(*) AS n
            FROM base
            WHERE valor_alvo IS NOT NULL
            GROUP BY 1
        ), totals AS (
            SELECT
                COUNT(*) AS total_registros,
                COUNT(*) FILTER (WHERE valor_alvo IS NOT NULL) AS registros_com_valor,
                COUNT(*) FILTER (WHERE valor_alvo IS NULL) AS registros_sem_valor
            FROM base
        )
        SELECT
            totals.total_registros,
            totals.registros_com_valor,
            totals.registros_sem_valor,
            COALESCE((SELECT COUNT(*) FROM counts), 0) AS valores_distintos,
            COALESCE((SELECT COUNT(*) FROM counts WHERE n > 1), 0) AS valores_com_sobreposicao,
            COALESCE((SELECT SUM(n) FROM counts WHERE n > 1), 0) AS registros_em_sobreposicao,
            CASE WHEN totals.registros_com_valor > 0
                 THEN ROUND(100.0 * COALESCE((SELECT SUM(n) FROM counts WHERE n > 1), 0) / totals.registros_com_valor, 2)
                 ELSE NULL END AS pct_registros_com_sobreposicao
        FROM totals
    """
    return run_query(table, sql)


def query_sinan_overlap_details(
    table: LoadedTable,
    target_col: str,
    where_sql: str,
    exprs: Dict[str, Optional[str]],
    value_label: str = "valor",
    extra_cols: Optional[List[Tuple[str, str]]] = None,
    limit: Optional[int] = 200,
) -> pd.DataFrame:
    """Detalhe agregado de sobreposição: uma linha por valor repetido.

    ``limit=None`` remove o limite SQL e permite que a exportação contenha todos
    os valores repetidos. ``extra_cols`` recebe pares (expressão SQL, nome da
    coluna) usados para agregar contexto, por exemplo NM_PACIENT ao analisar
    NU_NOTIFIC.
    """
    target_expr = clean_str_expr(target_col)
    dt_expr = exprs.get("dt")
    classi_expr = exprs.get("classi_label")
    evol_expr = exprs.get("evol_label")
    con_expr = exprs.get("con_group")
    select_bits = [f"{target_expr} AS valor_alvo"]
    if dt_expr:
        select_bits.append(f"{dt_expr} AS data_referencia")
    if classi_expr:
        select_bits.append(f"{classi_expr} AS classificacao_final")
    if evol_expr:
        select_bits.append(f"{evol_expr} AS evolucao")
    if con_expr:
        select_bits.append(f"{con_expr} AS grupo_etiologico")
    extra_cols = extra_cols or []
    extra_select_names: List[str] = []
    for idx, (extra_expr, extra_name) in enumerate(extra_cols):
        safe_name = extra_name or f"extra_{idx}"
        select_bits.append(f"{extra_expr} AS {safe_name}")
        extra_select_names.append(safe_name)
    select_sql = ",\n                ".join(select_bits)
    optional_cols = []
    if dt_expr:
        optional_cols.extend([
            "MIN(data_referencia) AS primeira_data",
            "MAX(data_referencia) AS ultima_data",
        ])
    if classi_expr:
        optional_cols.append("STRING_AGG(DISTINCT classificacao_final, '; ' ORDER BY classificacao_final) AS classificacoes")
    if evol_expr:
        optional_cols.append("STRING_AGG(DISTINCT evolucao, '; ' ORDER BY evolucao) AS evolucoes")
    if con_expr:
        optional_cols.append("STRING_AGG(DISTINCT grupo_etiologico, '; ' ORDER BY grupo_etiologico) AS grupos_etiologicos")
    for safe_name in extra_select_names:
        optional_cols.append(
            f"STRING_AGG(DISTINCT CAST({safe_name} AS VARCHAR), '; ' ORDER BY CAST({safe_name} AS VARCHAR)) AS {safe_name}_observados"
        )
    optional_sql = (",\n            " + ",\n            ".join(optional_cols)) if optional_cols else ""
    limit_sql = "" if limit is None else f"\n        LIMIT {max(1, int(limit))}"
    sql = f"""
        WITH base AS (
            SELECT
                {select_sql}
            FROM {table.ref_sql}
            {where_sql}
        ), counts AS (
            SELECT valor_alvo, COUNT(*) AS registros
            FROM base
            WHERE valor_alvo IS NOT NULL
            GROUP BY 1
            HAVING COUNT(*) > 1
        )
        SELECT
            base.valor_alvo AS "{value_label}",
            counts.registros{optional_sql}
        FROM base
        JOIN counts USING (valor_alvo)
        GROUP BY base.valor_alvo, counts.registros
        ORDER BY counts.registros DESC, base.valor_alvo{limit_sql}
    """
    return run_query(table, sql)


def _sinan_nu_notific_composite_components(
    target_col: str,
    exprs: Dict[str, Optional[str]],
    municipality_col: Optional[str],
) -> Tuple[str, Optional[str], str, Optional[str], str, str, List[str]]:
    """Monta, em um único ponto, os componentes da chave de NU_NOTIFIC.

    Prioriza DT_NOTIFIC quando detectada; usa a data geral selecionada apenas
    como fallback. A chave administrativa é formada pelo número, ano e
    município disponíveis no banco.
    """
    target_expr = clean_str_expr(target_col)
    dt_expr = exprs.get("dt_notificacao") or exprs.get("dt")
    year_expr = f"EXTRACT(YEAR FROM ({dt_expr}))" if dt_expr else "NULL"
    municipality_expr = clean_str_expr(municipality_col) if municipality_col else None

    key_parts = [f"COALESCE(CAST(({target_expr}) AS VARCHAR), '(sem número)')"]
    key_desc = ["NU_NOTIFIC"]
    incomplete_tests: List[str] = []

    if dt_expr:
        key_parts.append(f"COALESCE(CAST(({year_expr}) AS VARCHAR), '(sem ano)')")
        key_desc.append("ano da notificação")
        incomplete_tests.append(f"({year_expr}) IS NULL")
    if municipality_expr:
        key_parts.append(f"COALESCE(CAST(({municipality_expr}) AS VARCHAR), '(sem município)')")
        key_desc.append("município")
        incomplete_tests.append(f"({municipality_expr}) IS NULL")

    composite_key = " || '|' || ".join(key_parts)
    incomplete_expr = " OR ".join(incomplete_tests) if incomplete_tests else "FALSE"
    return (
        target_expr,
        dt_expr,
        year_expr,
        municipality_expr,
        composite_key,
        incomplete_expr,
        key_desc,
    )


def query_sinan_nu_notific_duplicate_records(
    table: LoadedTable,
    target_col: str,
    where_sql: str,
    exprs: Dict[str, Optional[str]],
    patient_col: Optional[str] = None,
    municipality_col: Optional[str] = None,
) -> pd.DataFrame:
    """Lista, sem amostragem, todos os registros cujo NU_NOTIFIC se repete.

    Diferentemente do resumo agregado do gráfico, esta tabela mantém uma linha
    por registro, permitindo localizar exatamente cada ocorrência envolvida na
    sobreposição simples do número de notificação.
    """
    target_expr = clean_str_expr(target_col)
    dt_expr = exprs.get("dt_notificacao") or exprs.get("dt")
    date_sql = dt_expr or "NULL"
    year_sql = f"EXTRACT(YEAR FROM ({dt_expr}))" if dt_expr else "NULL"
    municipality_sql = clean_str_expr(municipality_col) if municipality_col else "NULL"
    patient_sql = clean_str_expr(patient_col) if patient_col else "NULL"
    classi_sql = exprs.get("classi_label") or "NULL"
    evol_sql = exprs.get("evol_label") or "NULL"
    con_sql = exprs.get("con_group") or "NULL"

    order_sql = (
        "nu_notific, data_referencia NULLS LAST, municipio NULLS LAST, "
        "nm_pacient NULLS LAST, classificacao_final NULLS LAST"
    )
    sql = f"""
        WITH base AS (
            SELECT
                {target_expr} AS nu_notific,
                {date_sql} AS data_referencia,
                {year_sql} AS ano_referencia,
                {municipality_sql} AS municipio,
                {patient_sql} AS nm_pacient,
                {classi_sql} AS classificacao_final,
                {evol_sql} AS evolucao,
                {con_sql} AS grupo_etiologico
            FROM {table.ref_sql}
            {where_sql}
        ), marcados AS (
            SELECT
                *,
                COUNT(*) OVER (PARTITION BY nu_notific) AS registros_mesmo_numero
            FROM base
            WHERE nu_notific IS NOT NULL
        ), duplicados AS (
            SELECT *
            FROM marcados
            WHERE registros_mesmo_numero > 1
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY {order_sql}) AS ordem_exportacao,
            ROW_NUMBER() OVER (
                PARTITION BY nu_notific
                ORDER BY data_referencia NULLS LAST, municipio NULLS LAST,
                         nm_pacient NULLS LAST, classificacao_final NULLS LAST
            ) AS registro_no_numero,
            nu_notific,
            registros_mesmo_numero,
            data_referencia,
            ano_referencia,
            municipio,
            nm_pacient,
            classificacao_final,
            evolucao,
            grupo_etiologico
        FROM duplicados
        ORDER BY {order_sql}
    """
    return run_query(table, sql, cache=False)


def query_sinan_overlap_composite_summary(
    table: LoadedTable,
    target_col: str,
    where_sql: str,
    exprs: Dict[str, Optional[str]],
    municipality_col: Optional[str] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Resume a repetição simples e a classificação pela chave composta.

    Os registros cujo número se repete são particionados em duas classes
    mutuamente exclusivas no nível do registro:
      • Duplicidade provável: a mesma chave composta aparece mais de uma vez;
      • Reuso de numeração: o número se repete, mas aquela chave aparece uma
        única vez, em contexto administrativo diferente.
    """
    (
        target_expr,
        _dt_expr,
        _year_expr,
        _municipality_expr,
        composite_key,
        _incomplete_expr,
        key_desc,
    ) = _sinan_nu_notific_composite_components(target_col, exprs, municipality_col)

    if len(key_desc) == 1:
        return pd.DataFrame(), key_desc

    sql = f"""
        WITH base AS (
            SELECT
                {target_expr} AS valor,
                {composite_key} AS chave
            FROM {table.ref_sql}
            {where_sql}
        ), valid AS (
            SELECT valor, chave
            FROM base
            WHERE valor IS NOT NULL
        ), por_valor AS (
            SELECT valor, COUNT(*) AS n_numero
            FROM valid
            GROUP BY valor
        ), por_chave AS (
            SELECT valor, chave, COUNT(*) AS n_chave
            FROM valid
            GROUP BY valor, chave
        ), classificados AS (
            SELECT
                v.valor,
                v.chave,
                pv.n_numero,
                pc.n_chave
            FROM valid v
            JOIN por_valor pv USING (valor)
            JOIN por_chave pc USING (valor, chave)
        )
        SELECT
            COUNT(*) AS registros_com_valor,
            COALESCE(SUM(CASE WHEN n_numero > 1 THEN 1 ELSE 0 END), 0) AS registros_sobrepostos_simples,
            COALESCE(SUM(CASE WHEN n_numero > 1 AND n_chave > 1 THEN 1 ELSE 0 END), 0) AS registros_duplicidade_provavel,
            COALESCE(SUM(CASE WHEN n_numero > 1 AND n_chave = 1 THEN 1 ELSE 0 END), 0) AS registros_reuso_numeracao,
            COUNT(DISTINCT CASE WHEN n_numero > 1 AND n_chave > 1 THEN valor END) AS numeros_com_duplicidade_provavel,
            COUNT(DISTINCT CASE WHEN n_numero > 1 AND n_chave = 1 THEN valor END) AS numeros_com_reuso_numeracao,
            COUNT(DISTINCT CASE WHEN n_numero > 1 AND n_chave > 1 THEN chave END) AS chaves_compostas_com_sobreposicao
        FROM classificados
    """
    return run_query(table, sql), key_desc


def query_sinan_overlap_composite_details(
    table: LoadedTable,
    target_col: str,
    where_sql: str,
    exprs: Dict[str, Optional[str]],
    municipality_col: Optional[str] = None,
    patient_col: Optional[str] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Classifica todos os registros repetidos como duplicidade provável ou reuso.

    A saída não tem LIMIT e constitui a tabela auditável solicitada para indicar
    exatamente quais registros pertencem a cada classe.
    """
    (
        target_expr,
        dt_expr,
        year_expr,
        municipality_expr,
        composite_key,
        incomplete_expr,
        key_desc,
    ) = _sinan_nu_notific_composite_components(target_col, exprs, municipality_col)

    if len(key_desc) == 1:
        return pd.DataFrame(), key_desc

    date_sql = dt_expr or "NULL"
    municipality_sql = municipality_expr or "NULL"
    patient_sql = clean_str_expr(patient_col) if patient_col else "NULL"
    classi_sql = exprs.get("classi_label") or "NULL"
    evol_sql = exprs.get("evol_label") or "NULL"
    con_sql = exprs.get("con_group") or "NULL"
    key_label = " + ".join(key_desc)

    sql = f"""
        WITH base AS (
            SELECT
                {target_expr} AS nu_notific,
                {date_sql} AS data_referencia,
                {year_expr} AS ano_referencia,
                {municipality_sql} AS municipio,
                {patient_sql} AS nm_pacient,
                {classi_sql} AS classificacao_final,
                {evol_sql} AS evolucao,
                {con_sql} AS grupo_etiologico,
                {composite_key} AS chave_composta,
                CASE WHEN {incomplete_expr} THEN TRUE ELSE FALSE END AS chave_composta_incompleta
            FROM {table.ref_sql}
            {where_sql}
        ), valid AS (
            SELECT *
            FROM base
            WHERE nu_notific IS NOT NULL
        ), por_numero AS (
            SELECT
                nu_notific,
                COUNT(*) AS registros_mesmo_numero,
                COUNT(DISTINCT chave_composta) AS contextos_distintos_numero
            FROM valid
            GROUP BY nu_notific
        ), por_chave AS (
            SELECT
                nu_notific,
                chave_composta,
                COUNT(*) AS registros_mesma_chave
            FROM valid
            GROUP BY nu_notific, chave_composta
        ), classificados AS (
            SELECT
                v.*,
                pn.registros_mesmo_numero,
                pn.contextos_distintos_numero,
                pc.registros_mesma_chave,
                CASE
                    WHEN pc.registros_mesma_chave > 1 THEN 'Duplicidade provável'
                    ELSE 'Reuso de numeração'
                END AS classificacao_sobreposicao,
                CASE
                    WHEN pc.registros_mesma_chave > 1
                        THEN 'Mesmo NU_NOTIFIC repetido na mesma chave administrativa ({key_label}).'
                    ELSE 'NU_NOTIFIC repetido, mas esta chave administrativa aparece uma única vez.'
                END AS criterio_classificacao,
                CASE
                    WHEN v.chave_composta_incompleta
                        THEN 'Chave composta incompleta; revisar manualmente a classificação.'
                    ELSE 'Chave composta completa nos campos disponíveis.'
                END AS observacao_chave
            FROM valid v
            JOIN por_numero pn USING (nu_notific)
            JOIN por_chave pc USING (nu_notific, chave_composta)
            WHERE pn.registros_mesmo_numero > 1
        )
        SELECT
            ROW_NUMBER() OVER (
                ORDER BY
                    CASE classificacao_sobreposicao
                        WHEN 'Duplicidade provável' THEN 1
                        WHEN 'Reuso de numeração' THEN 2
                        ELSE 3
                    END,
                    nu_notific,
                    ano_referencia NULLS LAST,
                    municipio NULLS LAST,
                    data_referencia NULLS LAST,
                    nm_pacient NULLS LAST
            ) AS ordem_exportacao,
            ROW_NUMBER() OVER (
                PARTITION BY nu_notific
                ORDER BY ano_referencia NULLS LAST, municipio NULLS LAST,
                         data_referencia NULLS LAST, nm_pacient NULLS LAST
            ) AS registro_no_numero,
            classificacao_sobreposicao,
            nu_notific,
            registros_mesmo_numero,
            contextos_distintos_numero,
            registros_mesma_chave,
            chave_composta,
            chave_composta_incompleta,
            ano_referencia,
            municipio,
            data_referencia,
            nm_pacient,
            classificacao_final,
            evolucao,
            grupo_etiologico,
            criterio_classificacao,
            observacao_chave
        FROM classificados
        ORDER BY ordem_exportacao
    """
    return run_query(table, sql, cache=False), key_desc


def render_overlap_block(
    table: LoadedTable,
    base_where: str,
    exprs: Dict[str, Optional[str]],
    col_name: str,
    candidates: Sequence[str],
    value_label: str,
    display_label: str,
    file_slug: str,
    extra_cols: Optional[List[Tuple[str, str]]] = None,
    *,
    chart_title: Optional[str] = None,
    details_heading: Optional[str] = None,
    details_limit: Optional[int] = 200,
    download_all_details: bool = False,
) -> None:
    """Renderiza um bloco completo de análise de sobreposição para uma coluna."""
    st.markdown(f"### Sobreposição de `{display_label}`")
    st.caption(
        f"Esta análise verifica se o mesmo valor de `{display_label}` aparece em mais de um registro após os filtros-base. "
        "Sobreposição é um sinal operacional de possível duplicidade ou repetição de caso; a revisão final deve considerar datas, classificação e evolução."
    )
    schema = schema_df(table)
    columns = schema["coluna"].astype(str).tolist() if "coluna" in schema.columns else []
    target_col = choose_candidate(columns, candidates)
    if not target_col:
        st.warning(f"Não localizei o campo `{display_label}` no SINAN carregado.")
        return

    summary = query_sinan_overlap_summary(table, target_col, base_where)
    if summary.empty:
        st.info(f"Sem registros para avaliar `{display_label}` com os filtros atuais.")
        return
    row = summary.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Registros avaliados", f"{int(row['total_registros']):,}".replace(",", "."))
    c2.metric(f"{display_label} preenchidos", f"{int(row['registros_com_valor']):,}".replace(",", "."))
    c3.metric(f"{display_label} sobrepostos (valores distintos)", f"{int(row['valores_com_sobreposicao']):,}".replace(",", "."))
    pct = row.get("pct_registros_com_sobreposicao")
    c4.metric(
        "Registros em sobreposição",
        f"{int(row['registros_em_sobreposicao']):,}".replace(",", "."),
        None if pd.isna(pct) else f"{float(pct):.2f}%".replace(".", ","),
    )
    copyable_dataframe(summary, width="stretch", hide_index=True)
    download_button(summary, f"sinan_sobreposicao_{file_slug}_resumo.csv")

    details = query_sinan_overlap_details(
        table,
        target_col,
        base_where,
        exprs,
        value_label=value_label,
        extra_cols=extra_cols,
        limit=details_limit,
    )
    if details.empty:
        st.success(f"Não há `{display_label}` repetido no recorte atual.")
        return

    st.markdown(
        details_heading
        or f"**Valores de `{display_label}` que se repetem na planilha e quantas vezes cada um aparece:**"
    )
    plot_df = details.head(30).copy()
    plot_df["texto"] = plot_df["registros"].astype(int).astype(str)
    fig = px.bar(
        plot_df,
        x="registros",
        y=value_label,
        orientation="h",
        text="texto",
        title=chart_title or f"Principais {display_label} com sobreposição",
        labels={value_label: display_label, "registros": "Registros"},
    )
    fig.update_layout(yaxis={"categoryorder": "array", "categoryarray": plot_df[value_label].tolist()[::-1]})
    render_plotly_chart(fig)
    st.caption(
        "O gráfico exibe no máximo os 30 valores mais frequentes. A tabela e o CSV abaixo "
        + ("contêm todos os valores repetidos do recorte." if details_limit is None else f"contêm até {int(details_limit)} valores repetidos.")
    )
    copyable_dataframe(details, width="stretch", hide_index=True)
    download_button(
        details,
        f"sinan_sobreposicao_{file_slug}_detalhes.csv",
        label="Baixar CSV dos valores repetidos",
        max_rows=0 if download_all_details else None,
    )


def render_sinan_overlap_tab(table: LoadedTable, base_where: str, exprs: Dict[str, Optional[str]]) -> None:
    st.caption(
        "Verificação de duplicidade/repetição no SINAN: avalia separadamente se o mesmo `NU_NOTIFIC` (identificador "
        "operacional da notificação) e o mesmo `NM_PACIENT` (nome do paciente) aparecem em mais de um registro do "
        "arquivo carregado, após os filtros-base aplicados."
    )
    schema = schema_df(table)
    columns = schema["coluna"].astype(str).tolist() if "coluna" in schema.columns else []
    nu_col = choose_candidate(columns, ["NU_NOTIFIC", "NUM_NOTIFIC", "NUNOTIFIC", "NU_NOTIF"])
    nm_col = choose_candidate(columns, ["NM_PACIENT", "NOME_PACIENTE", "NM_PACIENTE", "PACIENTE"])
    muni_col = choose_candidate(
        columns,
        ["ID_MUNICIP", "ID_MN_OCORR", "CODMUNOCOR", "ID_MN_RESI", "CODMUNRES", "MUNIC_MOV", "MUNIC_RES"],
    )

    # Novo bloco solicitado: replica a leitura de NM_PACIENT para NU_NOTIFIC,
    # substitui a visualização anterior e exporta todos os
    # números repetidos, sem o LIMIT de 200 anteriormente aplicado.
    render_overlap_block(
        table,
        base_where,
        exprs,
        col_name="NU_NOTIFIC",
        candidates=["NU_NOTIFIC", "NUM_NOTIFIC", "NUNOTIFIC", "NU_NOTIF"],
        value_label="nu_notific",
        display_label="NU_NOTIFIC",
        file_slug="nu_notific",
        extra_cols=[(clean_str_expr(nm_col), "nm_pacient")] if nm_col else None,
        chart_title="Sobreposição de NU_NOTIFIC",
        details_heading=(
            "**Números de notificação repetidos no recorte e quantidade de registros associados:**"
        ),
        details_limit=None,
        download_all_details=True,
    )

    if nu_col:
        duplicate_records = query_sinan_nu_notific_duplicate_records(
            table,
            nu_col,
            base_where,
            exprs,
            patient_col=nm_col,
            municipality_col=muni_col,
        )
        if not duplicate_records.empty:
            st.markdown("**Todos os registros envolvidos na sobreposição simples de `NU_NOTIFIC`**")
            st.caption(
                "Esta é a tabela de conferência do gráfico: mantém uma linha por registro, sem amostragem e sem "
                "limite de download. Assim, o CSV identifica exatamente todas as ocorrências de números de "
                "notificação repetidos no recorte filtrado."
            )
            copyable_dataframe(duplicate_records, width="stretch", hide_index=True)
            download_button(
                duplicate_records,
                "sinan_sobreposicao_nu_notific_todos_os_registros.csv",
                label="Baixar CSV completo de todos os registros repetidos",
                max_rows=0,
            )

        composite_summary, key_desc = query_sinan_overlap_composite_summary(
            table,
            nu_col,
            base_where,
            exprs,
            municipality_col=muni_col,
        )
        composite_details, _ = query_sinan_overlap_composite_details(
            table,
            nu_col,
            base_where,
            exprs,
            municipality_col=muni_col,
            patient_col=nm_col,
        )

        st.markdown("#### Duplicidade provável × reuso de numeração (chave composta)")
        st.caption(
            "Chave composta usada: " + " + ".join(f"`{k}`" for k in key_desc)
            + ". O gráfico simples acima é uma triagem visual de qualquer repetição do número; esta análise "
            "não é redundante, porque usa ano/município para classificar cada registro como duplicidade provável "
            "ou reuso de numeração em outro contexto administrativo."
        )

        if composite_summary is None or composite_summary.empty:
            st.info(
                "Não foi possível construir a chave composta (faltam data e/ou município no recorte). "
                "Considere apenas a sobreposição simples acima."
            )
        else:
            crow = composite_summary.iloc[0]
            simples = int(crow.get("registros_sobrepostos_simples", 0) or 0)
            duplicidade = int(crow.get("registros_duplicidade_provavel", 0) or 0)
            reuso = int(crow.get("registros_reuso_numeracao", 0) or 0)
            m1, m2, m3 = st.columns(3)
            m1.metric(
                "Sobrepostos por número simples",
                f"{simples:,}".replace(",", "."),
                help="Todos os registros cujo NU_NOTIFIC se repete, antes de considerar ano/município.",
            )
            m2.metric(
                "Duplicidade provável",
                f"{duplicidade:,}".replace(",", "."),
                help="Registros cuja chave composta NU_NOTIFIC + ano/município também se repete.",
            )
            m3.metric(
                "Reuso de numeração",
                f"{reuso:,}".replace(",", "."),
                help="Registros de número repetido cuja chave composta aparece uma única vez.",
            )

            if simples > 0:
                st.caption(
                    f"Dos {simples:,} registros com número repetido, {duplicidade:,} foram classificados como "
                    f"duplicidade provável e {reuso:,} como reuso de numeração. "
                    "A classificação é uma triagem administrativa e deve ser revisada com os demais campos."
                    .replace(",", ".")
                )

            copyable_dataframe(composite_summary, width="stretch", hide_index=True)
            download_button(
                composite_summary,
                "sinan_sobreposicao_nu_notific_chave_composta_resumo.csv",
                label="Baixar resumo da chave composta",
            )

            if composite_details is None or composite_details.empty:
                st.success("Não há registros repetidos para classificar pela chave composta no recorte atual.")
            else:
                class_order = ["Duplicidade provável", "Reuso de numeração"]
                classification_summary = (
                    composite_details.groupby("classificacao_sobreposicao", dropna=False)
                    .agg(
                        registros=("nu_notific", "size"),
                        numeros_notificacao_distintos=("nu_notific", "nunique"),
                        chaves_compostas_distintas=("chave_composta", "nunique"),
                    )
                    .reindex(class_order, fill_value=0)
                    .rename_axis("classificacao_sobreposicao")
                    .reset_index()
                )
                st.markdown("**Resumo por classificação**")
                copyable_dataframe(classification_summary, width="stretch", hide_index=True)
                download_button(
                    classification_summary,
                    "sinan_nu_notific_duplicidade_provavel_reuso_resumo.csv",
                    label="Baixar CSV do resumo por classificação",
                    max_rows=0,
                )

                st.markdown("**Casos classificados: duplicidade provável e reuso de numeração**")
                st.caption(
                    "A tabela abaixo contém uma linha por registro repetido e informa a classe, a chave composta, "
                    "as contagens do número e da chave, os campos de contexto e um alerta quando a chave está "
                    "incompleta. O CSV não é truncado pelo limite genérico de downloads."
                )
                copyable_dataframe(composite_details, width="stretch", hide_index=True)
                download_button(
                    composite_details,
                    "sinan_nu_notific_duplicidade_provavel_reuso_casos.csv",
                    label="Baixar CSV completo dos casos classificados",
                    max_rows=0,
                )

    st.markdown("---")

    render_overlap_block(
        table,
        base_where,
        exprs,
        col_name="NM_PACIENT",
        candidates=["NM_PACIENT", "NOME_PACIENTE", "NM_PACIENTE", "PACIENTE"],
        value_label="nm_pacient",
        display_label="NM_PACIENT",
        file_slug="nm_pacient",
        extra_cols=[(clean_str_expr(nu_col), "nu_notific")] if nu_col else None,
    )


def render_indicators_tab(table: LoadedTable, source: str, base_where: str, graph_where: str, exprs: Dict[str, Optional[str]]) -> None:
    def br_int(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{int(value):,}".replace(",", ".")

    def br_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}%".replace(".", ",")

    def count_pct_text(n: object, pct: object = None) -> str:
        if pct is None or pd.isna(pct):
            return br_int(n)
        return f"{br_int(n)} ({br_pct(pct)})"

    def add_text_column(df: pd.DataFrame, n_col: str = "n", pct_col: str = "pct") -> pd.DataFrame:
        if df is None or df.empty:
            return df
        out = df.copy()
        out["texto"] = [count_pct_text(n, pct) for n, pct in zip(out[n_col], out[pct_col])]
        return out

    if source == "SINAN":
        ind = query_sinan_indicators(table, exprs, base_where)
        if ind.empty:
            st.warning("Não foi possível calcular indicadores do SINAN. Verifique CLASSI_FIN, EVOLUCAO e data.")
            return

        sinan_schema = schema_df(table)
        sinan_columns = sinan_schema["coluna"].astype(str).tolist() if "coluna" in sinan_schema.columns else []
        symptom_specs = _available_column_specs(sinan_columns, SINAN_SYMPTOM_FIELDS)
        vaccine_specs = _available_column_specs(sinan_columns, SINAN_VACCINE_FIELDS)
        hospital_col = choose_candidate(sinan_columns, ["ATE_HOSPIT"])
        communicants_col = choose_candidate(sinan_columns, ["MED_NUCOMU", "NU_COMUNICANTES", "NUM_COMUNICANTES", "COMUNICANTES"])
        prophylaxis_col = choose_candidate(sinan_columns, ["MED_QUIMIO", "QUIMIOPROFILAXIA", "PROFILAXIA", "QUIMIO"])

        count_specs = [
            ("notificacoes", "Total de notificações", None),
            ("confirmados", "Confirmados", "pct_confirmacao"),
            ("descartados", "Descartados", "pct_descarte"),
            ("sem_classificacao", "Sem classificação / ignorados", "pct_sem_classificacao"),
        ]
        count_rows = []
        for _, row in ind.iterrows():
            for n_col, label, pct_col in count_specs:
                pct = row[pct_col] if pct_col and pct_col in ind.columns else None
                count_rows.append({
                    "ano": row["ano"],
                    "indicador": label,
                    "n": row[n_col],
                    "pct": pct,
                    "texto": count_pct_text(row[n_col], pct),
                    "denominador_pct": row["notificacoes"] if pct_col else None,
                })
        count_long = pd.DataFrame(count_rows)
        fig = px.line(
            count_long,
            x="ano",
            y="n",
            color="indicador",
            markers=True,
            text="texto",
            title="Total de notificações, confirmados, descartados e sem classificação/ignorados",
            labels={"ano": "Ano", "n": "Registros", "indicador": "Indicador", "pct": "% das notificações"},
            hover_data={"texto": False, "pct": ":.2f", "denominador_pct": True},
        )
        fig.update_traces(textposition="top center")
        render_plotly_chart(fig)
        render_interval_total(count_long, value_col="n", by_col="indicador")

        if exprs.get("evol_label") and exprs.get("dt") and exprs.get("classi_code"):
            evol_geral = query_yearly_category(
                table,
                exprs["dt"],
                exprs["evol_label"],
                append_clause(base_where, f"{exprs['classi_code']} = '1'"),
            )
            if not evol_geral.empty:
                evol_geral = collapse_sinan_evolucao_ignorado(evol_geral)
                evol_geral = add_text_column(evol_geral)
                evol_geral_order = ["1 — alta", "2 — óbito por meningite", "3 — óbito por outra causa", "Sem evolução/ignorado"]
                evol_geral_order = [c for c in evol_geral_order if c in evol_geral["categoria"].unique().tolist()] + [
                    c for c in evol_geral["categoria"].dropna().unique().tolist() if c not in evol_geral_order
                ]
                fig_evol_geral = go.Figure()
                for idx, categoria in enumerate(evol_geral_order):
                    df_cat = evol_geral[evol_geral["categoria"].eq(categoria)].copy()
                    if df_cat.empty:
                        continue
                    color = {
                        "1 — alta": "#2CA02C",
                        "2 — óbito por meningite": DEATH_RED,
                        "3 — óbito por outra causa": "#1F77B4",
                        "Sem evolução/ignorado": "#7F7F7F",
                    }.get(str(categoria), APP_COLOR_SEQUENCE[idx % len(APP_COLOR_SEQUENCE)])
                    fig_evol_geral.add_trace(
                        go.Scatter(
                            x=df_cat["ano"],
                            y=df_cat["n"],
                            mode="lines+markers+text",
                            name=str(categoria),
                            text=df_cat["texto"],
                            textposition="top center",
                            line={"color": color},
                            marker={"color": color},
                            customdata=np.stack([df_cat["pct"], df_cat["total_ano"]], axis=-1),
                            hovertemplate=(
                                "Ano %{x}<br>" + str(categoria) + ": %{y}<br>"
                                "% no ano: %{customdata[0]:.2f}<br>"
                                "Total de confirmados no ano: %{customdata[1]}<extra></extra>"
                            ),
                        )
                    )
                fig_evol_geral.update_layout(
                    title="Evolução dos casos",
                    xaxis_title="Ano",
                    yaxis_title="Casos confirmados",
                )
                st.caption(
                    "Usa o campo Evolução (item 59 da ficha de investigação do SINAN) para todos os casos confirmados, "
                    "incluindo os registros sem evolução preenchida/ignorada. O gráfico abaixo restringe a mesma análise "
                    "aos casos com evolução efetivamente conhecida (alta ou óbito)."
                )
                disable_death_red(fig_evol_geral)
                preserve_trace_colors(fig_evol_geral)
                render_plotly_chart(fig_evol_geral)
                render_interval_total(evol_geral, value_col="n", by_col="categoria")
                copyable_dataframe(evol_geral, width="stretch", hide_index=True)
                download_button(evol_geral, "sinan_evolucao_casos_confirmados.csv")

        if exprs.get("evol_label") and exprs.get("dt") and exprs.get("classi_code") and exprs.get("evol_code"):
            evol_confirmados = query_yearly_category(
                table,
                exprs["dt"],
                exprs["evol_label"],
                append_clause(base_where, f"{exprs['classi_code']} = '1' AND {exprs['evol_code']} IN ('1','2','3')"),
            )
            if not evol_confirmados.empty:
                evol_confirmados = add_text_column(evol_confirmados)
                fig_evol_confirmados = make_subplots(
                    rows=2,
                    cols=1,
                    shared_xaxes=True,
                    row_heights=[0.7, 0.3],
                    vertical_spacing=0.1,
                )
                evol_color_map = {
                    "1 — alta": "#2CA02C",
                    "2 — óbito por meningite": DEATH_RED,
                    "3 — óbito por outra causa": "#1F77B4",
                    LETHALITY_LABEL: "#000000",
                    LETHALITY_KNOWN_EVOL_LABEL: DARK_GRAY,
                }
                for idx, categoria in enumerate(evol_confirmados["categoria"].dropna().unique().tolist()):
                    df_cat = evol_confirmados[evol_confirmados["categoria"].eq(categoria)].copy()
                    color = evol_color_map.get(str(categoria), APP_COLOR_SEQUENCE[idx % len(APP_COLOR_SEQUENCE)])
                    fig_evol_confirmados.add_trace(
                        go.Scatter(
                            x=df_cat["ano"],
                            y=df_cat["n"],
                            mode="lines+markers+text",
                            name=str(categoria),
                            text=df_cat["texto"],
                            textposition="top center",
                            line={"color": color},
                            marker={"color": color},
                            customdata=np.stack([df_cat["pct"], df_cat["total_ano"]], axis=-1),
                            hovertemplate=(
                                "Ano %{x}<br>" + str(categoria) + ": %{y}<br>"
                                "% no ano: %{customdata[0]:.2f}<br>"
                                "Total com evolução conhecida: %{customdata[1]}<extra></extra>"
                            ),
                        ),
                        row=1,
                        col=1,
                    )
                letalidade_df = ind[["ano", "letalidade_confirmados", "obitos_meningite_confirmados", "confirmados"]].copy()
                letalidade_df = letalidade_df[pd.to_numeric(letalidade_df["confirmados"], errors="coerce").fillna(0).gt(0)]
                if not letalidade_df.empty:
                    letalidade_df["texto_letalidade"] = [br_pct(v) for v in letalidade_df["letalidade_confirmados"]]
                    fig_evol_confirmados.add_trace(
                        go.Scatter(
                            x=letalidade_df["ano"],
                            y=letalidade_df["letalidade_confirmados"],
                            mode="lines+markers+text",
                            name=LETHALITY_LABEL,
                            text=letalidade_df["texto_letalidade"],
                            textposition="top center",
                            line={"color": "#000000", "dash": "dash"},
                            marker={"color": "#000000"},
                            customdata=np.stack([letalidade_df["obitos_meningite_confirmados"], letalidade_df["confirmados"]], axis=-1),
                            hovertemplate=(
                                "Ano %{x}<br>Letalidade: %{y:.2f}%<br>"
                                "Óbitos por meningite: %{customdata[0]}<br>"
                                "Confirmados: %{customdata[1]}<extra></extra>"
                            ),
                        ),
                        row=2,
                        col=1,
                    )
                # Segunda linha de letalidade: com evolução conhecida no denominador
                # (óbitos por meningite / confirmados com EVOLUCAO em alta/óbito),
                # trazida de volta a pedido, ao lado da letalidade bruta, para a mesma
                # leitura já usada no gráfico de letalidade por etiologia.
                letalidade_evol_df = ind[["ano", "letalidade_confirmados_evolucao_conhecida", "obitos_meningite_confirmados", "confirmados_evolucao_conhecida"]].copy()
                letalidade_evol_df = letalidade_evol_df[pd.to_numeric(letalidade_evol_df["confirmados_evolucao_conhecida"], errors="coerce").fillna(0).gt(0)]
                if not letalidade_evol_df.empty:
                    letalidade_evol_df["texto_letalidade"] = [br_pct(v) for v in letalidade_evol_df["letalidade_confirmados_evolucao_conhecida"]]
                    fig_evol_confirmados.add_trace(
                        go.Scatter(
                            x=letalidade_evol_df["ano"],
                            y=letalidade_evol_df["letalidade_confirmados_evolucao_conhecida"],
                            mode="lines+markers+text",
                            name=LETHALITY_KNOWN_EVOL_LABEL,
                            text=letalidade_evol_df["texto_letalidade"],
                            textposition="bottom center",
                            line={"color": DARK_GRAY},
                            marker={"color": DARK_GRAY},
                            customdata=np.stack([letalidade_evol_df["obitos_meningite_confirmados"], letalidade_evol_df["confirmados_evolucao_conhecida"]], axis=-1),
                            hovertemplate=(
                                "Ano %{x}<br>Letalidade (evolução conhecida): %{y:.2f}%<br>"
                                "Óbitos por meningite: %{customdata[0]}<br>"
                                "Confirmados com evolução conhecida: %{customdata[1]}<extra></extra>"
                            ),
                        ),
                        row=2,
                        col=1,
                    )
                fig_evol_confirmados.update_layout(
                    title="Evolução dos casos confirmados com evolução conhecida",
                )
                fig_evol_confirmados.update_xaxes(title_text="Ano", row=2, col=1)
                fig_evol_confirmados.update_yaxes(title_text="Confirmados com evolução conhecida", row=1, col=1)
                fig_evol_confirmados.update_yaxes(title_text="Letalidade (%)", ticksuffix="%", row=2, col=1)
                st.caption(
                    "O painel inferior mostra duas letalidades lado a lado: a **bruta** (óbitos por meningite / confirmados) "
                    "e a **com evolução conhecida** (óbitos por meningite / confirmados com EVOLUCAO em alta ou óbito). "
                    "Ambas ficam em painel separado das contagens absolutas acima para evitar confusão de escala (% x contagem). "
                    "O cálculo não foi alterado; a letalidade com evolução conhecida é a mais sensível quando há perda de evolução."
                )
                disable_death_red(fig_evol_confirmados)
                preserve_trace_colors(fig_evol_confirmados)
                render_plotly_chart(fig_evol_confirmados)
                render_interval_total(evol_confirmados, value_col="n", by_col="categoria")
                copyable_dataframe(evol_confirmados, width="stretch", hide_index=True)
                download_button(evol_confirmados, "sinan_evolucao_confirmados_evolucao_conhecida.csv")
        else:
            st.info("Para gerar o gráfico de evolução dos casos confirmados com evolução conhecida, CLASSI_FIN, EVOLUCAO e data precisam existir no SINAN.")

        assistencia = query_sinan_hospitalization_internment(table, exprs, base_where, hospital_col)
        if not assistencia.empty:
            assistencia = assistencia.copy()
            assistencia["texto"] = [f"{br_pct(p)} (n={br_int(n)})" for p, n in zip(assistencia["pct"], assistencia["n"])]
            grupo_hosp_order = ["Total de notificações", "Confirmados", "Descartados", "Sem confirmação / ignorados"]
            fig_assistencia = px.bar(
                assistencia,
                x="ano",
                y="pct",
                color="grupo_caso",
                barmode="group",
                text="texto",
                title="Ocorrência de hospitalização por definição de caso",
                labels={"ano": "Ano", "pct": "% no grupo", "grupo_caso": "Grupo", "n": "Registros", "denominador": "Denominador"},
                hover_data={"texto": False, "n": True, "denominador": True},
                category_orders={"grupo_caso": grupo_hosp_order},
            )
            render_plotly_chart(fig_assistencia)
            render_interval_total(assistencia, value_col="n", by_col="grupo_caso", denominator_col="denominador", denominator_label="registros do grupo")
            copyable_dataframe(assistencia.drop(columns=["ordem_grupo"], errors="ignore"), width="stretch", hide_index=True)
            download_button(assistencia.drop(columns=["ordem_grupo"], errors="ignore"), "sinan_hospitalizacao_total_notificacoes_confirmados_descartados_sem_confirmacao_ignorados.csv")
        else:
            st.info("Para gerar o gráfico comparativo de hospitalização, CLASSI_FIN, data e ATE_HOSPIT precisam existir no SINAN.")

        if symptom_specs and exprs.get("classi_code") and exprs.get("dt"):
            sintomas = query_sinan_symptom_prevalence(table, exprs, base_where, symptom_specs)
            if not sintomas.empty:
                sintomas_resumo = (
                    sintomas
                    .groupby("sintoma", dropna=False, as_index=False)
                    .agg(
                        confirmados=("confirmados", "sum"),
                        sintoma_sim=("sintoma_sim", "sum"),
                        sintoma_nao=("sintoma_nao", "sum"),
                        sintoma_ignorado=("sintoma_ignorado", "sum"),
                    )
                )
                sintomas_resumo["pct_sintoma_confirmados"] = (100.0 * sintomas_resumo["sintoma_sim"] / sintomas_resumo["confirmados"].replace({0: np.nan})).round(2)
                sintomas_resumo = sintomas_resumo.sort_values("pct_sintoma_confirmados", ascending=True)
                sintomas_resumo["texto"] = [f"{br_pct(p)} (n={br_int(n)})" for p, n in zip(sintomas_resumo["pct_sintoma_confirmados"], sintomas_resumo["sintoma_sim"])]
                fig_sintomas_resumo = px.bar(
                    sintomas_resumo,
                    x="pct_sintoma_confirmados",
                    y="sintoma",
                    orientation="h",
                    text="texto",
                    title="Prevalência acumulada dos sinais e sintomas entre confirmados",
                    labels={"pct_sintoma_confirmados": "% dos confirmados", "sintoma": "Sinal/sintoma"},
                    hover_data={"texto": False, "sintoma_sim": True, "confirmados": True, "sintoma_nao": True, "sintoma_ignorado": True},
                )
                render_plotly_chart(fig_sintomas_resumo)
                render_interval_total(sintomas_resumo, value_col="sintoma_sim", by_col="sintoma")

                st.markdown("### Escolha o sintoma para se analisar a curva anual entre confirmados")
                opcoes_sintomas = sorted(sintomas["sintoma"].dropna().unique().tolist())
                sintoma_sel = st.selectbox(
                    "Sintoma",
                    options=opcoes_sintomas,
                    key="sinan_indicadores_sintoma_prevalencia",
                    label_visibility="collapsed",
                )
                sintomas_sel = sintomas[sintomas["sintoma"].eq(sintoma_sel)].copy()
                sintomas_sel["texto"] = [f"{br_pct(p)} (n={br_int(n)})" for p, n in zip(sintomas_sel["pct_sintoma_confirmados"], sintomas_sel["sintoma_sim"])]
                fig_sintoma = px.line(
                    sintomas_sel,
                    x="ano",
                    y="pct_sintoma_confirmados",
                    markers=True,
                    text="texto",
                    title=f"Prevalência anual de {sintoma_sel} entre casos confirmados",
                    labels={"ano": "Ano", "pct_sintoma_confirmados": "% dos confirmados", "sintoma_sim": "Confirmados com sintoma"},
                    hover_data={"texto": False, "sintoma_sim": True, "confirmados": True, "sintoma_nao": True, "sintoma_ignorado": True},
                )
                fig_sintoma.update_traces(textposition="top center")
                render_plotly_chart(fig_sintoma)
                render_interval_total(sintomas_sel, value_col="sintoma_sim", denominator_col="confirmados", value_label="confirmados com sintoma", denominator_label="casos confirmados")

                copyable_dataframe(sintomas, width="stretch", hide_index=True)
                download_button(sintomas, "sinan_prevalencia_sintomas_confirmados.csv")
        else:
            st.info("Para gerar a prevalência de sintomas, CLASSI_FIN, data e os campos clínicos CLI_* precisam existir no SINAN.")

        comunicantes = query_sinan_communicants_prophylaxis(table, exprs, base_where, communicants_col, prophylaxis_col)
        if not comunicantes.empty:
            st.markdown("**Número de comunicantes por realização de quimioprofilaxia**")
            st.caption("Segundo a estrutura do dicionário de dados do SINAN para meningite, `MED_NUCOMU` registra o número de comunicantes identificados e `MED_QUIMIO` informa se foi realizada quimioprofilaxia, codificada como Sim, Não ou Ignorado. O gráfico cruza o total de comunicantes registrados por ano com a situação de realização da quimioprofilaxia e inclui a série total de comunicantes.")
            if exprs.get("con_code"):
                st.caption(
                    "Correção do denominador: agora há duas visões — todos os registros do recorte e elegíveis operacionais "
                    "para quimioprofilaxia de contatos (CON_DIAGES 02/03/09: formas meningocócicas e Haemophilus influenzae). "
                    "Use a visão elegível para interpretação de cobertura; a visão todos serve como sensibilidade/auditoria do denominador amplo."
                )
            else:
                st.caption(
                    "Observação: não foi possível criar a visão elegível (CON_DIAGES 02/03/09), porque CON_DIAGES não foi detectado. "
                    "Os valores abaixo correspondem ao contingente completo de registros ativos e podem subestimar a cobertura real."
                )
            comunicantes = comunicantes.copy()
            recortes_qp = comunicantes["recorte_quimioprofilaxia"].dropna().unique().tolist() if "recorte_quimioprofilaxia" in comunicantes.columns else []
            default_recorte = "Elegíveis operacionais (CON_DIAGES 02/03/09)" if "Elegíveis operacionais (CON_DIAGES 02/03/09)" in recortes_qp else (recortes_qp[0] if recortes_qp else None)
            if len(recortes_qp) > 1:
                recorte_sel = st.selectbox(
                    "Recorte do denominador da quimioprofilaxia",
                    recortes_qp,
                    index=recortes_qp.index(default_recorte) if default_recorte in recortes_qp else 0,
                    key="sinan_quimioprofilaxia_recorte",
                )
                comunicantes_plot_base = comunicantes[comunicantes["recorte_quimioprofilaxia"].eq(recorte_sel)].copy()
            else:
                recorte_sel = default_recorte
                comunicantes_plot_base = comunicantes.copy()
            comunicantes_plot_base["serie"] = comunicantes_plot_base["quimioprofilaxia"].astype(str)
            comunicantes_plot = comunicantes_plot_base.rename(columns={"comunicantes_total": "valor"}).copy()
            total_comunicantes = (
                comunicantes_plot_base
                .groupby("ano", as_index=False)
                .agg(
                    valor=("total_comunicantes_ano", "max"),
                    registros=("registros", "sum"),
                    registros_com_comunicantes=("registros_com_comunicantes", "sum"),
                    media_comunicantes=("media_comunicantes", "mean"),
                    pct_comunicantes_ano=("pct_comunicantes_ano", "sum"),
                )
            )
            total_comunicantes["serie"] = "Total de comunicantes"
            comunicantes_plot = pd.concat([
                comunicantes_plot[["ano", "serie", "valor", "registros", "registros_com_comunicantes", "media_comunicantes", "pct_comunicantes_ano"]],
                total_comunicantes[["ano", "serie", "valor", "registros", "registros_com_comunicantes", "media_comunicantes", "pct_comunicantes_ano"]],
            ], ignore_index=True)
            comunicantes_plot["texto_comunicantes"] = [br_int(v) for v in comunicantes_plot["valor"]]
            fig_comunicantes = px.line(
                comunicantes_plot,
                x="ano",
                y="valor",
                color="serie",
                markers=True,
                text="texto_comunicantes",
                title="Número de comunicantes por realização de quimioprofilaxia",
                labels={"ano": "Ano", "valor": "Comunicantes", "serie": "Quimioprofilaxia / total"},
                hover_data={"texto_comunicantes": False, "registros": True, "registros_com_comunicantes": True, "media_comunicantes": True, "pct_comunicantes_ano": ":.2f"},
            )
            fig_comunicantes.update_traces(textposition="top center")
            render_plotly_chart(fig_comunicantes)
            render_interval_total(comunicantes_plot, value_col="valor", by_col="serie", value_label="comunicantes")
            copyable_dataframe(comunicantes, width="stretch", hide_index=True)
            download_button(comunicantes, "sinan_comunicantes_quimioprofilaxia_todos_e_elegiveis.csv")
        else:
            st.info("Para gerar o gráfico de comunicantes/profilaxia, MED_NUCOMU e/ou MED_QUIMIO precisam existir no SINAN.")

        vacinacao = query_sinan_vaccination_by_classification(table, exprs, base_where, vaccine_specs)
        if not vacinacao.empty:
            st.markdown("**Vacinação por classificação final do caso**")
            vacinacao = vacinacao.copy()
            vacinacao["texto"] = [f"{br_pct(p)} (n={br_int(n)})" for p, n in zip(vacinacao["pct_vacinados_sim"], vacinacao["vacinados_sim"])]
            grupo_vacina_order = ["Confirmados", "Descartados", "Sem classificação / ignorados"]
            fig_vacinacao = px.bar(
                vacinacao,
                x="vacina",
                y="pct_vacinados_sim",
                color="grupo_classificacao",
                barmode="group",
                text="texto",
                title="Vacinação informada como “Sim” por classificação final do caso",
                labels={"vacina": "Vacina", "pct_vacinados_sim": "% com vacinação = Sim", "grupo_classificacao": "Classificação", "denominador": "Denominador"},
                hover_data={"texto": False, "vacinados_sim": True, "vacinados_nao": True, "vacinacao_ignorada": True, "denominador": True},
                category_orders={"grupo_classificacao": grupo_vacina_order},
            )
            fig_vacinacao.update_xaxes(tickangle=-30)
            render_plotly_chart(fig_vacinacao)
            render_interval_total(vacinacao, value_col="vacinados_sim", by_col="vacina", value_label="vacinados com informação = Sim")
            copyable_dataframe(vacinacao, width="stretch", hide_index=True)
            download_button(vacinacao, "sinan_vacinacao_por_classificacao_final.csv")
        else:
            st.info("Para gerar o gráfico de vacinação, CLASSI_FIN e campos ANT_* de vacinação precisam existir no SINAN.")

        render_sinan_lcr_indicators(table, exprs, base_where, graph_where)

        return


    if source == "SIM":
        ind = query_sim_indicators(table, exprs, base_where)
        if ind.empty:
            st.warning("Não foi possível calcular indicadores principais do SIM. Verifique data, CAUSABAS e campos CID.")
        else:
            sim_cid_specs = [
                ("obitos_registros", "Total de óbitos", None),
                ("obitos_com_mencao_meningite", "Meningite mencionada", "pct_mencao_meningite"),
                ("obitos_causa_basica_meningite", "Meningite como causa básica", "pct_causa_basica_meningite"),
            ]
            sim_cid_rows = []
            for _, row in ind.iterrows():
                for n_col, label, pct_col in sim_cid_specs:
                    pct = 100.0 if pct_col is None else (row[pct_col] if pct_col in ind.columns else None)
                    sim_cid_rows.append({
                        "ano": row["ano"],
                        "definicao": label,
                        "n": row[n_col],
                        "pct": pct,
                        "denominador": row["obitos_registros"],
                        "texto": count_pct_text(row[n_col], pct),
                    })
            sim_cid_long = pd.DataFrame(sim_cid_rows)
            fig = px.line(
                sim_cid_long,
                x="ano",
                y="n",
                color="definicao",
                markers=True,
                text="texto",
                title="Óbitos em que o agravo meningite foi mencionado ou atuou como causa básica",
                labels={
                    "ano": "Ano",
                    "n": "Óbitos",
                    "definicao": "Definição de CID",
                    "pct": "% dos óbitos no recorte",
                    "denominador": "Óbitos no recorte",
                },
                hover_data={"texto": False, "pct": ":.2f", "denominador": True},
                color_discrete_map={
                    "Total de óbitos": "#000000",
                    "Meningite mencionada": "#1F77B4",
                    "Meningite como causa básica": DEATH_RED,
                },
            )
            disable_death_red(fig)
            preserve_trace_colors(fig)
            fig.update_traces(textposition="top center")
            render_plotly_chart(fig)
            render_interval_total(sim_cid_long, value_col="n", by_col="definicao")

        sim_fertile_denominator_note = (
            "Denominador obrigatório: estes gráficos de Gravidez e Puerpério do SIM são restritos a mulheres em idade fértil, "
            "tradicionalmente 10 a 49 anos, usando as variáveis padronizadas `sex` e `age` geradas na ColumnSelection. "
            "Assim, registros de homens, de mulheres fora de 10–49 anos ou com sexo/idade ausentes ou inválidos não entram no numerador nem no denominador."
        )

        def render_sim_cycle_chart(
            category_sql: Optional[str],
            field_label: str,
            where_sql: Optional[str],
            markdown_title: str,
            figure_title: str,
            caption: str,
            filename: str,
        ) -> None:
            if not (exprs.get("dt") and category_sql and where_sql):
                return
            df = query_yearly_category(table, exprs["dt"], category_sql, where_sql)
            if df.empty:
                st.info(f"Sem dados para {markdown_title.lower()} com esta definição de meningite e com o denominador restrito a mulheres de 10 a 49 anos.")
                return
            st.markdown(f"**{markdown_title}**")
            st.caption(f"{caption} {sim_fertile_denominator_note}")
            df = add_text_column(df)
            fig_cycle = px.bar(
                df,
                x="ano",
                y="n",
                color="categoria",
                text="texto",
                title=figure_title + " — mulheres 10–49 anos",
                labels={"ano": "Ano", "n": "Óbitos em mulheres 10–49 anos", "categoria": field_label, "pct": "% no ano"},
                hover_data={"texto": False, "pct": ":.2f", "total_ano": True},
                color_discrete_sequence=APP_COLOR_SEQUENCE,
            )
            disable_death_red(fig_cycle)
            preserve_trace_colors(fig_cycle)
            render_plotly_chart(fig_cycle)
            render_interval_total(df, value_col="n", by_col="categoria")
            copyable_dataframe(df, width="stretch", hide_index=True)
            download_button(df, filename)

        cid_any = exprs.get("cid")
        causabas = exprs.get("causabas_cid")
        sim_sex = exprs.get("sex")
        sim_age = exprs.get("age")
        fertile_women_clause = f"({sim_sex}) = 'Feminino' AND ({sim_age}) BETWEEN 10 AND 49" if (sim_sex and sim_age) else None
        mention_where_base = append_clause(base_where, f"{cid_any} IS NOT NULL") if cid_any else None
        primary_cause_where_base = append_clause(base_where, f"{causabas} IS NOT NULL") if causabas else None
        mention_where = append_clause(mention_where_base, fertile_women_clause) if (mention_where_base and fertile_women_clause) else None
        primary_cause_where = append_clause(primary_cause_where_base, fertile_women_clause) if (primary_cause_where_base and fertile_women_clause) else None
        if not fertile_women_clause:
            st.warning(
                "Os gráficos de Gravidez e Puerpério do SIM exigem denominador restrito a mulheres em idade fértil (10 a 49 anos). "
                "Configure/detecte as colunas de sexo e idade para que o app gere `sex` e `age` na ColumnSelection."
            )

        if exprs.get("dt") and exprs.get("obitograv_label"):
            if mention_where:
                render_sim_cycle_chart(
                    exprs["obitograv_label"],
                    "OBITOGRAV",
                    mention_where,
                    "Óbito na gravidez — menção de meningite",
                    "SIM: óbito na gravidez (OBITOGRAV) — óbitos com menção de meningite",
                    "Observação: este gráfico foi construído com base nos óbitos em que houve menção de CID de meningite em qualquer campo do SIM.",
                    "sim_obito_gravidez_obitograv_mencao_meningite.csv",
                )
            else:
                if not cid_any:
                    st.info("Para gerar o gráfico de gravidez por menção de meningite, é necessário detectar algum campo CID no SIM.")
                elif not fertile_women_clause:
                    st.info("Para gerar o gráfico de gravidez por menção de meningite, é necessário restringir o denominador a mulheres de 10 a 49 anos usando `sex` e `age`.")
            if primary_cause_where:
                render_sim_cycle_chart(
                    exprs["obitograv_label"],
                    "OBITOGRAV",
                    primary_cause_where,
                    "Óbito na gravidez — meningite como causa primária/básica",
                    "SIM: óbito na gravidez (OBITOGRAV) — meningite como causa primária/básica",
                    "Este gráfico foi construído apenas com óbitos cuja causa primária/básica contém CID de meningite em CAUSABAS.",
                    "sim_obito_gravidez_obitograv_causa_basica_meningite.csv",
                )
            else:
                if not causabas:
                    st.info("Para gerar o gráfico de gravidez por causa primária/básica, o campo CAUSABAS precisa existir no SIM e ser detectado automaticamente.")
                elif not fertile_women_clause:
                    st.info("Para gerar o gráfico de gravidez por causa primária/básica, é necessário restringir o denominador a mulheres de 10 a 49 anos usando `sex` e `age`.")
        else:
            st.info("Para o gráfico de óbito na gravidez, o campo OBITOGRAV precisa existir no SIM e ser detectado automaticamente.")

        if exprs.get("dt") and exprs.get("obitopuerp_label"):
            if mention_where:
                render_sim_cycle_chart(
                    exprs["obitopuerp_label"],
                    "OBITOPUERP",
                    mention_where,
                    "Óbito no puerpério — menção de meningite",
                    "SIM: óbito no puerpério (OBITOPUERP) — óbitos com menção de meningite",
                    "Observação: este gráfico foi construído com base nos óbitos em que houve menção de CID de meningite em qualquer campo do SIM.",
                    "sim_obito_puerperio_obitopuerp_mencao_meningite.csv",
                )
            else:
                if not cid_any:
                    st.info("Para gerar o gráfico de puerpério por menção de meningite, é necessário detectar algum campo CID no SIM.")
                elif not fertile_women_clause:
                    st.info("Para gerar o gráfico de puerpério por menção de meningite, é necessário restringir o denominador a mulheres de 10 a 49 anos usando `sex` e `age`.")
            if primary_cause_where:
                render_sim_cycle_chart(
                    exprs["obitopuerp_label"],
                    "OBITOPUERP",
                    primary_cause_where,
                    "Óbito no puerpério — meningite como causa primária/básica",
                    "SIM: óbito no puerpério (OBITOPUERP) — meningite como causa primária/básica",
                    "Este gráfico foi construído apenas com óbitos cuja causa primária/básica contém CID de meningite em CAUSABAS.",
                    "sim_obito_puerperio_obitopuerp_causa_basica_meningite.csv",
                )
            else:
                if not causabas:
                    st.info("Para gerar o gráfico de puerpério por causa primária/básica, o campo CAUSABAS precisa existir no SIM e ser detectado automaticamente.")
                elif not fertile_women_clause:
                    st.info("Para gerar o gráfico de puerpério por causa primária/básica, é necessário restringir o denominador a mulheres de 10 a 49 anos usando `sex` e `age`.")
        else:
            st.info("Para o gráfico de óbito no puerpério, o campo OBITOPUERP precisa existir no SIM e ser detectado automaticamente.")
        return

    ind = query_ciha_indicators(table, exprs, base_where)
    st.info(
        "**Morte administrativa** é a contagem operacional do campo `MORTE = 1` na CIHA. "
        "Ela registra desfecho administrativo no atendimento e não substitui, sozinha, a causa básica do óbito do SIM.\n\n"
        "**Permanência zero** é a contagem de registros com `DIAS_PERM = 0`, isto é, sem dia completo de permanência registrado. "
        "Pode representar atendimento sem pernoite, saída no mesmo dia ou forma de preenchimento administrativo, conforme a regra da base."
    )
    if ind.empty:
        st.warning("Não foi possível calcular indicadores da CIHA. Verifique data, diagnóstico e campos MORTE/DIAS_PERM.")
    else:
        ciha_count_specs = [
            ("atendimentos", "Atendimentos", None),
            ("atendimentos_diag_principal_meningite", "Diagnóstico principal de meningite", "pct_atendimentos_diag_principal_meningite"),
            ("mortes_administrativas", "Mortes administrativas", "pct_morte_administrativa"),
        ]
        ciha_count_rows = []
        for _, row in ind.iterrows():
            for n_col, label, pct_col in ciha_count_specs:
                pct = 100.0 if pct_col is None else row[pct_col]
                ciha_count_rows.append({
                    "ano": row["ano"],
                    "indicador": label,
                    "n": row[n_col],
                    "pct": pct,
                    "denominador": row["atendimentos"],
                    "texto": count_pct_text(row[n_col], pct),
                })
        ciha_count_long = pd.DataFrame(ciha_count_rows)
        fig = px.line(
            ciha_count_long,
            x="ano",
            y="n",
            color="indicador",
            markers=True,
            text="texto",
            title="Atendimentos e mortes administrativas",
            labels={"ano": "Ano", "n": "Atendimentos/registros", "indicador": "Indicador", "pct": "% dos atendimentos"},
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
        )
        fig.update_traces(textposition="top center")
        render_plotly_chart(fig)
        render_interval_total(ciha_count_long, value_col="n", by_col="indicador")

    if exprs.get("dt") and exprs.get("modalidade_label"):
        modalidade = query_yearly_category(table, exprs["dt"], exprs["modalidade_label"], base_where)
        if not modalidade.empty:
            st.markdown("**Modalidade do atendimento — hospitalar vs ambulatorial**")
            modalidade = add_text_column(modalidade)
            fig_modalidade = px.bar(
                modalidade,
                x="ano",
                y="n",
                color="categoria",
                text="texto",
                title="Atendimentos por modalidade hospitalar e ambulatorial",
                labels={"ano": "Ano", "n": "Atendimentos", "categoria": "Modalidade", "pct": "% no ano"},
                hover_data={"texto": False, "pct": ":.2f", "total_ano": True},
            )
            fig_modalidade.update_layout(barmode="stack")
            render_plotly_chart(fig_modalidade)
            render_interval_total(modalidade, value_col="n", by_col="categoria")
            copyable_dataframe(modalidade, width="stretch", hide_index=True)
            download_button(modalidade, "ciha_modalidade_hospitalar_ambulatorial.csv")
        else:
            st.info("Sem dados de modalidade no recorte atual da CIHA.")
    else:
        st.info("Para gerar o gráfico de hospitalar vs ambulatorial, os campos de data e MODALIDADE precisam existir na CIHA e ser detectados automaticamente.")

    dias_dist = query_ciha_dias_perm_distribution(table, exprs, base_where)
    if not dias_dist.empty:
        st.markdown("**Distribuição dos dias de permanência**")
        dias_dist = add_text_column(dias_dist)
        fig_dias = px.bar(
            dias_dist,
            x="faixa_dias_perm",
            y="n",
            text="texto",
            title="Dias de permanência",
            labels={"faixa_dias_perm": "Dias de permanência", "n": "Atendimentos", "pct": "%"},
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
        )
        render_plotly_chart(fig_dias)
        render_interval_total(dias_dist, value_col="n")
        copyable_dataframe(dias_dist, width="stretch", hide_index=True)
        download_button(dias_dist, "ciha_dias_permanencia_distribuicao.csv")
    else:
        st.info("Para gerar o gráfico de dias de permanência, o campo DIAS_PERM precisa existir na CIHA e ser detectado automaticamente.")


def render_cid_tab(table: LoadedTable, source: str, graph_where: str, exprs: Dict[str, Optional[str]], base_where: Optional[str] = None) -> None:
    def br_int(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{int(value):,}".replace(",", ".")

    def br_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}%".replace(".", ",")

    def add_text(df: pd.DataFrame, pct_col: str = "pct") -> pd.DataFrame:
        out = df.copy()
        out["texto"] = [f"{br_int(n)} ({br_pct(pct)})" for n, pct in zip(out["n"], out[pct_col])]
        return out

    if source != "SINAN":
        st.markdown("### CID-10 do registro")
        render_cid_reference()
        cid_dist = query_cid_distribution(table, exprs, graph_where)
        if cid_dist.empty:
            st.warning("Não localizei campo CID-10 válido pela detecção automática para ativar esta análise.")
        else:
            cid_dist = add_text(cid_dist)
            fig = px.bar(
                cid_dist,
                x="n",
                y="tipo",
                orientation="h",
                text="texto",
                title="Distribuição por tipo CID-10",
                labels={"tipo": "Tipo CID-10", "n": "Registros", "pct": "%"},
                hover_data={"texto": False, "pct": ":.2f", "cids_encontrados": True, "campos_origem": True},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"})
            render_plotly_chart(fig)
            render_interval_total(cid_dist, value_col="n")
            copyable_dataframe(cid_dist, width="stretch", hide_index=True)
            download_button(cid_dist, f"{source.lower()}_cid10_distribuicao.csv")

            conv_adequacy = query_cid10_adequacy_conversion(table, exprs, graph_where)
            if not conv_adequacy.empty:
                conv_adequacy = add_text(conv_adequacy)
                conv_adequacy_plot = summarize_cid10_adequacy_plot(conv_adequacy)
                st.markdown("### Conversão para adequação ao CID-10 de meningite / encefalite")
                st.caption(CID10_ADEQUACY_OBSERVATION)
                if conv_adequacy_plot.empty:
                    st.info("Não houve CID-10 detectado para exibir no gráfico de adequação no recorte atual.")
                else:
                    conv_adequacy_plot = add_text(conv_adequacy_plot)
                    fig_conv = px.bar(
                        conv_adequacy_plot,
                        x="n",
                        y="categoria_grafico",
                        orientation="h",
                        text="texto",
                        title="Conversão para adequação ao CID-10 de meningite/encefalite",
                        labels={"categoria_grafico": "CID-10 adequado (prefixo)", "n": "Registros", "pct": "% do total detectado"},
                        hover_data={
                            "texto": False,
                            "pct": ":.2f",
                            "denominador": True,
                            "cid10_adequado_classificacao": True,
                            "status_conversao": True,
                            "cid10_originais": True,
                            "cids_detectados": True,
                            "campos_origem": True,
                        },
                    )
                    fig_conv.update_layout(yaxis={"categoryorder": "total ascending"})
                    render_plotly_chart(fig_conv)
                    render_interval_total(conv_adequacy_plot, value_col="n")
                    st.caption(
                        "Gráfico agregado pelo CID-10 adequado final: CID-10 convertidos somam no destino "
                        "e CID-10 prefixados já presentes permanecem em seu próprio grupo."
                    )
                    st.caption(build_cid10_adequacy_conversion_note(conv_adequacy))
                display_cols = [
                    c for c in [
                        "cid10_original", "cid10_adequado_grupo", "cid10_adequado_classificacao",
                        "status_conversao", "n", "pct", "denominador", "cids_detectados",
                        "classificacoes_originais", "observacoes", "campos_origem",
                    ]
                    if c in conv_adequacy.columns
                ]
                copyable_dataframe(conv_adequacy[display_cols], width="stretch", hide_index=True)
                download_button(conv_adequacy, f"{source.lower()}_cid10_conversao_adequacao_meningite_encefalite.csv")
                with st.expander("Regra usada para a conversão de adequação"):
                    copyable_dataframe(pd.DataFrame(CID10_ADEQUACY_MAPPING_ROWS), width="stretch", hide_index=True)
                    st.caption(CID10_ADEQUACY_OBSERVATION)

            g01_g02 = query_g01_g02_cid_distribution(table, exprs, graph_where)
            if not g01_g02.empty:
                g01_g02 = add_text(g01_g02)
                st.markdown("**Verificação específica — G01 e G02 em SIM/CIHA**")
                st.caption(
                    "Este bloco usa apenas CID-10 bruto informado no próprio SIM/CIHA. "
                    "Não há conversão automática da doença de base para G01/G02 quando o código G01/G02 não aparece no campo CID."
                )
                fig_g01_g02 = px.bar(
                    g01_g02,
                    x="n",
                    y="tipo",
                    orientation="h",
                    text="texto",
                    title="Registros classificados como G01 ou G02",
                    labels={"tipo": "Tipo CID-10", "n": "Registros", "pct": "%"},
                    hover_data={"texto": False, "pct": ":.2f", "denominador": True},
                )
                fig_g01_g02.update_layout(yaxis={"categoryorder": "total ascending"})
                render_plotly_chart(fig_g01_g02)
                render_interval_total(g01_g02, value_col="n")
                copyable_dataframe(g01_g02, width="stretch", hide_index=True)
                download_button(g01_g02, f"{source.lower()}_verificacao_g01_g02.csv")

        if source == "CIHA":
            st.markdown("### Óbitos CIHA — CID-10 destes")
            morte = exprs.get("morte_code")
            if not morte:
                st.info("Para mostrar os óbitos da CIHA e seus CID-10, o campo MORTE precisa existir na CIHA e ser detectado automaticamente.")
            elif not exprs.get("cid"):
                st.info("Para mostrar o CID-10 dos óbitos da CIHA, ao menos um campo de diagnóstico/CID-10 precisa existir e ser detectado automaticamente.")
            else:
                death_where = append_clause(graph_where, f"{morte} = '1'")
                total_deaths = count_rows(table, death_where)
                st.metric("Óbitos CIHA no recorte atual", f"{total_deaths:,}".replace(",", "."))
                if total_deaths == 0:
                    st.info("Não há registros com MORTE = 1 no recorte atual.")
                else:
                    death_cid = query_ciha_death_cid_distribution(table, exprs, graph_where)
                    if death_cid.empty:
                        st.warning("Há óbitos no recorte, mas não foi possível tabular CID-10 para esses registros.")
                    else:
                        death_cid = add_text(death_cid)
                        fig_death = px.bar(
                            death_cid,
                            x="n",
                            y="tipo",
                            orientation="h",
                            text="texto",
                            title="CID-10 dos registros com morte administrativa",
                            labels={"tipo": "Tipo CID-10", "n": "Óbitos CIHA", "pct": "% dos óbitos"},
                            hover_data={"texto": False, "pct": ":.2f", "cids_encontrados": True, "campos_origem": True},
                        )
                        fig_death.update_layout(yaxis={"categoryorder": "total ascending"})
                        render_plotly_chart(fig_death)
                        render_interval_total(death_cid, value_col="n", value_label="óbitos CIHA")
                        copyable_dataframe(death_cid, width="stretch", hide_index=True)
                        download_button(death_cid, "ciha_obitos_cid10_distribuicao.csv")
        return

    st.markdown("### Classificação específica do SINAN")
    st.info(
        "No SINAN, o CID bruto do agravo pode estar como G039 para muitos registros. "
        "Por isso, esta aba prioriza CON_DIAGES e os campos complementares CLA_ME_BAC, CLA_ME_ASS e CLA_ME_ETI. "
        "CON_DIAGES=05 deixou de ser convertido automaticamente para G04.2; ele é refinado como G00 ou G01 quando há informação suficiente."
    )

    conversion_base_where = base_where if base_where is not None else graph_where
    confirmed_conversion_where = (
        append_clause(conversion_base_where, f"{exprs['classi_code']} = '1'")
        if exprs.get("classi_code")
        else conversion_base_where
    )

    if exprs.get("con_label"):
        con_coverage_text = ""
        con_coverage_df = pd.DataFrame()
        if exprs.get("con_code"):
            con_coverage_df = query_field_coverage(table, exprs["con_code"], confirmed_conversion_where)
            con_coverage_text = coverage_subtitle_from_df(con_coverage_df)
        conclusao_df = query_category(table, exprs["con_label"], confirmed_conversion_where, top_n=40)
        if not conclusao_df.empty:
            conclusao_df = add_text(conclusao_df)
            fig_conclusao = px.bar(
                conclusao_df,
                x="n",
                y="categoria",
                orientation="h",
                text="texto",
                title="Conclusão diagnóstica entre casos confirmados" + (f"<br><sup>{con_coverage_text}</sup>" if con_coverage_text else ""),
                labels={"categoria": "Conclusão diagnóstica", "n": "Casos confirmados", "pct": "% dos confirmados"},
                hover_data={"texto": False, "pct": ":.2f"},
            )
            fig_conclusao.update_layout(yaxis={"categoryorder": "total ascending"})
            render_field_completeness_warning(con_coverage_df, "CON_DIAGES (conclusão diagnóstica)")
            render_plotly_chart(fig_conclusao)
            st.caption(
                "Prevalência das categorias específicas de CON_DIAGES entre casos confirmados. "
                "Categorias ausentes/ignoradas permanecem no gráfico para preservar a leitura do denominador."
            )
            if con_coverage_text:
                st.caption("CON_DIAGES — " + con_coverage_text)
            render_interval_total(conclusao_df, value_col="n", value_label="casos confirmados")
            copyable_dataframe(conclusao_df, width="stretch", hide_index=True)
            download_button(conclusao_df, "sinan_conclusao_diagnostica_confirmados.csv")

    if exprs.get("con_group"):
        con_coverage_text = ""
        con_coverage_df = pd.DataFrame()
        if exprs.get("con_code"):
            con_coverage_df = query_field_coverage(table, exprs["con_code"], confirmed_conversion_where)
            con_coverage_text = coverage_subtitle_from_df(con_coverage_df)
        df = query_category(table, exprs["con_group"], confirmed_conversion_where, top_n=40)
        if not df.empty:
            df = add_text(df)
            fig = px.bar(
                df,
                x="n",
                y="categoria",
                orientation="h",
                text="texto",
                title="Classificação etiológica conforme o SINAN para os casos confirmados" + (f"<br><sup>{con_coverage_text}</sup>" if con_coverage_text else ""),
                labels={"categoria": "Classificação etiológica conforme o SINAN", "n": "Casos confirmados", "pct": "%"},
                hover_data={"texto": False, "pct": ":.2f"},
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"})
            render_field_completeness_warning(con_coverage_df, "CON_DIAGES (classificação etiológica)")
            render_plotly_chart(fig)
            if con_coverage_text:
                st.caption("CON_DIAGES — " + con_coverage_text)
            render_interval_total(df, value_col="n", value_label="casos confirmados")
            copyable_dataframe(df, width="stretch", hide_index=True)

    if exprs.get("criterio_code"):
        criterio_presenca = query_field_presence(
            table,
            exprs["criterio_code"],
            confirmed_conversion_where,
            present_label="Sim — critério informado",
            absent_label="Não — sem critério informado",
        )
        if not criterio_presenca.empty:
            criterio_presenca = add_text(criterio_presenca)
            fig_criterio_presenca = px.bar(
                criterio_presenca,
                x="categoria",
                y="n",
                text="texto",
                title="Presença de critério de confirmação entre casos confirmados",
                labels={"categoria": "Critério de confirmação", "n": "Casos confirmados", "pct": "% dos confirmados"},
                hover_data={"texto": False, "pct": ":.2f", "denominador": True},
            )
            render_plotly_chart(fig_criterio_presenca)
            st.caption(
                "Este gráfico mostra se o campo CRITERIO está informado entre os casos confirmados. "
                "O gráfico seguinte detalha quais critérios foram registrados entre os preenchidos."
            )
            render_interval_total(criterio_presenca, value_col="n", value_label="casos confirmados")
            copyable_dataframe(criterio_presenca, width="stretch", hide_index=True)
            download_button(criterio_presenca, "sinan_presenca_criterio_confirmacao_confirmados.csv")

    if exprs.get("criterio_label"):
        criterio_coverage_text = ""
        criterio_coverage_df = pd.DataFrame()
        if exprs.get("criterio_code"):
            criterio_coverage_df = query_field_coverage(table, exprs["criterio_code"], confirmed_conversion_where)
            criterio_coverage_text = coverage_subtitle_from_df(criterio_coverage_df)
        criterio_df = query_category(table, exprs["criterio_label"], confirmed_conversion_where, top_n=40)
        if not criterio_df.empty:
            criterio_df = add_text(criterio_df)
            fig_criterio = px.bar(
                criterio_df,
                x="n",
                y="categoria",
                orientation="h",
                text="texto",
                title="Critério de confirmação entre casos confirmados" + (f"<br><sup>{criterio_coverage_text}</sup>" if criterio_coverage_text else ""),
                labels={"categoria": "Critério", "n": "Casos confirmados", "pct": "%"},
                hover_data={"texto": False, "pct": ":.2f"},
            )
            fig_criterio.update_layout(yaxis={"categoryorder": "total ascending"})
            render_field_completeness_warning(criterio_coverage_df, "CRITERIO (critério de confirmação)")
            render_plotly_chart(fig_criterio)
            if criterio_coverage_text:
                st.caption("CRITERIO — " + criterio_coverage_text)
            render_interval_total(criterio_df, value_col="n", value_label="casos confirmados")
            copyable_dataframe(criterio_df, width="stretch", hide_index=True)

    etio = query_sinan_etiology_lethality(table, exprs, conversion_base_where)
    if not etio.empty:
        st.caption("Denominador do gráfico: mostra lado a lado a letalidade bruta (óbitos por meningite / confirmados) e a letalidade com evolução conhecida (óbitos por meningite / confirmados com EVOLUCAO em alta, óbito por meningite ou óbito por outra causa).")
        etio = etio.copy()
        etio["denominador_letalidade"] = etio["confirmados"]
        etio["denominador_letalidade_evolucao_conhecida"] = etio["confirmados_evolucao_conhecida"]
        etio_long = pd.concat([
            etio.assign(
                estimativa=LETHALITY_LABEL,
                letalidade_valor=etio["letalidade_pct"],
                denominador_letalidade_plot=etio["denominador_letalidade"],
            ),
            etio.assign(
                estimativa=LETHALITY_KNOWN_EVOL_LABEL,
                letalidade_valor=etio["letalidade_evolucao_conhecida_pct"],
                denominador_letalidade_plot=etio["denominador_letalidade_evolucao_conhecida"],
            ),
        ], ignore_index=True)
        etio_long["texto"] = [
            f"{br_pct(p)} ({br_int(o)}/{br_int(c)})"
            for p, o, c in zip(etio_long["letalidade_valor"], etio_long["obitos_meningite"], etio_long["denominador_letalidade_plot"])
        ]
        fig3 = px.bar(
            etio_long,
            x="letalidade_valor",
            y="grupo_etiologico",
            color="estimativa",
            orientation="h",
            barmode="group",
            text="texto",
            title="Letalidade conforme grupo etiológico do SINAN",
            labels={"letalidade_valor": "Letalidade (%)", "grupo_etiologico": "Grupo etiológico", "denominador_letalidade_plot": "Denominador", "estimativa": "Estimativa"},
            hover_data={"obitos_meningite": True, "denominador_letalidade_plot": True, "texto": False},
            color_discrete_map={LETHALITY_LABEL: PLOTLY_DEFAULT_BLUE, LETHALITY_KNOWN_EVOL_LABEL: DARK_GRAY},
        )
        disable_death_red(fig3)
        preserve_trace_colors(fig3)
        fig3.update_layout(yaxis={"categoryorder": "total ascending"})
        render_plotly_chart(fig3)
        render_interval_total(etio, value_col="obitos_meningite", denominator_col="confirmados_evolucao_conhecida", value_label="óbitos por meningite", denominator_label="confirmados com evolução conhecida")
        copyable_dataframe(etio, width="stretch", hide_index=True)
        download_button(etio, "sinan_letalidade_por_etiologia.csv")

    meningococcemia_isolada_total = (
        count_rows(table, append_clause(confirmed_conversion_where, f"{exprs['con_code']} = '01'"))
        if exprs.get("classi_code") and exprs.get("con_code")
        else None
    )

    by_year = query_sinan_diagnostics_by_year(table, exprs, conversion_base_where)
    if not by_year.empty:
        st.caption(
            "Usa a classificação CID-10 derivada de CON_DIAGES, CLA_ME_BAC e campos complementares do SINAN, "
            "restrita a casos confirmados. O ID_AGRAVO/CID bruto do SINAN não é usado para estratificar este gráfico."
        )
        plot_by_year = by_year.copy()
        plot_by_year["ano"] = plot_by_year["ano"].astype(int).astype(str)
        fig4 = px.bar(
            plot_by_year,
            x="ano",
            y="confirmados",
            color="grupo_etiologico",
            title="Conversão da classificação do SINAN para a classificação conforme CID-10",
            labels={
                "grupo_etiologico": "CID-10 convertido / grupo etiológico",
                "confirmados": "Confirmados",
                "ano": "Ano",
                "cid10_grupo": "Família CID-10",
                "total_ano": "Total no ano",
                "pct_ano": "% no ano",
            },
            hover_data={"cid10_grupo": True, "total_ano": True, "pct_ano": ":.2f"},
        )
        fig4.update_layout(barmode="stack")
        fig4.update_xaxes(type="category")
        render_plotly_chart(fig4)
        render_interval_total(by_year, value_col="confirmados", by_col="grupo_etiologico", value_label="confirmados")

    conv = query_sinan_cid10_conversion(table, exprs, confirmed_conversion_where)
    if not conv.empty:
        conv_yes = conv[conv["incluido_comparacao"].eq("Sim")].copy()
        conv_no = conv[~conv["incluido_comparacao"].eq("Sim")].copy()

        if conv_yes.empty:
            st.warning("Não há casos confirmados com CON_DIAGES conversível pela regra definida.")
        else:
            conv_yes = add_text(conv_yes)
            fig_conv = px.bar(
                conv_yes,
                x="n",
                y="cid10_classificacao",
                orientation="h",
                text="texto",
                title="Classificação etiológica convertida para CID-10",
                labels={"cid10_classificacao": "CID-10 convertido", "n": "Confirmados", "pct": "%"},
                hover_data={"texto": False, "pct": ":.2f", "denominador": True, "grupos_sinan": True, "conclusoes_sinan": True},
            )
            fig_conv.update_layout(yaxis={"categoryorder": "total ascending"})
            render_plotly_chart(fig_conv)
            render_interval_total(conv_yes, value_col="n", value_label="confirmados")

        if meningococcemia_isolada_total is not None:
            st.caption(
                "Observação: a meningococcemia isolada (CON_DIAGES = 01) foi mantida fora da conversão CID-10 de meningite de forma proposital, "
                "para não incluir casos sem forma meningítica na comparação. "
                f"No recorte de casos confirmados analisado, isso corresponde a {br_int(meningococcemia_isolada_total)} caso(s)."
            )

        with st.expander("Regra usada para converter CON_DIAGES em CID-10"):
            st.markdown("**Conversão principal CON_DIAGES -> família CID-10**")
            copyable_dataframe(pd.DataFrame(SINAN_CID10_MAPPING_ROWS), width="stretch", hide_index=True)
            st.markdown("**Refinamento específico para CON_DIAGES=05 — meningite por outras bactérias**")
            copyable_dataframe(pd.DataFrame(SINAN_OTHER_BACTERIA_CID10_RULE_ROWS), width="stretch", hide_index=True)
            st.markdown("**G01 — doença bacteriana de base provável**")
            copyable_dataframe(pd.DataFrame(SINAN_G01_BASE_DISEASE_REFERENCE_ROWS), width="stretch", hide_index=True)
            meningococcemia_txt = br_int(meningococcemia_isolada_total) if meningococcemia_isolada_total is not None else "—"
            st.caption(
                "Observação: CON_DIAGES 01 (meningococcemia isolada) fica fora da conversão de forma proposital; "
                f"no recorte de casos confirmados analisado, foram {meningococcemia_txt} caso(s). "
                "CON_DIAGES 02 e 03 entram como A39.0; CON_DIAGES 05 entra como G00 por padrão e como G01 quando CLA_ME_BAC/texto sugerem doença bacteriana classificada em outra parte."
            )

        if not conv_no.empty:
            st.caption("Casos confirmados não convertidos para a comparação CID-10, mantendo transparência da exclusão/ausência de mapeamento:")
            conv_no_cols = [c for c in ["cid10_grupo", "cid10_classificacao", "n", "pct", "conclusoes_sinan", "justificativas"] if c in conv_no.columns]
            copyable_dataframe(conv_no[conv_no_cols], width="stretch", hide_index=True)



def render_demography_tab(table: LoadedTable, source: str, graph_where: str, exprs: Dict[str, Optional[str]], base_where: Optional[str] = None) -> None:
    def br_int(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{int(value):,}".replace(",", ".")

    def br_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}%".replace(".", ",")

    def add_text(df: pd.DataFrame, pct_col: str = "pct") -> pd.DataFrame:
        out = df.copy()
        out["texto"] = [f"{br_int(n)} ({br_pct(pct)})" for n, pct in zip(out["n"], out[pct_col])]
        return out

    classi_code = exprs.get("classi_code") if source == "SINAN" else None
    sex = exprs.get("sex")
    race = exprs.get("race")
    age = exprs.get("age")
    education = exprs.get("education")
    demography_case_base_where = base_where if (source == "SINAN" and base_where is not None) else graph_where
    outcome_demography_where = base_where if base_where is not None else graph_where
    sinan_case_filter_options = ["Casos confirmados", "Casos descartados / sem classificação"]

    def sinan_case_filter_where(selection: str) -> str:
        if not (source == "SINAN" and classi_code):
            return graph_where
        if selection == "Casos confirmados":
            return append_clause(demography_case_base_where, f"{classi_code} = '1'")
        return append_clause(demography_case_base_where, f"({classi_code} IS NULL OR {classi_code} <> '1')")

    def sinan_case_filter_suffix(selection: Optional[str]) -> str:
        if selection == "Casos confirmados":
            return "confirmados"
        if selection == "Casos descartados / sem classificação":
            return "descartados_sem_classificacao"
        return ""

    def sinan_case_filter_title(selection: Optional[str]) -> str:
        if not selection:
            return ""
        return " — " + selection.lower()

    def render_age_pyramid_chart(pyramid_where: str, selection: Optional[str] = None) -> None:
        if not (age and sex):
            return
        pyr = query_age_dist(table, age, pyramid_where, sex_sql=sex)
        if pyr.empty:
            st.info("Sem dados suficientes de idade e sexo para gerar a pirâmide etária com os filtros atuais.")
            return
        pyr = pyr.sort_values("faixa_ini").reset_index(drop=True)
        pyr["valor"] = np.where(pyr["sexo"].eq("Masculino"), -pyr["n"], pyr["n"])
        faixa_order_pyr = pyr.sort_values("faixa_ini")["faixa"].drop_duplicates().tolist()
        fig_pyr = px.bar(
            pyr,
            x="valor",
            y="faixa",
            color="sexo",
            orientation="h",
            title="Pirâmide etária por sexo" + sinan_case_filter_title(selection),
            labels={"valor": "Registros", "faixa": "Faixa etária"},
            category_orders={"faixa": faixa_order_pyr},
        )
        fig_pyr.update_layout(barmode="relative", yaxis={"categoryorder": "array", "categoryarray": faixa_order_pyr})
        render_plotly_chart(fig_pyr)
        st.caption(
            "A faixa quinquenal 0–4 anos foi desdobrada em '< 1 ano' e '1–4 anos' porque as janelas de imunização "
            "relevantes para meningite (Pentavalente/Meningo C no primeiro ano de vida; Pneumo 10 e reforços nos anos "
            "seguintes) atuam de forma concentrada nessas idades e ficariam escondidas dentro de um único bloco 0–4."
        )
        render_interval_total(pyr, value_col="n", by_col="sexo")
        suffix = sinan_case_filter_suffix(selection)
        filename = f"{source.lower()}_piramide_{suffix}.csv" if suffix else f"{source.lower()}_piramide.csv"
        download_button(pyr, filename)

    def render_age_distribution_chart(age_where: str, selection: Optional[str] = None) -> None:
        if not age:
            return
        age_df = query_age_dist(table, age, age_where)
        if age_df.empty:
            st.info("Sem dados suficientes de idade para gerar a distribuição por faixa etária com os filtros atuais.")
            return
        age_df = age_df.sort_values("faixa_ini").reset_index(drop=True)
        age_df["denominador"] = int(age_df["n"].sum())
        age_df["pct"] = np.where(age_df["denominador"].gt(0), (age_df["n"] / age_df["denominador"] * 100).round(2), np.nan)
        age_df = add_text(age_df)
        faixa_order = age_df["faixa"].tolist()
        fig_age = px.bar(
            age_df,
            x="faixa",
            y="n",
            text="texto",
            title="Distribuição de casos conforme faixa etária" + sinan_case_filter_title(selection),
            labels={"faixa": "Faixa etária", "n": "Registros", "pct": "%", "denominador": "Denominador"},
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
            category_orders={"faixa": faixa_order},
        )
        fig_age.update_traces(textposition="outside", cliponaxis=False)
        render_plotly_chart(fig_age)
        render_interval_total(age_df, value_col="n")
        suffix = sinan_case_filter_suffix(selection)
        filename = f"{source.lower()}_idade_{suffix}.csv" if suffix else f"{source.lower()}_idade.csv"
        download_button(age_df, filename)

    def _education_age_stratification_note() -> None:
        if age:
            st.caption(
                "Observação metodológica: a estratificação por faixa etária pode ser interessante porque a meningite é mais comum em crianças e idosos. "
                "Sem estratificar, a distribuição de escolaridade pode refletir a composição etária dos casos/óbitos/atendimentos, criando um viés de escolaridade que não necessariamente representa a realidade epidemiológica da doença."
            )
        else:
            st.caption(
                "Observação metodológica: a estratificação por faixa etária seria desejável, porque a meningite é mais comum em crianças e idosos e isso pode criar viés na leitura de escolaridade. "
                "Para habilitá-la, configure uma coluna de idade para gerar a variável `age` na seleção de colunas."
            )

    def _available_age_bands(df: pd.DataFrame) -> List[str]:
        if df.empty or not {"faixa_etaria", "faixa_ini", "denominador"}.issubset(df.columns):
            return []
        bands = (
            df[["faixa_etaria", "faixa_ini", "denominador"]]
            .drop_duplicates()
            .assign(denominador_num=lambda d: pd.to_numeric(d["denominador"], errors="coerce").fillna(0))
            .query("denominador_num > 0")
            .sort_values(["faixa_ini", "faixa_etaria"])
        )
        return bands["faixa_etaria"].astype(str).tolist()

    def render_sinan_education_chart() -> None:
        if not education:
            st.info("Para gerar o gráfico de escolaridade no SINAN, o campo CS_ESCOL_N/ESCOLARIDADE precisa existir e ser detectado automaticamente.")
            return
        if not exprs.get("classi_code"):
            st.info("Para gerar a escolaridade por confirmados e descartados no SINAN, o campo CLASSI_FIN precisa existir e ser detectado automaticamente.")
            return
        st.caption(
            "Atenção: no SINAN, a categoria 10 ('não se aplica') de CS_ESCOL_N costuma representar quase 40% da base e é "
            "composta majoritariamente por pacientes com menos de 5 anos, para quem o campo de escolaridade não é "
            "aplicável. Os gráficos abaixo incluem essa categoria como registrada na ficha; ela não deve ser lida como "
            "'sem preenchimento pela equipe', e sim como resultado esperado da composição etária dos casos de meningite."
        )
        _education_age_stratification_note()
        schooling_where = base_where if base_where is not None else graph_where
        stratify_by_age = False
        if age:
            stratify_by_age = st.checkbox(
                "Estratificar gráfico de escolaridade por faixa etária quinquenal (idade em anos)",
                value=False,
                key="sinan_education_age_stratify",
            )

        if stratify_by_age:
            edu_df = query_sinan_education_outcomes_by_age(
                table,
                education,
                exprs["classi_code"],
                age,
                schooling_where,
            )
            if edu_df.empty or pd.to_numeric(edu_df.get("denominador"), errors="coerce").fillna(0).max() <= 0:
                st.info("Sem casos confirmados ou descartados para calcular a escolaridade por faixa etária com os filtros atuais.")
                return
            age_band_options = _available_age_bands(edu_df)
            if not age_band_options:
                st.info("Não há faixas etárias válidas para estratificar a escolaridade com os filtros atuais.")
                return
            selected_age_band = st.selectbox(
                "Faixa etária usada no gráfico de escolaridade do SINAN",
                age_band_options,
                key="sinan_education_age_band",
            )
            plot_df = edu_df[edu_df["faixa_etaria"].astype(str).eq(str(selected_age_band))].copy()
            plot_df = add_text(plot_df)
            grupo_order = ["Casos confirmados", "Casos descartados"]
            categoria_order = education_category_labels("SINAN", education, include_missing=True)
            plot_df = plot_df.sort_values(["ordem_escolaridade", "ordem_grupo", "grupo"]).reset_index(drop=True)
            fig_edu = px.bar(
                plot_df,
                x="n",
                y="escolaridade",
                color="grupo",
                orientation="h",
                barmode="group",
                text="texto",
                title=f"Escolaridade — casos confirmados e descartados — faixa etária {selected_age_band}",
                labels={
                    "escolaridade": "Escolaridade",
                    "n": "Registros",
                    "grupo": "Grupo",
                    "pct": "% no grupo e faixa etária",
                    "denominador": "Total do grupo na faixa etária",
                    "faixa_etaria": "Faixa etária",
                },
                hover_data={"texto": False, "pct": ":.2f", "denominador": True, "faixa_etaria": True},
                category_orders={"escolaridade": categoria_order, "grupo": grupo_order},
            )
            fig_edu.update_layout(yaxis={"categoryorder": "array", "categoryarray": categoria_order[::-1]})
            st.caption(
                "Com a estratificação ativada, os percentuais usam como denominador apenas os registros da faixa etária selecionada, separados por grupo de classificação do SINAN. "
                "A tabela/exportação abaixo mantém todas as faixas etárias para auditoria."
            )
            render_plotly_chart(fig_edu)
            render_interval_total(plot_df, value_col="n", by_col="grupo")
            edu_out = edu_df.drop(columns=["ordem_escolaridade", "ordem_grupo"], errors="ignore")
            copyable_dataframe(edu_out, width="stretch", hide_index=True)
            download_button(edu_out, "sinan_escolaridade_confirmados_descartados_por_faixa_etaria.csv")
            return

        edu_df = query_sinan_education_outcomes(
            table,
            education,
            exprs["classi_code"],
            schooling_where,
        )
        if edu_df.empty or pd.to_numeric(edu_df.get("denominador"), errors="coerce").fillna(0).max() <= 0:
            st.info("Sem casos confirmados ou descartados para calcular a escolaridade com os filtros atuais.")
            return
        edu_df = add_text(edu_df)
        grupo_order = ["Casos confirmados", "Casos descartados"]
        categoria_order = education_category_labels("SINAN", education, include_missing=True)
        edu_df = edu_df.sort_values(["ordem_escolaridade", "ordem_grupo", "grupo"]).reset_index(drop=True)
        fig_edu = px.bar(
            edu_df,
            x="n",
            y="escolaridade",
            color="grupo",
            orientation="h",
            barmode="group",
            text="texto",
            title="Escolaridade — casos confirmados e descartados",
            labels={
                "escolaridade": "Escolaridade",
                "n": "Registros",
                "grupo": "Grupo",
                "pct": "% no grupo",
                "denominador": "Total no grupo",
            },
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
            category_orders={"escolaridade": categoria_order, "grupo": grupo_order},
        )
        fig_edu.update_layout(yaxis={"categoryorder": "array", "categoryarray": categoria_order[::-1]})
        st.caption("O gráfico exibe todas as categorias operacionais de escolaridade do SINAN; os percentuais usam o denominador do próprio grupo, separando casos confirmados e descartados.")
        render_plotly_chart(fig_edu)
        render_interval_total(edu_df, value_col="n", by_col="grupo")
        edu_out = edu_df.drop(columns=["ordem_escolaridade", "ordem_grupo"], errors="ignore")
        copyable_dataframe(edu_out, width="stretch", hide_index=True)
        download_button(edu_out, "sinan_escolaridade_confirmados_descartados.csv")

    def render_non_sinan_education_chart() -> None:
        source_label = "SIM" if source == "SIM" else "CIHA"
        record_label = "Óbitos" if source == "SIM" else "Atendimentos/registros"
        if not education:
            if source == "SIM":
                st.info("Para gerar o gráfico de escolaridade no SIM, o campo ESC2010/ESC precisa existir e ser detectado automaticamente.")
            else:
                st.info("Para gerar o gráfico de escolaridade na CIHA, um campo de escolaridade/instrução precisa existir e ser detectado automaticamente.")
            return
        _education_age_stratification_note()
        stratify_by_age = False
        if age:
            stratify_by_age = st.checkbox(
                "Estratificar gráfico de escolaridade por faixa etária quinquenal (idade em anos)",
                value=False,
                key=f"{source.lower()}_education_age_stratify",
            )

        if stratify_by_age:
            edu_df = query_education_distribution_by_age(table, source, education, age, graph_where)
            if edu_df.empty or pd.to_numeric(edu_df.get("denominador"), errors="coerce").fillna(0).max() <= 0:
                st.info(f"Sem dados de escolaridade no {source_label} por faixa etária com os filtros atuais.")
                return
            age_band_options = _available_age_bands(edu_df)
            if not age_band_options:
                st.info("Não há faixas etárias válidas para estratificar a escolaridade com os filtros atuais.")
                return
            selected_age_band = st.selectbox(
                f"Faixa etária usada no gráfico de escolaridade do {source_label}",
                age_band_options,
                key=f"{source.lower()}_education_age_band",
            )
            plot_df = edu_df[edu_df["faixa_etaria"].astype(str).eq(str(selected_age_band))].copy()
            plot_df = add_text(plot_df)
            if _education_labels_are_fixed(source):
                categoria_order = education_category_labels(source, education, include_missing=True)
            else:
                categoria_order = plot_df.sort_values("ordem_categoria")["categoria"].drop_duplicates().tolist()
            plot_df = plot_df.sort_values("ordem_categoria").reset_index(drop=True)
            fig_edu = px.bar(
                plot_df,
                x="n",
                y="categoria",
                orientation="h",
                text="texto",
                title=f"Distribuição por escolaridade — faixa etária {selected_age_band}",
                labels={
                    "categoria": "Escolaridade",
                    "n": record_label,
                    "pct": "% da faixa etária",
                    "denominador": "Total na faixa etária",
                    "faixa_etaria": "Faixa etária",
                },
                hover_data={"texto": False, "pct": ":.2f", "denominador": True, "faixa_etaria": True},
                category_orders={"categoria": categoria_order},
                color_discrete_sequence=[PLOTLY_DEFAULT_BLUE],
            )
            disable_death_red(fig_edu)
            preserve_trace_colors(fig_edu)
            fig_edu.update_traces(marker_color=PLOTLY_DEFAULT_BLUE)
            fig_edu.update_layout(yaxis={"categoryorder": "array", "categoryarray": categoria_order[::-1]})
            st.caption(
                "Com a estratificação ativada, os percentuais usam como denominador apenas os registros da faixa etária selecionada. "
                "A tabela/exportação abaixo mantém todas as faixas etárias para auditoria."
            )
            render_plotly_chart(fig_edu)
            render_interval_total(plot_df, value_col="n")
            edu_out = edu_df.drop(columns=["ordem_categoria"], errors="ignore")
            copyable_dataframe(edu_out, width="stretch", hide_index=True)
            download_button(edu_out, f"{source.lower()}_escolaridade_por_faixa_etaria.csv")
            return

        edu_df = query_education_distribution_all_categories(table, source, education, graph_where)
        if edu_df.empty or pd.to_numeric(edu_df.get("denominador"), errors="coerce").fillna(0).max() <= 0:
            st.info(f"Sem dados de escolaridade no {source_label} com os filtros atuais.")
            return
        edu_df = add_text(edu_df)
        if _education_labels_are_fixed(source):
            categoria_order = education_category_labels(source, education, include_missing=True)
        else:
            categoria_order = edu_df.sort_values("ordem_categoria")["categoria"].drop_duplicates().tolist()
        edu_df = edu_df.sort_values("ordem_categoria").reset_index(drop=True)
        fig_edu = px.bar(
            edu_df,
            x="n",
            y="categoria",
            orientation="h",
            text="texto",
            title="Distribuição por escolaridade",
            labels={"categoria": "Escolaridade", "n": record_label, "pct": "% do total filtrado", "denominador": "Total filtrado"},
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
            category_orders={"categoria": categoria_order},
            color_discrete_sequence=[PLOTLY_DEFAULT_BLUE],
        )
        disable_death_red(fig_edu)
        preserve_trace_colors(fig_edu)
        fig_edu.update_traces(marker_color=PLOTLY_DEFAULT_BLUE)
        fig_edu.update_layout(yaxis={"categoryorder": "array", "categoryarray": categoria_order[::-1]})
        st.caption(f"O gráfico exibe a distribuição de escolaridade do {source_label}; os percentuais usam o total de registros filtrados como denominador.")
        render_plotly_chart(fig_edu)
        render_interval_total(edu_df, value_col="n")
        edu_out = edu_df.drop(columns=["ordem_categoria"], errors="ignore")
        copyable_dataframe(edu_out, width="stretch", hide_index=True)
        download_button(edu_out, f"{source.lower()}_escolaridade.csv")

    def render_sim_education_chart() -> None:
        render_non_sinan_education_chart()

    def render_ciha_education_chart() -> None:
        render_non_sinan_education_chart()

    def render_simple_category_chart(label: str, expr: str, top_n: int = 25) -> None:
        df = query_category(table, expr, graph_where, top_n=top_n)
        if df.empty:
            return
        df["denominador"] = df["n"].sum()
        df = add_text(df)
        fig = px.bar(
            df,
            x="n",
            y="categoria",
            orientation="h",
            text="texto",
            title=label,
            labels={"categoria": label, "n": "Registros", "pct": "%", "denominador": "Denominador"},
            hover_data={"texto": False, "pct": ":.2f", "denominador": True},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        render_plotly_chart(fig)
        render_interval_total(df, value_col="n")
        copyable_dataframe(df, width="stretch", hide_index=True)
        download_button(df, f"{source.lower()}_{safe_filename(label)}.csv")

    def render_municipality_charts() -> None:
        mun_expr = exprs.get("mun_event_label") or exprs.get("mun_event")
        if not mun_expr:
            return
        mun_label = "Município de ocorrência/atendimento/notificação"
        top_municipios = 15
        st.caption("Nos gráficos de município, são exibidos os 15 principais códigos IBGE; todas as demais categorias são somadas em 'Outros municípios'. Assim, percentual e denominador continuam representando 100% dos dados filtrados.")

        if source == "SINAN" and classi_code:
            mun_specs: List[Tuple[str, str, str]] = [
                ("Total", graph_where, "total"),
                ("Confirmados", append_clause(demography_case_base_where, f"{classi_code} = '1'"), "confirmados"),
                ("Descartados", append_clause(demography_case_base_where, f"{classi_code} = '2'"), "descartados"),
                (
                    "Sem classificação / ignorados",
                    append_clause(demography_case_base_where, f"({classi_code} IS NULL OR {classi_code} NOT IN ('1', '2'))"),
                    "sem_classificacao_ignorados",
                ),
            ]
        else:
            mun_specs = [("Total", graph_where, "total")]

        for group_label, where_sql, suffix in mun_specs:
            df = query_municipality_top(table, mun_expr, where_sql, top_n=top_municipios)
            if df.empty:
                continue
            df = add_text(df)
            title = f"{mun_label} — {group_label} — Top {top_municipios} + Outros municípios" if len(mun_specs) > 1 else f"{mun_label} — Top {top_municipios} + Outros municípios"
            fig = px.bar(
                df,
                x="n",
                y="categoria",
                orientation="h",
                text="texto",
                title=title,
                labels={"categoria": mun_label, "n": "Registros", "pct": "%", "denominador": "Denominador"},
                hover_data={"texto": False, "pct": ":.2f", "denominador": True},
            )
            fig.update_layout(yaxis={"categoryorder": "array", "categoryarray": df["categoria"].tolist()[::-1]})
            render_plotly_chart(fig)
            render_interval_total(df, value_col="n")
            filename = f"{source.lower()}_{safe_filename(mun_label)}_{suffix}_top{top_municipios}_outros.csv"
            download_button(df, filename)

    if not age and not sex and not race:
        st.warning("Configure idade, sexo ou raça/cor para gerar os gráficos demográficos. As categorias territoriais ainda podem ser exibidas abaixo.")

    if source == "SINAN" and classi_code:
        # SINAN: distribuição por faixa etária permanece no topo, seguida por sexo + pirâmide
        # etária (logo abaixo do gráfico de sexo), raça/cor, escolaridade e, por último, município.
        if age:
            age_selection = st.selectbox(
                "Grupo de casos para a distribuição por faixa etária",
                sinan_case_filter_options,
                key="sinan_age_distribution_case_filter",
            )
            render_age_distribution_chart(sinan_case_filter_where(age_selection), age_selection)
        else:
            st.warning("Configure idade para gerar os gráficos etários. As categorias territoriais ainda podem ser exibidas abaixo.")

        if sex or race:
            outcome_specs = []
            if sex:
                outcome_specs.append(("Sexo", sex, "sexo"))
            if race:
                outcome_specs.append(("Raça/cor", race, "raca_cor"))
            for label, expr, cat_col in outcome_specs:
                out_df = query_sinan_category_outcomes(
                    table,
                    expr,
                    classi_code,
                    outcome_demography_where,
                    category_col=cat_col,
                )
                if out_df.empty or pd.to_numeric(out_df.get("denominador"), errors="coerce").fillna(0).max() <= 0:
                    st.info(f"Sem casos confirmados, descartados ou sem classificação para calcular {label.lower()} com os filtros atuais.")
                    continue
                out_df = add_text(out_df)
                categoria_order = out_df.sort_values("ordem_categoria")[cat_col].drop_duplicates().tolist()
                out_df = out_df.sort_values(["ordem_categoria", "ordem_grupo"]).reset_index(drop=True)
                fig_outcome = px.bar(
                    out_df,
                    x="n",
                    y=cat_col,
                    color="grupo",
                    orientation="h",
                    barmode="group",
                    text="texto",
                    title=f"{label} — confirmados, descartados e sem classificação/ignorados",
                    labels={
                        cat_col: label,
                        "n": "Registros",
                        "grupo": "Grupo",
                        "pct": "% no grupo",
                        "denominador": "Total no grupo",
                    },
                    hover_data={"texto": False, "pct": ":.2f", "denominador": True},
                    category_orders={cat_col: categoria_order, "grupo": SINAN_OUTCOME_GROUP_ORDER},
                )
                fig_outcome.update_layout(yaxis={"categoryorder": "array", "categoryarray": categoria_order[::-1]})
                render_plotly_chart(fig_outcome)
                render_interval_total(out_df, value_col="n", by_col="grupo")
                out_export = out_df.drop(columns=["ordem_categoria", "ordem_grupo"], errors="ignore")
                copyable_dataframe(out_export, width="stretch", hide_index=True)
                download_button(out_export, f"sinan_{safe_filename(label)}_confirmados_descartados_sem_classificacao.csv")

                if label == "Sexo" and age and sex:
                    pyramid_selection = st.selectbox(
                        "Grupo de casos para a pirâmide etária",
                        sinan_case_filter_options,
                        key="sinan_age_pyramid_case_filter",
                    )
                    render_age_pyramid_chart(sinan_case_filter_where(pyramid_selection), pyramid_selection)

        render_sinan_education_chart()

        render_municipality_charts()

    else:
        # SIM/CIHA: sexo, depois pirâmide etária por sexo, depois raça/cor, depois distribuição
        # por faixa etária, depois escolaridade (quando disponível) e, por último, município.
        st.markdown("### Categorias demográficas e territoriais")
        if sex:
            render_simple_category_chart("Sexo", sex, top_n=25)
            render_age_pyramid_chart(graph_where, None)
        if race:
            render_simple_category_chart("Raça/cor", race, top_n=25)
        if age:
            render_age_distribution_chart(graph_where, None)

        if source in {"SIM", "CIHA"}:
            st.markdown("### Escolaridade")
            if source == "SIM":
                render_sim_education_chart()
            else:
                render_ciha_education_chart()

        render_municipality_charts()


def render_quality_tab(table: LoadedTable, source: str, base_where: str, exprs: Dict[str, Optional[str]]) -> None:
    st.markdown("### Campos importantes não preenchidos")
    st.caption(
        "Esta aba usa os filtros-base e mede, para cada campo-chave detectado, quantos registros estão sem preenchimento válido. "
        "Os gráficos mostram a porcentagem e também o número absoluto de registros não preenchidos sobre o total analisado."
    )

    fields = {
        "data": exprs.get("dt"),
        "sexo": exprs.get("sex"),
        "idade": exprs.get("age"),
        "raça/cor": exprs.get("race"),
        "município residência": exprs.get("mun_res"),
        "município ocorrência/atendimento": exprs.get("mun_event"),
        "CID meningite detectado": exprs.get("cid"),
    }
    if source == "SINAN":
        fields.update({
            "CLASSI_FIN": exprs.get("classi_code"),
            "CON_DIAGES": exprs.get("con_code"),
            "CLA_ME_BAC": exprs.get("cla_me_bac_code"),
            "CLA_ME_ASS": exprs.get("cla_me_ass_code"),
            "CLA_ME_ETI": exprs.get("cla_me_eti_code"),
            "EVOLUCAO": exprs.get("evol_code"),
            "CRITERIO": exprs.get("criterio_code"),
            "Data dos primeiros sintomas": exprs.get("dt_sin_pri"),
            "Data da punção lombar": exprs.get("dt_puncao"),
            "Realização da Punção Laboratorial": exprs.get("puncao_code"),
            "Exame Quimiocitológico do líquor (LCR)": exprs.get("quimio_code"),
            "Hemácias": exprs.get("lab_hema"),
            "Neutrófilos": exprs.get("lab_neutro"),
            "Glicose": exprs.get("lab_glico"),
            "Leucócitos": exprs.get("lab_leuco"),
            "Eosinófilos": exprs.get("lab_eosi"),
            "Proteínas": exprs.get("lab_prot"),
            "Monócitos": exprs.get("lab_mono"),
            "Linfócitos": exprs.get("lab_linfo"),
            "Cloreto": exprs.get("lab_clor"),
        })
    elif source == "CIHA":
        fields.update({"MORTE": exprs.get("morte_code"), "DIAS_PERM": exprs.get("dias_perm"), "MODALIDADE": exprs.get("modalidade_label"), "PROCEDIMENTO": exprs.get("procedimento_label")})
    elif source == "SIM":
        fields.update({"CAUSABAS CID": exprs.get("causabas_cid"), "CAUSABAS_O CID": exprs.get("causabas_o_cid")})

    def fmt_int(value: object) -> str:
        if pd.isna(value):
            return "0"
        return f"{int(value):,}".replace(",", ".")

    def fmt_pct(value: object) -> str:
        if pd.isna(value):
            return "—"
        return f"{float(value):.2f}%".replace(".", ",")

    def add_missing_text(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["texto"] = out.apply(
            lambda r: f"{fmt_int(r['faltantes'])} de {fmt_int(r['total'])} ({fmt_pct(r['pct_faltante'])})",
            axis=1,
        )
        return out

    miss = query_missingness(table, fields, exprs.get("dt"), base_where)
    if miss.empty:
        st.info("Sem campos detectados automaticamente para avaliar preenchimento.")
    else:
        miss = add_missing_text(miss)
        fig = px.bar(
            miss,
            x="pct_faltante",
            y="campo",
            orientation="h",
            text="texto",
            title="Campos importantes não preenchidos — percentual e número absoluto",
            labels={
                "campo": "Campo",
                "pct_faltante": "% não preenchido",
                "faltantes": "Registros não preenchidos",
                "total": "Total analisado",
            },
            hover_data={"texto": False, "faltantes": True, "total": True, "pct_faltante": ":.2f"},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        fig.update_traces(textposition="outside", cliponaxis=False)
        render_plotly_chart(fig)
        st.caption("Total no intervalo filtrado: " + format_int_br(pd.to_numeric(miss["total"], errors="coerce").max()) + " registros analisados; faltantes são contados por campo.")
        copyable_dataframe(miss[["campo", "faltantes", "total", "pct_faltante", "texto"]], width="stretch", hide_index=True)
        download_button(miss.drop(columns=["texto"], errors="ignore"), f"{source.lower()}_campos_importantes_nao_preenchidos.csv")

    by_year = query_missingness_by_year(table, fields, exprs.get("dt"), base_where)
    if not by_year.empty:
        by_year = add_missing_text(by_year)
        focus_fields = st.multiselect(
            "Campos para visualizar por ano",
            sorted(by_year["campo"].unique()),
            default=sorted(by_year["campo"].unique())[:5],
            key=f"miss_fields_{source}",
        )
        filtered = by_year[by_year["campo"].isin(focus_fields)] if focus_fields else by_year
        fig = px.line(
            filtered,
            x="ano",
            y="pct_faltante",
            color="campo",
            markers=True,
            text="texto",
            title="Campos importantes não preenchidos por ano — percentual e número absoluto",
            labels={
                "ano": "Ano",
                "pct_faltante": "% não preenchido",
                "campo": "Campo",
                "faltantes": "Registros não preenchidos",
                "total": "Total analisado",
            },
            hover_data={"texto": False, "faltantes": True, "total": True, "pct_faltante": ":.2f"},
        )
        fig.update_traces(textposition="top center")
        render_plotly_chart(fig)
        render_interval_total(filtered, value_col="faltantes", by_col="campo", value_label="registros não preenchidos")
        copyable_dataframe(filtered[["ano", "campo", "faltantes", "total", "pct_faltante", "texto"]], width="stretch", hide_index=True)
        download_button(by_year.drop(columns=["texto"], errors="ignore"), f"{source.lower()}_campos_importantes_nao_preenchidos_por_ano.csv")

def render_sql_lab(table: LoadedTable, source: str) -> None:
    st.markdown("### Laboratório SQL")
    st.caption("Use `{{tabela}}` como placeholder para a tabela carregada. O app substituirá pelo nome/referência SQL correta.")
    example = """
    SELECT COUNT(*) AS registros
    FROM {tabela};
    """
    if source == "SINAN":
        example = """
        SELECT
          EXTRACT(YEAR FROM DT_SIN_PRI) AS ano,
          CLASSI_FIN,
          CON_DIAGES,
          EVOLUCAO,
          COUNT(*) AS n
        FROM {tabela}
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2, 3, 4;
        """
    elif source == "SIM":
        example = """
        SELECT
          SUBSTR(DTOBITO, 1, 4) AS ano,
          CAUSABAS,
          COUNT(*) AS n
        FROM {tabela}
        GROUP BY 1, 2
        ORDER BY 1, n DESC;
        """
    elif source == "CIHA":
        example = """
        SELECT
          ANO_CMPT AS ano,
          DIAG_PRINC,
          MORTE,
          COUNT(*) AS n
        FROM {tabela}
        GROUP BY 1, 2, 3
        ORDER BY 1, n DESC;
        """
    sql_text = st.text_area("SQL", value=textwrap.dedent(example).strip(), height=220, key=f"sql_lab_{source}")
    sql_limit = perf_int("perf_sql_lab_row_limit", DEFAULT_SQL_LAB_ROW_LIMIT)
    st.caption(f"O resultado do SQL Lab será limitado a {sql_limit:,} linhas.".replace(",", "."))
    if st.button("Executar SQL", key=f"run_sql_{source}"):
        sql = sql_text.replace("{tabela}", table.ref_sql).replace("{{tabela}}", table.ref_sql)
        sql_clean = sql.strip().rstrip(";")
        if not re.match(r"^(SELECT|WITH)\b", sql_clean, flags=re.IGNORECASE):
            st.error("Por segurança e desempenho, o SQL Lab aceita apenas consultas SELECT/WITH.")
            return
        try:
            limited_sql = f"SELECT * FROM ({sql_clean}) AS _sql_lab_result LIMIT {int(sql_limit)}"
            df = run_query(table, limited_sql, cache=False)
            copyable_dataframe(df, width="stretch", hide_index=True)
            download_button(df, f"{source.lower()}_sql_lab.csv", "Baixar resultado", max_rows=sql_limit)
        except Exception as exc:
            st.error(f"Erro ao executar SQL: {exc}")


def render_source(source: str) -> Optional[Dict[str, object]]:
    table = render_loader(source)
    if table is None:
        return None
    try:
        schema = schema_df(table)
    except Exception as exc:
        st.error(f"Não foi possível ler o schema: {exc}")
        return None
    columns = schema["coluna"].astype(str).tolist()

    st.success(f"Dados carregados: {table.label}")

    sel = render_column_config(source, columns)
    exprs = build_expressions(source, sel)
    base_where, graph_where, definition = render_filters(source, table, exprs)
    render_kpis(table, source, base_where, graph_where, exprs)

    analysis_sections = [
        "Principais indicadores epidemiológicos",
        "Temporal",
        "Análise epidemiológica e CID-10",
        "Demografia e território",
    ]
    if source == "SINAN":
        analysis_sections.append("Sobreposição NU_NOTIFIC / NM_PACIENT")
    analysis_sections.extend([
        "Campos importantes não preenchidos",
        "Prévia",
        "SQL Lab",
    ])
    analysis_section_key = f"analysis_section_{source}"
    if st.session_state.get(analysis_section_key) not in (None, *analysis_sections):
        st.session_state.pop(analysis_section_key, None)
    selected_section = st.radio(
        "Área de análise",
        analysis_sections,
        horizontal=True,
        key=analysis_section_key,
        help="Somente a área selecionada é calculada nesta execução para reduzir memória e tempo de rerun.",
    )

    if selected_section == "Principais indicadores epidemiológicos":
        render_indicators_tab(table, source, base_where, graph_where, exprs)
    elif selected_section == "Temporal":
        render_temporal_tab(table, source, graph_where, exprs)
    elif selected_section == "Análise epidemiológica e CID-10":
        render_cid_tab(table, source, graph_where, exprs, base_where=base_where)
    elif selected_section == "Demografia e território":
        render_demography_tab(table, source, graph_where, exprs, base_where=base_where)
    elif selected_section == "Sobreposição NU_NOTIFIC / NM_PACIENT" and source == "SINAN":
        render_sinan_overlap_tab(table, base_where, exprs)
    elif selected_section == "Campos importantes não preenchidos":
        render_quality_tab(table, source, base_where, exprs)
    elif selected_section == "Prévia":
        st.markdown("### Prévia enriquecida")
        total_preview = count_rows(table, graph_where)
        max_preview_rows = max(50, perf_int("perf_max_preview_rows", DEFAULT_MAX_PREVIEW_ROWS))
        default_preview = min(DEFAULT_PREVIEW_ROW_LIMIT, max_preview_rows)
        page_size = st.slider(
            "Linhas por página",
            50,
            int(max_preview_rows),
            int(default_preview),
            step=50,
            key=f"preview_limit_{source}",
        )
        max_page = max(1, int(np.ceil(total_preview / page_size))) if page_size else 1
        page = st.number_input("Página", min_value=1, max_value=max_page, value=1, step=1, key=f"preview_page_{source}")
        offset = (int(page) - 1) * int(page_size)
        st.caption(
            f"Exibindo página {int(page):,} de {max_page:,}; total filtrado: {total_preview:,} registros."
            .replace(",", ".")
        )
        try:
            df_prev = query_enriched_preview(table, sel, exprs, graph_where, int(page_size), offset=offset)
            copyable_dataframe(df_prev, width="stretch")
            download_button(df_prev, f"{source.lower()}_previa_enriquecida_pagina_{int(page)}.csv", max_rows=int(page_size))
        except Exception as exc:
            st.error(f"Erro ao montar prévia: {exc}")

        st.markdown("### Exportação completa dos casos filtrados")
        full_export_limit = perf_int("perf_full_export_row_limit", DEFAULT_FULL_EXPORT_ROW_LIMIT)
        if total_preview > full_export_limit:
            st.warning(
                f"Exportação completa bloqueada: {total_preview:,} registros excedem o limite atual de {full_export_limit:,}. "
                "Aplique filtros adicionais ou aumente o limite em Desempenho e memória se o ambiente suportar."
                .replace(",", ".")
            )
        else:
            st.caption(
                "A exportação completa é habilitada somente quando o total filtrado está dentro do limite defensivo configurado."
            )
            if st.button("Gerar CSV completo dos casos filtrados", key=f"full_export_{source}"):
                try:
                    df_full = query_enriched_preview(table, sel, exprs, graph_where, limit=None)
                    st.success(f"Exportação preparada com {len(df_full):,} linhas.".replace(",", "."))
                    download_button(
                        df_full,
                        f"{source.lower()}_casos_filtrados_completos.csv",
                        "Baixar CSV completo",
                        max_rows=max(1, len(df_full)),
                    )
                except Exception as exc:
                    st.error(f"Erro ao gerar exportação completa: {exc}")
    elif selected_section == "SQL Lab":
        render_sql_lab(table, source)

    context = {"source": source, "table": table, "sel": sel, "exprs": exprs, "base_where": base_where, "graph_where": graph_where, "definition": definition}
    st.session_state[f"loaded_context_{source}"] = context
    return context


def render_comparison(loaded: Sequence[Dict[str, object]]) -> None:
    st.markdown("### Comparação entre bancos de dados (sob revisão no momento)")
    available = [x for x in loaded if x and x.get("exprs", {}).get("dt")]
    if len(available) < 2:
        st.info("Carregue ao menos duas bases com data detectada para comparar séries.")
        return
    source_names = [x["source"] for x in available]
    chosen = st.multiselect("Bases", source_names, default=source_names, key="comp_sources")
    freq_label = st.selectbox("Agregação", ["Ano", "Mês", "Semana"], index=1, key="comp_freq")
    freq = {"Ano": "year", "Mês": "month", "Semana": "week"}[freq_label]
    normalize = st.checkbox("Normalizar em índice 100 no primeiro período não-zero", value=False, key="comp_norm")
    stratify_cid = st.checkbox("Estratificar por tipo CID-10 quando disponível", value=False, key="comp_cid")
    st.caption("Na comparação, o SINAN entra sempre como casos confirmados (CLASSI_FIN = 1), independentemente da definição exploratória escolhida na aba SINAN. Quando há estratificação por CID-10, o SINAN usa a conversão de CON_DIAGES; SIM/CIHA usam os mesmos CID-10 adequados prefixados do gráfico de conversão: os códigos convertidos somam no destino e os CID-10 prefixados já presentes permanecem em seu próprio grupo. Na agregação mensal, meses sem registros são mantidos com valor zero.")

    frames = []
    comparison_conversion_notes: List[str] = []
    for item in available:
        source_name = item["source"]
        if source_name not in chosen:
            continue
        table: LoadedTable = item["table"]
        exprs = item["exprs"]
        if stratify_cid:
            if source_name == "SINAN" and exprs.get("sinan_cid10_conversion_type"):
                cat = exprs.get("sinan_cid10_conversion_type")
            elif source_name in {"SIM", "CIHA"} and exprs.get("cid10_adequacy_plot_label"):
                cat = exprs.get("cid10_adequacy_plot_label")
            else:
                cat = exprs.get("cid_type")
        else:
            cat = None
        series_where = item["graph_where"]
        series_label = item.get("definition", "")
        if source_name == "SINAN":
            classi = exprs.get("classi_code")
            if not classi:
                st.warning("SINAN foi ignorado na comparação porque CLASSI_FIN não foi detectado automaticamente; não é possível isolar confirmados.")
                continue
            series_where = append_clause(item["base_where"], f"{classi} = '1'")
            series_label = "Confirmados (CLASSI_FIN = 1)"
        try:
            ts = query_timeseries(table, exprs["dt"], series_where, freq, cat)
        except Exception as exc:
            st.warning(f"Falha na série de {source_name}: {exc}")
            continue
        if stratify_cid and source_name in {"SIM", "CIHA"} and exprs.get("cid10_adequacy_plot_label"):
            try:
                conv_note_df = query_cid10_adequacy_conversion(table, exprs, series_where)
                if not conv_note_df.empty:
                    comparison_conversion_notes.append(f"{source_name}: {build_cid10_adequacy_conversion_note(conv_note_df)}")
            except Exception as exc:
                comparison_conversion_notes.append(f"{source_name}: não foi possível calcular a observação de conversão ({exc}).")
        if ts.empty:
            continue
        if cat:
            ts["serie"] = source_name + " — " + series_label + " — " + ts["categoria"].astype(str)
        else:
            ts["serie"] = source_name + " — " + series_label
        ts = ts.rename(columns={"n": "valor"})
        frames.append(ts[["periodo", "serie", "valor"]])
    if not frames:
        st.warning("Nenhuma série gerada.")
        return
    comp = pd.concat(frames, ignore_index=True)
    comp["periodo"] = pd.to_datetime(comp["periodo"])

    if freq == "month" and not comp.empty:
        comp["periodo"] = comp["periodo"].dt.to_period("M").dt.to_timestamp()
        full_months = pd.date_range(comp["periodo"].min(), comp["periodo"].max(), freq="MS")
        series_values = comp["serie"].dropna().unique().tolist()
        full_index = pd.MultiIndex.from_product([full_months, series_values], names=["periodo", "serie"])
        comp = (
            comp.groupby(["periodo", "serie"], as_index=False)["valor"].sum()
            .set_index(["periodo", "serie"])
            .reindex(full_index, fill_value=0)
            .reset_index()
        )

    if normalize:
        comp = comp.sort_values("periodo")
        for s in comp["serie"].unique():
            idx = comp["serie"].eq(s)
            nonzero = comp.loc[idx & comp["valor"].gt(0), "valor"]
            if not nonzero.empty:
                comp.loc[idx, "valor"] = comp.loc[idx, "valor"] / nonzero.iloc[0] * 100

    fig = px.line(comp, x="periodo", y="valor", color="serie", markers=True, title="Comparação entre bancos de dados — tendências", labels={"valor": "Índice" if normalize else "Registros", "periodo": "Período", "serie": "Série"})
    render_plotly_chart(fig)
    if not normalize:
        render_interval_total(comp, value_col="valor", by_col="serie")
    if comparison_conversion_notes:
        st.caption("Observação da conversão usada na comparação estratificada: " + " ".join(comparison_conversion_notes))
    copyable_dataframe(comp, width="stretch", hide_index=True)
    download_button(comp, "comparacao_series_bases.csv")

    st.markdown("**Cuidados de leitura**")
    st.write(
        "SINAN mede notificações/investigações; SIM mede óbitos; CIHA mede utilização de serviços. "
        "Compare tendências, composição e concordância agregada, mas evite interpretar contagens brutas entre bases como o mesmo fenômeno sem linkage e denominadores populacionais."
    )


def render_methodology():
    st.divider()
    st.markdown("### Como usar este app para investigação epidemiológica")
    st.markdown(
        """
        1. Comece pela aba **Principais indicadores epidemiológicos** do SINAN para separar total de notificações, confirmados, descartados e sem classificação/ignorados.
        2. Use **Análise epidemiológica e CID-10** para comparar o CID bruto com a classificação específica. No SINAN, dê prioridade a `CON_DIAGES`, `CLA_ME_BAC`, `CLA_ME_ASS` e `CLA_ME_ETI`.
        3. Use **Temporal** para verificar queda, recuperação e sazonalidade.
        4. Use **Demografia e território** para levantar hipóteses por idade, sexo, residência e atendimento.
        5. Use **Prévia** para inspecionar casos filtrados e exportar a planilha completa quando necessário.
        6. Use **SQL Lab** para transformar a hipótese em uma consulta reprodutível.
        """
    )
    st.info(
        "Adendo de atualização: espere a consolidação dos bancos antes de interpretar anos mais recentes como definitivos. "
        "SINAN pode mudar após investigação/encerramento do caso; SIM pode mudar após codificação e qualificação da causa básica; CIHA pode sofrer recomposição por competência e processamento administrativo."
    )
    st.markdown("### Referência CID-10")
    render_cid_reference()
    render_quimio_interpretation()


def render_main_navigation() -> str:
    """Navegação principal com separadores visuais entre metodologia, bases e comparação."""
    main_sections = ["Metodologia", "SINAN", "SIM", "CIHA", "Comparação entre bancos de dados (sob revisão no momento)"]
    main_section_key = "main_section"
    current = st.session_state.get(main_section_key, "Metodologia")
    if current not in main_sections:
        current = "Metodologia"
    st.session_state[main_section_key] = current

    def nav_button(label: str, key: str) -> None:
        if st.button(
            label,
            key=key,
            type="primary" if st.session_state[main_section_key] == label else "secondary",
            width="stretch",
        ):
            st.session_state[main_section_key] = label

    st.markdown("#### Metodologia")
    nav_button("Metodologia", "nav_metodologia")
    st.divider()
    st.markdown("#### Bases")
    for source_name in ["SINAN", "SIM", "CIHA"]:
        nav_button(source_name, f"nav_{source_name.lower()}")
    st.divider()
    st.markdown("#### Comparação entre bancos de dados (sob revisão no momento)")
    nav_button("Comparação entre bancos de dados (sob revisão no momento)", "nav_comparacao_bancos")
    return st.session_state[main_section_key]


def main() -> None:
    render_app_css()
    st.title("Painel epidemiológico de meningite — SINAN, SIM e CIHA")
    st.caption(f"Versão {APP_VERSION}. Lê upload de DuckDB, upload de Parquet ou bancos hospedados no github em Parquet e mantém regras analíticas explícitas.")

    with st.sidebar:
        render_performance_controls()
        section = render_main_navigation()

    if section in {"SINAN", "SIM", "CIHA"}:
        st.divider()
        render_source(section)
    elif section == "Metodologia":
        render_methodology()
    elif section == "Comparação entre bancos de dados (sob revisão no momento)":
        loaded = [
            st.session_state.get(f"loaded_context_{src}")
            for src in ["SINAN", "SIM", "CIHA"]
            if st.session_state.get(f"loaded_context_{src}")
        ]
        st.caption(
            "A Comparação entre bancos de dados usa as bases já carregadas nas seções SINAN/SIM/CIHA. "
            "Carregue cada base separadamente antes de comparar para evitar sobrecarga."
        )
        render_comparison([x for x in loaded if x])
    st.divider()


if __name__ == "__main__":
    main()
