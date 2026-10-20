-- Schema Wide-Column do Reservaí (Marco 1, seção 2.3).
-- Desenvolvimento: SimpleStrategy com RF=1 (nó único).
-- Produção (Marco 1, seção 3.4): NetworkTopologyStrategy com RF=3.

CREATE KEYSPACE IF NOT EXISTS reservai
  WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};

USE reservai;

-- Consulta A: reservas ativas de um espaço em uma data, por horário.
-- Uma linha existe apenas enquanto a reserva está ativa: ela É a ocupação do slot.
-- Toda escrita nesta tabela passa por LWT (IF NOT EXISTS / IF usuario_id = ...).
CREATE TABLE IF NOT EXISTS reservas_por_espaco (
  espaco_id    int,
  data_reserva date,
  horario      time,
  reserva_id   uuid,
  usuario_id   int,
  unidade_id   int,
  criado_em    timestamp,
  PRIMARY KEY ((espaco_id, data_reserva), horario)
) WITH CLUSTERING ORDER BY (horario ASC);

-- Consulta B: reservas de um usuário, das mais recentes às mais antigas.
CREATE TABLE IF NOT EXISTS reservas_por_usuario (
  usuario_id   int,
  data_reserva date,
  horario      time,
  reserva_id   uuid,
  espaco_id    int,
  unidade_id   int,
  status       text,
  criado_em    timestamp,
  PRIMARY KEY (usuario_id, data_reserva, horario, reserva_id)
) WITH CLUSTERING ORDER BY (data_reserva DESC, horario DESC, reserva_id ASC);

-- Consulta C: ocupação dos espaços de uma unidade em um mês (painel administrativo).
-- Correção em relação ao PDF do Marco 1: reserva_id entra na clustering key.
-- Sem ele, cancelar e reservar de novo o mesmo slot faria upsert na linha
-- cancelada, apagando o histórico de cancelamentos.
CREATE TABLE IF NOT EXISTS reservas_por_unidade (
  unidade_id   int,
  ano_mes      text,
  data_reserva date,
  espaco_id    int,
  horario      time,
  reserva_id   uuid,
  usuario_id   int,
  status       text,
  PRIMARY KEY ((unidade_id, ano_mes), data_reserva, espaco_id, horario, reserva_id)
) WITH CLUSTERING ORDER BY (data_reserva ASC, espaco_id ASC, horario ASC, reserva_id ASC);
