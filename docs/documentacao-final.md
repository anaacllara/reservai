# Reservaí: documentação final (Marco 2)

**Sistema de reserva de espaços compartilhados com persistência poliglota NoSQL**

Grupo **NoConflict** · Campus Taguatinga · Bancos de Dados NoSQL: Persistência e Distribuição de Big Data

Integrantes: Ana Clara Freitas de Carvalho · Douglas Ramos Jordão · Arthur Gabriel da Silva Barbosa · Mariana Rodrigues Sena

Repositório: <https://github.com/anaacllara/reservai>

---

## Sumário

1. O problema
2. Arquitetura
3. Modelagem dos dados por família
4. Justificativa tecnológica
5. Implementação
6. Validação e evidências
7. Evolução em relação ao Marco 1
8. Limitações e trabalhos futuros
9. Lições aprendidas
10. Uso de ferramentas de IA

---

## 1. O problema

### 1.1 Contexto

O Reservaí permite que alunos e frequentadores de um campus universitário reservem espaços de uso
coletivo (quadras esportivas, salas de estudo em grupo e salas de reunião de um coworking parceiro)
por um horário específico. Hoje essa reserva é feita por planilha e grupo de WhatsApp, o que gera o
problema que dá nome ao grupo: duas pessoas "garantem" o mesmo horário e só descobrem o conflito ao
chegar no local.

No Marco 1, o escopo era o núcleo do sistema: consultar a disponibilidade e confirmar uma reserva sem
conflito, com Chave-Valor e Wide-Column. No Marco 2, o sistema foi completado com as duas famílias das
unidades seguintes:

- **Documentos (MongoDB)** para o catálogo de espaços, que o Marco 1 já previa como "dado semiestruturado";
- **Grafos (Neo4j)** para os relacionamentos entre pessoas e espaços, que passaram a existir quando a
  reserva ganhou participantes.

### 1.2 Requisitos

| ID | Requisito | Família principal |
|----|-----------|-------------------|
| RF01 | Grade de disponibilidade do dia em tempo quase real | Redis |
| RF02 | Reservar um slot sem que dois usuários fiquem com o mesmo (espaço, data, horário) | Redis + Cassandra |
| RF03 | Cancelar uma reserva, liberando o horário | Cassandra |
| RF04 | Histórico de reservas por usuário | Cassandra |
| RF05 | Relatório mensal por unidade, com taxas de ocupação e cancelamento por espaço | Cassandra |
| RF06 | No máximo 2 reservas futuras ativas por usuário | Redis |
| RF07 | Ranking dos espaços mais reservados por unidade | Redis |
| RF08 | Recomendar espaços usados por quem divide espaços com o usuário | Neo4j |
| RF09 | Sugerir parceiros: amigos de amigos com quem o usuário nunca dividiu uma reserva | Neo4j |
| RF10 | Buscar espaços por qualquer atributo do tipo (capacidade, modalidade, recursos) | MongoDB |
| RF11 | Resumo do catálogo: espaços por unidade e tipo, capacidade e modalidades | MongoDB |

RF01 a RF07 vêm do Marco 1. RF08 a RF11 são do Marco 2.

| ID | Regra de negócio |
|----|------------------|
| RN01 | Slots de 1 h, na hora cheia, dentro do funcionamento do espaço (padrão 08:00–20:00). |
| RN02 | Só é possível reservar horários futuros, até 7 dias à frente. |
| RN03 | A reserva conta na cota do dono até o horário de início. |
| RN04 | Só o dono cancela, e apenas antes do início. |
| RN05 | Reserva cancelada continua nos históricos com `status = 'cancelada'`. |
| RN06 | Até 30 participantes além do dono; participantes não consomem cota. |
| RN07 | Reservar e cancelar exigem sessão; dados pessoais só pelo próprio usuário ou pelo `admin`; relatório só `admin`. |
| RN08 | Regras do espaço (ex.: mínimo de 6 jogadores) e capacidade são validadas antes do lock. |

### 1.3 Estimativa de volume

Os números de reservas são os do Marco 1. As linhas novas cobrem as famílias do Marco 2; quando um
valor é uma suposição do grupo, isso está indicado.

| Métrica | Valor estimado |
|---------|----------------|
| Unidades parceiras | 8 |
| Espaços reserváveis | 50 (~6 por unidade) |
| Slots disponíveis | 600/dia (50 × 12); 4.200 na agenda aberta de 7 dias |
| Usuários ativos | 3.000 no mês 1, +25% por trimestre (~7.300 ao fim do 1º ano) |
| Reservas confirmadas | ~240/dia (40% de ocupação), ~87.600 no 1º ano |
| Escrita fora do pico | ~0,0056 reservas/s |
| **Pico de abertura da agenda (segunda, 00h00)** | ~1.200 usuários × ~5 tentativas em ~2 min ≈ **50 tentativas/s**, com rajadas de **150 a 200/s** |
| Linhas no Cassandra no 1º ano | ~263.000 (cada reserva em 3 tabelas) |
| Documentos no MongoDB | 50 espaços, ~1 KB cada (**~50 KB**), alterados raramente |
| Nós no Neo4j no 1º ano | ~7.300 usuários + 50 espaços + 8 unidades + ~80.000 reservas ativas |
| Arestas no Neo4j no 1º ano | ~400.000, supondo em média 3 participantes por reserva (suposição) |
| Memória do Redis no pico | ~1.200 sessões (~200 B), locks transitórios, ~400 hashes de grade: **poucos MB** |

