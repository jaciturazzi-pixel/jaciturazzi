import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import os
import sys

# Permite importar módulos do projecto quando executado como script standalone pelo Streamlit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.classifiers import classify_essential
from src.normalizer import enrich_dataframe

# Configuração da Página
st.set_page_config(
    page_title="Supermarket Analytics | Datadog Pessoal",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilo CSS personalizado
st.markdown("""
<style>
    .metric-card {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 15px;
        border-left: 5px solid #1f77b4;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 45px;
        white-space: pre-wrap;
        border-radius: 4px 4px 0px 0px;
        padding-top: 10px;
        padding-bottom: 10px;
    }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=60)
def load_data(db_path: str = "data/faturas.db"):
    if not os.path.exists(db_path):
        return pd.DataFrame()
        
    conn = sqlite3.connect(db_path)
    query = """
        SELECT 
            i.invoice_date as Data,
            i.store as Loja,
            i.invoice_number as Fatura,
            l.category as Categoria,
            l.description as Produto,
            l.quantity as Quantidade,
            l.unit as Unidade,
            l.unit_price as Preco_Unitario,
            l.total_price as Total,
            l.discount as Desconto
        FROM line_items l
        JOIN invoices i ON l.invoice_id = i.id
        ORDER BY i.invoice_date DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if df.empty:
        return df

    df['Data'] = pd.to_datetime(df['Data'])
    df['Mês'] = df['Data'].dt.to_period('M').astype(str)
    
    # Semana ISO: ex: 2026-W34
    df['Ano_Semana'] = df['Data'].dt.strftime('%G-W%V')
    # Início da semana (Segunda-feira)
    df['Semana_Inicio'] = df['Data'].apply(lambda d: d - pd.Timedelta(days=d.weekday()))
    df['Semana_Label'] = df['Semana_Inicio'].dt.strftime('%d/%m') + " a " + (df['Semana_Inicio'] + pd.Timedelta(days=6)).dt.strftime('%d/%m')
    df['Semana_Completa'] = df['Ano_Semana'] + " (" + df['Semana_Label'] + ")"

    for col in ['Quantidade', 'Preco_Unitario', 'Total', 'Desconto']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        
    # Classificação Cesta Básica (usando função unificada de src.classifiers)
    df['Subgrupo_Cesta'] = df.apply(classify_essential, axis=1)
    df['Tipo_Gasto'] = df['Subgrupo_Cesta'].apply(lambda x: 'Cesta Básica (Essencial)' if pd.notnull(x) else 'Outros / Extras')

    # Normalização de medidas (Preço por KG, Litro, etc.)
    df = enrich_dataframe(df)
    return df

df_raw = load_data()

if df_raw.empty:
    st.error("Nenhum dado encontrado na base de dados `data/faturas.db`. Por favor corra o comando `fetch` e `process` primeiro.")
    st.stop()

# ==============================================================================
# BARRA LATERAL: FILTROS DINÂMICOS
# ==============================================================================
st.sidebar.title("🎛️ Painel de Filtros")

min_date = df_raw['Data'].min().date()
max_date = df_raw['Data'].max().date()

date_range = st.sidebar.date_input(
    "🗓️ Intervalo de Datas",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date
)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_filter, end_filter = date_range
else:
    start_filter, end_filter = min_date, max_date

# Filtro por Loja
lojas_disponiveis = ["Todas"] + sorted(df_raw['Loja'].unique().tolist())
loja_selecionada = st.sidebar.selectbox("🏪 Loja", lojas_disponiveis)

# Filtro de Cesta Básica
tipo_gasto_filtro = st.sidebar.radio(
    "🛒 Filtro de Tipo de Gasto",
    ["Todos os Produtos", "Só Cesta Básica (Essenciais)", "Só Outros / Extras"]
)

# Granularidade Temporal
resolucao = st.sidebar.selectbox(
    "⏱️ Resolução Temporal",
    ["Semanal (Recomendado)", "Mensal", "Diário"]
)

# Botão de refresh
if st.sidebar.button("🔄 Atualizar Dados"):
    st.cache_data.clear()
    st.rerun()

# Aplicação dos Filtros
df = df_raw[(df_raw['Data'].dt.date >= start_filter) & (df_raw['Data'].dt.date <= end_filter)].copy()

if loja_selecionada != "Todas":
    df = df[df['Loja'] == loja_selecionada]

if tipo_gasto_filtro == "Só Cesta Básica (Essenciais)":
    df = df[df['Tipo_Gasto'] == 'Cesta Básica (Essencial)']
elif tipo_gasto_filtro == "Só Outros / Extras":
    df = df[df['Tipo_Gasto'] == 'Outros / Extras']

if df.empty:
    st.warning("Nenhum gasto encontrado para os filtros selecionados.")
    st.stop()

# ==============================================================================
# CABEÇALHO & KPIS EXECUTIVOS
# ==============================================================================
st.title("🛒 Dashboard de Inteligência de Mercado")
st.caption(f"Analisando de **{start_filter.strftime('%d/%m/%Y')}** até **{end_filter.strftime('%d/%m/%Y')}** | Loja: **{loja_selecionada}**")

total_gasto = df['Total'].sum()
total_essencial = df[df['Tipo_Gasto'] == 'Cesta Básica (Essencial)']['Total'].sum()
pct_essencial = (total_essencial / total_gasto * 100) if total_gasto > 0 else 0

num_semanas = max(df['Ano_Semana'].nunique(), 1)
media_semanal = total_gasto / num_semanas
num_faturas = df['Fatura'].nunique()
num_itens = len(df)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("💰 Total Gasto", f"€ {total_gasto:,.2f}")
col2.metric("📅 Média por Semana", f"€ {media_semanal:,.2f}")
col3.metric("🧺 Cesta Básica", f"€ {total_essencial:,.2f}", f"{pct_essencial:.1f}% do total")
col4.metric("🧾 Faturas", f"{num_faturas}")
col5.metric("📦 Itens Comprados", f"{num_itens:,}")

st.divider()

# ==============================================================================
# ABAS PRINCIPAIS
# ==============================================================================
tab_tempo, tab_cesta, tab_busca, tab_comparador, tab_viloes, tab_dados = st.tabs([
    "📈 Evolução & Semanas",
    "🧺 Cesta Básica vs Extras",
    "🔍 Pesquisa de Produtos & Preço por KG/L",
    "⚖️ Comparador Continente vs Pingo Doce & Inflação",
    "🚨 Vilões do Orçamento",
    "📄 Detalhes das Faturas"
])

# ------------------------------------------------------------------------------
# TAB 1: EVOLUÇÃO TEMPORAL (SEMANAL / MENSAL / DIÁRIO)
# ------------------------------------------------------------------------------
with tab_tempo:
    st.subheader("Evolução Temporal dos Gastos")
    
    if resolucao == "Semanal (Recomendado)":
        time_col = 'Semana_Completa'
        chart_title = "Gastos Semana a Semana (€)"
        df_time = df.groupby(['Semana_Inicio', 'Semana_Completa', 'Loja'])['Total'].sum().reset_index()
        df_time = df_time.sort_values('Semana_Inicio')
        fig_time = px.bar(
            df_time, 
            x='Semana_Completa', 
            y='Total', 
            color='Loja',
            title=chart_title,
            labels={'Total': 'Total Gasto (€)', 'Semana_Completa': 'Semana'},
            text_auto='.2f'
        )
    elif resolucao == "Mensal":
        time_col = 'Mês'
        chart_title = "Gastos Mês a Mês (€)"
        df_time = df.groupby(['Mês', 'Loja'])['Total'].sum().reset_index().sort_values('Mês')
        fig_time = px.bar(
            df_time, 
            x='Mês', 
            y='Total', 
            color='Loja',
            title=chart_title,
            labels={'Total': 'Total Gasto (€)', 'Mês': 'Mês'},
            text_auto='.2f'
        )
    else:
        chart_title = "Gastos Diários (€)"
        df_time = df.groupby(['Data', 'Loja'])['Total'].sum().reset_index().sort_values('Data')
        fig_time = px.bar(
            df_time, 
            x='Data', 
            y='Total', 
            color='Loja',
            title=chart_title,
            labels={'Total': 'Total Gasto (€)', 'Data': 'Dia'}
        )

    fig_time.update_layout(hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fig_time, use_container_width=True)

    st.markdown("### 🏆 Ranking das Semanas com Mais Gastos")
    df_rank_semanas = df.groupby(['Ano_Semana', 'Semana_Label']).agg(
        Total_Gasto=('Total', 'sum'),
        Compras=('Fatura', 'nunique'),
        Produtos_Comprados=('Quantidade', 'sum')
    ).reset_index().sort_values('Total_Gasto', ascending=False)
    
    df_rank_semanas['Média_por_Compra'] = (df_rank_semanas['Total_Gasto'] / df_rank_semanas['Compras']).round(2)
    df_rank_semanas['Total_Gasto'] = df_rank_semanas['Total_Gasto'].apply(lambda x: f"€ {x:,.2f}")
    df_rank_semanas['Média_por_Compra'] = df_rank_semanas['Média_por_Compra'].apply(lambda x: f"€ {x:,.2f}")
    df_rank_semanas['Produtos_Comprados'] = df_rank_semanas['Produtos_Comprados'].round(1)

    st.dataframe(df_rank_semanas, use_container_width=True)

# ------------------------------------------------------------------------------
# TAB 2: CESTA BÁSICA VS EXTRAS
# ------------------------------------------------------------------------------
with tab_cesta:
    st.subheader("Análise da Cesta Básica (Essenciais)")
    c1, c2 = st.columns([1, 1])
    
    with c1:
        # Donut Chart: Essenciais vs Outros
        split_df = df.groupby('Tipo_Gasto')['Total'].sum().reset_index()
        fig_donut = px.pie(
            split_df, 
            names='Tipo_Gasto', 
            values='Total', 
            hole=0.45,
            title="Divisão do Orçamento",
            color_discrete_sequence=['#2ca02c', '#ff7f0e']
        )
        fig_donut.update_traces(textposition='inside', textinfo='percent+label')
        st.plotly_chart(fig_donut, use_container_width=True)
        
    with c2:
        # Bar Chart: Subgrupos da Cesta Básica
        df_sub = df[df['Subgrupo_Cesta'].notnull()].groupby('Subgrupo_Cesta')['Total'].sum().reset_index()
        df_sub = df_sub.sort_values('Total', ascending=True)
        fig_sub = px.bar(
            df_sub, 
            x='Total', 
            y='Subgrupo_Cesta', 
            orientation='h',
            title="Gastos por Subgrupo Essencial (€)",
            labels={'Total': 'Total Gasto (€)', 'Subgrupo_Cesta': 'Subgrupo'},
            text_auto='.2f',
            color='Total',
            color_continuous_scale='Greens'
        )
        st.plotly_chart(fig_sub, use_container_width=True)

    st.markdown("### 📋 Top Produtos da Cesta Básica no Período")
    df_top_cesta = df[df['Subgrupo_Cesta'].notnull()].groupby(['Produto', 'Subgrupo_Cesta']).agg(
        Vezes=('Produto', 'count'),
        Qtd_Total=('Quantidade', 'sum'),
        Gasto_Total=('Total', 'sum')
    ).reset_index().sort_values('Gasto_Total', ascending=False).head(30)
    
    df_top_cesta['% da Cesta'] = ((df_top_cesta['Gasto_Total'] / total_essencial) * 100).round(1).astype(str) + "%"
    df_top_cesta['Gasto_Total'] = df_top_cesta['Gasto_Total'].apply(lambda x: f"€ {x:,.2f}")
    df_top_cesta['Qtd_Total'] = df_top_cesta['Qtd_Total'].round(2)
    st.dataframe(df_top_cesta, use_container_width=True)

# ------------------------------------------------------------------------------
# TAB 3: BUSCA & EXPLORADOR DE PRODUTOS ESPECÍFICOS (AÇAÍ, DODOT, ETC.)
# ------------------------------------------------------------------------------
with tab_busca:
    st.subheader("🔍 Investigador de Produtos & Preço por KG / Litro")
    st.caption("Pesquise qualquer produto para ver o consumo semanal, histórico de preços normalizados (€/kg ou €/L) e comparação entre lojas.")
    
    # Pré-preencher a partir de uma seleção feita noutra aba (ex: Vilões)
    default_busca = st.session_state.get('produto_selecionado', '')
    busca = st.text_input(
        "Nome do Produto / Marca (ex: cornet, fralda, azeite, queijo, cerveja, frango):",
        value=default_busca,
        key="busca_input"
    )
    
    if busca.strip():
        termo = busca.strip().lower()
        df_match = df_raw[
            df_raw['Produto_Limpo'].str.lower().str.contains(termo, na=False) |
            df_raw['Produto'].str.lower().str.contains(termo, na=False)
        ].copy()
        
        if df_match.empty:
            st.info(f"Nenhum produto encontrado com o termo '{busca}'.")
        else:
            total_termo = df_match['Total'].sum()
            qtd_termo = df_match['Quantidade'].sum()
            vezes_termo = len(df_match)
            lojas_no_match = df_match['Loja'].unique().tolist()
            n_semanas = max(df_match['Ano_Semana'].nunique(), 1)
            media_semanal_termo = total_termo / n_semanas
            media_qtd_semanal = qtd_termo / n_semanas

            b1, b2, b3, b4, b5 = st.columns(5)
            b1.metric(f"💰 Gasto Total", f"€ {total_termo:,.2f}")
            b2.metric("📦 Qtd Total", f"{qtd_termo:,.2f}")
            b3.metric("📅 Média Semanal (€)", f"€ {media_semanal_termo:,.2f}")
            b4.metric("📦 Média Semanal (qtd)", f"{media_qtd_semanal:,.2f}")
            b5.metric("🏪 Loja Mais Frequente", f"{df_match['Loja'].mode()[0]}")

            # Comparação direta entre lojas se o produto foi comprado em ambas
            if len(lojas_no_match) > 1 and 'Preco_Base_Norm' in df_match.columns:
                unit_predominante = df_match['Unidade_Norm'].mode()[0]
                df_unit = df_match[df_match['Unidade_Norm'] == unit_predominante]
                
                if not df_unit.empty and len(df_unit['Loja'].unique()) > 1:
                    preco_cont = df_unit[df_unit['Loja'] == 'Continente']['Preco_Base_Norm'].mean()
                    preco_pd = df_unit[df_unit['Loja'] == 'Pingo Doce']['Preco_Base_Norm'].mean()
                    
                    st.markdown(f"##### ⚖️ Comparação Direta de Preço Médio (Base: €/{unit_predominante})")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Continente (Média)", f"€ {preco_cont:.2f}/{unit_predominante}" if pd.notnull(preco_cont) else "Sem dados")
                    c2.metric("Pingo Doce (Média)", f"€ {preco_pd:.2f}/{unit_predominante}" if pd.notnull(preco_pd) else "Sem dados")
                    
                    if pd.notnull(preco_cont) and pd.notnull(preco_pd) and preco_cont > 0 and preco_pd > 0:
                        diff_pct = ((preco_pd - preco_cont) / preco_cont) * 100
                        if diff_pct < 0:
                            c3.metric("Veredito", "Pingo Doce mais barato", f"{abs(diff_pct):.1f}% mais económico")
                        elif diff_pct > 0:
                            c3.metric("Veredito", "Continente mais barato", f"{abs(diff_pct):.1f}% mais económico")
                        else:
                            c3.metric("Veredito", "Preços equivalentes", "0.0%")

            st.divider()

            # ------------------------------------------------------------------
            # VISÃO SEMANAL: quantos comprei e quanto gastei por semana
            # ------------------------------------------------------------------
            st.markdown("##### 📅 Consumo Semanal — Quantidade & Gasto")

            df_semanal = (
                df_match
                .groupby(['Semana_Inicio', 'Semana_Completa', 'Loja'], as_index=False)
                .agg(Qtd_Semana=('Quantidade', 'sum'), Gasto_Semana=('Total', 'sum'), Compras=('Total', 'count'))
                .sort_values('Semana_Inicio')
            )

            col_qtd, col_gasto = st.columns(2)

            with col_qtd:
                fig_qtd = px.bar(
                    df_semanal,
                    x='Semana_Completa',
                    y='Qtd_Semana',
                    color='Loja',
                    title=f"Quantidade por Semana — '{busca}'",
                    labels={'Qtd_Semana': 'Quantidade', 'Semana_Completa': 'Semana'},
                    text_auto='.2f'
                )
                fig_qtd.update_layout(height=380, xaxis_tickangle=-40)
                st.plotly_chart(fig_qtd, use_container_width=True)

            with col_gasto:
                fig_gasto = px.bar(
                    df_semanal,
                    x='Semana_Completa',
                    y='Gasto_Semana',
                    color='Loja',
                    title=f"Gasto por Semana (€) — '{busca}'",
                    labels={'Gasto_Semana': 'Gasto (€)', 'Semana_Completa': 'Semana'},
                    text_auto='.2f'
                )
                fig_gasto.update_layout(height=380, xaxis_tickangle=-40)
                st.plotly_chart(fig_gasto, use_container_width=True)

            # Tabela semanal resumida
            tabela_semanal = (
                df_match
                .groupby(['Semana_Completa'], as_index=False)
                .agg(
                    Qtd_Total=('Quantidade', 'sum'),
                    Gasto_Total=('Total', 'sum'),
                    Nº_Compras=('Total', 'count'),
                    Lojas=('Loja', lambda x: ' & '.join(sorted(x.unique())))
                )
                .sort_values('Semana_Completa', ascending=False)
                .rename(columns={'Semana_Completa': 'Semana'})
            )
            st.dataframe(
                tabela_semanal.style.format({'Qtd_Total': '{:.2f}', 'Gasto_Total': '€ {:.2f}'}),
                use_container_width=True,
                hide_index=True
            )

            st.divider()

            # Gráfico de evolução de preço base
            st.markdown("##### 📈 Evolução de Preço Base (€/KG, €/L ou €/UN)")
            fig_preco = px.scatter(
                df_match, 
                x='Data', 
                y='Preco_Base_Norm', 
                size='Total', 
                color='Loja',
                hover_data=['Produto_Limpo', 'Quantidade', 'Unidade_Norm', 'Preco_Unitario', 'Total', 'Fatura'],
                labels={'Preco_Base_Norm': 'Preço Base (€/KG, €/L ou €/UN)', 'Data': 'Data da Compra'},
                title=f"Evolução de Preço Base: '{busca}'"
            )
            fig_preco.update_layout(height=360)
            st.plotly_chart(fig_preco, use_container_width=True)
            
            st.markdown("**Histórico detalhado de todas as compras:**")
            display_cols = ['Data', 'Loja', 'Produto_Limpo', 'Categoria', 'Quantidade', 'Preco_Unitario', 'Unidade_Norm', 'Preco_Base_Norm', 'Total']
            st.dataframe(
                df_match[display_cols]
                .sort_values('Data', ascending=False)
                .rename(columns={'Produto_Limpo': 'Produto', 'Preco_Base_Norm': 'Preço Base (€/kg, €/L, €/un)'}),
                use_container_width=True,
                hide_index=True
            )
    else:
        st.write("👉 Experimente pesquisar por `frango` para ver o consumo semanal, `cerveja` para latas, `dodot` para fraldas ou `arroz`!")

# ------------------------------------------------------------------------------
# TAB 4: COMPARADOR DE LOJAS & INFLAÇÃO PESSOAL
# ------------------------------------------------------------------------------
with tab_comparador:
    st.subheader("⚖️ Continente vs Pingo Doce — Onde Vale Mais a Pena Comprar?")
    st.caption("Análise comparativa de preços por kg/litro, hábitos de compra e variação de preços ao longo de 2026.")
    
    # 1. Cards Comparativos Gerais
    lojas = df['Loja'].unique()
    if len(lojas) < 2:
        st.info("Para comparar lojas, selecione 'Todas as Lojas' ou um intervalo com compras em ambas as lojas no painel lateral.")
    else:
        df_cont = df[df['Loja'] == 'Continente']
        df_pd = df[df['Loja'] == 'Pingo Doce']
        
        gasto_cont = df_cont['Total'].sum()
        gasto_pd = df_pd['Total'].sum()
        faturas_cont = df_cont['Fatura'].nunique()
        faturas_pd = df_pd['Fatura'].nunique()
        ticket_cont = gasto_cont / faturas_cont if faturas_cont > 0 else 0
        ticket_pd = gasto_pd / faturas_pd if faturas_pd > 0 else 0
        desc_cont = df_cont['Desconto'].sum()
        desc_pd = df_pd['Desconto'].sum()
        pct_desc_cont = (desc_cont / (gasto_cont + desc_cont) * 100) if (gasto_cont + desc_cont) > 0 else 0
        pct_desc_pd = (desc_pd / (gasto_pd + desc_pd) * 100) if (gasto_pd + desc_pd) > 0 else 0
        
        st.markdown("##### 📊 Indicadores Globais por Loja")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Gasto Total", f"PD: € {gasto_pd:,.2f}", f"CNT: € {gasto_cont:,.2f}")
        k2.metric("Nº de Faturas", f"PD: {faturas_pd}", f"CNT: {faturas_cont}")
        k3.metric("Ticket Médio / Fatura", f"PD: € {ticket_pd:.2f}", f"CNT: € {ticket_cont:.2f}")
        k4.metric("Taxa de Poupança (Descontos)", f"PD: {pct_desc_pd:.1f}%", f"CNT: {pct_desc_cont:.1f}%")
        
        st.divider()

        # 2. Comparativo de Categoria por Loja
        st.markdown("##### 🛒 Onde Gasta Mais em Cada Categoria?")
        df_cat_loja = df.groupby(['Categoria', 'Loja'])['Total'].sum().reset_index()
        fig_cat_comp = px.bar(
            df_cat_loja,
            x='Total',
            y='Categoria',
            color='Loja',
            barmode='group',
            orientation='h',
            title="Gasto Acumulado por Categoria (€): Continente vs Pingo Doce",
            labels={'Total': 'Total Gasto (€)', 'Categoria': 'Categoria'},
            text_auto='.1f'
        )
        fig_cat_comp.update_layout(height=550)
        st.plotly_chart(fig_cat_comp, use_container_width=True)

        st.divider()

        # 3. Radar de Preços de Bens Essenciais (€/KG e €/L)
        st.markdown("##### 🎯 Radar de Preços: Bens Essenciais e Comuns (€/kg e €/L)")
        st.caption("Comparação de produtos essenciais comparáveis com peso/volume normalizado.")

        comparaveis_sugestoes = ['Frango', 'Arroz', 'Dodot', 'Leite', 'Queijo', 'Ovos', 'Cerveja', 'Azeite', 'Banana', 'Água']
        escolha = st.selectbox("Selecione um produto para comparar preços:", options=comparaveis_sugestoes)
        
        df_comp = df_raw[df_raw['Produto_Limpo'].str.lower().str.contains(escolha.lower())].copy()
        
        if not df_comp.empty and len(df_comp['Loja'].unique()) > 1:
            u_mode = df_comp['Unidade_Norm'].mode()[0]
            df_comp_u = df_comp[df_comp['Unidade_Norm'] == u_mode]
            
            p_cont = df_comp_u[df_comp_u['Loja'] == 'Continente']['Preco_Base_Norm'].mean()
            p_pd = df_comp_u[df_comp_u['Loja'] == 'Pingo Doce']['Preco_Base_Norm'].mean()
            
            rc1, rc2, rc3 = st.columns(3)
            rc1.metric(f"Continente (€/{u_mode})", f"€ {p_cont:.2f}" if pd.notnull(p_cont) else "N/A")
            rc2.metric(f"Pingo Doce (€/{u_mode})", f"€ {p_pd:.2f}" if pd.notnull(p_pd) else "N/A")
            if pd.notnull(p_cont) and pd.notnull(p_pd):
                dif = ((p_pd - p_cont) / p_cont) * 100
                if dif < 0:
                    rc3.metric("Melhor Opção", "Pingo Doce", f"{abs(dif):.1f}% mais barato")
                else:
                    rc3.metric("Melhor Opção", "Continente", f"{abs(dif):.1f}% mais barato")

            # Evolução temporal do preço deste produto (Inflação)
            st.markdown(f"###### 📈 Tendência de Preço (€/{u_mode}) ao Longo de 2026")
            fig_inf = px.line(
                df_comp_u.sort_values('Data'),
                x='Data',
                y='Preco_Base_Norm',
                color='Loja',
                markers=True,
                hover_data=['Produto_Limpo', 'Preco_Unitario', 'Total'],
                labels={'Preco_Base_Norm': f'Preço (€/{u_mode})', 'Data': 'Data'},
                title=f"Histórico e Inflação de Preço: {escolha}"
            )
            fig_inf.update_layout(height=350)
            st.plotly_chart(fig_inf, use_container_width=True)
        else:
            st.info(f"Não há compras suficientes de '{escolha}' em ambas as lojas para calcular a comparação direta.")

# ------------------------------------------------------------------------------
# TAB 5: VILÕES DO ORÇAMENTO (CURVA ABC)
# ------------------------------------------------------------------------------
with tab_viloes:
    st.subheader("🚨 Vilões do Orçamento (Onde realmente vai o seu dinheiro)")
    
    top_n = st.slider("Quantidade de Produtos no Ranking:", min_value=10, max_value=100, value=25)
    
    viloes_df = df.groupby(['Produto', 'Categoria']).agg(
        Vezes=('Produto', 'count'),
        Qtd=('Quantidade', 'sum'),
        Total_Gasto=('Total', 'sum')
    ).reset_index().sort_values('Total_Gasto', ascending=False).head(top_n)
    
    viloes_df['% Orçamento'] = (viloes_df['Total_Gasto'] / total_gasto * 100).round(1)
    
    # Gráfico horizontal dos top vilões
    fig_viloes = px.bar(
        viloes_df.sort_values('Total_Gasto', ascending=True),
        x='Total_Gasto',
        y='Produto',
        orientation='h',
        color='Categoria',
        title=f"Top {top_n} Maiores Despesas do Período (€)",
        labels={'Total_Gasto': 'Total Gasto (€)', 'Produto': 'Produto'},
        text_auto='.2f'
    )
    fig_viloes.update_layout(height=max(450, top_n * 25))
    st.plotly_chart(fig_viloes, use_container_width=True)
    
    # Tabela formatada
    tabela_v = viloes_df[['Produto', 'Categoria', 'Vezes', 'Qtd', 'Total_Gasto', '% Orçamento']].copy()

    st.caption("💡 **Clique numa linha** para ver o consumo semanal desse produto")
    sel = st.dataframe(
        tabela_v,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Total_Gasto": st.column_config.NumberColumn("Total Gasto", format="€ %.2f"),
            "% Orçamento": st.column_config.NumberColumn("% Orçamento", format="%.1f%%"),
            "Qtd": st.column_config.NumberColumn("Qtd", format="%.2f"),
        },
    )

    # Detalhe semanal inline quando uma linha é selecionada
    rows_sel = sel.selection.rows if sel and sel.selection else []
    if rows_sel:
        produto_sel = tabela_v.iloc[rows_sel[0]]['Produto']
        # Guarda no session_state para a aba de pesquisa também poder usar
        st.session_state['produto_selecionado'] = produto_sel

        st.markdown(f"---\n#### 📅 Consumo Semanal — {produto_sel}")

        df_prod = df_raw[
            df_raw['Produto'].str.lower().str.contains(produto_sel.lower(), na=False) |
            df_raw['Produto_Limpo'].str.lower().str.contains(produto_sel.lower(), na=False)
        ].copy()

        if not df_prod.empty:
            total_p = df_prod['Total'].sum()
            qtd_p = df_prod['Quantidade'].sum()
            n_sem = max(df_prod['Ano_Semana'].nunique(), 1)
            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Gasto Total", f"€ {total_p:,.2f}")
            p2.metric("Qtd Total", f"{qtd_p:,.2f}")
            p3.metric("Média/Semana (€)", f"€ {total_p/n_sem:,.2f}")
            p4.metric("Média/Semana (qtd)", f"{qtd_p/n_sem:,.2f}")

            df_sem_p = (
                df_prod
                .groupby(['Semana_Inicio', 'Semana_Completa', 'Loja'], as_index=False)
                .agg(Qtd_Semana=('Quantidade', 'sum'), Gasto_Semana=('Total', 'sum'))
                .sort_values('Semana_Inicio')
            )

            gc1, gc2 = st.columns(2)
            with gc1:
                fg_q = px.bar(
                    df_sem_p, x='Semana_Completa', y='Qtd_Semana', color='Loja',
                    title="Quantidade por Semana",
                    labels={'Qtd_Semana': 'Qtd', 'Semana_Completa': 'Semana'},
                    text_auto='.2f'
                )
                fg_q.update_layout(height=320, xaxis_tickangle=-40)
                st.plotly_chart(fg_q, use_container_width=True)
            with gc2:
                fg_g = px.bar(
                    df_sem_p, x='Semana_Completa', y='Gasto_Semana', color='Loja',
                    title="Gasto por Semana (€)",
                    labels={'Gasto_Semana': 'Gasto (€)', 'Semana_Completa': 'Semana'},
                    text_auto='.2f'
                )
                fg_g.update_layout(height=320, xaxis_tickangle=-40)
                st.plotly_chart(fg_g, use_container_width=True)

# ------------------------------------------------------------------------------
# TAB 5: FATURAS E DADOS BRUTOS
# ------------------------------------------------------------------------------
with tab_dados:
    st.subheader("📄 Tabela Completa de Registos")
    
    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        filtro_texto = st.text_input("Filtrar tabela por texto:", "")
    with col_f2:
        st.write("")
        st.write("")
        csv = df.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Descarregar Dados Filtrados (CSV)",
            data=csv,
            file_name="faturas_filtradas.csv",
            mime="text/csv"
        )
        
    df_view = df.copy()
    if filtro_texto.strip():
        txt = filtro_texto.strip().lower()
        df_view = df_view[
            df_view['Produto'].str.lower().str.contains(txt) | 
            df_view['Categoria'].str.lower().str.contains(txt) |
            df_view['Loja'].str.lower().str.contains(txt)
        ]
        
    df_view['Data'] = df_view['Data'].dt.strftime('%Y-%m-%d')
    st.dataframe(
        df_view[['Data', 'Loja', 'Fatura', 'Categoria', 'Subgrupo_Cesta', 'Produto', 'Quantidade', 'Preco_Unitario', 'Total']]
        .style.format({'Total': '€ {:.2f}', 'Preco_Unitario': '€ {:.2f}', 'Quantidade': '{:.2f}'}),
        use_container_width=True
    )
