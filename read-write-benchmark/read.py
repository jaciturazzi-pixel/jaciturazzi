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
    'connection_timeout': 2
}

NUM_THREADS = 5

def worker(thread_id):
    while True:
        conn = None
        try:
            conn = mysql.connector.connect(**DB_CONFIG)
            cursor = conn.cursor()
            
            # Query leve apenas para testar conectividade
            cursor.execute("SELECT 1")
            cursor.fetchall()
            
            sys.stdout.write(f"*") # Asterisco para diferenciar do writer
            sys.stdout.flush()
            time.sleep(0.1)

        except mysql.connector.Error as err:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            print(f"\n[LEITURA DOWN] {timestamp} - Thread {thread_id}: {err.msg}")
            time.sleep(0.5)

        finally:
            if conn and conn.is_connected():
                cursor.close()
                conn.close()

if __name__ == "__main__":
    print(f"--- Iniciando Teste de Leitura (Reader) ---")
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