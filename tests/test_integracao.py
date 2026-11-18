"""Suíte de testes de integração ponta a ponta contra os bancos reais (Issue #14).

Executa contra todos os serviços do docker-compose.yml:
- Redis: lock, cota via script Lua, sessão e ranking
- Cassandra: LWT (SERIAL), ocupação e históricos
- MongoDB: catálogo de espaços e validação de regras
- Neo4j: projeção de participantes, recomendações e parceiros
- API: endpoints REST e script de concorrência (disputa_slot.py)
"""

import os
import subprocess
import sys
import time
from datetime import date, timedelta

import httpx
import pytest

from app import chaves

pytestmark = pytest.mark.integracao

# Ignora o módulo caso os drivers reais de banco não estejam instalados no ambiente (ex.: CI unitário)
cassandra_pkg = pytest.importorskip("cassandra", reason="cassandra-driver não instalado (ambiente unitário sem Docker)")
from cassandra.cluster import Cluster

neo4j_pkg = pytest.importorskip("neo4j", reason="neo4j não instalado (ambiente unitário sem Docker)")
from neo4j import GraphDatabase

mongo_pkg = pytest.importorskip("pymongo", reason="pymongo não instalado (ambiente unitário sem Docker)")
from pymongo import MongoClient

redis_pkg = pytest.importorskip("redis", reason="redis não instalado")
from redis import Redis

API_URL = os.getenv("RESERVAI_API_URL", "http://localhost:8000")
REDIS_URL = os.getenv("RESERVAI_REDIS_URL", "redis://localhost:6379/0")
CASSANDRA_HOSTS = os.getenv("RESERVAI_CASSANDRA_HOSTS", "localhost").split(",")
CASSANDRA_KEYSPACE = os.getenv("RESERVAI_CASSANDRA_KEYSPACE", "reservai")
MONGO_URL = os.getenv("RESERVAI_MONGO_URL", "mongodb://localhost:27017")
MONGO_DB = os.getenv("RESERVAI_MONGO_DB", "reservai")
NEO4J_URL = os.getenv("RESERVAI_NEO4J_URL", "bolt://localhost:7687")
NEO4J_USER = os.getenv("RESERVAI_NEO4J_USUARIO", "neo4j")
NEO4J_PASS = os.getenv("RESERVAI_NEO4J_SENHA", "reservai-dev")


@pytest.fixture(scope="session")
def redis_client():
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    client.close()


@pytest.fixture(scope="session")
def cassandra_session():
    cluster = Cluster(CASSANDRA_HOSTS)
    session = cluster.connect(CASSANDRA_KEYSPACE)
    yield session
    cluster.shutdown()


@pytest.fixture(scope="session")
def neo4j_driver():
    driver = GraphDatabase.driver(NEO4J_URL, auth=(NEO4J_USER, NEO4J_PASS))
    yield driver
    driver.close()


@pytest.fixture(scope="session")
def mongo_client():
    client = MongoClient(MONGO_URL)
    yield client
    client.close()


@pytest.fixture(autouse=True)
def limpar_dados(redis_client, cassandra_session, neo4j_driver):
    """Garante isolamento completo entre os testes truncando os dados voláteis."""
    def _limpar():
        # Redis: limpa chaves de teste
        redis_client.flushdb()
        # Cassandra: trunca tabelas de ocupação e histórico
        cassandra_session.execute("TRUNCATE reservas_por_espaco")
        cassandra_session.execute("TRUNCATE reservas_por_usuario")
        cassandra_session.execute("TRUNCATE reservas_por_unidade")
        # Neo4j: remove todos os nós do grafo
        with neo4j_driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")

    _limpar()
    yield
    _limpar()


@pytest.fixture
def http():
    with httpx.Client(base_url=API_URL, timeout=30.0) as client:
        yield client


def autenticar(http: httpx.Client, usuario_id: int = 842, senha: str = "senha123") -> dict[str, str]:
    resp = http.post("/auth/login", json={"usuario_id": usuario_id, "senha": senha})
    assert resp.status_code == 200, f"Falha no login do usuário {usuario_id}: {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


AMANHA = (date.today() + timedelta(days=1)).isoformat()
DEPOIS_DE_AMANHA = (date.today() + timedelta(days=2)).isoformat()