O ponto central continua sendo o do Marco 1. O volume total é modesto, mas a concorrência de escrita
no pico é cerca de 9.000 vezes a média do resto do dia, e concentrada em poucos slots populares.
O sistema precisa absorver essa rajada sem deixar dois usuários vencerem o mesmo slot.

---

## 2. Arquitetura

### 2.1 Visão geral

```text
                     ┌────────────────────────────────────────────┐
  Navegador ───────► │ API FastAPI (app/)  +  front em /          │
                     └──┬──────────┬──────────┬──────────┬────────┘
                        │          │          │          │ (fila em 2º plano)
                        ▼          ▼          ▼          ▼
                  ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌─────────┐
                  │  Redis  │ │Cassandra │ │ MongoDB │ │  Neo4j  │
                  │ Chave-  │ │  Wide-   │ │Documen- │ │ Grafos  │
                  │  Valor  │ │  Column  │ │  tos    │ │         │
                  └─────────┘ └──────────┘ └─────────┘ └─────────┘
                   camada      fonte de     catálogo     projeção
                   quente      verdade das  (fonte de    das reservas
                   (soft       reservas     verdade dos  para
                   state)                   espaços)     travessias
```

| Família | Banco | Papel | É fonte de verdade? |
|---------|-------|-------|---------------------|
| Chave-Valor | Redis 7 | Lock do slot, cache da grade, cota, ranking, sessão, contadores | Não: estado reconstruível |
| Wide-Column | Cassandra 5 | Ocupação dos slots (LWT) e históricos | **Sim, das reservas** |
| Documentos | MongoDB 7 | Catálogo de espaços, atributos e regras | **Sim, do catálogo** |
| Grafos | Neo4j 5 | Quem usou qual espaço junto com quem | Não: projeção reconstruível |

### 2.2 Fluxo de reserva

```text
POST /reservas  {espaco, data, horario, participantes}   (usuário vem do token)
 │
 ├─ 0. MongoDB   lê o espaço: funcionamento, regras e capacidade (RN08)
 │               regra violada → 422, sem tocar nos outros bancos
 ├─ 1. Redis     SET lock:slot:… NX EX 30          ocupado → 409 "barrada no lock"
 ├─ 2. Redis     Lua: cota (ZREMRANGEBYSCORE+ZCARD+ZADD)   cheia → 409 "barrada na cota"
 ├─ 3. Cassandra INSERT … IF NOT EXISTS (LWT/Paxos)  já existe → devolve a cota, 409
 ├─ 4. Cassandra BATCH logged nos dois históricos
 ├─ 5. Redis     atualiza a grade (se em cache), ZINCRBY no ranking, libera o lock (finally)
 └─ 6. Neo4j     projeta a reserva, em segundo plano, depois de liberar o lock
```

Cada passo que pode falhar depois de outro já ter escrito tem uma **compensação**: se a LWT falhar,
a cota é devolvida (`ZREM`); o lock é liberado em `finally` e, no pior caso, expira em 30 s. Não há
transação distribuída entre os bancos. A ordem dos passos foi escolhida para que qualquer falha
deixe o sistema num estado seguro, ou seja, sem dois donos para o mesmo slot.

### 2.3 Fluxo de cancelamento

1. O `usuario_id` vem da sessão no Redis (nunca do corpo da requisição).
2. Cassandra: lê a ocupação com `SERIAL`, para obter o `reserva_id`.
3. Cassandra: `DELETE … IF usuario_id = ? AND reserva_id = ?` (LWT: só o dono, e só se a reserva ainda é a mesma).
4. Cassandra: BATCH logged com `status = 'cancelada'` nos dois históricos (RN05).
5. Redis: devolve a cota e marca o slot como livre na grade.
6. Neo4j: remove o nó da reserva, em segundo plano.

---

## 3. Modelagem dos dados por família

### 3.1 Redis (Chave-Valor)

| Chave | Estrutura | Uso | TTL |
|-------|-----------|-----|-----|
| `lock:slot:{espaco}:{data}:{HH:MM}` | String, `SET NX EX` | Lock do slot durante a decisão | 30 s |
| `disponibilidade:espaco:{espaco}:{data}` | Hash `HH:MM → livre/ocupado` | Cache da grade (RF01) | 300 s |
| `ativas:usuario:{usuario}` | Sorted Set, score = início do slot | Cota de reservas futuras (RF06) | — |
| `ranking:espacos:unidade:{unidade}` | Sorted Set, `ZINCRBY` | Ranking (RF07) | — |
| `sessao:{token}` | Hash `usuario_id, nome, email, perfil` | Sessão (RN07), renovada a cada uso | 3.600 s |
| `metricas:reservas` | Hash de contadores | Onde cada tentativa parou | — |

Dois scripts Lua garantem atomicidade sem transação:

```lua
-- reservar_cota.lua: checa e consome a cota numa operação só
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", ARGV[1])   -- reservas que já começaram saem
if redis.call("ZCARD", KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
redis.call("ZADD", KEYS[1], ARGV[2], ARGV[3])
return 1

-- liberar_lock.lua: só o dono do lock o libera
if redis.call("GET", KEYS[1]) == ARGV[1] then return redis.call("DEL", KEYS[1]) end
return 0
```

O Sorted Set da cota usa o **horário de início como score**. Assim, uma reserva que já aconteceu sai
do conjunto na próxima verificação (`ZREMRANGEBYSCORE`), sem depender de cancelamento ou de job de limpeza.

