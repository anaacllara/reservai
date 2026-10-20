# reservai

Aplicação que permite que alunos e frequentadores de um campus reservem espaços compartilhados
(quadras, salas de estudo, salas de reunião) sem que duas pessoas fiquem com o mesmo horário.
Projeto incremental da disciplina **Bancos de Dados NoSQL** (CEUB), do grupo **NoConflict**.

| Família NoSQL | Banco | Papel |
|---------------|-------|-------|
| Chave-Valor | Redis | Lock do slot, cache da grade, cota por usuário, ranking |
| Wide-Column | Cassandra | Ocupação (LWT, que garante a unicidade) e históricos query-first |
| Documentos | MongoDB | Catálogo de espaços, com atributos e regras por tipo |

A especificação (requisitos, modelo de dados e fluxos) está em [`docs/marco1.md`](docs/marco1.md).

## Como rodar

Pré-requisito: Docker Desktop.

```bash
docker compose up -d --build
```

O primeiro start do Cassandra leva cerca de 1 minuto. Quando terminar, abra a documentação interativa da API em
**<http://localhost:8000/docs>**.

Para zerar os bancos: `docker compose down -v`.

## Demo: disputa por um slot

Simula o pico de abertura da agenda: N usuários tentando o mesmo horário ao mesmo tempo.

```bash
pip install httpx
python scripts/disputa_slot.py --tentativas 200 --espaco 1 --horario 19:00
```

Saída esperada: **exatamente 1 resposta 201**. As outras mostram onde cada tentativa foi barrada,
no lock do Redis (em memória) ou na LWT do Cassandra.

## Testes

Os testes unitários usam fakes (fakeredis e repositórios em memória) e não precisam do Docker.

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements-dev.txt
pytest
```

Use **Python 3.11**. O `cassandra-driver` ainda não funciona no 3.12+, e o CI também roda no 3.11.

## Estrutura

```text
app/
  api.py              rotas FastAPI
  servicos.py         regras de negócio (fluxos de reserva e cancelamento)
  dominio.py          entidades, erros e interfaces dos repositórios
  chaves.py           nomes das chaves do Redis
  lua/                scripts atômicos do Redis
  repositorios/       implementações reais: cassandra.py, mongo.py
schema/               CQL do Cassandra e seed do Mongo
scripts/              demo de concorrência
tests/                testes unitários + fakes
docs/                 especificação
```

## Fluxo de trabalho

- Branches saem de `develop`: `feat/...`, `fix/...`, `docs/...`
- PR para `develop` usando o template, com pelo menos 1 aprovação e o CI verde
- `develop` vai para `main` nas entregas
