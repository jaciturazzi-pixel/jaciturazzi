import os
import yaml
from typing import List, Dict
from google import genai
from google.genai import types
from dotenv import load_dotenv

from .database import Database

class Categorizer:
    def __init__(self, db: Database, config_path: str = "config.yaml"):
        self.db = db
        load_dotenv()
        
        # Carrega as categorias do config.yaml
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            self.categories = config.get('categories', [])
            
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY não encontrada no ficheiro .env")
            
        self.client = genai.Client(api_key=api_key)
        
    def _build_prompt(self, items: List[str]) -> str:
        cat_list = "\n".join([f"- {c}" for c in self.categories])
        item_list = "\n".join([f"{i+1}. {item}" for i, item in enumerate(items)])
        
        prompt = f"""
        És um assistente especializado em recibos de supermercado em Portugal (Continente, Pingo Doce, etc.).
        Abaixo tens uma lista de nomes de produtos extraídos de faturas. Estes nomes têm frequentemente abreviaturas difíceis (ex: 'MACA GALA NAC CAL' = Maçã Gala, 'QJ FLAM FAT' = Queijo Flamengo).
        
        A tua tarefa é classificar cada produto numa das seguintes categorias permitidas:
        
        {cat_list}
        
        Se não tiveres a certeza, usa 'Outros'.
        
        Devolve a resposta APENAS no formato de lista onde cada linha corresponde ao formato:
        ID. Categoria
        
        Lista de produtos:
        {item_list}
        """
        return prompt

    def categorize_items(self, item_names: List[str]) -> Dict[str, str]:
        """Recebe uma lista de nomes e retorna um dicionário {nome_original: Categoria}"""
        if not item_names:
            return {}
            
        results = {}
        items_to_ai = []
        
        # 1. Verifica no Cache primeiro
        for item in item_names:
            cached_cat = self.db.get_cached_category(item)
            if cached_cat:
                results[item] = cached_cat
            else:
                items_to_ai.append(item)
                
        if not items_to_ai:
            return results
            
        # 2. Envia para a API em blocos (batches de 35 itens) para nunca sobrecarregar a API
        print(f"A enviar {len(items_to_ai)} itens novos para a IA classificar (em lotes de 35)...")
        import time

        batch_size = 35
        for i in range(0, len(items_to_ai), batch_size):
            chunk = items_to_ai[i:i + batch_size]
            prompt = self._build_prompt(chunk)
            
            response = None
            candidate_models = ['gemini-3.5-flash-lite', 'gemini-3.6-flash']
            api_succeeded = False

            for model_name in candidate_models:
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = self.client.models.generate_content(
                            model=model_name,
                            contents=prompt,
                        )
                        api_succeeded = True
                        break
                    except Exception as e:
                        err_str = str(e)
                        wait_time = (attempt + 1) * 3
                        if ("503" in err_str or "429" in err_str) and attempt < max_retries - 1:
                            print(f"  Modelo {model_name} ocupado/quota ({err_str[:40]}...). A aguardar {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            print(f"  Tentativa com {model_name} esgotada ({err_str[:60]}).")
                            break
                if api_succeeded:
                    break

            # Parse da resposta deste lote
            if response and response.text:
                response_text = response.text.strip().split('\n')
                for line in response_text:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parts = line.split('.', 1)
                        if len(parts) == 2:
                            idx = int(parts[0]) - 1
                            category = parts[1].strip().replace('*', '').strip()
                            if 0 <= idx < len(chunk):
                                original_name = chunk[idx]
                                if category not in self.categories:
                                    category = "Outros"
                                results[original_name] = category
                                self.db.cache_category(original_name, category)
                    except ValueError:
                        continue

            # Para qualquer item do lote não classificado pela IA:
            # Se a API teve sucesso e a IA escolheu Outros ou não classificou, guarda na cache.
            # Se a API falhou (erro de rede/quota), define temporariamente na resposta mas NÃO grava na cache!
            for item in chunk:
                if item not in results:
                    results[item] = "Outros"
                    if api_succeeded:
                        self.db.cache_category(item, "Outros")
            
            time.sleep(1) # pausa suave entre lotes

        return results
        
    def process_uncategorized_in_db(self):
        """Encontra itens sem categoria na DB e classifica-os."""
        uncategorized = self.db.get_uncategorized_items()
        if not uncategorized:
            print("Nenhum item sem categoria encontrado na DB.")
            return
            
        # Extrai nomes únicos (para não enviar duplicados para a IA)
        unique_names = list(set([item[1] for item in uncategorized]))
        
        # Classifica e coloca em cache
        cat_map = self.categorize_items(unique_names)
        
        # Atualiza a tabela de line_items
        for item_id, description in uncategorized:
            if description in cat_map:
                self.db.update_item_category(item_id, cat_map[description])
                
        print(f"Foram classificados e atualizados {len(uncategorized)} itens.")