### 3.2 Cassandra (Wide-Column)

A modelagem é **query-first**: uma tabela por consulta, com a chave escolhida a partir do acesso.

```sql
-- Consulta A: ocupação de um espaço num dia (grade oficial). Só reservas ativas; escrita só via LWT.
CREATE TABLE reservas_por_espaco (
  espaco_id int, data_reserva date, horario time,
  reserva_id uuid, usuario_id int, unidade_id int, criado_em timestamp, participantes set<int>,
  PRIMARY KEY ((espaco_id, data_reserva), horario)
) WITH CLUSTERING ORDER BY (horario ASC);

-- Consulta B: histórico do usuário, das mais recentes às mais antigas.
CREATE TABLE reservas_por_usuario (
  usuario_id int, data_reserva date, horario time, reserva_id uuid,
  espaco_id int, unidade_id int, status text, criado_em timestamp, participantes set<int>,
  PRIMARY KEY (usuario_id, data_reserva, horario, reserva_id)
) WITH CLUSTERING ORDER BY (data_reserva DESC, horario DESC, reserva_id ASC);

-- Consulta C: ocupação mensal da unidade (painel administrativo).
CREATE TABLE reservas_por_unidade (
  unidade_id int, ano_mes text, data_reserva date, espaco_id int, horario time, reserva_id uuid,
  usuario_id int, status text, participantes set<int>,
  PRIMARY KEY ((unidade_id, ano_mes), data_reserva, espaco_id, horario, reserva_id)
) WITH CLUSTERING ORDER BY (data_reserva ASC, espaco_id ASC, horario ASC, reserva_id ASC);
```

| Tabela (column family) | Partition key | Clustering | Tamanho da partição |
|------------------------|---------------|------------|---------------------|
| `reservas_por_espaco` | `(espaco_id, data_reserva)` | `horario` | ≤ 12 linhas (um espaço num dia) |
| `reservas_por_usuario` | `usuario_id` | `data_reserva DESC, horario DESC, reserva_id` | dezenas a centenas por usuário |
| `reservas_por_unidade` | `(unidade_id, ano_mes)` | `data_reserva, espaco_id, horario, reserva_id` | ~900 linhas por mês |

Decisões que vale destacar:

- **A existência da linha em `reservas_por_espaco` é a ocupação do slot.** O `INSERT … IF NOT EXISTS`
  nessa partição é a garantia real de unicidade; todas as escritas nela passam por LWT, porque misturar
  escritas condicionais e não condicionais na mesma partição compromete as garantias do Paxos.
- **`reserva_id` está na clustering key dos históricos.** Em CQL, um `INSERT` com chave repetida é um
  upsert. Sem o `reserva_id`, cancelar e reservar de novo o mesmo slot sobrescreveria a linha
  cancelada e apagaria o cancelamento (esse era um bug do Marco 1, ver seção 7).
- **`ano_mes` na partition key de `reservas_por_unidade`** limita a partição e faz o relatório mensal
  ler uma única partição.
- **Participantes vazios são gravados como `UNSET`**, e não como `null`, para não criar um tombstone a cada reserva.

### 3.3 MongoDB (Documentos)

Coleção `espacos`, um documento por espaço. Cada tipo de espaço tem atributos próprios:

```js
{ _id: 1, unidade_id: 1, nome: "Quadra Poliesportiva", tipo: "quadra",
  funcionamento: { abertura: 8, fechamento: 20 },
  atributos: { modalidades: ["futsal", "vôlei", "basquete"], coberta: true, iluminacao: true },
  regras: [ { tipo: "min_participantes", valor: 6, descricao: "Mínimo de 6 jogadores" },
            { tipo: "informativa", descricao: "Calçado adequado obrigatório" } ] }

{ _id: 3, unidade_id: 1, nome: "Sala de Estudo A", tipo: "sala_estudo",
  funcionamento: { abertura: 8, fechamento: 20 },
  atributos: { capacidade: 6, quadro_branco: true, tomadas: 8, tv: false },
  regras: [ { tipo: "informativa", descricao: "Silêncio moderado" } ] }

{ _id: 7, unidade_id: 3, nome: "Sala de Reunião Ipê", tipo: "sala_reuniao",
  funcionamento: { abertura: 8, fechamento: 20 },
  atributos: { capacidade: 12, videoconferencia: true, projetor: true, cafe_incluso: true },
  regras: [ { tipo: "informativa", descricao: "Reserva vinculada ao convênio do coworking" } ] }
```

**Busca (RF10).** Um único `find`, com filtros sobre atributos que só existem em alguns tipos:

```js
// GET /espacos?tipo=sala_reuniao&capacidade_min=8&recursos=videoconferencia
db.espacos.find({ tipo: "sala_reuniao",
                  "atributos.capacidade": { $gte: 8 },
                  "atributos.videoconferencia": true })
// GET /espacos?modalidade=futsal  → igualdade em array: casa com qualquer elemento
db.espacos.find({ "atributos.modalidades": "futsal" })
```

O nome de cada recurso é validado (`^[a-z][a-z0-9_]*$`) antes de virar caminho na consulta, para que
o parâmetro não vire operador do Mongo (`$where`) nem caminho arbitrário no documento.

