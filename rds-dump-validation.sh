(
  echo "Iniciando o dump do Aurora v2 (Origem)..."
  mysqldump -h localhost -u admin -p'rootroot' -P 3355 \
    --single-transaction --no-create-info --skip-extended-insert \
    --compact --skip-comments \
    --hex-blob --order-by-primary \
    clubs > dump_v2.sql &
  PID1=$!

  echo "Iniciando o dump do Aurora v3 (Destino)..."
  mysqldump -h localhost -u admin -p'rootroot' -P 5555 \
    --single-transaction --no-create-info --skip-extended-insert \
    --compact --skip-comments\
    --hex-blob --order-by-primary \
    clubs > dump_v3.sql &
  PID2=$!

  echo "Os dois dumps estão rodando em background (PIDs: $PID1 e $PID2)."
  echo "Aguardando a conclusão de ambos..."
  
  # O comando wait trava o terminal até que os dois processos terminem
  wait $PID1 $PID2
  echo "Dumps finalizados com sucesso! Prontos para o diff."
)