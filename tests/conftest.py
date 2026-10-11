from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import fakeredis
import pytest

from app.config import Settings
from app.dominio import Espaco
from app.servicos import ServicoReservas
from tests.fakes import CatalogoFake, RepositorioFake

FUSO = ZoneInfo("America/Sao_Paulo")
# Segunda-feira, 12/10/2026, 10h: a agenda está aberta até 19/10.
AGORA = datetime(2026, 10, 12, 10, 0, tzinfo=FUSO)
AMANHA = date(2026, 10, 13)
H19 = time(19, 0)

QUADRA = Espaco(1, 1, "Quadra Poliesportiva", "quadra", 8, 20, {"coberta": True}, [])
SALA = Espaco(3, 1, "Sala de Estudo A", "sala_estudo", 8, 20, {"capacidade": 6}, [])
COWORKING = Espaco(7, 3, "Sala de Reunião Ipê", "sala_reuniao", 8, 20, {"capacidade": 12}, [])


class Relogio:
    def __init__(self, agora: datetime):
        self.agora = agora

    def __call__(self) -> datetime:
        return self.agora


@pytest.fixture
def relogio():
    return Relogio(AGORA)


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def repo():
    return RepositorioFake()


@pytest.fixture
def catalogo():
    return CatalogoFake([QUADRA, SALA, COWORKING])


@pytest.fixture
def servico(redis, repo, catalogo, relogio):
    return ServicoReservas(redis, repo, catalogo, Settings(_env_file=None), agora=relogio)