**Índices.** `{unidade_id: 1, tipo: 1}` e um **índice curinga** `{"atributos.$**": 1}`. Ele cobre
consultas em qualquer atributo, inclusive atributos de tipos de espaço que ainda não existem. O
`explain()` da busca por capacidade confirma o uso: `IXSCAN via atributos.$**_1`.

**Resumo (RF11).** Aggregation pipeline com duas visões numa ida ao banco:

```js
[ { $match: { unidade_id: 1 } }, { $sort: { _id: 1 } },
  { $facet: {
      por_tipo:    [ { $group: { _id: { unidade_id: "$unidade_id", tipo: "$tipo" },
                                 espacos: { $sum: 1 }, capacidade_total: { $sum: "$atributos.capacidade" },
                                 nomes: { $push: "$nome" } } },
                     { $sort: { "_id.unidade_id": 1, "_id.tipo": 1 } } ],
      modalidades: [ { $unwind: "$atributos.modalidades" },
                     { $group: { _id: "$atributos.modalidades", espacos: { $sum: 1 } } },
                     { $sort: { espacos: -1, _id: 1 } } ] } } ]
```

**Regras como documentos (RN08).** `{tipo, valor?, descricao}`. Um tipo de regra novo é só um
documento com outro `tipo`, sem migração de schema; versões da API que não o conhecem o ignoram.
A regra `min_participantes` e a capacidade da sala são validadas na reserva, antes do lock.

### 3.4 Neo4j (Grafos)

```text
(:Usuario)-[:RESERVOU]->(:Reserva)-[:EM]->(:Espaco)-[:FICA_EM]->(:Unidade)
(:Usuario)-[:PARTICIPOU]->(:Reserva)
```

**A reserva é um nó, e não uma aresta** `Usuario–Espaco` com contador:

- ela liga N pessoas ao mesmo evento, que é exatamente o que define "quem usou o espaço junto";
- a escrita é idempotente (`MERGE` pelo id), e o cancelamento é um `DETACH DELETE`, sem contadores
  que podem divergir se uma operação for repetida ou falhar no meio.

Restrições de unicidade em `id` nos quatro rótulos evitam nós duplicados no `MERGE` e servem de
índice para o ponto de partida das travessias. Só reservas ativas ficam no grafo.

**RF08: recomendação (filtragem colaborativa em 2 saltos).**

```cypher
MATCH (eu:Usuario {id: $usuario_id})-[:RESERVOU|PARTICIPOU]->(:Reserva)-[:EM]->(meu:Espaco)
WITH eu, collect(DISTINCT meu) AS meus
UNWIND meus AS meu
MATCH (meu)<-[:EM]-(:Reserva)<-[:RESERVOU|PARTICIPOU]-(outro:Usuario)
WHERE outro <> eu
MATCH (outro)-[:RESERVOU|PARTICIPOU]->(:Reserva)-[:EM]->(rec:Espaco)
WHERE NOT rec IN meus
RETURN rec.id AS espaco_id, count(DISTINCT outro) AS pontuacao
ORDER BY pontuacao DESC, espaco_id LIMIT $limite
```

**RF09: parceiros (amigos de amigos).** Um amigo é quem já dividiu uma reserva com o usuário. A
consulta sugere quem dividiu reserva com meus amigos mas nunca comigo, ordenado por amigos em comum.
O filtro opcional por tipo vale para o segundo salto ("quem **joga** com meus amigos", e não "quem estuda com eles"):

```cypher
MATCH (eu:Usuario {id: $usuario_id})-[:RESERVOU|PARTICIPOU]->(:Reserva)<-[:RESERVOU|PARTICIPOU]-(amigo:Usuario)
WHERE amigo <> eu
WITH eu, collect(DISTINCT amigo) AS amigos
UNWIND amigos AS amigo
MATCH (amigo)-[:RESERVOU|PARTICIPOU]->(r:Reserva)<-[:RESERVOU|PARTICIPOU]-(sugestao:Usuario)
WHERE sugestao <> eu AND NOT sugestao IN amigos
  AND ($tipo IS NULL OR EXISTS { (r)-[:EM]->(:Espaco {tipo: $tipo}) })
RETURN sugestao.id AS usuario_id, count(DISTINCT amigo) AS amigos_em_comum
ORDER BY amigos_em_comum DESC, usuario_id LIMIT $limite
```

---

## 4. Justificativa tecnológica

### 4.1 Limites do relacional

1. **Joins e locks sob concorrência concentrada.** No pico, centenas de tentativas por segundo miram os
   mesmos poucos slots. Num relacional, a grade exige cruzar espaço e reservas a cada consulta, e a
   disputa por linha gera contenção de locks num único servidor. O problema não é o tamanho da base,
   e sim a concentração temporal da carga.
2. **Transação distribuída a cada tentativa.** Confirmar uma reserva verifica e ocupa o slot
   (particionado por espaço) e, na mesma operação, verifica e consome a cota do usuário (particionada
   por usuário). Num relacional escalado horizontalmente, esses dados ficariam em shards diferentes, e
   a regra "slot único e no máximo 2 reservas" exigiria two-phase commit a cada tentativa, justamente
   o mais lento no pico. A arquitetura separa as duas verificações: a cota fica atômica no Redis (Lua)
   e a ocupação fica numa LWT restrita a uma única partição do Cassandra.
