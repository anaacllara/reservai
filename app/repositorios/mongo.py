"""Camada Documentos: catálogo de espaços, com atributos e regras que variam por tipo."""

from pymongo.database import Database

from app.dominio import Espaco


class CatalogoMongo:
    def __init__(self, db: Database):
        self.col = db["espacos"]

    def obter(self, espaco_id: int) -> Espaco | None:
        doc = self.col.find_one({"_id": espaco_id})
        return self._de_doc(doc) if doc else None

    def listar(self, unidade_id: int | None = None) -> list[Espaco]:
        filtro = {} if unidade_id is None else {"unidade_id": unidade_id}
        return [self._de_doc(d) for d in self.col.find(filtro).sort("_id")]

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
            regras=doc.get("regras", []),
        )
