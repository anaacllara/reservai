"""Camada Wide-Column (Marco 1, seção 2.3).

Níveis de consistência por operação (Marco 1, seções 3.2 e 3.4):
- escrita da ocupação: LWT com SERIAL (Paxos) + QUORUM no commit -> CP;
- leitura antes do cancelamento: SERIAL, para enxergar o último estado do Paxos;
- histórico, painel e reconstrução do cache: ONE -> AP.
"""

from datetime import date, datetime, time, timezone

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster, Session
from cassandra.query import BatchStatement, BatchType, dict_factory

from app.config import Settings
from app.dominio import Reserva


def conectar(cfg: Settings) -> tuple[Cluster, Session]:
    cluster = Cluster(cfg.cassandra_hosts.split(","))
    session = cluster.connect(cfg.cassandra_keyspace)
    session.row_factory = dict_factory
    session.default_consistency_level = ConsistencyLevel.ONE
    return cluster, session


def _data(valor) -> date:
    return valor if isinstance(valor, date) else valor.date()


def _hora(valor) -> time:
    return valor if isinstance(valor, time) else valor.time()


def _instante(valor: datetime | None) -> datetime | None:
    # O driver devolve timestamps em UTC, sem tzinfo.
    return valor.replace(tzinfo=timezone.utc) if valor else None


class RepositorioCassandra:
    def __init__(self, session: Session):
        self.s = session
        prep = session.prepare

        self._inserir_ocupacao = prep(
            "INSERT INTO reservas_por_espaco "
            "(espaco_id, data_reserva, horario, reserva_id, usuario_id, unidade_id, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) IF NOT EXISTS"
        )
        self._remover_ocupacao = prep(
            "DELETE FROM reservas_por_espaco "
            "WHERE espaco_id = ? AND data_reserva = ? AND horario = ? "
            "IF usuario_id = ? AND reserva_id = ?"
        )
        for lwt in (self._inserir_ocupacao, self._remover_ocupacao):
            lwt.consistency_level = ConsistencyLevel.QUORUM
            lwt.serial_consistency_level = ConsistencyLevel.SERIAL

        self._obter_ocupacao = prep(
            "SELECT * FROM reservas_por_espaco WHERE espaco_id = ? AND data_reserva = ? AND horario = ?"
        )
        self._obter_ocupacao.consistency_level = ConsistencyLevel.SERIAL

        self._ocupacoes_do_dia = prep(
            "SELECT * FROM reservas_por_espaco WHERE espaco_id = ? AND data_reserva = ?"
        )
        self._inserir_por_usuario = prep(
            "INSERT INTO reservas_por_usuario "
            "(usuario_id, data_reserva, horario, reserva_id, espaco_id, unidade_id, status, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        self._inserir_por_unidade = prep(
            "INSERT INTO reservas_por_unidade "
            "(unidade_id, ano_mes, data_reserva, espaco_id, horario, reserva_id, usuario_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        self._cancelar_por_usuario = prep(
            "UPDATE reservas_por_usuario SET status = 'cancelada' "
            "WHERE usuario_id = ? AND data_reserva = ? AND horario = ? AND reserva_id = ?"
        )
        self._cancelar_por_unidade = prep(
            "UPDATE reservas_por_unidade SET status = 'cancelada' "
            "WHERE unidade_id = ? AND ano_mes = ? AND data_reserva = ? "
            "AND espaco_id = ? AND horario = ? AND reserva_id = ?"
        )
        self._reservas_do_usuario = prep(
            "SELECT * FROM reservas_por_usuario WHERE usuario_id = ? LIMIT ?"
        )
        self._reservas_da_unidade = prep(
            "SELECT * FROM reservas_por_unidade WHERE unidade_id = ? AND ano_mes = ?"
        )

    # ---- ocupação (reservas_por_espaco, sempre via LWT) ---------------

    def inserir_ocupacao(self, r: Reserva) -> bool:
        resultado = self.s.execute(
            self._inserir_ocupacao,
            (r.espaco_id, r.data, r.horario, r.reserva_id, r.usuario_id, r.unidade_id, r.criado_em),
        )
        return resultado.was_applied

    def remover_ocupacao(self, r: Reserva) -> bool:
        resultado = self.s.execute(
            self._remover_ocupacao,
            (r.espaco_id, r.data, r.horario, r.usuario_id, r.reserva_id),
        )
        return resultado.was_applied

    def obter_ocupacao(self, espaco_id: int, data: date, horario: time) -> Reserva | None:
        linha = self.s.execute(self._obter_ocupacao, (espaco_id, data, horario)).one()
        return self._de_ocupacao(linha) if linha else None

    def ocupacoes_do_dia(self, espaco_id: int, data: date) -> list[Reserva]:
        return [self._de_ocupacao(l) for l in self.s.execute(self._ocupacoes_do_dia, (espaco_id, data))]

    # ---- histórico (BATCH logged: as duas escritas acabam acontecendo) ----

    def registrar_historico(self, r: Reserva) -> None:
        batch = BatchStatement(batch_type=BatchType.LOGGED)
        batch.add(
            self._inserir_por_usuario,
            (r.usuario_id, r.data, r.horario, r.reserva_id, r.espaco_id, r.unidade_id, r.status, r.criado_em),
        )
        batch.add(
            self._inserir_por_unidade,
            (r.unidade_id, r.ano_mes, r.data, r.espaco_id, r.horario, r.reserva_id, r.usuario_id, r.status),
        )
        self.s.execute(batch)

    def marcar_cancelada(self, r: Reserva) -> None:
        batch = BatchStatement(batch_type=BatchType.LOGGED)
        batch.add(self._cancelar_por_usuario, (r.usuario_id, r.data, r.horario, r.reserva_id))
        batch.add(
            self._cancelar_por_unidade,
            (r.unidade_id, r.ano_mes, r.data, r.espaco_id, r.horario, r.reserva_id),
        )
        self.s.execute(batch)

    def reservas_do_usuario(self, usuario_id: int, limite: int) -> list[Reserva]:
        return [self._de_historico(l) for l in self.s.execute(self._reservas_do_usuario, (usuario_id, limite))]

    def reservas_da_unidade(self, unidade_id: int, ano_mes: str) -> list[Reserva]:
        return [self._de_historico(l) for l in self.s.execute(self._reservas_da_unidade, (unidade_id, ano_mes))]

    # ---- conversão ------------------------------------------------------

    @staticmethod
    def _de_ocupacao(l: dict) -> Reserva:
        return Reserva(
            reserva_id=l["reserva_id"],
            usuario_id=l["usuario_id"],
            espaco_id=l["espaco_id"],
            unidade_id=l["unidade_id"],
            data=_data(l["data_reserva"]),
            horario=_hora(l["horario"]),
            status="ativa",
            criado_em=_instante(l["criado_em"]),
        )

    @staticmethod
    def _de_historico(l: dict) -> Reserva:
        return Reserva(
            reserva_id=l["reserva_id"],
            usuario_id=l["usuario_id"],
            espaco_id=l["espaco_id"],
            unidade_id=l["unidade_id"],
            data=_data(l["data_reserva"]),
            horario=_hora(l["horario"]),
            status=l["status"],
            criado_em=_instante(l.get("criado_em")),
        )