3. **Atributos heterogêneos no catálogo.** Uma quadra tem modalidades e cobertura; uma sala tem
   capacidade e tomadas; uma sala de reunião tem videoconferência. No relacional, as opções seriam uma
   tabela com dezenas de colunas quase sempre nulas, uma tabela por tipo (com `UNION` para buscar entre
   tipos) ou o modelo EAV (entidade–atributo–valor). No EAV, a busca "capacidade ≥ 8 **e**
   videoconferência" vira um self-join por atributo. No documento, é um único `find` com índice curinga.
4. **Travessias de relacionamento.** "Amigos de amigos com quem nunca joguei" é, em SQL, uma cadeia de
   self-joins sobre a tabela de participantes (usuário → reserva → amigo → reserva → sugestão), mais
   um anti-join, e o custo cresce a cada salto. No grafo, a travessia segue ponteiros a partir do nó do
   usuário (adjacência sem índice), e o custo é proporcional à vizinhança visitada, não ao tamanho
   total das tabelas.
5. **Padrão de acesso conhecido.** As consultas do sistema são conhecidas desde a concepção, o que
   favorece a modelagem query-first em vez de tabelas normalizadas otimizadas para consultas ad hoc
   que o sistema não faz.

### 4.2 Teorema CAP: a escolha é por operação

A tolerância a partição é obrigatória num sistema distribuído real, então a escolha é entre consistência
e disponibilidade. O Reservaí faz essa escolha **por operação**, não para o sistema inteiro:

| Operação | Escolha | Mecanismo | O que acontece numa falha |
|----------|---------|-----------|---------------------------|
| Confirmar reserva (RF02) | **CP** | LWT `SERIAL` (Paxos) + commit `QUORUM`, RF = 3 | Com 1 nó fora, continua confirmando (2 de 3); com 2 fora, recusa a reserva em vez de arriscar dois donos |
| Cancelar (RF03) | **CP** | LWT condicional + leitura `SERIAL` | Igual à confirmação |
| Grade de disponibilidade (RF01) | **AP** | Cache Redis; reconstrução com `ONE` | Pode mostrar um slot como livre por alguns segundos; o conflito é barrado na confirmação |
| Histórico e relatório (RF04, RF05) | **AP** | Leitura com `ONE` | Pode atrasar alguns segundos em relação à última reserva |
| Catálogo (RF10, RF11) | **AP na leitura** | Dado quase estático; em produção, leitura em secundários do replica set | Leitura continua com o primário fora; a escrita (rara) espera a eleição |
| Recomendação e parceiros (RF08, RF09) | **AP / best-effort** | Projeção assíncrona no Neo4j | A reserva não é afetada; as rotas do grafo respondem 503 |
| Sessão | **AP (soft state)** | Hash com TTL no Redis | Na pior hipótese, o usuário faz login de novo |

O argumento para a ilha de CP: um "empate" entre dois usuários tem consequência no mundo físico (dois
grupos chegando para a mesma quadra), e isso é pior do que responder "não foi possível confirmar,
tente outro horário" durante uma indisponibilidade. Em todo o resto, mostrar um dado alguns segundos
defasado é melhor do que a tela falhar.

**Evidência medida.** Com o Neo4j desligado no meio da execução, a reserva continuou respondendo
**201 em 0,03 a 0,11 s**, a falha foi contada em `falhas_no_grafo`, e as rotas do grafo responderam
503. Na primeira versão, a projeção era síncrona e a reserva levava **4 a 5 s** esperando o driver
desistir. Esse teste de falha foi o que levou à fila em segundo plano (seção 9).

### 4.3 ACID vs. BASE

O sistema segue **predominantemente o modelo BASE**, com garantias fortes aplicadas de forma
cirúrgica onde o dado não tolera inconsistência:

- **Soft state:** cache da grade, sessão, ranking e contadores no Redis expiram ou podem ser
  reconstruídos; nenhum deles é fonte de verdade.
- **Eventual consistency:** réplicas do histórico (`ONE`) e a projeção no grafo convergem em segundos.
- **Garantia forte só na ocupação do slot:** a LWT dá um comportamento semelhante a atomicidade e
  isolamento para aquela partição, ao custo de 4 idas e voltas do Paxos. Esse custo é pago só nessa operação.
- **Atomicidade local onde basta:**
  - o script Lua da cota é atômico porque o Redis executa um script por vez;
  - no MongoDB, cada espaço é **um documento**, e escrita num documento é atômica. O modelo foi
    desenhado para nunca precisar de transação multi-documento;
  - no Neo4j, cada projeção é uma transação ACID: o subgrafo da reserva aparece inteiro ou não aparece.
- **Entre bancos, não há transação distribuída.** A consistência entre eles vem da ordem das operações e
  de compensações (devolver a cota se a LWT falhar; lock com TTL). É um raciocínio próximo de uma saga.

Pagamentos de taxas, que exigiriam ACID amplo (estorno em caso de cancelamento), continuam fora do escopo.

### 4.4 Sharding e replicação

| Banco | Particionamento | Replicação | Por que é compatível com o volume |
|-------|-----------------|------------|-----------------------------------|
| **Redis** | Redis Cluster por **hash slots**: CRC16(chave) mod 16.384; cada master responde por uma faixa | **Master-Slave** assíncrona; réplicas atendem leituras da grade | Espalha o tráfego de lock e cache do pico entre masters |
| **Cassandra** | **Hash** por Consistent Hashing (Murmur3 + vnodes), o mesmo princípio do Dynamo | **Peer-to-peer (Master-Master)**, RF = 3, `NetworkTopologyStrategy` | 263 mil linhas/ano não exigem capacidade; o RF 3 é para disponibilidade no pico |
| **MongoDB** | **Sem sharding**: 50 documentos (~50 KB) | **Replica set** (1 primário + 2 secundários, eleição automática, oplog): modelo **Master-Slave** | O catálogo inteiro cabe na memória de qualquer nó |
| **Neo4j** | **Sem sharding**: ~90 mil nós e ~400 mil arestas | Cluster com primários (consenso **Raft**) e réplicas de leitura secundárias | O grafo cabe numa máquina, e particioná-lo quebraria a localidade das travessias |

