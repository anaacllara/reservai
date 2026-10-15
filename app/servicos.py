"""Regras de negócio do Reservaí. Implementa os fluxos da seção 2.3 do Marco 1.

O serviço só conhece interfaces (RepositorioReservas, CatalogoEspacos) e um cliente
Redis, o que permite testá-lo com fakes, sem subir Cassandra nem Mongo.
"""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis import Redis

from app import chaves
from app.config import Settings
from app.dominio import (
    CatalogoEspacos,
    Espaco,
    EspacoNaoEncontrado,
    HorarioInvalido,
    LimiteReservasAtingido,
    RepositorioReservas,
    Reserva,
    ReservaNaoEncontrada,
    SlotIndisponivel,
)

_LUA = Path(__file__).parent / "lua"


class ServicoReservas:
    def __init__(
        self,
        redis: Redis,
        repositorio: RepositorioReservas,
        catalogo: CatalogoEspacos,
        cfg: Settings,
        agora: Callable[[], datetime] | None = None,
    ):
        self.redis = redis
        self.repo = repositorio
        self.catalogo = catalogo
        self.cfg = cfg
        self.fuso = ZoneInfo(cfg.fuso)
        self._agora = agora or (lambda: datetime.now(self.fuso))
        self._liberar_lock = redis.register_script((_LUA / "liberar_lock.lua").read_text())
        self._reservar_cota = redis.register_script((_LUA / "reservar_cota.lua").read_text())
        # Pré-carrega os scripts: no pico, a primeira chamada não paga o NOSCRIPT + reenvio.
        for script in (self._liberar_lock, self._reservar_cota):
            redis.script_load(script.script)

    # ---- RF02: reservar -------------------------------------------------

    def reservar(self, usuario_id: int, espaco_id: int, data: date, horario: time) -> Reserva:
        espaco = self._espaco(espaco_id)
        inicio = self._validar_slot(espaco, data, horario)
        reserva = Reserva(
            reserva_id=uuid4(),
            usuario_id=usuario_id,
            espaco_id=espaco_id,
            unidade_id=espaco.unidade_id,
            data=data,
            horario=horario,
            status="ativa",
            criado_em=self._agora(),
        )
        self._metrica("tentativas")

        # Passo 1a: lock do slot. É filtro de desempenho, não a garantia (Marco 1, 3.4):
        # só um candidato por slot chega à LWT, os demais são barrados em memória.
        lock = chaves.lock_slot(espaco_id, data, horario)
        token = str(reserva.reserva_id)
        if not self.redis.set(lock, token, nx=True, ex=self.cfg.lock_ttl_s):
            self._metrica("barradas_no_lock")
            raise SlotIndisponivel("Horário em disputa ou já reservado. Tente outro horário.")
        try:
            return self._confirmar(reserva, inicio)
        finally:
            self._liberar_lock(keys=[lock], args=[token])

    def _confirmar(self, reserva: Reserva, inicio: datetime) -> Reserva:
        # Passo 1b: cota de reservas futuras do usuário (RF06), atômica via Lua.
        cota = chaves.reservas_ativas(reserva.usuario_id)
        membro = str(reserva.reserva_id)
        consumiu = self._reservar_cota(
            keys=[cota],
            args=[self._agora().timestamp(), inicio.timestamp(), membro, self.cfg.limite_reservas_ativas],
        )
        if not consumiu:
            self._metrica("barradas_na_cota")
            raise LimiteReservasAtingido(
                f"Limite de {self.cfg.limite_reservas_ativas} reservas futuras atingido."
            )

        # Passo 2: LWT no Cassandra. Esta é a garantia real de unicidade do slot.
        try:
            aplicada = self.repo.inserir_ocupacao(reserva)
        except Exception:
            self.redis.zrem(cota, membro)
            raise
        if not aplicada:
            self.redis.zrem(cota, membro)
            self._metrica("rejeitadas_na_lwt")
            raise SlotIndisponivel("Horário já reservado. Tente outro horário.")

        # Passo 3: histórico (BATCH logged). A ocupação já é a fonte de verdade;
        # se esta escrita falhar, o slot continua corretamente ocupado.
        self.repo.registrar_historico(reserva)

        # Passo 4: cache e ranking (RF07).
        self._atualizar_cache(reserva, "ocupado")
        self.redis.zincrby(chaves.ranking(reserva.unidade_id), 1, chaves.membro_ranking(reserva.espaco_id))
        self._metrica("confirmadas")
        return reserva

    # ---- RF03: cancelar -------------------------------------------------

    def cancelar(self, usuario_id: int, espaco_id: int, data: date, horario: time) -> Reserva:
        # Lê a ocupação antes: o UPDATE do histórico precisa da PK completa (reserva_id).
        reserva = self.repo.obter_ocupacao(espaco_id, data, horario)
        if reserva is None or reserva.usuario_id != usuario_id:
            raise ReservaNaoEncontrada("Nenhuma reserva ativa sua neste horário.")
        if datetime.combine(data, horario, tzinfo=self.fuso) <= self._agora():
            raise HorarioInvalido("Não é possível cancelar uma reserva que já começou.")

        # LWT: só o dono remove, e só se a reserva ainda é a mesma.
        if not self.repo.remover_ocupacao(reserva):
            raise ReservaNaoEncontrada("A reserva foi alterada por outra operação.")

        cancelada = reserva.cancelada()
        self.repo.marcar_cancelada(cancelada)
        self.redis.zrem(chaves.reservas_ativas(usuario_id), str(reserva.reserva_id))
        self._atualizar_cache(reserva, "livre")
        self._metrica("canceladas")
        return cancelada

    # ---- RF01: disponibilidade -----------------------------------------

    def grade(self, espaco_id: int, data: date) -> dict[str, str]:
        espaco = self._espaco(espaco_id)
        chave = chaves.disponibilidade(espaco_id, data)
        if cache := self.redis.hgetall(chave):
            return dict(sorted(cache.items()))

        # Cache não é fonte de verdade: reconstrói a partir de uma única partição.
        ocupados = {r.horario for r in self.repo.ocupacoes_do_dia(espaco_id, data)}
        grade = {
            f"{h:02d}:00": "ocupado" if time(h) in ocupados else "livre"
            for h in range(espaco.abertura, espaco.fechamento)
        }
        pipe = self.redis.pipeline()
        pipe.hset(chave, mapping=grade)
        pipe.expire(chave, self.cfg.cache_disponibilidade_ttl_s)
        pipe.execute()
        return grade

    # ---- RF04, RF05, RF07: consultas ----------------------------------

    def historico_usuario(self, usuario_id: int, limite: int = 50) -> list[Reserva]:
        return self.repo.reservas_do_usuario(usuario_id, limite)

    def ocupacao_unidade(self, unidade_id: int, ano_mes: str) -> list[Reserva]:
        return self.repo.reservas_da_unidade(unidade_id, ano_mes)

    def ranking(self, unidade_id: int, top: int = 10) -> list[tuple[int, int]]:
        itens = self.redis.zrevrange(chaves.ranking(unidade_id), 0, top - 1, withscores=True)
        return [(int(membro.split(":")[1]), int(score)) for membro, score in itens]

    def metricas(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.redis.hgetall(chaves.METRICAS).items()}

    # ---- auxiliares -----------------------------------------------------

    def _espaco(self, espaco_id: int) -> Espaco:
        espaco = self.catalogo.obter(espaco_id)
        if espaco is None:
            raise EspacoNaoEncontrado(f"Espaço {espaco_id} não existe.")
        return espaco

    def _validar_slot(self, espaco: Espaco, data: date, horario: time) -> datetime:
        if horario.minute or horario.second or horario.microsecond:
            raise HorarioInvalido("Os slots começam sempre na hora cheia (ex.: 19:00).")
        if not espaco.abertura <= horario.hour < espaco.fechamento:
            raise HorarioInvalido(
                f"{espaco.nome} funciona das {espaco.abertura:02d}:00 às {espaco.fechamento:02d}:00."
            )
        inicio = datetime.combine(data, horario, tzinfo=self.fuso)
        agora = self._agora()
        if inicio <= agora:
            raise HorarioInvalido("Este horário já passou.")
        if data > agora.date() + timedelta(days=self.cfg.dias_agenda_aberta):
            raise HorarioInvalido(f"A agenda só abre {self.cfg.dias_agenda_aberta} dias à frente.")
        return inicio

    def _atualizar_cache(self, reserva: Reserva, estado: str) -> None:
        # Só atualiza um cache existente; criar um hash parcial esconderia os outros slots.
        chave = chaves.disponibilidade(reserva.espaco_id, reserva.data)
        if self.redis.exists(chave):
            self.redis.hset(chave, f"{reserva.horario:%H:%M}", estado)

    def _metrica(self, nome: str) -> None:
        self.redis.hincrby(chaves.METRICAS, nome, 1)