def test_fluxo_reserva_real(http, redis_client, cassandra_session, neo4j_driver):
    """Testa reserva real contra Redis (lock + cota), Cassandra (LWT) e Neo4j (grafo)."""
    headers = autenticar(http, 842)

    # Sala de Estudo A (espaço 3, unidade 1)
    payload = {
        "espaco_id": 3,
        "data": AMANHA,
        "horario": "14:00",
        "participantes": [900],
    }

    resp = http.post("/reservas", json=payload, headers=headers)
    assert resp.status_code == 201
    dados = resp.json()
    reserva_id = dados["reserva_id"]
    assert dados["status"] == "ativa"
    assert dados["usuario_id"] == 842
    assert dados["participantes"] == [900]

    # 1. Validação no Cassandra Real
    cql = f"SELECT * FROM reservas_por_espaco WHERE espaco_id = 3 AND data_reserva = '{AMANHA}' AND horario = '14:00:00'"
    linhas = list(cassandra_session.execute(cql))
    assert len(linhas) == 1
    assert str(linhas[0].reserva_id) == reserva_id
    assert linhas[0].usuario_id == 842

    # Histórico por usuário
    linhas_user = list(cassandra_session.execute(f"SELECT * FROM reservas_por_usuario WHERE usuario_id = 842"))
    assert len(linhas_user) == 1
    assert str(linhas_user[0].reserva_id) == reserva_id

    # 2. Validação no Redis Real
    # Lock já deve estar liberado
    lock_chave = chaves.lock_slot(3, date.fromisoformat(AMANHA), time(14, 0))
    assert not redis_client.exists(lock_chave)
    # Cota consumida
    cota_chave = chaves.reservas_ativas(842)
    assert redis_client.zcard(cota_chave) == 1
    # Ranking atualizado
    ranking_chave = chaves.ranking(1)
    assert redis_client.zscore(ranking_chave, chaves.membro_ranking(3)) == 1

    # 3. Validação no Neo4j Real (espera thread em segundo plano persistir)
    time.sleep(0.5)
    with neo4j_driver.session() as s:
        resultado = s.run(
            "MATCH (u:Usuario {id: 842})-[:RESERVOU]->(r:Reserva {id: $rid})-[:EM]->(e:Espaco {id: 3}) "
            "MATCH (p:Usuario {id: 900})-[:PARTICIPOU]->(r) "
            "RETURN r.id AS id",
            rid=reserva_id,
        ).single()
        assert resultado is not None
        assert resultado["id"] == reserva_id


def test_conflito_mesmo_slot_e_estouro_cota_real(http):
    """Testa unicidade garantida pela LWT e bloqueio de cota via script Lua."""
    headers_842 = autenticar(http, 842)
    headers_900 = autenticar(http, 900)

    payload1 = {"espaco_id": 3, "data": AMANHA, "horario": "15:00"}

    # Primeira reserva: sucesso
    r1 = http.post("/reservas", json=payload1, headers=headers_842)
    assert r1.status_code == 201

    # Conflito: usuário 900 tenta o mesmo slot -> barrado (409)
    r_conflito = http.post("/reservas", json=payload1, headers=headers_900)
    assert r_conflito.status_code == 409

    # Usuário 842 faz a 2ª reserva futura -> sucesso (limite é 2)
    payload2 = {"espaco_id": 3, "data": AMANHA, "horario": "16:00"}
    r2 = http.post("/reservas", json=payload2, headers=headers_842)
    assert r2.status_code == 201

    # Usuário 842 tenta a 3ª reserva futura -> barrado na cota (409)
    payload3 = {"espaco_id": 3, "data": AMANHA, "horario": "17:00"}
    r3 = http.post("/reservas", json=payload3, headers=headers_842)
    assert r3.status_code == 409
    assert "Limite de 2 reservas futuras atingido" in r3.json()["detail"]


def test_cancelamento_real_libera_slot_e_cota(http, redis_client, cassandra_session, neo4j_driver):
    """Testa cancelamento RESTful via DELETE com LWT, atualização de histórico e limpeza do grafo."""
    headers = autenticar(http, 842)
    payload = {"espaco_id": 3, "data": AMANHA, "horario": "18:00"}

    # Cria reserva
    criada = http.post("/reservas", json=payload, headers=headers)
    assert criada.status_code == 201
    reserva_id = criada.json()["reserva_id"]

    # Cancela reserva
    resp_cancela = http.delete(f"/espacos/3/reservas/{AMANHA}/18:00", headers=headers)
    assert resp_cancela.status_code == 200
    assert resp_cancela.json()["status"] == "cancelada"

    # 1. Cassandra: ocupação removida
    cql = f"SELECT * FROM reservas_por_espaco WHERE espaco_id = 3 AND data_reserva = '{AMANHA}' AND horario = '18:00:00'"
    assert len(list(cassandra_session.execute(cql))) == 0

    # Cassandra: histórico mantém com status cancelada (RN05)
    linhas_hist = list(cassandra_session.execute(f"SELECT * FROM reservas_por_usuario WHERE usuario_id = 842"))
    assert len(linhas_hist) == 1
    assert linhas_hist[0].status == "cancelada"

    # 2. Redis: cota liberada
    cota_chave = chaves.reservas_ativas(842)
    assert redis_client.zcard(cota_chave) == 0

    # Slot liberado pode ser reservado novamente por outro usuário
    headers_outro = autenticar(http, 900)
    re_reserva = http.post("/reservas", json=payload, headers=headers_outro)
    assert re_reserva.status_code == 201

    # 3. Neo4j: nó da reserva cancelada removido
    time.sleep(0.5)
    with neo4j_driver.session() as s:
        res = s.run("MATCH (r:Reserva {id: $rid}) RETURN r", rid=reserva_id).single()
        assert res is None