Pontos que vale destacar:

- **Os scripts Lua tocam uma única chave cada.** Isso é compatível com o Redis Cluster, que recusa
  scripts com chaves em hash slots diferentes (`CROSSSLOT`).
- **O lock do Redis é filtro, não garantia.** Como a replicação é assíncrona, um master pode cair logo
  depois de conceder um lock, e a réplica promovida pode não tê-lo recebido. Por isso a decisão final
  sempre passa pela LWT no Cassandra.
- **O lock também protege o Cassandra.** O hashing distribui partições diferentes, mas não divide a mesma
  partição: todas as tentativas pelo mesmo slot popular caem nela, e LWTs concorrentes disputam rodadas
  de Paxos (latência e timeouts). Com o lock, a LWT recebe em regra um candidato por vez. Na demo,
  160 de 200 tentativas foram barradas em memória, no lock.
- **Se o catálogo crescesse a ponto de exigir sharding,** a shard key natural seria `unidade_id`, já que
  as buscas são quase sempre por unidade e virariam consultas direcionadas a um shard. O mapa de chunks
  nos config servers do MongoDB é, na prática, um particionamento directory-based sobre faixas.
- **Grafos são difíceis de particionar:** cada aresta cortada entre máquinas transforma um salto local
  num salto de rede. Com o volume estimado, escalar leitura com réplicas é a estratégia adequada.
- Em desenvolvimento, cada banco roda como nó único no `docker-compose.yml` (Cassandra com RF = 1).
  A configuração de produção acima é a proposta para o volume estimado.

### 4.5 Por família: uso do modelo e técnica

**Chave-Valor (Unidade 2).**
- *Uso:* lock (dado efêmero de decisão única), cache da grade (leitura de altíssima frequência),
  contador de cota, ranking e sessão, os casos clássicos do modelo.
- *Técnicas:* `SET NX EX` (String com expiração), Hash com TTL, Sorted Set com `ZADD`,
  `ZREMRANGEBYSCORE` e `ZCARD` (cota com expiração natural pelo score) e com `ZINCRBY` (ranking),
  scripts Lua atômicos e Consistent Hashing (no anel do Cassandra).

**Wide-Column (Unidade 3).**
- *Uso:* durabilidade e histórico que cresce continuamente, organizado pelas três consultas do sistema.
- *Técnicas:* modelagem query-first, column families (uma tabela por consulta), partition keys compostas
  que limitam o tamanho da partição, clustering columns que ordenam fisicamente na ordem da consulta,
  lightweight transactions (Paxos), BATCH logged e nível de consistência por operação.

**Documentos.**
- *Uso:* catálogo de espaços com atributos e regras que variam por tipo.
- *Técnicas:* esquema flexível (um documento por espaço, só com os atributos que fazem sentido),
  consultas sobre campos aninhados e arrays, índice composto, **índice curinga**, aggregation pipeline
  (`$match`, `$facet`, `$group`, `$unwind`) e atomicidade por documento.

**Grafos.**
- *Uso:* relações entre pessoas, reservas e espaços, para recomendação e sugestão de parceiros.
- *Técnicas:* modelo de grafo de propriedades, reserva como nó (hiperaresta reificada), Cypher com
  padrões de vários saltos, `MERGE` idempotente, restrições de unicidade e filtragem colaborativa.

### 4.6 Por que combinar quatro famílias

Cada família resolve bem uma parte que as outras resolvem mal (persistência poliglota):

- O **Redis** dá a velocidade de decisão em memória para absorver o pico.
- O **Cassandra** dá durabilidade, distribuição sem ponto único de falha e a única verificação forte do sistema.
- O **MongoDB** dá flexibilidade para um catálogo heterogêneo que muda de forma, mas não de volume.
- O **Neo4j** responde perguntas de relacionamento que seriam cadeias de joins em qualquer outra família.

O custo da combinação é manter a coerência entre os bancos. A regra adotada é ter **uma fonte de
verdade por tipo de dado**: Cassandra para reservas e MongoDB para o catálogo. Redis e Neo4j guardam
estado derivado que pode ser reconstruído:

- grade a partir de `reservas_por_espaco`;
- cota a partir de `reservas_por_usuario`;
- grafo a partir de `reservas_por_unidade`.

---

## 5. Implementação

### 5.1 Tecnologias

| Camada | Tecnologia |
|--------|------------|
| API | Python 3.11, FastAPI, Pydantic |
| Drivers | `redis-py`, `cassandra-driver`, `pymongo`, `neo4j` |
| Bancos | Redis 7.4, Cassandra 5.0, MongoDB 7.0, Neo4j 5.26 (Community) |
| Front da demo | HTML + JavaScript puro, servido pela própria API em `/` |
| Infraestrutura | Docker Compose (5 serviços + inicialização do schema CQL) |
| Testes | pytest, fakeredis, fakes em memória; suíte de integração contra os bancos reais |
| CI | GitHub Actions: job de testes unitários e job de integração com Docker |

