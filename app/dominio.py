from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class Espaco:
    id: int
    unidade_id: int
    nome: str
    tipo: str
    abertura: int  # hora cheia, ex.: 8
    fechamento: int  # hora cheia exclusiva, ex.: 20 (último slot 19:00)
    atributos: dict
    regras: list[str]


@dataclass(frozen=True)
class Reserva:
    reserva_id: UUID
    usuario_id: int
    espaco_id: int
    unidade_id: int
    data: date
    horario: time
    status: str  # "ativa" | "cancelada"
    criado_em: datetime | None  # reservas_por_unidade não guarda este campo
    # Outros usuários que vão usar o espaço junto com o dono (o dono não entra aqui).
    participantes: tuple[int, ...] = ()

    @property
    def ano_mes(self) -> str:
        return self.data.strftime("%Y-%m")

    def cancelada(self) -> "Reserva":
        return replace(self, status="cancelada")


@dataclass(frozen=True)
class MetricasEspaco:
    espaco_id: int
    nome: str
    tipo: str
    total_reservas: int
    ativas: int
    canceladas: int
    taxa_cancelamento: float
    slots_disponiveis_mes: int
    taxa_ocupacao: float


@dataclass(frozen=True)
class RelatorioUnidade:
    unidade_id: int
    ano_mes: str
    total_reservas: int
    total_ativas: int
    total_canceladas: int
    taxa_cancelamento_unidade: float
    taxa_ocupacao_unidade: float
    espacos: list[MetricasEspaco]
    reservas: list[Reserva]


class ErroReserva(Exception):
    status_http = 400


class EspacoNaoEncontrado(ErroReserva):
    status_http = 404


class ReservaNaoEncontrada(ErroReserva):
    status_http = 404


class HorarioInvalido(ErroReserva):
    status_http = 422


class ParticipantesInvalidos(ErroReserva):
    status_http = 422


class SlotIndisponivel(ErroReserva):
    status_http = 409


class LimiteReservasAtingido(ErroReserva):
    status_http = 409


class GrafoIndisponivel(ErroReserva):
    status_http = 503


class RepositorioReservas(Protocol):
    """Camada Wide-Column (Cassandra). Implementação real em app/repositorios/cassandra.py."""

    def inserir_ocupacao(self, reserva: Reserva) -> bool: ...
    def registrar_historico(self, reserva: Reserva) -> None: ...
    def obter_ocupacao(self, espaco_id: int, data: date, horario: time) -> Reserva | None: ...
    def remover_ocupacao(self, reserva: Reserva) -> bool: ...
    def marcar_cancelada(self, reserva: Reserva) -> None: ...
    def ocupacoes_do_dia(self, espaco_id: int, data: date) -> list[Reserva]: ...
    def reservas_do_usuario(self, usuario_id: int, limite: int) -> list[Reserva]: ...
    def reservas_da_unidade(self, unidade_id: int, ano_mes: str) -> list[Reserva]: ...


class CatalogoEspacos(Protocol):
    """Camada Documentos (MongoDB). Implementação real em app/repositorios/mongo.py."""

    def obter(self, espaco_id: int) -> Espaco | None: ...
    def listar(self, unidade_id: int | None = None) -> list[Espaco]: ...


class GrafoRelacionamentos(Protocol):
    """Camada Grafos (Neo4j). Implementação real em app/repositorios/grafo.py.

    É uma projeção das reservas para consultas de travessia (recomendação e parceiros).
    A fonte de verdade continua no Cassandra: o grafo pode ser reconstruído a partir dele.
    """

    def registrar_reserva(self, reserva: Reserva, espaco: Espaco) -> None: ...
    def remover_reserva(self, reserva: Reserva) -> None: ...
    def recomendar_espacos(self, usuario_id: int, limite: int) -> list[tuple[int, int]]: ...
    def sugerir_parceiros(
        self, usuario_id: int, tipo: str | None, limite: int
    ) -> list[tuple[int, int]]: ...
