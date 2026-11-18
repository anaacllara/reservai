"""RF10, RF11 e RN08: busca por atributos, resumo do catálogo e regras dos espaços (MongoDB)."""

from datetime import time

import pytest
from fastapi.testclient import TestClient

from app import chaves
from app.config import Settings
from app.dominio import Espaco, RegraDoEspacoViolada
from app.main import criar_app
from app.servicos import ServicoReservas
from tests.conftest import AMANHA, H19
from tests.fakes import CatalogoFake, GrafoFake, RepositorioFake

MIN6 = {"tipo": "min_participantes", "valor": 6, "descricao": "Mínimo de 6 jogadores"}
INFO = {"tipo": "informativa", "descricao": "Silêncio moderado"}

# Espelha schema/mongo-seed.js: cada tipo de espaço tem atributos diferentes.
ESPACOS = [
    Espaco(1, 1, "Quadra Poliesportiva", "quadra", 8, 20,
           {"modalidades": ["futsal", "vôlei", "basquete"], "coberta": True}, [MIN6]),
    Espaco(2, 1, "Quadra de Areia", "quadra", 8, 20,
           {"modalidades": ["vôlei de praia", "futevôlei"], "coberta": False},
           [{"tipo": "min_participantes", "valor": 4, "descricao": "Mínimo de 4 jogadores (2 duplas)"}]),
    Espaco(3, 1, "Sala de Estudo A", "sala_estudo", 8, 20, {"capacidade": 6, "tv": False}, [INFO]),
    # Regra de um tipo que esta versão da API não conhece: deve ser ignorada, não quebrar.
    Espaco(4, 1, "Sala de Estudo B", "sala_estudo", 8, 20, {"capacidade": 10, "tv": True},
           [{"tipo": "silencio_apos", "valor": "18:00"}]),
    Espaco(5, 2, "Quadra Society", "quadra", 8, 20, {"modalidades": ["futsal"], "gramado": "sintético"}, []),
    Espaco(7, 3, "Sala de Reunião Ipê", "sala_reuniao", 8, 20,
           {"capacidade": 12, "videoconferencia": True, "projetor": True}, []),
    Espaco(8, 3, "Sala de Reunião Buriti", "sala_reuniao", 8, 20,
           {"capacidade": 4, "videoconferencia": True, "projetor": False}, []),
]


@pytest.fixture
def catalogo():
    return CatalogoFake(ESPACOS)


@pytest.fixture
def servico(redis, repo, catalogo, relogio):
    return ServicoReservas(redis, repo, catalogo, GrafoFake(), Settings(_env_file=None), agora=relogio)


@pytest.fixture
def client(servico, catalogo):
    return TestClient(criar_app(servico, catalogo))


def ids(resp) -> list[int]:
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()]


# ---- RN08: regras do espaço na reserva -----------------------------------


def test_quadra_sem_jogadores_suficientes_e_barrada_antes_do_lock(servico, repo, redis):
    with pytest.raises(RegraDoEspacoViolada, match="Mínimo de 6 jogadores"):
        servico.reservar(842, 1, AMANHA, H19, participantes=[900, 901, 902, 903])  # 5 pessoas
    assert repo.lwt_tentativas == 0
    assert not redis.exists(chaves.lock_slot(1, AMANHA, H19))
    assert redis.zcard(chaves.reservas_ativas(842)) == 0  # não consumiu a cota
    assert "tentativas" not in servico.metricas()  # nem entrou na disputa


def test_quadra_com_o_minimo_exato_e_aceita(servico):
    r = servico.reservar(842, 1, AMANHA, H19, participantes=[900, 901, 902, 903, 904])  # 6 pessoas
    assert len(r.participantes) == 5


def test_o_dono_conta_como_jogador_e_duplicatas_nao(servico):
    # 5 ids distintos + o dono repetido na lista = 6 pessoas, não 7
    servico.reservar(842, 1, AMANHA, H19, participantes=[900, 901, 902, 903, 904, 842, 900])
    with pytest.raises(RegraDoEspacoViolada):
        servico.reservar(843, 1, AMANHA, time(18), participantes=[900, 900, 901, 902, 903])  # 5 pessoas


