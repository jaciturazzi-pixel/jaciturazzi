# Switchover Validation Tool

Mede o impacto real de um switchover/failover num cluster MariaDB. Gera carga contínua de escrita e leitura nos endpoints enquanto o operador executa o switchover manualmente, depois valida integridade dos dados.

## Uso

```bash
python3 switchover_test.py \
  --writer-host write.stag-sql-awards.us-west-2.pool.minicliptech.com \
  --reader-host read.stag-sql-awards.us-west-2.pool.minicliptech.com \
  --port 3306 \
  --user admin \
  --password 'siNSp2314skSmf.sk!s3' \
  --threads 5 \
  --interval 0.05 \
  --skip-ssl
```

1. Inicia o script
2. Executa o switchover (DNS flip, VIP change, failover manual, etc.)
3. Espera o serviço estabilizar
4. `Ctrl+C` para parar e gerar o relatório

## O que valida

| Validação | Descrição | Critério |
|-----------|-----------|----------|
| **Downtime do writer** | Tempo entre primeira falha e recovery da escrita | Duração real sem escrita |
| **Downtime do reader** | Idem para o endpoint de leitura | Normalmente menor com LB |
| **Data loss** | SEQ commitados pelo client vs o que está no DB | Commit confirmado mas ausente = dado perdido |
| **Gaps na sequência** | Buracos na numeração sequencial | Writes que falharam durante switchover |
| **Phantom writes** | Rows no DB sem confirmação de commit ao client | Edge case de replicação async |
| **Split-brain** | Conta transições de `@@server_id` na sequência | 1 transição = limpo, >1 = split-brain |
| **Read consistency** | Compara MAX(seq) writer vs reader pós-switchover | Divergência = replication lag |

## Cenários suportados

### Master-Master (active-passive)
- Writer endpoint = DNS/VIP que flip para o novo master
- Reader endpoint = LB ou DNS do(s) slave(s)
- Split-brain detection funciona: detecta se writes saltaram entre nós

### Failover (promote replica)
- Writer endpoint = DNS/VIP que aponta para o master; após failover aponta para a replica promovida
- Reader endpoint = LB com os slaves restantes
- Sem risco de split-brain (master morreu, só um nó aceita writes)

### Reader atrás de Load Balancer
- Funciona sem alteração — testa exactamente o que a aplicação vê
- Reader downtime ~0 se LB tiver backends saudáveis durante o switchover
- Read consistency pode falhar temporariamente se algum nó atrás do LB tiver lag

## Output

- **Terminal**: relatório PASS/FAIL com métricas detalhadas
- **JSON**: ficheiro `switchover_report_<uuid>.json` no directório corrente

## Veredicto

PASS se:
- Zero data loss
- Sem split-brain
- Reader consistente com writer
- Writer downtime < 30s

FAIL com lista dos problemas detectados.

## Requisitos

- Python 3.8+
- `mysql-connector-python`
- O `--writer-host` e `--reader-host` devem ser endpoints que flutuam (DNS/VIP/LB), não IPs fixos de nós que vão morrer
