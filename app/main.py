from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.dominio import CatalogoEspacos, ErroReserva
from app.servicos import ServicoReservas


@asynccontextmanager
async def _conectar_bancos(app: FastAPI):
    # Imports locais: os testes unitários não precisam dos drivers de Cassandra e Mongo.
    from pymongo import MongoClient
    from redis import Redis

    from app.config import get_settings
    from app.repositorios import cassandra
    from app.repositorios.mongo import CatalogoMongo

    cfg = get_settings()
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    cluster, session = cassandra.conectar(cfg)
    mongo = MongoClient(cfg.mongo_url)

    app.state.catalogo = CatalogoMongo(mongo[cfg.mongo_db])
    app.state.servico = ServicoReservas(
        redis, cassandra.RepositorioCassandra(session), app.state.catalogo, cfg
    )
    yield
    cluster.shutdown()
    mongo.close()
    redis.close()


def criar_app(servico: ServicoReservas | None = None, catalogo: CatalogoEspacos | None = None) -> FastAPI:
    """Sem argumentos, conecta nos bancos reais. Nos testes, recebe o serviço já montado."""
    app = FastAPI(title="Reservaí", lifespan=None if servico else _conectar_bancos)
    if servico:
        app.state.servico = servico
        app.state.catalogo = catalogo

    @app.exception_handler(ErroReserva)
    def _erro_reserva(_: Request, exc: ErroReserva):
        return JSONResponse(status_code=exc.status_http, content={"detail": str(exc)})

    app.include_router(router)
    return app


app = criar_app()