### 5.2 Estrutura do código

```text
app/
  api.py              rotas FastAPI
  auth.py             login de demonstração e sessão no Redis
  servicos.py         regras de negócio: fluxos de reserva e cancelamento, validações
  dominio.py          entidades, erros e interfaces dos repositórios
  chaves.py           nomes das chaves do Redis
  lua/                scripts atômicos do Redis
  repositorios/       cassandra.py, mongo.py, grafo.py (Neo4j)
  static/index.html   front da demo
schema/               CQL do Cassandra e seed do MongoDB
scripts/              demo de disputa por slot e espera dos bancos no CI
tests/                testes unitários, fakes e integração
docs/                 especificação (marco1.md) e esta documentação
```

O serviço de negócio depende só de interfaces (`RepositorioReservas`, `CatalogoEspacos`,
`GrafoRelacionamentos`). Isso permite testá-lo com fakes em memória e trocar a implementação sem tocar nas regras.

### 5.3 API

| Método e rota | Requisito | Acesso |
|---------------|-----------|--------|
| `POST /auth/login`, `POST /auth/logout` | sessão | público / sessão |
| `GET /espacos?tipo=&capacidade_min=&modalidade=&recursos=` | RF10 | público |
| `GET /espacos/resumo` | RF11 | público |
| `GET /espacos/{id}` | catálogo | público |
| `GET /espacos/{id}/disponibilidade?data=` | RF01 | público |
| `POST /reservas` | RF02, RF06, RN06, RN08 | sessão |
| `DELETE /espacos/{id}/reservas/{data}/{horario}` | RF03 | sessão (dono) |
| `GET /usuarios/{id}/reservas` | RF04 | o próprio usuário ou admin |
| `GET /usuarios/{id}/recomendacoes` | RF08 | o próprio usuário ou admin |
| `GET /usuarios/{id}/parceiros?tipo=` | RF09 | o próprio usuário ou admin |
| `GET /unidades/{id}/relatorio?mes=AAAA-MM` | RF05 | admin |
| `GET /unidades/{id}/ranking` | RF07 | público |
| `GET /metricas` | demo | público |

A rota de cancelamento carrega na URL a partition key de `reservas_por_espaco`: a rota foi desenhada
a partir da tabela, coerente com a modelagem query-first.

### 5.4 Como executar

```bash
docker compose up -d --build        # ~1 min no primeiro start do Cassandra
# http://localhost:8000        front da demo (usuário 842, senha senha123; usuário 1 = admin)
# http://localhost:8000/docs   Swagger
# http://localhost:7474        Neo4j Browser (neo4j / reservai-dev)
python scripts/disputa_slot.py --tentativas 200 --espaco 1 --horario 19:00
```

---

## 6. Validação e evidências

### 6.1 Testes automatizados

| Suíte | O que cobre | Resultado |
|-------|-------------|-----------|
| Unitários (`pytest -m "not integracao"`) | fluxos de reserva e cancelamento, cota, lock, regras do catálogo, busca, resumo, grafo, autenticação e controle de acesso, falhas simuladas | **96 testes** |
| Integração (`pytest -m integracao`) | os mesmos fluxos contra Redis, Cassandra, MongoDB e Neo4j reais no `docker compose`, incluindo concorrência | **7 testes**, em job próprio do CI |

Os fakes (Cassandra, MongoDB e Neo4j em memória) reproduzem a semântica que importa: LWT atômica e
upsert por chave primária no Cassandra, igualdade em array e `$gte` com campo ausente no Mongo,
travessias do Cypher no Neo4j. Para confiar neles, o comportamento foi **comparado com os bancos reais**:
14 filtros de busca e 3 resumos devolveram resultados idênticos no MongoDB e no fake.

### 6.2 Demo de concorrência

200 usuários, todos já logados, disputam o mesmo slot da Quadra Poliesportiva (com os 5 participantes
que a regra exige), disparados juntos:

```text
Respostas HTTP:  201: 1   409: 199
barradas_no_lock     160    (rejeitadas em memória, sem tocar no Cassandra)
rejeitadas_na_lwt     39    (passaram pelo lock entre uma liberação e outra; a LWT barrou)
confirmadas            1
OK: 1 vencedor para o slot.
```

A demo mostra as duas camadas trabalhando: o lock absorve a maior parte da rajada, e a LWT garante o
resultado nos casos que passam por ele.

### 6.3 Injeção de falha no Neo4j

| Cenário | Reserva | Rotas do grafo |
|---------|---------|----------------|
| Neo4j no ar | 201 | 200 |
| Neo4j derrubado (versão síncrona inicial) | 201 em **4 a 5 s** | 503 |
| Neo4j derrubado (versão final, fila em segundo plano) | 201 em **0,03 a 0,11 s** | 503 em ~4 s |
| Neo4j de volta | 201 | 200; novas reservas voltam ao grafo |

Ao religar, o grafo tinha 11 reservas = 17 confirmadas − 1 cancelada − 5 feitas com o Neo4j fora.
A conta fecha e expõe a limitação descrita na seção 8.

### 6.4 Front ponta a ponta

O Chrome headless foi dirigido pelo DevTools Protocol, usando a página contra a stack real. Os 14 passos
passaram sem erro de JavaScript: busca por atributos, login, reserva barrada pela regra de 6 jogadores,
reserva válida, grade atualizada, cancelamento, relatório negado para não-admin e liberado para o admin.

