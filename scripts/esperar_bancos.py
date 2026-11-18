"""Aguarda a prontidão de todos os serviços do Docker Compose e da API.

Usado no CI (GitHub Actions) e localmente antes de rodar os testes de integração.
Garante que o Cassandra (que leva ~1 minuto), o MongoDB (seed), o Redis, o Neo4j
e a API estejam saudáveis e respondendo antes de qualquer teste executar.
"""

import os
import sys
import time

import httpx
from cassandra.cluster import Cluster
from neo4j import GraphDatabase
from pymongo import MongoClient
from redis import Redis


def aguardar(timeout_s: int = 120) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    inicio = time.time()

    redis_url = os.getenv("RESERVAI_REDIS_URL", "redis://localhost:6379/0")
    cassandra_hosts = os.getenv("RESERVAI_CASSANDRA_HOSTS", "localhost").split(",")
    cassandra_keyspace = os.getenv("RESERVAI_CASSANDRA_KEYSPACE", "reservai")
    mongo_url = os.getenv("RESERVAI_MONGO_URL", "mongodb://localhost:27017")
    mongo_db = os.getenv("RESERVAI_MONGO_DB", "reservai")
    neo4j_url = os.getenv("RESERVAI_NEO4J_URL", "bolt://localhost:7687")
    neo4j_user = os.getenv("RESERVAI_NEO4J_USUARIO", "neo4j")
    neo4j_pass = os.getenv("RESERVAI_NEO4J_SENHA", "reservai-dev")
    api_url = os.getenv("RESERVAI_API_URL", "http://localhost:8000")

    prontos = {
        "redis": False,
        "mongo": False,
        "neo4j": False,
        "cassandra": False,
        "api": False,
    }

    print(f"Aguardando prontidão dos serviços (timeout: {timeout_s}s)...")

    while time.time() - inicio < timeout_s:
        # 1. Redis
        if not prontos["redis"]:
            try:
                r = Redis.from_url(redis_url, socket_timeout=2)
                if r.ping():
                    prontos["redis"] = True
                    print(f"  [OK] Redis pronto ({time.time() - inicio:.1f}s)")
            except Exception:
                pass

        # 2. MongoDB (incluindo catálogo populado pelo seed)
        if not prontos["mongo"]:
            try:
                m = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
                m.admin.command("ping")
                if m[mongo_db]["espacos"].count_documents({}) > 0:
                    prontos["mongo"] = True
                    print(f"  [OK] MongoDB e catálogo de espaços prontos ({time.time() - inicio:.1f}s)")
            except Exception:
                pass

        # 3. Neo4j
        if not prontos["neo4j"]:
            try:
                driver = GraphDatabase.driver(
                    neo4j_url,
                    auth=(neo4j_user, neo4j_pass),
                    connection_timeout=2.0,
                )
                driver.verify_connectivity()
                driver.close()
                prontos["neo4j"] = True
                print(f"  [OK] Neo4j pronto ({time.time() - inicio:.1f}s)")
            except Exception:
                pass

        # 4. Cassandra (keyspace e tabelas criadas pelo cassandra-init)
        if not prontos["cassandra"]:
            try:
                cluster = Cluster(cassandra_hosts, connect_timeout=3)
                session = cluster.connect()
                # Verifica se o keyspace e as tabelas já foram criados pelo cassandra-init
                tabelas = cluster.metadata.keyspaces.get(cassandra_keyspace)
                if tabelas and "reservas_por_espaco" in tabelas.tables:
                    prontos["cassandra"] = True
                    print(f"  [OK] Cassandra e schema CQL prontos ({time.time() - inicio:.1f}s)")
                cluster.shutdown()
            except Exception:
                pass

        # 5. API FastAPI (/health)
        if not prontos["api"]:
            try:
                resp = httpx.get(f"{api_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    prontos["api"] = True
                    print(f"  [OK] API FastAPI pronta em {api_url} ({time.time() - inicio:.1f}s)")
            except Exception:
                pass

        if all(prontos.values()):
            duracao = time.time() - inicio
            print(f"\nTodos os serviços e a API estão saudáveis e operacionais ({duracao:.1f}s)!")
            return

        time.sleep(2)

    pendentes = [k for k, v in prontos.items() if not v]
    print(f"\nERRO: Timeout de {timeout_s}s atingido. Serviços ainda pendentes: {pendentes}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    aguardar()
