"""Camada Documentos: catálogo de espaços, com atributos e regras que variam por tipo.

Por que Documentos aqui: uma quadra tem modalidades e cobertura, uma sala de estudo tem
capacidade e tomadas, uma sala de reunião tem videoconferência. Em vez de uma tabela com
dezenas de colunas quase sempre nulas (ou uma tabela por tipo), cada espaço é um documento
com só os atributos que fazem sentido para ele, e a busca filtra por qualquer um deles.
"""

from pymongo.database import Database

from app.dominio import Espaco, FiltroEspacos


def consulta(filtro: FiltroEspacos) -> dict:
    """Traduz o filtro para uma consulta do Mongo. Os campos são validados na API."""
    q: dict = {}
    if filtro.unidade_id is not None:
        q["unidade_id"] = filtro.unidade_id
    if filtro.tipo:
        q["tipo"] = filtro.tipo
    if filtro.capacidade_min is not None:
        q["atributos.capacidade"] = {"$gte": filtro.capacidade_min}
    if filtro.modalidade:
        q["atributos.modalidades"] = filtro.modalidade  # casa com qualquer elemento do array
    for recurso in filtro.recursos:
        q[f"atributos.{recurso}"] = True
    return q


def pipeline_resumo(unidade_id: int | None) -> list[dict]:
    """Aggregation pipeline do resumo do catálogo: uma ida ao banco, duas visões ($facet)."""
    match = {} if unidade_id is None else {"unidade_id": unidade_id}
    return [
        {"$match": match},
        {"$sort": {"_id": 1}},  # ordem estável dos nomes no $push
        {
            "$facet": {
                # Espaços por unidade e tipo, com a capacidade somada (quadras não declaram: somam 0).
                "por_tipo": [
                    {
                        "$group": {
                            "_id": {"unidade_id": "$unidade_id", "tipo": "$tipo"},
                            "espacos": {"$sum": 1},
                            "capacidade_total": {"$sum": "$atributos.capacidade"},
                            "nomes": {"$push": "$nome"},
                        }
                    },
                    {"$sort": {"_id.unidade_id": 1, "_id.tipo": 1}},
                    {
                        "$project": {
                            "_id": 0,
                            "unidade_id": "$_id.unidade_id",
                            "tipo": "$_id.tipo",
                            "espacos": 1,
                            "capacidade_total": 1,
                            "nomes": 1,
                        }
                    },
                ],
                # Quantos espaços oferecem cada modalidade ($unwind abre o array em um doc por item).
                "modalidades": [
                    {"$unwind": "$atributos.modalidades"},
                    {"$group": {"_id": "$atributos.modalidades", "espacos": {"$sum": 1}}},
                    {"$sort": {"espacos": -1, "_id": 1}},
                    {"$project": {"_id": 0, "modalidade": "$_id", "espacos": 1}},
                ],
            }
        },
    ]


class CatalogoMongo:
    def __init__(self, db: Database):
        self.col = db["espacos"]

    def obter(self, espaco_id: int) -> Espaco | None:
        doc = self.col.find_one({"_id": espaco_id})
        return self._de_doc(doc) if doc else None

    def listar(self, unidade_id: int | None = None) -> list[Espaco]:
        return self.buscar(FiltroEspacos(unidade_id=unidade_id))

    def buscar(self, filtro: FiltroEspacos) -> list[Espaco]:
        return [self._de_doc(d) for d in self.col.find(consulta(filtro)).sort("_id")]

    def resumo(self, unidade_id: int | None = None) -> dict:
        return next(self.col.aggregate(pipeline_resumo(unidade_id)))

    @staticmethod
    def _de_doc(doc: dict) -> Espaco:
        return Espaco(
            id=doc["_id"],
            unidade_id=doc["unidade_id"],
            nome=doc["nome"],
            tipo=doc["tipo"],
            abertura=doc["funcionamento"]["abertura"],
            fechamento=doc["funcionamento"]["fechamento"],
            atributos=doc.get("atributos", {}),
            regras=[_regra(r) for r in doc.get("regras", [])],
        )


def _regra(r) -> dict:
    # Volumes criados antes do Marco 2 guardam as regras como texto livre.
    return {"tipo": "informativa", "descricao": r} if isinstance(r, str) else r
