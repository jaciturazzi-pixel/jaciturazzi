"""Módulo de normalização de produtos e cálculo de medidas base.

Extrai gramagem (KG), litragem (L) e contagem de embalagens multipack
para calcular o Preço Efetivo por KG ou por Litro (€/kg e €/L), permitindo
comparações justas entre lojas e tamanhos de embalagens diferentes.
"""

import re
import pandas as pd
from typing import Dict, Any


def clean_product_name(desc: str) -> str:
    """Limpa caracteres especiais de recibos de supermercado (ex: 'ºSUPER BOCK' -> 'SUPER BOCK')."""
    if not desc:
        return ""
    # Remove o símbolo de grau 'º' comum nas faturas do Pingo Doce
    cleaned = re.sub(r'^[º°]\s*', '', desc.strip())
    # Normaliza múltiplos espaços
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def extract_measurement(desc: str, unit: str, unit_price: float) -> Dict[str, Any]:
    """Extrai peso/volume/unidade a partir da descrição e calcula o preço por unidade base (€/kg, €/L ou €/un).
    
    Retorna:
        - unit_norm: 'KG', 'L' ou 'UN'
        - qty_norm: quantidade base na embalagem (ex: 0.5 para 500g ou 0.33 para 33cl)
        - price_per_norm: preço por KG, por Litro, ou preço unitário
        - clean_name: nome limpo sem ruído inicial
    """
    desc_clean = clean_product_name(desc)
    desc_upper = desc_clean.upper()
    unit_price = float(unit_price or 0.0)

    # 1. Se a fatura já indica venda a peso (KG)
    if str(unit).upper() == 'KG':
        return {
            'unit_norm': 'KG',
            'qty_norm': 1.0,
            'price_per_norm': round(unit_price, 2),
            'clean_name': desc_clean
        }

    # 2. Padrão Multipack com peso/volume: ex: 2X200G, 6X33CL, 2*400G, 4X125G
    m_multi = re.search(r'(\d+)\s*[X\*]\s*(\d+(?:[.,]\d+)?)\s*(G|KG|CL|ML|LT|L)\b', desc_upper)
    if m_multi:
        n = int(m_multi.group(1))
        val = float(m_multi.group(2).replace(',', '.'))
        u = m_multi.group(3)
        if u == 'G':
            total_kg = (n * val) / 1000.0
            p_norm = round(unit_price / total_kg, 2) if total_kg > 0 else unit_price
            return {'unit_norm': 'KG', 'qty_norm': total_kg, 'price_per_norm': p_norm, 'clean_name': desc_clean}
        elif u == 'KG':
            total_kg = n * val
            p_norm = round(unit_price / total_kg, 2) if total_kg > 0 else unit_price
            return {'unit_norm': 'KG', 'qty_norm': total_kg, 'price_per_norm': p_norm, 'clean_name': desc_clean}
        elif u in ('CL', 'ML', 'LT', 'L'):
            litros = (n * val) / (100.0 if u == 'CL' else 1000.0 if u == 'ML' else 1.0)
            p_norm = round(unit_price / litros, 2) if litros > 0 else unit_price
            return {'unit_norm': 'L', 'qty_norm': litros, 'price_per_norm': p_norm, 'clean_name': desc_clean}

    # 3. Peso em Gramas ou KG simples: ex: 600G, 1KG, 250G, 1.5KG
    m_weight = re.search(r'(\d+(?:[.,]\d+)?)\s*(KG|G)\b', desc_upper)
    if m_weight:
        val = float(m_weight.group(1).replace(',', '.'))
        u = m_weight.group(2)
        total_kg = val if u == 'KG' else val / 1000.0
        p_norm = round(unit_price / total_kg, 2) if total_kg > 0 else unit_price
        return {'unit_norm': 'KG', 'qty_norm': total_kg, 'price_per_norm': p_norm, 'clean_name': desc_clean}

    # 4. Volume em Litros, Centilitros ou Mililitros: ex: 33CL, 50CL, 6LT, 1.5L, 750ML
    m_vol = re.search(r'(\d+(?:[.,]\d+)?)\s*(CL|ML|LT|L)\b', desc_upper)
    if m_vol:
        val = float(m_vol.group(1).replace(',', '.'))
        u = m_vol.group(2)
        litros = val / 100.0 if u == 'CL' else val / 1000.0 if u == 'ML' else val
        p_norm = round(unit_price / litros, 2) if litros > 0 else unit_price
        return {'unit_norm': 'L', 'qty_norm': litros, 'price_per_norm': p_norm, 'clean_name': desc_clean}

    # 5. Fallback por Unidade
    return {
        'unit_norm': 'UN',
        'qty_norm': 1.0,
        'price_per_norm': round(unit_price, 2),
        'clean_name': desc_clean
    }


def enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica a normalização de medidas a um DataFrame com colunas ['Produto', 'Unidade', 'Preco_Unitario']."""
    if df.empty:
        return df

    measurements = df.apply(
        lambda r: extract_measurement(
            r.get('Produto', ''),
            r.get('Unidade', 'UN'),
            r.get('Preco_Unitario', 0.0)
        ),
        axis=1
    )

    df['Unidade_Norm'] = measurements.apply(lambda m: m['unit_norm'])
    df['Qtd_Embalagem_Norm'] = measurements.apply(lambda m: m['qty_norm'])
    df['Preco_Base_Norm'] = measurements.apply(lambda m: m['price_per_norm'])
    df['Produto_Limpo'] = measurements.apply(lambda m: m['clean_name'])
    return df
