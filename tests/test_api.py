from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from app import chaves
from app.main import criar_app


@pytest.fixture
def client(servico, catalogo):
    return TestClient(criar_app(servico, catalogo))


def auth(client: TestClient, usuario_id: int = 842) -> dict[str, str]:
    resp = client.post("/auth/login", json={"usuario_id": usuario_id, "senha": "senha123"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


SLOT = {"espaco_id": 1, "data": "2026-10-13", "horario": "19:00"}


def test_reservar_e_consultar(client):
    headers = auth(client, 842)
    resp = client.post("/reservas", json=SLOT, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "ativa"
    assert resp.json()["usuario_id"] == 842

    grade = client.get("/espacos/1/disponibilidade", params={"data": "2026-10-13"}).json()["grade"]
    assert grade["19:00"] == "ocupado"

    historico = client.get("/usuarios/842/reservas", headers=headers).json()
    assert len(historico) == 1


def test_disponibilidade_de_espaco_inexistente_retorna_404(client):
    resp = client.get("/espacos/999/disponibilidade", params={"data": "2026-10-13"})
    assert resp.status_code == 404


def test_conflito_retorna_409(client):
    client.post("/reservas", json=SLOT, headers=auth(client, 842))
    resp = client.post("/reservas", json=SLOT, headers=auth(client, 900))
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_horario_invalido_retorna_422(client):
    resp = client.post("/reservas", json={**SLOT, "horario": "07:00"}, headers=auth(client, 842))
    assert resp.status_code == 422


def test_cancelar(client):
    headers = auth(client, 842)
    client.post("/reservas", json=SLOT, headers=headers)

    url_cancelar = f"/espacos/{SLOT['espaco_id']}/reservas/{SLOT['data']}/{SLOT['horario']}"
    resp = client.delete(url_cancelar, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelada"
    assert client.delete(url_cancelar, headers=headers).status_code == 404


def test_painel_da_unidade_e_ranking(client):
    client.post("/reservas", json=SLOT, headers=auth(client, 842))
    admin = auth(client, 1)
    relatorio = client.get("/unidades/1/relatorio", params={"mes": "2026-10"}, headers=admin).json()

    assert relatorio["unidade_id"] == 1
    assert relatorio["ano_mes"] == "2026-10"
    assert relatorio["total_reservas"] == 1
    assert relatorio["total_ativas"] == 1
    assert relatorio["total_canceladas"] == 0
    assert relatorio["taxa_ocupacao_unidade"] > 0
    assert relatorio["taxa_cancelamento_unidade"] == 0.0

    # Na unidade 1 há 2 espaços configurados no conftest (1: quadra, 3: sala de estudo)
    assert len(relatorio["espacos"]) == 2
    espaco_1 = next(e for e in relatorio["espacos"] if e["espaco_id"] == 1)
    assert espaco_1["total_reservas"] == 1
    assert espaco_1["ativas"] == 1
    assert espaco_1["canceladas"] == 0
    assert espaco_1["taxa_ocupacao"] > 0
    assert espaco_1["taxa_cancelamento"] == 0.0
    assert len(relatorio["reservas"]) == 1

    assert client.get("/unidades/1/ranking").json() == [{"espaco_id": 1, "reservas": 1}]
    assert client.get("/unidades/1/relatorio", params={"mes": "out/26"}, headers=admin).status_code == 422


def test_reserva_com_participantes_e_consultas_do_grafo(client):
    headers_842 = auth(client, 842)
    headers_900 = auth(client, 900)

    resp = client.post("/reservas", json={**SLOT, "participantes": [900, 842]}, headers=headers_842)
    assert resp.status_code == 201
    assert resp.json()["participantes"] == [900]

    client.post(
        "/reservas",
        json={"espaco_id": 7, "data": "2026-10-13", "horario": "09:00"},
        headers=headers_900,
    )

    recs = client.get("/usuarios/842/recomendacoes", headers=headers_842).json()
    assert recs == [
        {"espaco_id": 7, "nome": "Sala de Reunião Ipê", "tipo": "sala_reuniao", "unidade_id": 3, "pessoas_em_comum": 1}
    ]
    assert client.get("/usuarios/842/parceiros", params={"tipo": "quadra"}, headers=headers_842).json() == []


def test_reserva_sem_participantes_mantem_contrato_antigo(client):
    assert client.post("/reservas", json=SLOT, headers=auth(client, 842)).json()["participantes"] == []


def test_participantes_demais_retorna_422(client):
    resp = client.post(
        "/reservas",
        json={**SLOT, "participantes": list(range(1000, 1031))},
        headers=auth(client, 842),
    )
    assert resp.status_code == 422


def test_grafo_fora_do_ar_retorna_503(client, grafo):
    grafo.falhar = True
    headers = auth(client, 842)
    assert client.post("/reservas", json=SLOT, headers=headers).status_code == 201
    assert client.get("/usuarios/842/recomendacoes", headers=headers).status_code == 503
    assert client.get("/usuarios/842/parceiros", headers=headers).status_code == 503


def test_catalogo(client):
    assert len(client.get("/espacos", params={"unidade_id": 1}).json()) == 2
    assert client.get("/espacos/7").json()["atributos"] == {"capacidade": 12}
    assert client.get("/espacos/999").status_code == 404


# ---- testes específicos da Issue #12 (Autenticação e Sessão no Redis) ---


def test_requisicao_sem_token_retorna_401(client):
    resp_reserva = client.post("/reservas", json=SLOT)
    assert resp_reserva.status_code == 401
    assert resp_reserva.headers.get("WWW-Authenticate") == "Bearer"

    resp_cancela = client.delete(f"/espacos/{SLOT['espaco_id']}/reservas/{SLOT['data']}/{SLOT['horario']}")
    assert resp_cancela.status_code == 401


def test_requisicao_com_token_invalido_ou_expirado_retorna_401(client, servico):
    # Token inexistente/inválido
    resp = client.post("/reservas", json=SLOT, headers={"Authorization": "Bearer token-inexistente"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Sessão inválida ou expirada."

    # Token expirado (removido do Redis)
    headers = auth(client, 842)
    token = headers["Authorization"].split(" ")[1]
    servico.redis.delete(chaves.sessao(token))

    resp_expirado = client.post("/reservas", json=SLOT, headers=headers)
    assert resp_expirado.status_code == 401
    assert resp_expirado.json()["detail"] == "Sessão inválida ou expirada."


def test_usuario_a_nao_consegue_cancelar_reserva_do_usuario_b(client, repo):
    data_reserva = date.fromisoformat(SLOT["data"])
    horario_reserva = time.fromisoformat(SLOT["horario"])

    # Usuário 842 reserva o slot
    client.post("/reservas", json=SLOT, headers=auth(client, 842))
    assert repo.obter_ocupacao(1, data_reserva, horario_reserva) is not None

    # Usuário 900 tenta cancelar a reserva de 842 via DELETE (RN04)
    url_cancelar = f"/espacos/{SLOT['espaco_id']}/reservas/{SLOT['data']}/{SLOT['horario']}"
    resp = client.delete(url_cancelar, headers=auth(client, 900))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Nenhuma reserva ativa sua neste horário."

    # A reserva do usuário 842 permanece intacta
    ocupacao = repo.obter_ocupacao(1, data_reserva, horario_reserva)
    assert ocupacao is not None
    assert ocupacao.usuario_id == 842


def test_renovacao_automatica_ttl_sessao_no_redis(client, servico):
    # Login cria sessão com TTL de 3600 s
    resp = client.post("/auth/login", json={"usuario_id": 842, "senha": "senha123"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    chave = chaves.sessao(token)

    ttl_inicial = servico.redis.ttl(chave)
    assert 3500 <= ttl_inicial <= 3600

    # Simula passagem de tempo reduzindo artificialmente o TTL para 60 s
    servico.redis.expire(chave, 60)
    assert servico.redis.ttl(chave) <= 60

    # Requisição autenticada dispara sliding expiration
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post("/reservas", json=SLOT, headers=headers)
    assert resp.status_code == 201

    # TTL deve ter sido renovado de volta para próximo de 3600 s
    novo_ttl = servico.redis.ttl(chave)
    assert novo_ttl > 60
    assert 3500 <= novo_ttl <= 3600


def test_logout_invalida_sessao(client, servico):
    headers = auth(client, 842)
    token = headers["Authorization"].split(" ")[1]
    chave = chaves.sessao(token)

    assert servico.redis.exists(chave)

    resp_logout = client.post("/auth/logout", headers=headers)
    assert resp_logout.status_code == 200
    assert not servico.redis.exists(chave)

    # Requisição seguinte com o mesmo token deve retornar 401
    resp_seguinte = client.post("/reservas", json=SLOT, headers=headers)
    assert resp_seguinte.status_code == 401


def test_login_credenciais_invalidas(client):
    # Senha incorreta
    resp = client.post("/auth/login", json={"usuario_id": 842, "senha": "senha-incorreta"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Credenciais inválidas."

    # Usuário inválido / inexistente
    resp = client.post("/auth/login", json={"usuario_id": -10, "senha": "senha123"})
    assert resp.status_code == 401


def test_login_sem_senha_e_rejeitado(client):
    assert client.post("/auth/login", json={"usuario_id": 842}).status_code == 422


# ---- controle de acesso às rotas de leitura --------------------------


@pytest.mark.parametrize("rota", ["reservas", "recomendacoes", "parceiros"])
def test_dados_pessoais_exigem_token(client, rota):
    assert client.get(f"/usuarios/842/{rota}").status_code == 401


@pytest.mark.parametrize("rota", ["reservas", "recomendacoes", "parceiros"])
def test_usuario_nao_ve_dados_pessoais_de_outro(client, rota):
    resp = client.get(f"/usuarios/842/{rota}", headers=auth(client, 900))
    assert resp.status_code == 403


def test_admin_ve_dados_de_qualquer_usuario(client):
    client.post("/reservas", json=SLOT, headers=auth(client, 842))
    resp = client.get("/usuarios/842/reservas", headers=auth(client, 1))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_relatorio_exige_admin(client):
    params = {"mes": "2026-10"}
    assert client.get("/unidades/1/relatorio", params=params).status_code == 401
    assert client.get("/unidades/1/relatorio", params=params, headers=auth(client, 842)).status_code == 403
    assert client.get("/unidades/1/relatorio", params=params, headers=auth(client, 1)).status_code == 200


def test_ranking_continua_publico(client):
    # Agregado, sem dado pessoal: pode aparecer na tela inicial sem login.
    assert client.get("/unidades/1/ranking").status_code == 200


def test_front_da_demo_e_servido_na_raiz(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Reserv" in resp.text
