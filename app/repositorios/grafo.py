"""Camada Grafos (Neo4j): projeção das reservas para consultas de travessia.

Modelo:
    (:Usuario)-[:RESERVOU]->(:Reserva)-[:EM]->(:Espaco)-[:FICA_EM]->(:Unidade)
    (:Usuario)-[:PARTICIPOU]->(:Reserva)

A reserva é um nó (e não uma aresta Usuario-Espaco com contador) por dois motivos:
- liga N pessoas ao mesmo evento, que é o que define "quem usou o espaço junto";
- torna a escrita idempotente (MERGE pelo id) e o cancelamento trivial (DETACH DELETE),
  sem contadores que podem divergir se uma operação for repetida ou falhar no meio.

Reservas canceladas saem do grafo; o histórico delas continua no Cassandra.
"""

from neo4j import Driver, RoutingControl

from app.dominio import Espaco, Reserva

# "Usou o espaço" = foi o dono da reserva ou participou dela.
_USOU = "RESERVOU|PARTICIPOU"

_RESTRICOES = [
    "CREATE CONSTRAINT usuario_id IF NOT EXISTS FOR (n:Usuario) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT espaco_id IF NOT EXISTS FOR (n:Espaco) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT unidade_id IF NOT EXISTS FOR (n:Unidade) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT reserva_id IF NOT EXISTS FOR (n:Reserva) REQUIRE n.id IS UNIQUE",
]

_REGISTRAR = """
MERGE (un:Unidade {id: $unidade_id})
MERGE (e:Espaco {id: $espaco_id})
  SET e.tipo = $tipo, e.nome = $nome
MERGE (e)-[:FICA_EM]->(un)
MERGE (r:Reserva {id: $reserva_id})
  SET r.data = date($data), r.horario = $horario
MERGE (r)-[:EM]->(e)
MERGE (dono:Usuario {id: $usuario_id})
MERGE (dono)-[:RESERVOU]->(r)
WITH r
UNWIND $participantes AS pid
MERGE (p:Usuario {id: pid})
MERGE (p)-[:PARTICIPOU]->(r)
"""

_REMOVER = "MATCH (r:Reserva {id: $reserva_id}) DETACH DELETE r"

# RF08: filtragem colaborativa em 2 saltos.
# Eu -> espaços que usei <- outras pessoas que usaram -> espaços que elas usam e eu não.
# Pontuação = quantas pessoas distintas "em comum" levam até o espaço recomendado.
_RECOMENDAR = f"""
MATCH (eu:Usuario {{id: $usuario_id}})-[:{_USOU}]->(:Reserva)-[:EM]->(meu:Espaco)
WITH eu, collect(DISTINCT meu) AS meus
UNWIND meus AS meu
MATCH (meu)<-[:EM]-(:Reserva)<-[:{_USOU}]-(outro:Usuario)
WHERE outro <> eu
MATCH (outro)-[:{_USOU}]->(:Reserva)-[:EM]->(rec:Espaco)
WHERE NOT rec IN meus
RETURN rec.id AS espaco_id, count(DISTINCT outro) AS pontuacao
ORDER BY pontuacao DESC, espaco_id
LIMIT $limite
"""

# RF09: amigos de amigos. "Amigo" = já dividiu uma reserva comigo.
# Sugere quem dividiu reserva com meus amigos, mas nunca comigo,
# ordenado pelo número de amigos em comum. O filtro por tipo vale para o segundo salto
# (ex.: tipo='quadra' sugere quem joga com meus amigos, não quem estuda com eles).
_PARCEIROS = f"""
MATCH (eu:Usuario {{id: $usuario_id}})-[:{_USOU}]->(:Reserva)<-[:{_USOU}]-(amigo:Usuario)
WHERE amigo <> eu
WITH eu, collect(DISTINCT amigo) AS amigos
UNWIND amigos AS amigo
MATCH (amigo)-[:{_USOU}]->(r:Reserva)<-[:{_USOU}]-(sugestao:Usuario)
WHERE sugestao <> eu AND NOT sugestao IN amigos
  AND ($tipo IS NULL OR EXISTS {{ (r)-[:EM]->(:Espaco {{tipo: $tipo}}) }})
RETURN sugestao.id AS usuario_id, count(DISTINCT amigo) AS amigos_em_comum
ORDER BY amigos_em_comum DESC, usuario_id
LIMIT $limite
"""


class GrafoNeo4j:
    def __init__(self, driver: Driver):
        self.driver = driver

    def criar_restricoes(self) -> None:
        """Unicidade por id: garante que MERGE não duplique nós e cria os índices de busca."""
        for cypher in _RESTRICOES:
            self.driver.execute_query(cypher)

    def registrar_reserva(self, reserva: Reserva, espaco: Espaco) -> None:
        self.driver.execute_query(
            _REGISTRAR,
            unidade_id=espaco.unidade_id,
            espaco_id=espaco.id,
            tipo=espaco.tipo,
            nome=espaco.nome,
            reserva_id=str(reserva.reserva_id),
            data=reserva.data.isoformat(),
            horario=f"{reserva.horario:%H:%M}",
            usuario_id=reserva.usuario_id,
            participantes=list(reserva.participantes),
        )

    def remover_reserva(self, reserva: Reserva) -> None:
        self.driver.execute_query(_REMOVER, reserva_id=str(reserva.reserva_id))

    def recomendar_espacos(self, usuario_id: int, limite: int) -> list[tuple[int, int]]:
        registros, _, _ = self.driver.execute_query(
            _RECOMENDAR, usuario_id=usuario_id, limite=limite, routing_=RoutingControl.READ
        )
        return [(r["espaco_id"], r["pontuacao"]) for r in registros]

    def sugerir_parceiros(self, usuario_id: int, tipo: str | None, limite: int) -> list[tuple[int, int]]:
        registros, _, _ = self.driver.execute_query(
            _PARCEIROS, usuario_id=usuario_id, tipo=tipo, limite=limite, routing_=RoutingControl.READ
        )
        return [(r["usuario_id"], r["amigos_em_comum"]) for r in registros]
