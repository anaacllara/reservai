from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta

import pytest

from app import chaves
from app.dominio import (
    EspacoNaoEncontrado,
    HorarioInvalido,
    LimiteReservasAtingido,
    ReservaNaoEncontrada,
    SlotIndisponivel,
)
from tests.conftest import AGORA, AMANHA, H19

# ---- RF02: reservar ----------------------------------------------------


def test_reserva_grava_ocupacao_historico_cota_e_ranking(servico, repo, redis):
    r = servico.reservar(842, 1, AMANHA, H19)

    assert repo.obter_ocupacao(1, AMANHA, H19) == r
    assert r in repo.reservas_do_usuario(842, 10)
    assert r in repo.reservas_da_unidade(1, "2026-10")
    assert redis.zscore(chaves.reservas_ativas(842), str(r.reserva_id)) is not None
    assert servico.ranking(1) == [(1, 1)]


def test_lock_e_liberado_apos_confirmar(servico, redis):
    servico.reservar(842, 1, AMANHA, H19)
    assert not redis.exists(chaves.lock_slot(1, AMANHA, H19))


def test_segundo_usuario_no_mesmo_slot_e_rejeitado_pela_lwt(servico, repo):
    servico.reservar(842, 1, AMANHA, H19)
    with pytest.raises(SlotIndisponivel):
        servico.reservar(900, 1, AMANHA, H19)
    assert repo.lwt_tentativas == 2
    assert servico.metricas()["rejeitadas_na_lwt"] == 1


def test_slot_com_lock_ativo_e_barrado_sem_chegar_na_lwt(servico, repo, redis):
    redis.set(chaves.lock_slot(1, AMANHA, H19), "outro-candidato", ex=30)
    with pytest.raises(SlotIndisponivel):
        servico.reservar(842, 1, AMANHA, H19)
    assert repo.lwt_tentativas == 0
    assert redis.get(chaves.lock_slot(1, AMANHA, H19)) == "outro-candidato"


def test_disputa_concorrente_tem_exatamente_um_vencedor(servico, repo):
    def tentar(usuario_id):
        try:
            servico.reservar(usuario_id, 1, AMANHA, H19)
            return True
        except SlotIndisponivel:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        resultados = list(pool.map(tentar, range(1, 201)))

    assert resultados.count(True) == 1
    assert len(repo.ocupacao) == 1
    m = servico.metricas()
    assert m["tentativas"] == 200
    assert m["confirmadas"] == 1


# ---- RF06: cota de reservas futuras --------------------------------


def test_terceira_reserva_futura_e_bloqueada(servico, repo):
    servico.reservar(842, 1, AMANHA, time(8))
    servico.reservar(842, 1, AMANHA, time(9))
    with pytest.raises(LimiteReservasAtingido):
        servico.reservar(842, 3, AMANHA, time(10))
    assert repo.obter_ocupacao(3, AMANHA, time(10)) is None


def test_reserva_que_ja_aconteceu_libera_a_cota(servico, relogio):
    servico.reservar(842, 1, AMANHA, time(8))
    servico.reservar(842, 1, AMANHA, time(9))
    relogio.agora = relogio.agora.replace(day=13, hour=8, minute=30)  # a das 8h já começou
    servico.reservar(842, 1, AMANHA, time(10))


def test_lwt_rejeitada_devolve_a_cota(servico, redis):
    servico.reservar(900, 1, AMANHA, H19)
    with pytest.raises(SlotIndisponivel):
        servico.reservar(842, 1, AMANHA, H19)
    assert redis.zcard(chaves.reservas_ativas(842)) == 0


def test_falha_no_cassandra_devolve_a_cota_e_libera_lock(servico, repo, redis):
    repo.falhar_lwt = True
    with pytest.raises(TimeoutError):
        servico.reservar(842, 1, AMANHA, H19)
    assert redis.zcard(chaves.reservas_ativas(842)) == 0
    assert not redis.exists(chaves.lock_slot(1, AMANHA, H19))


# ---- validação do slot -----------------------------------------------


@pytest.mark.parametrize(
    ("data", "horario"),
    [
        (AMANHA, time(19, 30)),  # fora da hora cheia
        (AMANHA, time(7)),  # antes da abertura
        (AMANHA, time(20)),  # no fechamento (último slot é 19h)
        (AGORA.date(), time(9)),  # já passou
        (AGORA.date() + timedelta(days=8), time(9)),  # agenda ainda fechada
    ],
)
def test_slot_invalido(servico, repo, data, horario):
    with pytest.raises(HorarioInvalido):
        servico.reservar(842, 1, data, horario)
    assert repo.lwt_tentativas == 0


