"""Módulo partilhado de classificação de produtos.

Centraliza a lógica de Cesta Básica / Essenciais usada pelo Reporter e Dashboard.
"""

import pandas as pd
from typing import Optional


def classify_essential(row) -> Optional[str]:
    """Classifica produtos em subgrupos da Cesta Básica / Essenciais.

    Recebe uma row com campos 'Produto' e 'Categoria'.
    Retorna o nome do subgrupo ou None se não for essencial.
    """
    desc = str(row['Produto']).lower()
    cat = str(row['Categoria'])

    # Exclusões estritas (bebidas alcoólicas, energéticos, doces industrializados, brinquedos)
    if cat in ['Cervejas', 'Vinhos', 'Energéticos', 'Livraria & Brinquedos', 'Cashback / Depósitos']:
        return None
    if any(k in desc for k in ['cerveja', 'red bull', 'monster', 'vinho']):
        return None

    # 1. Carnes, Peixe & Charcutaria (mortadela, fiambre, etc.)
    if cat in ['Carnes & Aves', 'Peixaria & Marisco'] or any(
        k in desc for k in ['mortad', 'fiambre', 'presunto', 'chouric', 'bacon', 'frango', 'carne', 'vitel', 'bife', 'lombo']
    ):
        return 'Carnes & Charcutaria'

    # 2. Frutas & Legumes
    if cat == 'Frutas & Legumes':
        return 'Frutas & Legumes'

    # 3. Laticínios, Queijos & Ovos
    if any(k in desc for k in ['ovo', 'ovos']) or cat in ['Queijos', 'Outros Laticínios (Leite, Manteiga)', 'Iogurtes'] or any(
        k in desc for k in ['queijo', 'leite', 'manteiga', 'iogurt']
    ):
        return 'Laticínios, Queijos & Ovos'

    # 4. Pães & Padaria básica
    if cat == 'Padaria & Pastelaria' or any(k in desc for k in ['pao', 'pão', 'baguete', 'broa', 'torrada']):
        if any(k in desc for k in ['croissant', 'bolo', 'pastel', 'donuts', 'muffin', 'queque']):
            return None
        return 'Pães & Padaria'

    # 5. Mercearia Básica (Arroz, Feijão, Massas, Azeite, Geleia/Compota, etc.)
    if any(k in desc for k in ['feij', 'arroz', 'massa', 'esparguete', 'macarrao', 'macarrão', 'gelei', 'compota', 'azeite', 'oleo', 'óleo', 'farinha', 'acucar', 'açucar', 'sal ']):
        return 'Mercearia Básica (Arroz, Feijão, Massas, Geleia)'
    if cat == 'Mercearia (Massa, Arroz, Conservas)':
        return 'Mercearia Básica (Arroz, Feijão, Massas, Geleia)'

    # 6. Água Mineral
    if any(k in desc for k in ['agua', 'água']) and 'tónica' not in desc:
        return 'Água'

    # 7. Produtos de Limpeza & Papel Higiénico
    if cat == 'Limpeza da Casa' or any(
        k in desc for k in ['papel hig', 'detergente', 'lixivia', 'lixívia', 'lava tudo', 'sabao', 'sabão', 'guardanapo', 'rolo cozinha']
    ):
        return 'Produtos de Limpeza & Papel'

    # 8. Higiene Básica Essencial
    if cat == 'Higiene Pessoal' and any(
        k in desc for k in ['dent', 'pasta', 'sabonete', 'gel banho', 'champ', 'shamp', 'desodor']
    ):
        return 'Higiene Pessoal Básica'

    return None
