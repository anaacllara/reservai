"""Regras de negócio do Reservaí. Implementa os fluxos da seção 2.3 do Marco 1.

O serviço só conhece interfaces (RepositorioReservas, CatalogoEspacos, GrafoRelacionamentos)
e um cliente Redis, o que permite testá-lo com fakes, sem subir Cassandra, Mongo nem Neo4j.
"""

import calendar
import logging
from collections.abc import Callable, Iterable
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
    GrafoIndisponivel,
    GrafoRelacionamentos,
    HorarioInvalido,
    LimiteReservasAtingido,
    MetricasEspaco,
    ParticipantesInvalidos,
    RegraDoEspacoViolada,
    RelatorioUnidade,
    RepositorioReservas,
    Reserva,
    ReservaNaoEncontrada,
    SlotIndisponivel,
)

_LUA = Path(__file__).parent / "lua"
log = logging.getLogger(__name__)


class ServicoReservas:
    def __init__(
        self,
        redis: Redis,
        repositorio: RepositorioReservas,
        catalogo: CatalogoEspacos,
        grafo: GrafoRelacionamentos,
        cfg: Settings,
        agora: Callable[[], datetime] | None = None,
        segundo_plano: Callable[[Callable[[], None]], object] | None = None,
    ):
        self.redis = redis
        self.repo = repositorio
        self.catalogo = catalogo
        self.grafo = grafo
        self.cfg = cfg
        # Onde rodam as escritas no grafo. Na aplicação: uma fila com uma única thread
        # (a resposta não espera o Neo4j e a ordem registrar -> remover é preservada).
        # Nos testes: na hora, para as asserções serem determinísticas.
        self._segundo_plano = segundo_plano or (lambda tarefa: tarefa())
        self.fuso = ZoneInfo(cfg.fuso)
        self._agora = agora or (lambda: datetime.now(self.fuso))
        self._liberar_lock = redis.register_script((_LUA / "liberar_lock.lua").read_text())
        self._reservar_cota = redis.register_script((_LUA / "reservar_cota.lua").read_text())
        # Pré-carrega os scripts: no pico, a primeira chamada não paga o NOSCRIPT + reenvio.
        for script in (self._liberar_lock, self._reservar_cota):
            redis.script_load(script.script)

    # ---- RF02: reservar -------------------------------------------------

    def reservar(
        self,
        usuario_id: int,
        espaco_id: int,
        data: date,
        horario: time,
        participantes: Iterable[int] = (),
    ) -> Reserva:
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
            participantes=self._normalizar_participantes(usuario_id, participantes),
        )
        # Regras do catálogo (Mongo) antes do lock: uma reserva inválida não disputa o slot.
        self._validar_regras(espaco, reserva)
        self._metrica("tentativas")

        # Passo 1a: lock do slot. É filtro de desempenho, não a garantia (Marco 1, 3.4):
        # só um candidato por slot chega à LWT, os demais são barrados em memória.
        lock = chaves.lock_slot(espaco_id, data, horario)
        token = str(reserva.reserva_id)
        if not self.redis.set(lock, token, nx=True, ex=self.cfg.lock_ttl_s):
            self._metrica("barradas_no_lock")
            raise SlotIndisponivel("Horário em disputa ou já reservado. Tente outro horário.")
        try:
            confirmada = self._confirmar(reserva, inicio)
        finally:
            self._liberar_lock(keys=[lock], args=[token])

        # Passo 5: projeção no grafo, depois de liberar o lock (não alonga a disputa no pico).
        self._no_grafo(self.grafo.registrar_reserva, confirmada, espaco)
        return confirmada

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
        self._no_grafo(self.grafo.remover_reserva, cancelada)
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

    def relatorio_unidade(self, unidade_id: int, ano_mes: str) -> RelatorioUnidade:
        """RF05: reservas da unidade no mês agregadas com taxa de ocupação e cancelamento por espaço."""
        ano, mes = map(int, ano_mes.split("-"))
        _, dias_no_mes = calendar.monthrange(ano, mes)
        reservas = self.repo.reservas_da_unidade(unidade_id, ano_mes)
        espacos_unidade = self.catalogo.listar(unidade_id)

        metricas_espacos: list[MetricasEspaco] = []
        for e in espacos_unidade:
            total_espaco = sum(1 for r in reservas if r.espaco_id == e.id)
            ativas_espaco = sum(1 for r in reservas if r.espaco_id == e.id and r.status == "ativa")
            canceladas_espaco = sum(1 for r in reservas if r.espaco_id == e.id and r.status == "cancelada")
            slots_dia = max(0, e.fechamento - e.abertura)
            slots_mes = slots_dia * dias_no_mes
            taxa_canc = round(canceladas_espaco / total_espaco, 4) if total_espaco > 0 else 0.0
            taxa_ocup = round(ativas_espaco / slots_mes, 4) if slots_mes > 0 else 0.0

            metricas_espacos.append(
                MetricasEspaco(
                    espaco_id=e.id,
                    nome=e.nome,
                    tipo=e.tipo,
                    total_reservas=total_espaco,
                    ativas=ativas_espaco,
                    canceladas=canceladas_espaco,
                    taxa_cancelamento=taxa_canc,
                    slots_disponiveis_mes=slots_mes,
                    taxa_ocupacao=taxa_ocup,
                )
            )

        total_reservas = len(reservas)
        total_ativas = sum(1 for r in reservas if r.status == "ativa")
        total_canceladas = sum(1 for r in reservas if r.status == "cancelada")
        slots_unidade = sum(m.slots_disponiveis_mes for m in metricas_espacos)
        taxa_canc_unidade = round(total_canceladas / total_reservas, 4) if total_reservas > 0 else 0.0
        taxa_ocup_unidade = round(total_ativas / slots_unidade, 4) if slots_unidade > 0 else 0.0

        return RelatorioUnidade(
            unidade_id=unidade_id,
            ano_mes=ano_mes,
            total_reservas=total_reservas,
            total_ativas=total_ativas,
            total_canceladas=total_canceladas,
            taxa_cancelamento_unidade=taxa_canc_unidade,
            taxa_ocupacao_unidade=taxa_ocup_unidade,
            espacos=metricas_espacos,
            reservas=reservas,
        )

    def ranking(self, unidade_id: int, top: int = 10) -> list[tuple[int, int]]:
        itens = self.redis.zrevrange(chaves.ranking(unidade_id), 0, top - 1, withscores=True)
        return [(int(membro.split(":")[1]), int(score)) for membro, score in itens]

    def metricas(self) -> dict[str, int]:
        return {k: int(v) for k, v in self.redis.hgetall(chaves.METRICAS).items()}

    # ---- RF08, RF09: grafo -----------------------------------------------

    def recomendacoes(self, usuario_id: int, limite: int = 5) -> list[tuple[Espaco, int]]:
        """RF08: espaços usados por quem divide espaços comigo e que eu ainda não usei."""
        try:
            itens = self.grafo.recomendar_espacos(usuario_id, limite)
        except Exception as exc:
            raise GrafoIndisponivel("Recomendações indisponíveis no momento.") from exc
        # O grafo guarda só o necessário para a travessia; nome e detalhes vêm do catálogo.
        return [(e, n) for espaco_id, n in itens if (e := self.catalogo.obter(espaco_id))]

    def parceiros(self, usuario_id: int, tipo: str | None = None, limite: int = 5) -> list[tuple[int, int]]:
        """RF09: amigos de amigos com quem o usuário nunca dividiu um espaço."""
        try:
            return self.grafo.sugerir_parceiros(usuario_id, tipo, limite)
        except Exception as exc:
            raise GrafoIndisponivel("Sugestões de parceiros indisponíveis no momento.") from exc

    # ---- auxiliares -----------------------------------------------------

    def _normalizar_participantes(self, usuario_id: int, participantes: Iterable[int]) -> tuple[int, ...]:
        unicos = set(participantes) - {usuario_id}  # o dono já está na reserva
        if len(unicos) > self.cfg.max_participantes:
            raise ParticipantesInvalidos(f"No máximo {self.cfg.max_participantes} participantes por reserva.")
        return tuple(sorted(unicos))

    def _validar_regras(self, espaco: Espaco, reserva: Reserva) -> None:
        """RN08: regras estruturadas do documento do espaço."""
        pessoas = 1 + len(reserva.participantes)
        for regra in espaco.regras:
            if regra.get("tipo") == "min_participantes" and pessoas < regra["valor"]:
                raise RegraDoEspacoViolada(
                    f"{espaco.nome}: {regra.get('descricao', 'mínimo de participantes')}. "
                    f"A reserva tem {pessoas} pessoa(s), contando você."
                )
            # Outros tipos ("informativa" ou desconhecidos) não bloqueiam a reserva.
        capacidade = espaco.atributos.get("capacidade")
        if capacidade is not None and pessoas > capacidade:
            raise RegraDoEspacoViolada(
                f"{espaco.nome} comporta {capacidade} pessoas. A reserva tem {pessoas}, contando você."
            )

    def _no_grafo(self, operacao: Callable, *args) -> None:
        # O grafo é uma projeção (AP, best-effort): se o Neo4j falhar, a reserva já está
        # garantida no Cassandra e não pode ser desfeita nem atrasada por isso.
        def tarefa() -> None:
            try:
                operacao(*args)
            except Exception:
                log.warning("Falha ao atualizar o grafo", exc_info=True)
                self._metrica("falhas_no_grafo")

        self._segundo_plano(tarefa)

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
