import os
import yaml
import base64
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

class GmailFetcher:
    def __init__(self, config_path: str = "config.yaml", creds_path: str = "credentials.json", token_path: str = "token.json"):
        self.config_path = config_path
        self.creds_path = creds_path
        self.token_path = token_path
        self.creds = None
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
            
        self.invoices_dir = self.config.get('paths', {}).get('invoices_dir', 'data/invoices')
        os.makedirs(self.invoices_dir, exist_ok=True)
        
    def authenticate(self):
        """Authenticates with the Gmail API and stores the token."""
        if os.path.exists(self.token_path):
            self.creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                if not os.path.exists(self.creds_path):
                    raise FileNotFoundError(
                        f"Ficheiro de credenciais '{self.creds_path}' não encontrado. "
                        "É necessário criar um projeto na Google Cloud e descarregar as credenciais OAuth2."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.creds_path, SCOPES)
                # Opens a local browser for the user to log in
                self.creds = flow.run_local_server(port=0)
                
            # Save the credentials for the next run
            with open(self.token_path, 'w') as token:
                token.write(self.creds.to_json())
                
        self.service = build('gmail', 'v1', credentials=self.creds)
        print("Autenticação com Gmail realizada com sucesso.")

    def _get_attachment(self, message_id, attachment_id, filename):
        attachment = self.service.users().messages().attachments().get(
            userId='me', messageId=message_id, id=attachment_id
        ).execute()
        
        file_data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
        
        # Save to month folder
        month_folder = datetime.now().strftime("%Y-%m")
        target_dir = os.path.join(self.invoices_dir, month_folder)
        os.makedirs(target_dir, exist_ok=True)
        
        filepath = os.path.join(target_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(file_data)
            
        return filepath

    def fetch_invoices(self, start_date: str = None, end_date: str = None) -> list:
        """Busca emails de faturas, baixa os PDFs e retorna a lista de caminhos locais."""
        if not self.creds:
            self.authenticate()
            
        downloaded_files = []
        queries = self.config.get('gmail', {}).get('queries', [])
        
        for q in queries:
            query_str = q['query']
            store_name = q['store']
            
            if start_date:
                query_str += f" after:{start_date}"
            if end_date:
                query_str += f" before:{end_date}"
                
            print(f"A pesquisar faturas de {store_name}...")
            
            results = self.service.users().messages().list(userId='me', q=query_str).execute()
            messages = results.get('messages', [])
            
            if not messages:
                print(f"Sem novas faturas para {store_name}.")
                continue
                
            for msg in messages:
                msg_id = msg['id']
                try:
                    message = self.service.users().messages().get(userId='me', id=msg_id).execute()
                except Exception as e:
                    import time
                    print("\nAtingido limite de velocidade do Gmail. A aguardar 10 segundos para retomar...")
                    time.sleep(10)
                    message = self.service.users().messages().get(userId='me', id=msg_id).execute()
                
                # Check for parts
                parts = message.get('payload', {}).get('parts', [])
                for part in parts:
                    if part.get('filename') and part['filename'].endswith('.pdf'):
                        # Found a PDF attachment!
                        att_id = part['body'].get('attachmentId')
                        if att_id:
                            filename = f"{msg_id}_{part['filename']}"
                            filepath = self._get_attachment(msg_id, att_id, filename)
                            downloaded_files.append((msg_id, filepath))
                            print(f"Download e gravação: {filename}")
                            
                # Pequena pausa para não exceder o limite da API (Quota)
                import time
                time.sleep(0.5)
                
        return downloaded_files