def test_historico_mantem_canceladas_conforme_rn05(http):
    """Valida retorno do histórico do usuário incluindo status ativa e cancelada."""
    headers = autenticar(http, 842)

    # 1 ativa e 1 cancelada
    http.post("/reservas", json={"espaco_id": 3, "data": AMANHA, "horario": "09:00"}, headers=headers)
    http.post("/reservas", json={"espaco_id": 3, "data": AMANHA, "horario": "10:00"}, headers=headers)
    http.delete(f"/espacos/3/reservas/{AMANHA}/10:00", headers=headers)

    historico = http.get("/usuarios/842/reservas", headers=headers).json()
    assert len(historico) == 2
    status_list = {r["status"] for r in historico}
    assert status_list == {"ativa", "cancelada"}


def test_relatorio_unidade_real(http):
    """Valida agregação do relatório da unidade calculada sobre a partição Cassandra e catálogo."""
    headers = autenticar(http, 842)
    mes = AMANHA[:7]  # AAAA-MM

    http.post("/reservas", json={"espaco_id": 3, "data": AMANHA, "horario": "11:00"}, headers=headers)
    http.post("/reservas", json={"espaco_id": 3, "data": AMANHA, "horario": "12:00"}, headers=headers)
    http.delete(f"/espacos/3/reservas/{AMANHA}/12:00", headers=headers)

    resp = http.get("/unidades/1/relatorio", params={"mes": mes}, headers=headers)
    assert resp.status_code == 200
    rel = resp.json()

    assert rel["unidade_id"] == 1
    assert rel["ano_mes"] == mes
    assert rel["total_reservas"] == 2
    assert rel["total_ativas"] == 1
    assert rel["total_canceladas"] == 1
    assert rel["taxa_cancelamento_unidade"] == 0.5
    assert rel["taxa_ocupacao_unidade"] > 0
    assert len(rel["espacos"]) >= 1


def test_ranking_real_redis(http):
    """Valida o ranking por unidade utilizando o Sorted Set do Redis."""
    headers = autenticar(http, 842)

    # Reserva nos espaços 3 e 4 da unidade 1
    http.post("/reservas", json={"espaco_id": 3, "data": AMANHA, "horario": "13:00"}, headers=headers)
    http.post("/reservas", json={"espaco_id": 4, "data": AMANHA, "horario": "13:00"}, headers=headers)

    resp = http.get("/unidades/1/ranking")
    assert resp.status_code == 200
    ranking = resp.json()
    assert len(ranking) >= 2
    espacos_rank = [item["espaco_id"] for item in ranking]
    assert 3 in espacos_rank
    assert 4 in espacos_rank


def test_concorrencia_disputa_slot_automatizada(http):
    """Testa concorrência real executando scripts/disputa_slot.py contra a API real.

    Garante que entre dezenas de clientes disputando simultaneamente, exatamente 1 vence (201)
    e os demais são barrados (409) no lock do Redis ou na LWT do Cassandra.
    """
    data_disputa = DEPOIS_DE_AMANHA
    cmd = [
        sys.executable,
        "scripts/disputa_slot.py",
        "--url", API_URL,
        "--tentativas", "30",
        "--espaco", "1",
        "--data", data_disputa,
        "--horario", "19:00",
        "--usuario-inicial", "30000",
        "--participantes", "5",  # Quadra exige 6 pessoas (dono + 5)
    ]

    resultado = subprocess.run(cmd, capture_output=True, text=True)
    assert resultado.returncode == 0, f"Falha na execução de disputa_slot.py:\n{resultado.stderr}"
    assert "OK: 1 vencedor(es) para o slot." in resultado.stdout
    assert "201: 1" in resultado.stdout
    assert "409: 29" in resultado.stdout

    # Valida métricas reportadas pela API
    metricas = http.get("/metricas").json()
    assert metricas.get("confirmadas", 0) >= 1
    assert metricas.get("barradas_no_lock", 0) + metricas.get("rejeitadas_na_lwt", 0) >= 29
