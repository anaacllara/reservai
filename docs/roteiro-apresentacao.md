# Roteiro da apresentação final (Marco 2)

Seminário de **10 a 12 minutos**, com avaliação por pares e devolutiva do professor.

Os slides estão no deck compartilhado. As **notas de cada slide** trazem o texto sugerido para a fala;
este arquivo organiza o tempo, a divisão entre os integrantes e a preparação da demo.

## 1. Tempo e divisão das falas

| Bloco | Slides | Conteúdo | Tempo | Quem fala |
|-------|--------|----------|-------|-----------|
| 1 | 1–3 | Abertura, problema, por que é difícil (o pico) | 1 min 30 s | [definir] |
| 2 | 4–6 | Arquitetura, fluxo da reserva, CAP por operação | 2 min 20 s | [definir] |
| 3 | 7–9 | Lock + LWT, MongoDB, Neo4j | 2 min 05 s | [definir] |
| 4 | 10–12 | **Demo ao vivo**, resultado da disputa, teste de falha | 3 min 30 s | [definir] (quem opera a demo) |
| 5 | 13–15 | Sharding e replicação, lições, perguntas | 1 min 30 s | [definir] |
| | | **Total** | **~11 min** | |

A margem de 1 minuto até o limite de 12 cobre as trocas de quem fala e um imprevisto na demo.
Se o tempo apertar no ensaio, os slides mais fáceis de encurtar são o 13 (sharding) e o 12 (falha).

Sugestão: quem opera a demo fica no computador desde o slide 10 e não troca de lugar no meio.

## 2. Roteiro da demo (slide 10, ~2 min 30 s)

| # | Ação | O que mostrar | O que falar |
|---|------|---------------|-------------|
| 1 | Aba **Reservar**: tipo "Sala de reunião", capacidade 8, marcar videoconferência, **Buscar** | Só a Sala Ipê aparece; etiqueta verde do MongoDB | "Um único find sobre atributos que só existem em salas de reunião." |
| 2 | Clicar na **Quadra Poliesportiva**, escolher um horário livre (**não o das 19h**), **Reservar** sem participantes | Erro "Mínimo de 6 jogadores…" | "A regra vem do documento no Mongo e é conferida antes do lock." |
| 3 | Preencher `900, 901, 902, 903, 904` e reservar o mesmo horário | O horário fica vermelho na grade | "Passou por lock, cota e LWT, e a grade em cache foi atualizada." |
| 4 | No terminal: `python scripts/disputa_slot.py --tentativas 200 --espaco 1 --horario 19:00` | `201: 1`, `409: 199`, contadores do lock e da LWT | "200 pessoas no mesmo horário, ao mesmo tempo: exatamente um vencedor." |
| 5 | Aba **Painel**; se sobrar tempo, aba **Para você** e o Neo4j Browser | Contadores; o grafo das reservas | "Cada número aqui é um contador do Redis." |

Consulta para o Neo4j Browser (<http://localhost:7474>, usuário `neo4j`, senha `reservai-dev`):

```cypher
MATCH p=(:Usuario)-[]->(:Reserva)-[:EM]->(:Espaco) RETURN p LIMIT 50
```

## 3. Checklist antes da aula

**Na véspera**
- [ ] Ensaio completo cronometrado, com a demo de verdade
- [ ] **Gravar o vídeo de backup da demo** (os 5 passos acima, ~2 min)
- [ ] Conferir que a `main` está atualizada e que o `docker compose up` sobe do zero

**No dia, uns 15 minutos antes**
- [ ] `docker compose down -v && docker compose up -d --build` (o Cassandra leva cerca de 1 minuto)
- [ ] Abrir <http://localhost:8000> e entrar com o usuário **842** (senha `senha123`)
- [ ] Deixar um terminal aberto na pasta do projeto, com o comando da disputa já digitado
- [ ] Abrir o Neo4j Browser numa aba (opcional)
- [ ] Zoom do navegador em 125% para quem está no fundo da sala
- [ ] Testar o projetor com o deck e com o navegador
- [ ] Vídeo de backup aberto, pronto para tocar

**Cuidados durante a demo**
- A disputa usa o horário das **19h** do dia seguinte: não reserve esse horário nos passos 2 e 3.
- O usuário 842 só pode ter **2 reservas futuras** (RF06). A demo usa uma; não faça reservas extras com ele antes da aula.

## 4. Plano B

| Problema | O que fazer |
|----------|-------------|
| Docker não sobe ou o Cassandra demora | Tocar o vídeo de backup e seguir para o slide 11, que traz os números |
| Um passo da demo dá erro | Explicar o que deveria acontecer, mostrar o slide 11 e seguir |
| Projetor não mostra o navegador | Só os slides; os slides 11 e 12 têm os resultados medidos |
| Tempo estourando | Pular o passo 5 da demo e encurtar o slide 13 |

## 5. Perguntas prováveis

**Por que não usar só o Cassandra?**
A LWT sozinha sofre com disputa na mesma partição: várias tentativas pelo mesmo horário competem em
rodadas de Paxos e geram timeouts. O lock do Redis absorve a rajada (160 de 200 na demo).

**Por que não usar só o Redis?**
A replicação do Redis é assíncrona. Se o master cair logo depois de conceder um lock, a réplica
promovida pode não ter esse lock, e duas pessoas venceriam. A LWT é a garantia.

**E se o Redis perder a cota num failover?**
A cota é uma regra de justiça, não de integridade física. No pior caso alguém consegue uma terceira
reserva; o conjunto pode ser reconstruído a partir de `reservas_por_usuario`.

**E as reservas feitas com o Neo4j fora do ar?**
Ficam fora do grafo. A correção é um script que relê `reservas_por_unidade` e refaz a projeção; o
`MERGE` idempotente permite rodar sem apagar nada. Está nas limitações da documentação.

**Por que a reserva é um nó no grafo, e não uma aresta?**
Porque ela liga várias pessoas ao mesmo evento e deixa o cancelamento simples (`DETACH DELETE`),
sem contadores que podem divergir.

**Por que MongoDB e Neo4j não têm sharding?**
O catálogo tem uns 50 KB e o grafo cabe numa máquina. Particionar um grafo transforma saltos locais
em saltos de rede; com esse volume, réplicas de leitura bastam.

**A autenticação é segura?**
É de demonstração: senha única e qualquer usuário positivo, porque a demo simula 200 pessoas. O que
ela demonstra é a sessão no Redis e o `usuario_id` vindo do token, nunca do corpo da requisição.

**Como vocês sabem que os testes em memória valem?**
Comparamos os fakes com os bancos reais (14 buscas e 3 resumos idênticos no MongoDB), e a suíte de
integração roda no CI contra os quatro bancos.
