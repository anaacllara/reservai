"""Autenticação e gerenciamento de sessão no Redis (Issue #12)."""

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis import Redis

from app import chaves

_bearer = HTTPBearer(auto_error=False)

USUARIOS_SEED: dict[int, dict[str, Any]] = {
    842: {"nome": "Mariana Sena", "email": "842@ceub.edu.br", "perfil": "aluno"},
    843: {"nome": "Lucas Prado", "email": "843@ceub.edu.br", "perfil": "aluno"},
    900: {"nome": "Carlos Silva", "email": "900@ceub.edu.br", "perfil": "aluno"},
    901: {"nome": "Beatriz Lima", "email": "901@ceub.edu.br", "perfil": "aluno"},
    902: {"nome": "Daniel Santos", "email": "902@ceub.edu.br", "perfil": "aluno"},
    950: {"nome": "Eduardo Ramos", "email": "950@ceub.edu.br", "perfil": "aluno"},
    951: {"nome": "Fernanda Souza", "email": "951@ceub.edu.br", "perfil": "aluno"},
    960: {"nome": "Gabriel Rocha", "email": "960@ceub.edu.br", "perfil": "aluno"},
}

SENHA_PADRAO = "senha123"


@dataclass(frozen=True)
class UsuarioSessao:
    usuario_id: int
    nome: str
    email: str
    perfil: str


def localizar_usuario(usuario_id: int | None = None, email: str | None = None) -> dict[str, Any] | None:
    if usuario_id is not None:
        if usuario_id in USUARIOS_SEED:
            return {"usuario_id": usuario_id, **USUARIOS_SEED[usuario_id]}
        if usuario_id > 0:
            return {
                "usuario_id": usuario_id,
                "nome": f"Usuário {usuario_id}",
                "email": f"{usuario_id}@ceub.edu.br",
                "perfil": "aluno",
            }
    if email is not None:
        for uid, dados in USUARIOS_SEED.items():
            if dados["email"] == email:
                return {"usuario_id": uid, **dados}
    return None


def criar_sessao(redis: Redis, usuario: dict[str, Any], ttl_s: int) -> str:
    token = secrets.token_urlsafe(32)
    chave = chaves.sessao(token)
    pipe = redis.pipeline()
    pipe.hset(
        chave,
        mapping={
            "usuario_id": str(usuario["usuario_id"]),
            "nome": str(usuario.get("nome", "")),
            "email": str(usuario.get("email", "")),
            "perfil": str(usuario.get("perfil", "aluno")),
        },
    )
    pipe.expire(chave, ttl_s)
    pipe.execute()
    return token


def obter_sessao(redis: Redis, token: str, renovar_ttl_s: int | None = None) -> UsuarioSessao | None:
    chave = chaves.sessao(token)
    dados = redis.hgetall(chave)
    if not dados:
        return None
    if renovar_ttl_s:
        redis.expire(chave, renovar_ttl_s)
    return UsuarioSessao(
        usuario_id=int(dados["usuario_id"]),
        nome=dados.get("nome", ""),
        email=dados.get("email", ""),
        perfil=dados.get("perfil", ""),
    )


def encerrar_sessao(redis: Redis, token: str) -> bool:
    return bool(redis.delete(chaves.sessao(token)))


def obter_usuario_autenticado(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UsuarioSessao:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticação não fornecido.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    svc = request.app.state.servico
    usuario = obter_sessao(svc.redis, creds.credentials, renovar_ttl_s=svc.cfg.sessao_ttl_s)
    if usuario is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida ou expirada.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return usuario

