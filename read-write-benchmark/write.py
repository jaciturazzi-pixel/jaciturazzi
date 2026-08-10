import mysql.connector
import time
import threading
from datetime import datetime
import sys

# --- CONFIGURAÇÃO ---
DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 5555,
    'user': 'admin',
    'password': 'rootroot',
    'database': 'teste_failover',
    'connection_timeout': 2 # Timeout baixo para falhar rápido
}

NUM_THREADS = 5

def setup_database():
    """Cria o banco e a tabela automaticamente antes de iniciar o teste."""
    print("--- Configurando Banco de Dados ---")
    try:
        # 1. Conecta sem especificar o banco de dados para poder criar o schema
        config_inicial = DB_CONFIG.copy()
        if 'database' in config_inicial:
            del config_inicial['database']
            
        conn = mysql.connector.connect(**config_inicial)
        cursor = conn.cursor()
        
        # 2. Cria o Database
        db_name = DB_CONFIG['database']
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name}")
        print(f"Database '{db_name}' verificado/criado.")
        
        # 3. Seleciona o banco e cria a tabela
        conn.database = db_name
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conexoes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                thread_id INT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("Tabela 'conexoes' verificada/criada.\n")
        
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"ERRO AO CONFIGURAR BANCO: {e}")
        sys.exit(1)

def worker(thread_id):
    while True:
        conn = None
        try:
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor()
            
            cursor.execute("INSERT INTO conexoes (thread_id) VALUES (%s)", (thread_id,))
            conn.commit()
            
            sys.stdout.write(f".") 
            sys.stdout.flush()
            time.sleep(0.1)

        except mysql.connector.Error as err:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            print(f"\n[FALHA] {timestamp} - Erro na Thread {thread_id}: {err.msg}")
            time.sleep(0.5) 
            
        except Exception as e:
            print(f"\n[ERRO GENÉRICO] {e}")
            
        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()

if __name__ == "__main__":
    # Roda a configuração inicial antes das threads
    setup_database()

    print(f"--- Iniciando Teste de Escrita (Writer) em {DB_CONFIG['host']} ---")
    print("Monitorando falhas... Pressione Ctrl+C para parar.")
    
    threads = []
    for i in range(NUM_THREADS):
        t = threading.Thread(target=worker, args=(i,))
        t.daemon = True
        t.start()
        threads.append(t)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("\nTeste finalizado.")