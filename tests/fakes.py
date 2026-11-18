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


class GrafoFake:
    """Mesma semântica das consultas Cypher de app/repositorios/grafo.py, em Python.

    Cada reserva é um "nó" que liga o dono, os participantes e o espaço (com o tipo).
    """

    def __init__(self):
        self.reservas: dict = {}  # reserva_id -> (pessoas, espaco_id, tipo)
        self.falhar = False

    def _checar(self):
        if self.falhar:
            raise ConnectionError("Neo4j indisponível (simulado)")

    def registrar_reserva(self, r: Reserva, espaco: Espaco) -> None:
        self._checar()
        self.reservas[r.reserva_id] = ({r.usuario_id, *r.participantes}, espaco.id, espaco.tipo)

    def remover_reserva(self, r: Reserva) -> None:
        self._checar()
        self.reservas.pop(r.reserva_id, None)

    def _espacos_de(self, usuario_id: int) -> set[int]:
        return {e for pessoas, e, _ in self.reservas.values() if usuario_id in pessoas}

    def _amigos_de(self, usuario_id: int, tipo: str | None = None) -> set[int]:
        return {
            p
            for pessoas, _, t in self.reservas.values()
            if usuario_id in pessoas and tipo in (None, t)
            for p in pessoas
        } - {usuario_id}

    def recomendar_espacos(self, usuario_id: int, limite: int) -> list[tuple[int, int]]:
        self._checar()
        meus = self._espacos_de(usuario_id)
        outros = {p for pessoas, e, _ in self.reservas.values() if e in meus for p in pessoas} - {usuario_id}
        pontos: dict[int, set[int]] = {}
        for outro in outros:
            for e in self._espacos_de(outro) - meus:
                pontos.setdefault(e, set()).add(outro)
        ordenado = sorted(((e, len(p)) for e, p in pontos.items()), key=lambda x: (-x[1], x[0]))
        return ordenado[:limite]

    def sugerir_parceiros(self, usuario_id: int, tipo: str | None, limite: int) -> list[tuple[int, int]]:
        self._checar()
        amigos = self._amigos_de(usuario_id)
        comum: dict[int, set[int]] = {}
        for amigo in amigos:
            for s in self._amigos_de(amigo, tipo) - amigos - {usuario_id}:
                comum.setdefault(s, set()).add(amigo)
        ordenado = sorted(((s, len(a)) for s, a in comum.items()), key=lambda x: (-x[1], x[0]))
        return ordenado[:limite]