---

## 7. Evolução em relação ao Marco 1

### 7.1 Correções do modelo entregue

1. **`reservas_por_unidade` ganhou `reserva_id` na clustering key.** Sem ele, cancelar e reservar de novo
   o mesmo slot sobrescrevia a linha cancelada (contradizia a RN05 e a taxa de cancelamento).
2. **O cancelamento lê a ocupação antes do `DELETE`**, para obter o `reserva_id` exigido pela chave do
   histórico; a LWT de remoção também confere o `reserva_id`.
3. **A cota é um Sorted Set**, e não uma String (seção 2.2c do PDF).
4. **`criado_em` não é clustering column** de nenhuma tabela (seção 3.6 do PDF).
5. **O painel lê com `ONE`, não `QUORUM`** (o diagrama da pág. 4 contradizia as seções 3.2 e 3.4).
6. **São três consultas query-first, não duas**; a seção 3.6 omitia `reservas_por_unidade`.
7. **A grade em cache só é atualizada se já existir**, para não criar uma grade parcial.
8. O texto corrompido da pág. 12 e o diagrama cortado da pág. 4 foram refeitos neste documento.

Os três primeiros apareceram ao implementar e testar o modelo: a modelagem query-first só se prova
quando as escritas e leituras reais são executadas contra ela.

### 7.2 O que foi acrescentado

| Marco 1 | Marco 2 |
|---------|---------|
| 2 famílias (Chave-Valor e Wide-Column) | 4 famílias (+ Documentos e Grafos) |
| Catálogo fora do escopo | Catálogo no MongoDB com busca, regras e aggregation |
| Reserva de um usuário | Reserva com participantes, regras validadas e projeção no grafo |
| `usuario_id` no corpo da requisição | Sessão no Redis e controle de acesso por perfil |
| Modelagem no papel | Sistema funcional, front de demonstração, 96 + 7 testes, CI |

---

## 8. Limitações e trabalhos futuros

- **Reconstrução do grafo:** reservas confirmadas com o Neo4j fora do ar não entram no grafo. A correção
  é um script que relê `reservas_por_unidade` e refaz a projeção (o `MERGE` idempotente permite rodar sem
  apagar nada).
- **Autenticação de demonstração:** senha única e qualquer `usuario_id` positivo é aceito, porque a demo
  simula centenas de usuários. Em produção, seria um cadastro com hash de senha ou login institucional.
- **Cota em failover do Redis:** com replicação assíncrona, uma falha de master pode perder a última
  escrita da cota e permitir uma terceira reserva. Isso foi aceito porque a cota é uma regra de justiça,
  e não de integridade física; o conjunto pode ser reconstruído a partir de `reservas_por_usuario`.
- **Ranking não desconta cancelamentos:** mede procura (reservas feitas), não ocupação final.
- **Taxa de ocupação do relatório:** usa todos os slots do mês como denominador, inclusive dias que ainda
  não abriram na agenda; num mês em andamento, aparece mais baixa.
- **Ambiente de desenvolvimento em nó único:** a configuração de produção da seção 4.4 (RF = 3, replica set,
  Redis Cluster, cluster Neo4j) não foi montada.
- **Rotas administrativas do catálogo** (criar e editar espaços) não foram implementadas.

---

## 9. Lições aprendidas

1. **CAP é uma escolha por operação, não um rótulo do sistema.** A mesma falha (um nó fora) afeta a
   confirmação e a consulta de formas diferentes, e isso foi decidido de propósito.
2. **Separar filtro de garantia.** O lock do Redis é rápido mas não é seguro sozinho; a LWT é segura mas
   cara sob disputa. Juntos, cada um cobre a fraqueza do outro.
3. **A modelagem query-first precisa ser executada para ser validada.** O bug do `reserva_id` estava
   correto no papel e só apareceu quando cancelamento e nova reserva rodaram contra a tabela.
4. **"Fora do caminho crítico" precisa ser medido.** A projeção no grafo já era best-effort no código,
   mas só o teste com o Neo4j desligado mostrou que ela ainda atrasava cada reserva em 4 a 5 s.
5. **Fakes precisam ser conferidos com o banco real.** Os testes em memória são rápidos e rodam no CI,
   mas só valem porque o comportamento foi comparado com o MongoDB, o Cassandra e o Neo4j de verdade.
6. **Uma fonte de verdade por tipo de dado** torna a persistência poliglota administrável: o resto é
   cache ou projeção, e pode ser reconstruído.

---

## 10. Uso de ferramentas de IA

> **Rascunho para o grupo revisar e completar antes da entrega.**

O grupo usou o Claude Code (assistente de programação da Anthropic) durante o desenvolvimento. O uso
foi registrado em cada pull request do repositório, numa seção "Uso de IA" que descreve
concretamente o que a ferramenta fez naquele PR. Entre os usos registrados:

- implementação da família Grafos (Neo4j), do aprofundamento do MongoDB e do front da demonstração;
- revisão de código dos PRs do grupo, que apontou falhas no controle de acesso, depois corrigidas;
- execução dos testes de falha (Neo4j desligado), da comparação entre fakes e bancos reais e do teste do front no navegador;
- revisão do PDF do Marco 1 contra o checklist e redação desta documentação.

As decisões de escopo, a escolha das tecnologias e a validação final foram feitas pelo grupo.
