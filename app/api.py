from dataclasses import asdict
from datetime import date, datetime, time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.auth import (
    SENHA_PADRAO,
    UsuarioSessao,
    criar_sessao,
    encerrar_sessao,
    localizar_usuario,
    obter_usuario_autenticado,
)
from app.dominio import CatalogoEspacos, Reserva
from app.servicos import ServicoReservas

router = APIRouter()


def servico(request: Request) -> ServicoReservas:
    return request.app.state.servico


def catalogo(request: Request) -> CatalogoEspacos:
    return request.app.state.catalogo


# ---- modelos de autenticação -------------------------------------------


class LoginIn(BaseModel):
    usuario_id: int | None = None
    email: str | None = None
    senha: str = SENHA_PADRAO


class LoginOut(BaseModel):
    token: str
    token_type: str = "bearer"
    usuario_id: int
    nome: str


# ---- modelos de reservas -----------------------------------------------


class SlotIn(BaseModel):
    espaco_id: int
    data: date
    horario: time


class ReservaIn(SlotIn):
    # Outros usuários que vão usar o espaço junto. Alimentam o grafo (RF08, RF09).
    participantes: list[int] = Field(default_factory=list, max_length=100)


class ReservaOut(BaseModel):
    reserva_id: UUID
    usuario_id: int
    espaco_id: int
    unidade_id: int
    data: date
    horario: time
    status: str
    criado_em: datetime | None
    participantes: list[int]

    @classmethod
    def de(cls, r: Reserva) -> "ReservaOut":
        return cls(**asdict(r))


@router.get("/health")
def health():
    return {"status": "ok"}


# ---- autenticação (Redis) ---------------------------------------------


@router.post("/auth/login", response_model=LoginOut)
def login(dados: LoginIn, svc: ServicoReservas = Depends(servico)):
    usuario = localizar_usuario(dados.usuario_id, dados.email)
    if usuario is None or dados.senha != SENHA_PADRAO:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas.")
    token = criar_sessao(svc.redis, usuario, svc.cfg.sessao_ttl_s)
    return LoginOut(token=token, usuario_id=usuario["usuario_id"], nome=usuario["nome"])


@router.post("/auth/logout")
def logout(
    request: Request,
    usuario: UsuarioSessao = Depends(obter_usuario_autenticado),
    svc: ServicoReservas = Depends(servico),
):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
    encerrar_sessao(svc.redis, token)
    return {"status": "ok"}


# ---- catálogo (MongoDB) ------------------------------------------------


@router.get("/espacos")
def listar_espacos(unidade_id: int | None = None, cat: CatalogoEspacos = Depends(catalogo)):
    return [asdict(e) for e in cat.listar(unidade_id)]


@router.get("/espacos/{espaco_id}")
def obter_espaco(espaco_id: int, cat: CatalogoEspacos = Depends(catalogo)):
    espaco = cat.obter(espaco_id)
    if espaco is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Espaço {espaco_id} não existe.")
    return asdict(espaco)


@router.get("/espacos/{espaco_id}/disponibilidade")
def disponibilidade(espaco_id: int, data: date, svc: ServicoReservas = Depends(servico)):
    """RF01: grade de horários livres/ocupados do dia (cache Redis, AP)."""
    return {"espaco_id": espaco_id, "data": data, "grade": svc.grade(espaco_id, data)}


# ---- reservas ----------------------------------------------------------


@router.post("/reservas", status_code=status.HTTP_201_CREATED, response_model=ReservaOut)
def reservar(
    slot: ReservaIn,
    usuario: UsuarioSessao = Depends(obter_usuario_autenticado),
    svc: ServicoReservas = Depends(servico),
):
    """RF02 e RF06: reserva com lock (Redis), cota (Redis) e LWT (Cassandra); projeção no grafo."""
    return ReservaOut.de(
        svc.reservar(usuario.usuario_id, slot.espaco_id, slot.data, slot.horario, slot.participantes)
    )


@router.post("/reservas/cancelar", response_model=ReservaOut)
def cancelar(
    slot: SlotIn,
    usuario: UsuarioSessao = Depends(obter_usuario_autenticado),
    svc: ServicoReservas = Depends(servico),
):
    """RF03: cancela e libera o horário."""
    return ReservaOut.de(svc.cancelar(usuario.usuario_id, slot.espaco_id, slot.data, slot.horario))


@router.get("/usuarios/{usuario_id}/reservas", response_model=list[ReservaOut])
def reservas_do_usuario(
    usuario_id: int, limite: int = Query(50, ge=1, le=500), svc: ServicoReservas = Depends(servico)
):
    """RF04: histórico do usuário, das mais recentes às mais antigas."""
    return [ReservaOut.de(r) for r in svc.historico_usuario(usuario_id, limite)]


# ---- grafo (Neo4j) ----------------------------------------------------


@router.get("/usuarios/{usuario_id}/recomendacoes")
def recomendacoes(
    usuario_id: int, limite: int = Query(5, ge=1, le=50), svc: ServicoReservas = Depends(servico)
):
    """RF08: espaços usados por quem divide espaços com o usuário e que ele ainda não usou."""
    return [
        {"espaco_id": e.id, "nome": e.nome, "tipo": e.tipo, "unidade_id": e.unidade_id, "pessoas_em_comum": n}
        for e, n in svc.recomendacoes(usuario_id, limite)
    ]


@router.get("/usuarios/{usuario_id}/parceiros")
def parceiros(
    usuario_id: int,
    tipo: str | None = Query(None, examples=["quadra"]),
    limite: int = Query(5, ge=1, le=50),
    svc: ServicoReservas = Depends(servico),
):
    """RF09: amigos de amigos com quem o usuário nunca dividiu um espaço (opcionalmente por tipo)."""
    return [
        {"usuario_id": u, "amigos_em_comum": n} for u, n in svc.parceiros(usuario_id, tipo, limite)
    ]


# ---- painel administrativo -------------------------------------------


@router.get("/unidades/{unidade_id}/relatorio", response_model=list[ReservaOut])
def relatorio_unidade(
    unidade_id: int,
    mes: str = Query(..., pattern=r"^\d{4}-\d{2}$", examples=["2026-10"]),
    svc: ServicoReservas = Depends(servico),
):
    """RF05: reservas (ativas e canceladas) da unidade no mês, em uma única partição."""
    return [ReservaOut.de(r) for r in svc.ocupacao_unidade(unidade_id, mes)]


@router.get("/unidades/{unidade_id}/ranking")
def ranking(unidade_id: int, top: int = Query(10, ge=1, le=50), svc: ServicoReservas = Depends(servico)):
    """RF07: espaços mais reservados da unidade (Sorted Set)."""
    return [{"espaco_id": e, "reservas": n} for e, n in svc.ranking(unidade_id, top)]


@router.get("/metricas")
def metricas(svc: ServicoReservas = Depends(servico)):
    """Contadores do fluxo de reserva: mostram onde cada tentativa foi barrada."""
    return svc.metricas()
