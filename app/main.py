import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.dominio import CatalogoEspacos, ErroReserva
from app.servicos import ServicoReservas


log = logging.getLogger(__name__)


def _criar_restricoes(grafo) -> None:
    try:
        grafo.criar_restricoes()
    except Exception:
        log.warning("Neo4j indisponível no startup; restrições serão criadas no próximo start", exc_info=True)


@asynccontextmanager
async def _conectar_bancos(app: FastAPI):
    # Imports locais: os testes unitários não precisam dos drivers de Cassandra, Mongo e Neo4j.
    from neo4j import GraphDatabase
    from pymongo import MongoClient
    from redis import Redis

    from app.config import get_settings
    from app.repositorios import cassandra
    from app.repositorios.grafo import GrafoNeo4j
    from app.repositorios.mongo import CatalogoMongo

    cfg = get_settings()
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    cluster, session = cassandra.conectar(cfg)
    mongo = MongoClient(cfg.mongo_url)
    # Timeouts curtos: com o Neo4j fora do ar, as consultas do grafo respondem 503 rápido
    # em vez de segurar a requisição pelos ~30 s padrão de retry do driver.
    neo4j = GraphDatabase.driver(
        cfg.neo4j_url,
        auth=(cfg.neo4j_usuario, cfg.neo4j_senha),
        connection_timeout=2.0,
        max_transaction_retry_time=2.0,
    )
    grafo = GrafoNeo4j(neo4j)
    # Uma única thread: as escritas no grafo saem da requisição, mas mantêm a ordem.
    fila_grafo = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grafo")
    # Primeira tarefa da fila (vem antes de qualquer escrita). Se o Neo4j estiver fora,
    # a API sobe mesmo assim: o grafo é a única parte que fica indisponível.
    fila_grafo.submit(_criar_restricoes, grafo)

    app.state.catalogo = CatalogoMongo(mongo[cfg.mongo_db])
    app.state.servico = ServicoReservas(
        redis,
        cassandra.RepositorioCassandra(session),
        app.state.catalogo,
        grafo,
        cfg,
        segundo_plano=fila_grafo.submit,
    )
    yield
    fila_grafo.shutdown(wait=True)  # termina as projeções pendentes antes de fechar o driver
    neo4j.close()
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
