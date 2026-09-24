# Relatório Automático de Faturas de Supermercado

Um sistema automatizado em Python para extrair, categorizar e gerar relatórios financeiros a partir de faturas de supermercado (Pingo Doce e Continente) recebidas por e-mail.

## 🚀 Funcionalidades

- **Integração com Gmail (OAuth2):** Procura automaticamente e faz download dos PDFs das faturas recebidas das entidades Continente e Pingo Doce.
- **Extração de Dados em PDF:** Faz *parsing* das tabelas de faturas usando Expressões Regulares (RegEx) para extrair produtos, quantidades, preços unitários, descontos e os metadados da fatura.
- **Categorização Inteligente com IA (Gemini):** Envia os nomes abreviados dos produtos para a API do Google Gemini, categorizando-os corretamente (ex: `QJ FLAM FAT` -> `Lacticínios`).
- **Cache Local:** As categorias aprendidas pela IA são guardadas numa base de dados SQLite local, garantindo que o sistema só consulta a IA uma vez por produto novo.
- **Relatório Excel Avançado:** Gera um ficheiro Excel (`.xlsx`) com abas para as linhas de faturas, resumo por categoria, resumo por loja e top 50 produtos mais comprados.

---

## 🛠 Pré-requisitos e Instalação

Necessita de ter o **Python 3.11+** instalado no seu sistema.

1. **Instalar Dependências:**
   Abra o terminal na pasta do projeto e instale as bibliotecas necessárias:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Chave da API do Gemini (Para a Categorização):**
   * Vá ao [Google AI Studio](https://aistudio.google.com/apikey) e crie uma API Key gratuita.
   * Crie um ficheiro `.env` na raiz deste projeto e insira a chave:
     ```env
     GEMINI_API_KEY=sua_chave_aqui
     ```

3. **Credenciais do Gmail (Para Descarregar Faturas):**
   * Vá ao [Google Cloud Console](https://console.cloud.google.com/).
   * Crie um projeto novo, procure por **Gmail API** e ative-a.
   * Vá a **APIs e Serviços > Credenciais**, e crie um "ID de cliente OAuth" (Tipo de aplicação: App de Computador).
   * Descarregue o ficheiro JSON, renomeie-o para `credentials.json` e coloque-o na raiz deste projeto.

---

## 💻 Como Usar

O projeto é controlado totalmente pela linha de comandos (CLI) através do ficheiro central `src/main.py`.

### 1. Primeira Autenticação (Setup)
Na primeira utilização, tem de autorizar o script a ler os seus emails (para buscar os PDFs).
```bash
python -m src.main setup
```
*Isto irá abrir uma janela no browser para fazer login com a sua conta Google. Será criado um ficheiro `token.json` que mantém a sessão guardada.*

### 2. Sincronizar Faturas (Fetch)
Pesquisa no Gmail por faturas do Continente e Pingo Doce no período que desejar, fazendo o download dos anexos PDF.
```bash
python -m src.main fetch --start-date 2024-01-01 --end-date 2024-12-31
```
*(Os PDFs ficarão guardados em `data/invoices/YYYY-MM/`)*

### 3. Processar e Categorizar (Process)
Lê todos os PDFs armazenados localmente, extrai cada linha de compra, e usa o Gemini para classificar os itens que ainda não conhece.
```bash
python -m src.main process
```

### 4. Gerar Relatório (Report)
Lê a base de dados SQLite e gera o ficheiro Excel com gráficos e painéis analíticos.
```bash
# Relatório de todo o histórico acumulado
python -m src.main report

# Relatório filtrado por período específico
python -m src.main report --start-date 2026-08-21 --end-date 2026-09-21

# Relatório com nome personalizado
python -m src.main report --start-date 2026-08-21 -o compras_setembro.xlsx
```
*Cada execução gera um ficheiro único com a data e timestamp (ex: `relatorio_desde_2026-08-21_20260921_112126.xlsx`) e mantém também uma cópia atualizada em `relatorio_faturas.xlsx`.*

### 5. Dashboard Interativo Web (Estilo Datadog)
Inicia uma aplicação web interativa em Streamlit no seu browser, com filtros dinâmicos de datas, lojas e pesquisa por produto.
```bash
python -m src.main dashboard
```
*Abre automaticamente em `http://localhost:8501`.*

### 6. Limpar Histórico de Faturas (Opcional)
Se quiser recomeçar uma base de dados limpa (apenas com as faturas que vai buscar a seguir, mantendo toda a memória da IA intacta):
```bash
python -m src.main clean-db
```

---

## ⚙️ Configuração Adicional (`config.yaml`)

O ficheiro `config.yaml` contém a configuração mestre do projeto. Lá pode:
* Adicionar **novas categorias** de mercado.
* Mudar as *queries* de busca de e-mail (caso a loja mude o assunto ou e-mail de envio).
* Alterar o caminho de saída dos relatórios.

Se no futuro encontrar um item classificado de forma errada, pode editar manualmente o ficheiro `data/faturas.db` (usando um programa como o *DB Browser for SQLite*) na tabela `category_cache`, para corrigir a categoria para sempre.
