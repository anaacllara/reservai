"""Implementações em memória das interfaces de app/dominio.py.

O RepositorioFake reproduz a semântica que importa do Cassandra:
- inserir_ocupacao/remover_ocupacao são condicionais e atômicas (como a LWT);
- o histórico usa a mesma chave primária das tabelas reais, então upserts
  se comportam igual.
"""

import threading
from datetime import date, time

from app.dominio import Espaco, Reserva


class CatalogoFake:
    def __init__(self, espacos: list[Espaco]):
        self.espacos = {e.id: e for e in espacos}

    def obter(self, espaco_id: int) -> Espaco | None:
        return self.espacos.get(espaco_id)

    def listar(self, unidade_id: int | None = None) -> list[Espaco]:
        return [e for e in self.espacos.values() if unidade_id in (None, e.unidade_id)]


class RepositorioFake:
    def __init__(self):
        self._paxos = threading.Lock()
        self.ocupacao: dict[tuple, Reserva] = {}  # (espaco, data, horario)
        self.por_usuario: dict[tuple, Reserva] = {}  # (usuario, data, horario, reserva_id)
        self.por_unidade: dict[tuple, Reserva] = {}  # (unidade, ano_mes, data, espaco, horario, reserva_id)
        self.lwt_tentativas = 0
        self.falhar_lwt = False

    def inserir_ocupacao(self, r: Reserva) -> bool:
        with self._paxos:
            self.lwt_tentativas += 1
            if self.falhar_lwt:
                raise TimeoutError("WriteTimeout simulado")
            chave = (r.espaco_id, r.data, r.horario)
            if chave in self.ocupacao:
                return False
            self.ocupacao[chave] = r
            return True

    def remover_ocupacao(self, r: Reserva) -> bool:
        with self._paxos:
            chave = (r.espaco_id, r.data, r.horario)
            atual = self.ocupacao.get(chave)
            if atual is None or atual.usuario_id != r.usuario_id or atual.reserva_id != r.reserva_id:
                return False
            del self.ocupacao[chave]
            return True

    def obter_ocupacao(self, espaco_id: int, data: date, horario: time) -> Reserva | None:
        return self.ocupacao.get((espaco_id, data, horario))

    def ocupacoes_do_dia(self, espaco_id: int, data: date) -> list[Reserva]:
        return sorted(
            (r for (e, d, _), r in self.ocupacao.items() if e == espaco_id and d == data),
            key=lambda r: r.horario,
        )

    def registrar_historico(self, r: Reserva) -> None:
        self.por_usuario[(r.usuario_id, r.data, r.horario, r.reserva_id)] = r
        self.por_unidade[(r.unidade_id, r.ano_mes, r.data, r.espaco_id, r.horario, r.reserva_id)] = r

    def marcar_cancelada(self, r: Reserva) -> None:
        self.registrar_historico(r)

    def reservas_do_usuario(self, usuario_id: int, limite: int) -> list[Reserva]:
        itens = [r for (u, *_), r in self.por_usuario.items() if u == usuario_id]
        return sorted(itens, key=lambda r: (r.data, r.horario), reverse=True)[:limite]

    def reservas_da_unidade(self, unidade_id: int, ano_mes: str) -> list[Reserva]:
        itens = [r for (u, m, *_), r in self.por_unidade.items() if (u, m) == (unidade_id, ano_mes)]
        return sorted(itens, key=lambda r: (r.data, r.espaco_id, r.horario))
