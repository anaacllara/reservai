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

    @property
    def ano_mes(self) -> str:
        return self.data.strftime("%Y-%m")

    def cancelada(self) -> "Reserva":
        return replace(self, status="cancelada")


class ErroReserva(Exception):
    status_http = 400


class EspacoNaoEncontrado(ErroReserva):
    status_http = 404


class ReservaNaoEncontrada(ErroReserva):
    status_http = 404


class HorarioInvalido(ErroReserva):
    status_http = 422


class SlotIndisponivel(ErroReserva):
    status_http = 409


class LimiteReservasAtingido(ErroReserva):
    status_http = 409


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
