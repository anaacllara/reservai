"""RF08, RF09 e a projeção das reservas no grafo (Neo4j, aqui com o GrafoFake)."""

from datetime import time

import pytest

from app import chaves
from app.config import Settings
from app.dominio import GrafoIndisponivel, ParticipantesInvalidos
from app.servicos import ServicoReservas
from tests.conftest import AMANHA, H19

# Espaços do conftest: 1 = quadra, 3 = sala_estudo (unidade 1); 7 = sala_reuniao (unidade 3).


# ---- participantes -----------------------------------------------------


def test_participantes_sao_normalizados_e_gravados_no_cassandra_e_no_grafo(servico, repo, grafo):
    r = servico.reservar(842, 1, AMANHA, H19, participantes=[901, 900, 901, 842])

    assert r.participantes == (900, 901)  # sem duplicatas, sem o dono, ordenados
    assert repo.obter_ocupacao(1, AMANHA, H19).participantes == (900, 901)
    assert repo.reservas_do_usuario(842, 1)[0].participantes == (900, 901)
    assert grafo.reservas[r.reserva_id] == ({842, 900, 901}, 1, "quadra")


def test_reserva_sem_participantes_continua_valida(servico, grafo):
    r = servico.reservar(842, 1, AMANHA, H19)
    assert r.participantes == ()
    assert grafo.reservas[r.reserva_id] == ({842}, 1, "quadra")


def test_participantes_demais_sao_rejeitados_antes_de_tocar_os_bancos(servico, repo, redis):
    with pytest.raises(ParticipantesInvalidos):
        servico.reservar(842, 1, AMANHA, H19, participantes=range(1000, 1031))  # 31 > 30
    assert repo.lwt_tentativas == 0
    assert not redis.exists(chaves.lock_slot(1, AMANHA, H19))


def test_o_dono_nao_conta_no_limite_de_participantes(servico):
    servico.reservar(842, 1, AMANHA, H19, participantes=[842, *range(1000, 1030)])  # 30 + dono


# ---- consistência com o fluxo principal --------------------------------


def test_cancelar_remove_a_reserva_do_grafo(servico, grafo):
    r = servico.reservar(842, 1, AMANHA, H19, participantes=[900])
    servico.cancelar(842, 1, AMANHA, H19)
    assert r.reserva_id not in grafo.reservas


def test_projecao_no_grafo_roda_fora_da_requisicao_e_em_ordem(redis, repo, catalogo, grafo, relogio):
    pendentes = []
    servico = ServicoReservas(
        redis, repo, catalogo, grafo, Settings(_env_file=None), agora=relogio, segundo_plano=pendentes.append
    )
    r = servico.reservar(842, 1, AMANHA, H19)
    servico.cancelar(842, 1, AMANHA, H19)

    assert grafo.reservas == {}  # a reserva e o cancelamento responderam sem esperar o grafo
    pendentes[0]()
    assert r.reserva_id in grafo.reservas
    pendentes[1]()
    assert grafo.reservas == {}  # registrar -> remover, na ordem em que aconteceram


def test_falha_no_grafo_nao_impede_a_reserva(servico, repo, redis, grafo):
    grafo.falhar = True
    r = servico.reservar(842, 1, AMANHA, H19, participantes=[900])

    assert repo.obter_ocupacao(1, AMANHA, H19) == r  # a reserva vale: o Cassandra é a fonte de verdade
    assert not redis.exists(chaves.lock_slot(1, AMANHA, H19))
    assert servico.metricas()["falhas_no_grafo"] == 1
    assert servico.metricas()["confirmadas"] == 1


def test_falha_no_grafo_nao_impede_o_cancelamento(servico, repo, grafo):
    servico.reservar(842, 1, AMANHA, H19)
    grafo.falhar = True
    servico.cancelar(842, 1, AMANHA, H19)
    assert repo.obter_ocupacao(1, AMANHA, H19) is None
    assert servico.metricas()["falhas_no_grafo"] == 1


def test_grafo_fora_do_ar_nas_consultas_vira_erro_503(servico, grafo):
    grafo.falhar = True
    with pytest.raises(GrafoIndisponivel):
        servico.recomendacoes(842)
    with pytest.raises(GrafoIndisponivel):
        servico.parceiros(842)


# ---- RF08: recomendação de espaços -------------------------------------


def test_recomenda_espacos_de_quem_usa_os_mesmos_espacos(servico):
    servico.reservar(842, 1, AMANHA, time(8), participantes=[900])  # 842 e 900 na quadra
    servico.reservar(900, 3, AMANHA, time(9))  # 900 também usa a sala de estudo
    servico.reservar(901, 1, AMANHA, time(10))  # 901 usa a quadra (em outro horário)
    servico.reservar(901, 7, AMANHA, time(8))  # ...e a sala de reunião
    servico.reservar(902, 1, AMANHA, time(11))
    servico.reservar(902, 7, AMANHA, time(9))

    recs = [(e.id, n) for e, n in servico.recomendacoes(842)]

    # Sala de reunião: 901 e 902 levam até ela. Sala de estudo: só 900. A quadra já é minha.
    assert recs == [(7, 2), (3, 1)]


def test_sem_historico_nao_ha_recomendacao(servico):
    servico.reservar(900, 1, AMANHA, H19)
    assert servico.recomendacoes(842) == []


def test_reserva_cancelada_nao_entra_na_recomendacao(servico):
    servico.reservar(842, 1, AMANHA, time(8))
    servico.reservar(901, 1, AMANHA, time(10))
    servico.reservar(901, 7, AMANHA, time(8))
    servico.cancelar(901, 1, AMANHA, time(10))  # 901 deixa de "usar" a quadra
    assert servico.recomendacoes(842) == []


def test_limite_de_recomendacoes(servico):
    servico.reservar(842, 1, AMANHA, time(8), participantes=[900])
    servico.reservar(900, 3, AMANHA, time(9))
    servico.reservar(900, 7, AMANHA, time(9))
    assert len(servico.recomendacoes(842, limite=1)) == 1


# ---- RF09: sugestão de parceiros ---------------------------------------


@pytest.fixture
def rede(servico):
    """842 já dividiu reserva com 900 e 843. Ninguém mais é amigo direto de 842."""
    servico.reservar(842, 1, AMANHA, time(8), participantes=[900])  # quadra
    servico.reservar(843, 3, AMANHA, time(8), participantes=[842])  # sala de estudo
    servico.reservar(900, 3, AMANHA, time(9), participantes=[950])  # 900 estuda com 950
    servico.reservar(960, 1, AMANHA, time(12), participantes=[900, 951])  # 900 joga com 960 e 951
    servico.reservar(843, 1, AMANHA, time(16), participantes=[951])  # 843 também joga com 951


def test_sugere_amigos_de_amigos_por_amigos_em_comum(servico, rede):
    # 951 joga com 900 e com 843 (2 amigos em comum); 950 e 960, só com 900.
    assert servico.parceiros(842) == [(951, 2), (950, 1), (960, 1)]


def test_filtro_por_tipo_vale_para_o_segundo_salto(servico, rede):
    # 950 só estuda com 900 (sala_estudo), então some quando o filtro é quadra.
    assert servico.parceiros(842, tipo="quadra") == [(951, 2), (960, 1)]
    assert servico.parceiros(842, tipo="sala_estudo") == [(950, 1)]


def test_quem_ja_e_amigo_nao_e_sugerido(servico, rede):
    sugeridos = {u for u, _ in servico.parceiros(842)}
    assert sugeridos.isdisjoint({842, 900, 843})