def test_capacidade_da_sala_e_respeitada(servico):
    servico.reservar(842, 3, AMANHA, H19, participantes=[900, 901, 902, 903, 904])  # 6 = capacidade
    with pytest.raises(RegraDoEspacoViolada, match="comporta 6"):
        servico.reservar(843, 3, AMANHA, time(18), participantes=list(range(900, 906)))  # 7


def test_regra_informativa_nao_bloqueia(servico):
    servico.reservar(842, 3, AMANHA, H19)


def test_regra_de_tipo_desconhecido_e_ignorada(servico):
    servico.reservar(842, 4, AMANHA, H19)


def test_espaco_sem_regras_nem_capacidade_aceita_qualquer_grupo(servico):
    servico.reservar(842, 5, AMANHA, H19)
    servico.reservar(843, 5, AMANHA, time(18), participantes=list(range(900, 920)))


def test_api_devolve_422_com_a_descricao_da_regra(client):
    login = client.post("/auth/login", json={"usuario_id": 842, "senha": "senha123"}).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    resp = client.post(
        "/reservas", json={"espaco_id": 1, "data": AMANHA.isoformat(), "horario": "19:00"}, headers=headers
    )
    assert resp.status_code == 422
    assert "Mínimo de 6 jogadores" in resp.json()["detail"]


# ---- RF10: busca por atributos -------------------------------------------


def test_sem_filtro_lista_tudo(client):
    assert ids(client.get("/espacos")) == [1, 2, 3, 4, 5, 7, 8]


def test_filtro_por_unidade_continua_funcionando(client):
    assert ids(client.get("/espacos", params={"unidade_id": 3})) == [7, 8]


def test_salas_de_reuniao_com_capacidade_e_videoconferencia(client):
    params = {"tipo": "sala_reuniao", "capacidade_min": 8, "recursos": "videoconferencia"}
    assert ids(client.get("/espacos", params=params)) == [7]


def test_varios_recursos_sao_exigidos_juntos(client):
    resp = client.get("/espacos", params=[("recursos", "videoconferencia"), ("recursos", "projetor")])
    assert ids(resp) == [7]


def test_capacidade_minima_ignora_quem_nao_declara_capacidade(client):
    # Quadras não têm o atributo capacidade: não entram, em vez de contarem como 0 ou infinito.
    assert ids(client.get("/espacos", params={"capacidade_min": 1})) == [3, 4, 7, 8]


def test_modalidade_casa_com_elemento_do_array(client):
    assert ids(client.get("/espacos", params={"modalidade": "futsal"})) == [1, 5]
    assert ids(client.get("/espacos", params={"modalidade": "futsal", "unidade_id": 2})) == [5]


def test_recurso_booleano_falso_nao_casa(client):
    assert ids(client.get("/espacos", params={"recursos": "coberta"})) == [1]  # a de areia tem coberta=false


def test_busca_sem_resultado(client):
    assert ids(client.get("/espacos", params={"tipo": "auditorio"})) == []


@pytest.mark.parametrize("recurso", ["$where", "atributos.x", "Video", "a b", ""])
def test_recurso_com_nome_invalido_e_rejeitado(client, recurso):
    # Impede que o parâmetro vire operador do Mongo ou caminho arbitrário no documento.
    assert client.get("/espacos", params={"recursos": recurso}).status_code == 422


def test_obter_espaco_devolve_regras_estruturadas(client):
    assert client.get("/espacos/1").json()["regras"] == [MIN6]


# ---- RF11: resumo do catálogo (aggregation) ------------------------------


def test_resumo_por_unidade_e_tipo(client):
    resumo = client.get("/espacos/resumo", params={"unidade_id": 1}).json()
    assert resumo["por_tipo"] == [
        {"unidade_id": 1, "tipo": "quadra", "espacos": 2, "capacidade_total": 0,
         "nomes": ["Quadra Poliesportiva", "Quadra de Areia"]},
        {"unidade_id": 1, "tipo": "sala_estudo", "espacos": 2, "capacidade_total": 16,
         "nomes": ["Sala de Estudo A", "Sala de Estudo B"]},
    ]


def test_resumo_conta_modalidades(client):
    modalidades = client.get("/espacos/resumo").json()["modalidades"]
    assert modalidades[0] == {"modalidade": "futsal", "espacos": 2}
    assert len(modalidades) == 5


def test_rota_de_resumo_nao_e_confundida_com_id(client):
    assert client.get("/espacos/resumo").status_code == 200  # e não 422 de "resumo" não ser int
