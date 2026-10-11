# Especificação — Reservaí

Fonte da verdade para o comportamento do sistema (ver checklist do template de PR).
Resume o PDF entregue no Marco 1 (`Marco1_NoConflict_Taguatinga.pdf`), já com as
correções identificadas depois da entrega. A justificativa completa (CAP, ACID/BASE,
sharding e replicação) está no PDF e será revisada na documentação final do Marco 2.

## 1. Requisitos

| ID   | Requisito | Onde está implementado |
|------|-----------|------------------------|
| RF01 | Grade de disponibilidade do dia em tempo quase real | `ServicoReservas.grade`, `GET /espacos/{id}/disponibilidade` |
| RF02 | Reservar um slot, sem que dois usuários fiquem com o mesmo (espaço, data, horário) | `ServicoReservas.reservar`, `POST /reservas` |
| RF03 | Cancelar uma reserva, liberando o horário | `ServicoReservas.cancelar`, `POST /reservas/cancelar` |
| RF04 | Histórico de reservas por usuário | `GET /usuarios/{id}/reservas` |
| RF05 | Histórico de reservas por unidade (painel administrativo) | `GET /unidades/{id}/relatorio?mes=AAAA-MM` |
| RF06 | No máximo 2 reservas futuras ativas por usuário | script `app/lua/reservar_cota.lua` |
| RF07 | Ranking dos espaços mais reservados por unidade | `GET /unidades/{id}/ranking` |

## 2. Regras de negócio

| ID   | Regra |
|------|-------|
| RN01 | Slots de 1h, começando na hora cheia, dentro do funcionamento do espaço (padrão 08:00–20:00, último slot 19:00). |
| RN02 | Só é possível reservar horários futuros, até `dias_agenda_aberta` (7) dias à frente. |
| RN03 | Uma reserva conta na cota do usuário até o horário de início; depois disso, libera a vaga da cota. |
| RN04 | Só o dono cancela, e apenas antes do horário de início. |
| RN05 | Reserva cancelada continua no histórico do usuário e da unidade com `status = 'cancelada'`. |

## 3. Modelo de dados

### 3.1 Redis (Chave-Valor) — camada quente

| Chave | Estrutura | Uso |
|-------|-----------|-----|
| `lock:slot:{espaco}:{data}:{HH:MM}` | String, `SET NX EX 30` | Lock do slot durante a decisão. Liberado por Lua só pelo dono. |
| `disponibilidade:espaco:{espaco}:{data}` | Hash `HH:MM -> livre/ocupado`, TTL 300 s | Cache da grade (RF01). Não é fonte de verdade. |
| `ativas:usuario:{usuario}` | Sorted Set, score = início do slot | Cota de reservas futuras (RF06). |
| `ranking:espacos:unidade:{unidade}` | Sorted Set, `ZINCRBY` | Ranking (RF07). |
| `metricas:reservas` | Hash de contadores | Onde cada tentativa parou (usado na demo). |

### 3.2 Cassandra (Wide-Column) — camada durável

Schema completo em [`schema/cassandra.cql`](../schema/cassandra.cql).

| Tabela | Partition key | Clustering | Consulta |
|--------|---------------|------------|----------|
| `reservas_por_espaco` | `(espaco_id, data_reserva)` | `horario ASC` | A: ocupação de um espaço no dia. Só reservas ativas; escrita sempre via LWT. |
| `reservas_por_usuario` | `usuario_id` | `data_reserva DESC, horario DESC, reserva_id` | B: histórico do usuário. |
| `reservas_por_unidade` | `(unidade_id, ano_mes)` | `data_reserva, espaco_id, horario, reserva_id` | C: ocupação mensal da unidade. |

Consistência por operação: LWT (`SERIAL` + `QUORUM`) na ocupação, que é CP; `ONE` no histórico, no painel e no cache, que é AP.

### 3.3 MongoDB (Documentos) — catálogo

Coleção `espacos`. Atributos e regras variam por tipo de espaço (quadra, sala de estudo,
sala de reunião), o que justifica o esquema flexível. Seed em [`schema/mongo-seed.js`](../schema/mongo-seed.js).

## 4. Fluxos

### Reserva (RF02 + RF06)
1. Redis: `SET lock NX EX 30`. Se falhar, retorna **409** (barrada no lock).
2. Redis: script Lua da cota. Se o limite foi atingido, retorna **409**.
3. Cassandra: `INSERT ... IF NOT EXISTS` em `reservas_por_espaco`. Se `[applied] = false` ou se houver erro, devolve a cota (`ZREM`) e retorna **409** ou propaga o erro.
4. Cassandra: BATCH logged em `reservas_por_usuario` e `reservas_por_unidade`.
5. Redis: marca o slot como `ocupado` no cache (se ele existir), `ZINCRBY` no ranking e libera o lock (sempre, em `finally`).

### Cancelamento (RF03)
1. Cassandra: lê a ocupação com `SERIAL`. A leitura é necessária porque o `reserva_id` faz parte da PK do histórico.
2. Cassandra: `DELETE ... IF usuario_id = ? AND reserva_id = ?`.
3. Cassandra: BATCH logged com `status = 'cancelada'` nas duas tabelas de histórico.
4. Redis: `ZREM` na cota e marca o slot como `livre` no cache.

## 5. Correções em relação ao PDF do Marco 1

1. **`reservas_por_unidade` agora tem `reserva_id` na clustering key.** Sem ele, cancelar e reservar de novo o mesmo slot fazia upsert na linha cancelada, apagando o cancelamento (contradizia a RN05).
2. **O cancelamento lê a ocupação antes do DELETE**, para obter o `reserva_id` exigido pelo UPDATE do histórico. A LWT de remoção também confere o `reserva_id`.
3. **Seção 2.2c**: a estrutura da cota é **Sorted Set**, e não String.
4. **Seção 3.6**: `criado_em` não é clustering column em nenhuma tabela. As clustering columns são `horario`, `data_reserva` e `reserva_id`.
5. **Pág. 12**: o texto entre o fim da seção 3.5 e o título da 3.6 está corrompido no PDF.
6. **A cache só é atualizada se já existir**, para não criar uma grade parcial que esconderia os outros slots.
