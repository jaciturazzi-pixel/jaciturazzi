# mariadb-runner

Biblioteca Python reutilizável para executar queries e varreduras em bases MariaDB/MySQL de forma simples e paralela.

Três modos de uso:
- **Query simples** — executa e retorna todas as linhas
- **Queries paralelas** — N queries independentes ao mesmo tempo, resultado em ordem
- **Chunked scan** — varre uma tabela inteira em chunks paralelos por range de coluna, sem full scan sequencial

---

## Instalação

```bash
pip install -e /path/to/jaciturazzi/mariadb-runner/
```

A partir daí, `from mariadb_runner import MariaDBRunner` funciona em qualquer projeto que use o mesmo Python/venv.

---

## Como funciona

### Chunked scan

O método mais importante. Divide a tabela em intervalos numéricos baseados em qualquer coluna indexada (PK simples, componente de PK composta, ou qualquer outro índice numérico) e executa os chunks em paralelo.

Padrão de range espelhado do job Glue:
```
WHERE col > %(lower)s AND col <= %(upper)s
```

Cada chunk roda numa conexão própria com sua própria thread. O número de threads simultâneas é controlado por `workers`.

### Backpressure

Antes de cada query, o runner lê `Threads_running` do servidor. Se estiver acima do threshold (padrão: 50), pausa 3 segundos e verifica de novo. Evita sobrecarregar o banco em automações longas.

### Read Uncommitted

Por padrão, cada sessão roda com `READ UNCOMMITTED`. Elimina lock contention em leituras analíticas. Pode ser desligado via `read_uncommitted=False`.

---

## Uso

### Setup

```python
from mariadb_runner import MariaDBRunner

runner = MariaDBRunner(
    host="sql.example.com",
    user="jaci",
    password="...",
    db="main",
    workers=4,          # threads paralelas (padrão: 4)
    chunk_size=100_000, # linhas por chunk (padrão: 100k)
)
```

---

### Query simples

```python
rows = runner.execute(
    "SELECT user_id, email FROM user_profile WHERE country = %s",
    ("PT",)
)
# rows → list[dict]
```

---

### Queries paralelas independentes

Útil para agregar métricas de várias tabelas de uma vez.

```python
results = runner.parallel_execute([
    ("SELECT COUNT(*) AS c FROM orders WHERE status = %s", ("open",)),
    ("SELECT MAX(user_id) AS m FROM user_profile", None),
    ("SELECT SUM(amount) AS total FROM payments WHERE year = %s", (2025,)),
])

open_orders  = results[0][0]["c"]
max_user     = results[1][0]["m"]
total_amount = results[2][0]["total"]
```

---

### Chunked scan — PK simples

```python
rows = runner.chunked_scan(
    table="orders",
    partition_col="order_id",
    sql=(
        "SELECT {partition_col}, user_id, status, total "
        "FROM {table} "
        "WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s"
    ),
)
```

---

### Chunked scan — PK composta

Para tabelas com PK composta `(user_id, item_id, created_at)`, escolhe qualquer componente numérico como coluna de partição.

```python
rows = runner.chunked_scan(
    table="user_items",
    partition_col="user_id",   # componente da PK composta
    index_hint="PRIMARY",      # força uso do índice primário no MIN/MAX
    sql=(
        "SELECT user_id, item_id, created_at, quantity "
        "FROM {table} "
        "WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s"
    ),
    chunk_size=50_000,
    workers=6,
)
```

---

### Chunked scan — streaming para CSV (sem acumular memória)

Para tabelas com bilhões de linhas, usar `on_chunk` evita segurar todas as linhas em memória.

```python
import csv

with open("output.csv", "w", newline="") as f:
    writer = None

    def write(chunk_rows):
        nonlocal writer
        if not chunk_rows:
            return
        if writer is None:
            writer = csv.DictWriter(f, fieldnames=chunk_rows[0].keys())
            writer.writeheader()
        writer.writerows(chunk_rows)

    runner.chunked_scan(
        table="user_profile",
        partition_col="user_id",
        sql=(
            "SELECT {partition_col}, email, country "
            "FROM {table} "
            "WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s"
        ),
        on_chunk=write,
    )
```

---

## Parâmetros do construtor

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `host` | — | Host MariaDB/MySQL |
| `user` | — | Usuário |
| `password` | — | Senha |
| `db` | — | Base de dados |
| `workers` | `4` | Threads paralelas |
| `chunk_size` | `100_000` | Linhas por chunk no `chunked_scan` |
| `backpressure_threshold` | `50` | Pausa se `Threads_running` ultrapassar este valor |
| `read_uncommitted` | `True` | Define `READ UNCOMMITTED` na sessão |
| `connect_timeout` | `15` | Timeout de conexão em segundos |

## Placeholders do SQL no `chunked_scan`

| Placeholder | Substituído por |
|---|---|
| `{table}` | nome da tabela entre backticks |
| `{partition_col}` | coluna de partição entre backticks |
| `%(lower)s` | limite inferior exclusivo do chunk |
| `%(upper)s` | limite superior inclusivo do chunk |

---

## Estender para outros scripts

O padrão recomendado é herdar `MariaDBRunner` e adicionar o comportamento específico do script, sem re-implementar conexão, backpressure ou chunking.

```python
from mariadb_runner import MariaDBRunner

class MyReport(MariaDBRunner):
    def run(self):
        rows = self.chunked_scan(
            table="my_table",
            partition_col="id",
            sql="SELECT {partition_col}, col_a FROM {table} WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s",
        )
        # processa rows...
```