def test_ultimo_dia_da_agenda_e_aceito(servico):
    servico.reservar(842, 1, AGORA.date() + timedelta(days=7), time(19))


def test_espaco_inexistente(servico):
    with pytest.raises(EspacoNaoEncontrado):
        servico.reservar(842, 999, AMANHA, H19)


# ---- RF03: cancelar --------------------------------------------------


def test_cancelar_libera_slot_cota_e_mantem_historico(servico, repo, redis):
    r = servico.reservar(842, 1, AMANHA, H19)
    servico.cancelar(842, 1, AMANHA, H19)

    assert repo.obter_ocupacao(1, AMANHA, H19) is None
    assert redis.zcard(chaves.reservas_ativas(842)) == 0
    assert [x.status for x in repo.reservas_do_usuario(842, 10)] == ["cancelada"]
    servico.reservar(900, 1, AMANHA, H19)  # o slot volta a aceitar reserva

    # Correção do Marco 1: o cancelamento continua no histórico da unidade.
    historico = repo.reservas_da_unidade(1, "2026-10")
    assert {(x.reserva_id == r.reserva_id, x.status) for x in historico} == {
        (True, "cancelada"),
        (False, "ativa"),
    }


def test_so_o_dono_cancela(servico, repo):
    servico.reservar(842, 1, AMANHA, H19)
    with pytest.raises(ReservaNaoEncontrada):
        servico.cancelar(900, 1, AMANHA, H19)
    assert repo.obter_ocupacao(1, AMANHA, H19) is not None


def test_cancelar_slot_vazio(servico):
    with pytest.raises(ReservaNaoEncontrada):
        servico.cancelar(842, 1, AMANHA, H19)


def test_nao_cancela_reserva_que_ja_comecou(servico, relogio):
    servico.reservar(842, 1, AMANHA, H19)
    relogio.agora = relogio.agora.replace(day=13, hour=19, minute=10)
    with pytest.raises(HorarioInvalido):
        servico.cancelar(842, 1, AMANHA, H19)


# ---- RF01: disponibilidade -------------------------------------------


def test_grade_reconstroi_do_repositorio_e_grava_cache_com_ttl(servico, redis):
    servico.reservar(842, 1, AMANHA, H19)
    grade = servico.grade(1, AMANHA)

    assert len(grade) == 12
    assert grade["19:00"] == "ocupado"
    assert grade["08:00"] == "livre"
    assert 0 < redis.ttl(chaves.disponibilidade(1, AMANHA)) <= 300


def test_cache_e_atualizado_em_reserva_e_cancelamento(servico):
    servico.grade(1, AMANHA)  # aquece o cache
    servico.reservar(842, 1, AMANHA, H19)
    assert servico.grade(1, AMANHA)["19:00"] == "ocupado"
    servico.cancelar(842, 1, AMANHA, H19)
    assert servico.grade(1, AMANHA)["19:00"] == "livre"


def test_grade_de_espaco_inexistente(servico, redis):
    with pytest.raises(EspacoNaoEncontrado):
        servico.grade(999, AMANHA)
    assert not redis.exists(chaves.disponibilidade(999, AMANHA))


def test_reserva_sem_cache_nao_cria_grade_parcial(servico, redis):
    servico.reservar(842, 1, AMANHA, H19)
    assert not redis.exists(chaves.disponibilidade(1, AMANHA))


# ---- RF04, RF07 ------------------------------------------------------


def test_historico_do_usuario_mais_recente_primeiro(servico):
    servico.reservar(842, 1, AMANHA, time(8))
    servico.reservar(842, 3, date(2026, 10, 14), time(9))
    datas = [r.data for r in servico.historico_usuario(842)]
    assert datas == [date(2026, 10, 14), AMANHA]


def test_ranking_por_unidade(servico):
    servico.reservar(1, 1, AMANHA, time(8))
    servico.reservar(2, 1, AMANHA, time(9))
    servico.reservar(3, 3, AMANHA, time(8))
    servico.reservar(4, 7, AMANHA, time(8))  # outra unidade
    assert servico.ranking(1) == [(1, 2), (3, 1)]
    assert servico.ranking(3) == [(7, 1)]
