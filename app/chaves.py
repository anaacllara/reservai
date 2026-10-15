"""Nomes das chaves do Redis (Marco 1, seção 2.2)."""

from datetime import date, time


def lock_slot(espaco_id: int, data: date, horario: time) -> str:
    return f"lock:slot:{espaco_id}:{data.isoformat()}:{horario:%H:%M}"


def disponibilidade(espaco_id: int, data: date) -> str:
    return f"disponibilidade:espaco:{espaco_id}:{data.isoformat()}"


def reservas_ativas(usuario_id: int) -> str:
    return f"ativas:usuario:{usuario_id}"


def ranking(unidade_id: int) -> str:
    return f"ranking:espacos:unidade:{unidade_id}"


def membro_ranking(espaco_id: int) -> str:
    return f"espaco:{espaco_id}"


METRICAS = "metricas:reservas"
