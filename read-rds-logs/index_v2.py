import glob
import re
import mysql.connector
import csv
from collections import defaultdict

# --- CONFIGURAÇÃO ---
DB_CONFIG = {
    'user': 'admin',
    'password': 'rootroot',     
    'host': '127.0.0.1',        
    'database': 'clubs',        
    'raise_on_warnings': False,
    'port': 5555
}

LOG_FILES = [
    "mysql-general-writer.log",
    "mysql-general-reads-replicas.log",
    "mysql-general-read 3.log",
    "mysql-general-read 2.log"
]

def clean_query(query_text):
    """Remove timestamp final e espaços extras."""
    match_date = re.search(r'\s\d{4}-\d{2}-\d{2}T', query_text)
    if match_date:
        return query_text[:match_date.start()].strip()
    return query_text.strip()

def parse_log_line(line):
    # Regex flexível para pegar Query ou Execute
    match = re.search(r'[\t\s]+(Query|Execute)[\t\s]+(.*)', line)
    
    if match:
        command_type = match.group(1)
        raw_query = match.group(2).strip()
        
        # 1. Limpeza
        query = clean_query(raw_query)

        # 2. Filtros
        if not query: return None
        if command_type == 'Prepare': return None
        if command_type == 'Execute' and '?' in query: return None

        # 3. Filtros Administrativos
        q_upper = query.upper()
        ignore_starts = ('SET ', 'SHOW ', 'COMMIT', 'ROLLBACK', 'PING', 'STATISTICS', 'QUIT', 'CONNECT', 'USE ', 'DEALLOCATE ', 'INIT ', 'KILL ', 'PURGE ', 'CALL ')
        
        if q_upper.startswith(ignore_starts): return None
        if q_upper == 'SELECT 1' or q_upper.startswith('SELECT @@'): return None
        if any(x in q_upper for x in ['INFORMATION_SCHEMA', 'PERFORMANCE_SCHEMA', 'MYSQL.']): return None

        return query
    return None

def main():
    print("--- Gerador de SQL e Validação MySQL 8.0 ---")
    
    unique_queries = set()
    current_query = ""
    
    # 1. Leitura dos Logs
    print("1. Lendo logs e extraindo queries...")
    for log_file in LOG_FILES:
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if re.search(r'[\t\s]+(Query|Execute)[\t\s]+', line):
                        if current_query:
                            cleaned = clean_query(current_query)
                            if cleaned: unique_queries.add(cleaned)
                        parsed = parse_log_line(line)
                        current_query = parsed if parsed else ""
                    else:
                        if current_query: current_query += " " + line.strip()
                
                if current_query:
                    cleaned = clean_query(current_query)
                    if cleaned: unique_queries.add(cleaned)
        except FileNotFoundError:
            print(f"   [AVISO] {log_file} não encontrado.")

    # Lista final ordenada para consistência
    final_queries = sorted(list({q for q in unique_queries if q and len(q) > 5}))
    print(f"   > Total de queries únicas extraídas: {len(final_queries)}")

    # 2. GERAÇÃO DO ARQUIVO .SQL (O que você pediu)
    print("\n2. Gerando arquivo 'queries_testadas.sql'...")
    with open('queries_testadas.sql', 'w', encoding='utf-8') as f_sql:
        f_sql.write("-- Dump das queries extraídas dos logs para validação\n")
        f_sql.write(f"-- Total: {len(final_queries)}\n\n")
        for q in final_queries:
            # Adiciona ponto e vírgula se não tiver
            if not q.endswith(';'):
                f_sql.write(f"{q};\n")
            else:
                f_sql.write(f"{q}\n")
    print("   > Arquivo gerado com sucesso!")

    # 3. Validação no Banco (Opcional se já rodou, mas bom pra garantir)
    print("\n3. Validando conexões e sintaxe (EXPLAIN)...")
    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor()
        
        with open('relatorio_final.csv', 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=['Error_Code', 'Error_Message', 'Query'])
            writer.writeheader()
            
            errors = 0
            for i, query in enumerate(final_queries):
                try:
                    cursor.execute(f"EXPLAIN {query}")
                    cursor.fetchall()
                except mysql.connector.Error as err:
                    if err.errno not in [1146, 1054]: # Ignora tabela/coluna inexistente
                        errors += 1
                        writer.writerow({'Error_Code': err.errno, 'Error_Message': err.msg, 'Query': query})
                        print(f"   [ERRO] {err.msg}")

        print(f"\nRESULTADO: {len(final_queries)} queries geradas no .sql | {errors} erros de sintaxe encontrados.")
        cnx.close()
        
    except Exception as e:
        print(f"   [Erro de Conexão] {e}")
        print("   Mas o arquivo .sql já foi gerado no passo anterior.")

if __name__ == "__main__":
    main()