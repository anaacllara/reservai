import pytest
from fastapi.testclient import TestClient

from app.main import criar_app


@pytest.fixture
def client(servico, catalogo):
    return TestClient(criar_app(servico, catalogo))


SLOT = {"usuario_id": 842, "espaco_id": 1, "data": "2026-10-13", "horario": "19:00"}


def test_reservar_e_consultar(client):
    resp = client.post("/reservas", json=SLOT)
    assert resp.status_code == 201
    assert resp.json()["status"] == "ativa"

    grade = client.get("/espacos/1/disponibilidade", params={"data": "2026-10-13"}).json()["grade"]
    assert grade["19:00"] == "ocupado"

    historico = client.get("/usuarios/842/reservas").json()
    assert len(historico) == 1


def test_disponibilidade_de_espaco_inexistente_retorna_404(client):
    resp = client.get("/espacos/999/disponibilidade", params={"data": "2026-10-13"})
    assert resp.status_code == 404


def test_conflito_retorna_409(client):
    client.post("/reservas", json=SLOT)
    resp = client.post("/reservas", json={**SLOT, "usuario_id": 900})
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_horario_invalido_retorna_422(client):
    resp = client.post("/reservas", json={**SLOT, "horario": "07:00"})
    assert resp.status_code == 422


def test_cancelar(client):
    client.post("/reservas", json=SLOT)
    resp = client.post("/reservas/cancelar", json=SLOT)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelada"
    assert client.post("/reservas/cancelar", json=SLOT).status_code == 404


def test_painel_da_unidade_e_ranking(client):
    client.post("/reservas", json=SLOT)
    relatorio = client.get("/unidades/1/relatorio", params={"mes": "2026-10"}).json()
    assert len(relatorio) == 1
    assert client.get("/unidades/1/ranking").json() == [{"espaco_id": 1, "reservas": 1}]
    assert client.get("/unidades/1/relatorio", params={"mes": "out/26"}).status_code == 422


def test_reserva_com_participantes_e_consultas_do_grafo(client):
    resp = client.post("/reservas", json={**SLOT, "participantes": [900, 842]})
    assert resp.status_code == 201
    assert resp.json()["participantes"] == [900]
    client.post("/reservas", json={**SLOT, "usuario_id": 900, "espaco_id": 7, "horario": "09:00"})

    recs = client.get("/usuarios/842/recomendacoes").json()
    assert recs == [
        {"espaco_id": 7, "nome": "Sala de Reunião Ipê", "tipo": "sala_reuniao", "unidade_id": 3, "pessoas_em_comum": 1}
    ]
    assert client.get("/usuarios/842/parceiros", params={"tipo": "quadra"}).json() == []


def test_reserva_sem_participantes_mantem_contrato_antigo(client):
    assert client.post("/reservas", json=SLOT).json()["participantes"] == []


def test_participantes_demais_retorna_422(client):
    resp = client.post("/reservas", json={**SLOT, "participantes": list(range(1000, 1031))})
    assert resp.status_code == 422


def test_grafo_fora_do_ar_retorna_503(client, grafo):
    grafo.falhar = True
    assert client.post("/reservas", json=SLOT).status_code == 201  # a reserva não depende do grafo
    assert client.get("/usuarios/842/recomendacoes").status_code == 503
    assert client.get("/usuarios/842/parceiros").status_code == 503


def test_catalogo(client):
    assert len(client.get("/espacos", params={"unidade_id": 1}).json()) == 2
    assert client.get("/espacos/7").json()["atributos"] == {"capacidade": 12}
    assert client.get("/espacos/999").status_code == 404
