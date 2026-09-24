import argparse
import sys
from .database import Database
from .categorizer import Categorizer
from .reporter import Reporter

from .gmail_fetcher import GmailFetcher
from .pdf_parser import ParserFactory
import os
import glob

def cmd_setup(args):
    print("A configurar o sistema...")
    fetcher = GmailFetcher()
    fetcher.authenticate()
    print("Pronto! Pode agora usar o comando fetch.")

def cmd_fetch(args):
    print(f"A descarregar faturas do Gmail (De: {args.start_date} Até: {args.end_date})")
    fetcher = GmailFetcher()
    downloaded = fetcher.fetch_invoices(args.start_date, args.end_date)
    print(f"Total de {len(downloaded)} PDFs descarregados.")

def cmd_process(args):
    print(f"A processar PDFs locais na pasta: {args.input_dir}")
    db = Database()
    
    # Encontrar todos os PDFs na pasta e subpastas
    pdf_files = glob.glob(os.path.join(args.input_dir, "**/*.pdf"), recursive=True)
    
    for pdf_path in pdf_files:
        print(f"A processar {os.path.basename(pdf_path)}...")
        try:
            parser = ParserFactory.get_parser(pdf_path)
            data = parser.parse()
            
            # Evita duplicados
            if db.invoice_exists(data['invoice_number']):
                print(f" Fatura {data['invoice_number']} já existe na DB. A ignorar.")
                continue
                
            inv_id = db.insert_invoice(data)
            db.insert_line_items(inv_id, data['items'])
            print(f" Fatura {data['invoice_number']} inserida com sucesso!")
        except Exception as e:
            print(f" Erro ao processar {pdf_path}: {e}")
    
    print("\nA classificar itens sem categoria com a IA...")
    cat = Categorizer(db)
    cat.process_uncategorized_in_db()
    
    db.close()
    print("Processamento concluído.")

def cmd_report(args):
    print("A gerar relatório...")
    if args.start_date or args.end_date:
        print(f" 🗓️  Filtro ativo: De {args.start_date or 'início'} até {args.end_date or 'hoje'}")
    else:
        print(" 🗓️  Filtro ativo: Todo o histórico acumulado na base de dados")
        
    db = Database()
    reporter = Reporter(db)
    reporter.generate_excel_report(start_date=args.start_date, end_date=args.end_date, filename=args.output)
    db.close()

def cmd_clean_db(args):
    db = Database()
    cursor = db.conn.cursor()
    cursor.execute("DELETE FROM line_items;")
    cursor.execute("DELETE FROM invoices;")
    db.conn.commit()
    db.close()
    print("🧹 Base de dados de faturas limpa com sucesso! (A memória de categorias da IA foi preservada).")

def cmd_reclassify(args):
    print("🔄 A reclassificar itens marcados como 'Outros'...")
    db = Database()
    
    # 1. Encontra quantos itens Outros existem
    outros_items = db.get_outros_items()
    if not outros_items:
        print("✅ Nenhum item classificado como 'Outros' encontrado. Tudo limpo!")
        db.close()
        return
    
    print(f"   Encontrados {len(outros_items)} itens como 'Outros'.")
    
    # 2. Limpa o cache de 'Outros' para forçar nova classificação
    cleared = db.clear_outros_cache()
    print(f"   Removidas {cleared} entradas do cache de categorias 'Outros'.")
    
    # 3. Reseta a categoria dos itens na tabela line_items para NULL
    cursor = db.conn.cursor()
    cursor.execute("UPDATE line_items SET category = NULL WHERE category = 'Outros'")
    reset_count = cursor.rowcount
    db.conn.commit()
    print(f"   {reset_count} itens resetados para reclassificação.")
    
    # 4. Reclassifica com a IA
    cat = Categorizer(db)
    cat.process_uncategorized_in_db()
    
    # 5. Verifica quantos ainda ficaram como Outros
    remaining = db.get_outros_items()
    if remaining:
        print(f"⚠️  {len(remaining)} itens ainda classificados como 'Outros' (possível falha da API).")
    else:
        print("✅ Todos os itens foram reclassificados com sucesso!")
    
    db.close()

def cmd_dashboard(args):
    import subprocess
    print("🚀 A iniciar o Dashboard Interativo (Datadog do Mercado)...")
    print("🌐 O dashboard vai abrir no seu browser (padrão: http://localhost:8501)")
    cmd = [sys.executable, "-m", "streamlit", "run", "src/dashboard.py"]
    if args.port:
        cmd.extend(["--server.port", str(args.port)])
    subprocess.run(cmd)

def main():
    parser = argparse.ArgumentParser(description="Gestor de Faturas de Supermercado")
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponíveis")
    
    # Setup
    subparsers.add_parser("setup", help="Configura a autenticação do Gmail")
    
    # Fetch
    parser_fetch = subparsers.add_parser("fetch", help="Descarrega faturas do Gmail")
    parser_fetch.add_argument("--start-date", help="Data início (YYYY-MM-DD)")
    parser_fetch.add_argument("--end-date", help="Data fim (YYYY-MM-DD)")
    
    # Process
    parser_process = subparsers.add_parser("process", help="Extrai dados dos PDFs e categoriza")
    parser_process.add_argument("--input-dir", help="Pasta com PDFs (se não usar o gmail)", default="data/invoices")
    
    # Report
    parser_report = subparsers.add_parser("report", help="Gera o relatório Excel")
    parser_report.add_argument("--start-date", help="Filtro data início (YYYY-MM-DD)")
    parser_report.add_argument("--end-date", help="Filtro data fim (YYYY-MM-DD)")
    parser_report.add_argument("--output", "-o", help="Nome personalizado para o ficheiro Excel")

    # Clean DB
    subparsers.add_parser("clean-db", help="Limpa as faturas da base de dados (mantém cache da IA)")

    # Reclassify
    subparsers.add_parser("reclassify-others", help="Reclassifica itens marcados como 'Outros' pela IA")

    # Dashboard (Nova Versão Interativa)
    parser_dash = subparsers.add_parser("dashboard", help="Inicia o dashboard web dinâmico (estilo Datadog)")
    parser_dash.add_argument("--port", type=int, help="Porta do servidor web (padrão: 8501)", default=8501)
    
    args = parser.parse_args()
    
    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "process":
        cmd_process(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "clean-db":
        cmd_clean_db(args)
    elif args.command == "reclassify-others":
        cmd_reclassify(args)
    elif args.command == "dashboard":
        cmd_dashboard(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
