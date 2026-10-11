// Catálogo de espaços (família Documentos, prevista no Marco 1, seção 1.1).
// Cada tipo de espaço tem atributos próprios: o esquema flexível é o motivo do Mongo aqui.
db = db.getSiblingDB("reservai");

db.espacos.insertMany([
  {
    _id: 1, unidade_id: 1, nome: "Quadra Poliesportiva", tipo: "quadra",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { modalidades: ["futsal", "vôlei", "basquete"], coberta: true, iluminacao: true },
    regras: ["Calçado adequado obrigatório", "Mínimo de 6 jogadores"]
  },
  {
    _id: 2, unidade_id: 1, nome: "Quadra de Areia", tipo: "quadra",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { modalidades: ["vôlei de praia", "futevôlei"], coberta: false, iluminacao: true },
    regras: ["Proibido uso em caso de chuva forte"]
  },
  {
    _id: 3, unidade_id: 1, nome: "Sala de Estudo A", tipo: "sala_estudo",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { capacidade: 6, quadro_branco: true, tomadas: 8, tv: false },
    regras: ["Silêncio moderado", "Proibido alimentos"]
  },
  {
    _id: 4, unidade_id: 1, nome: "Sala de Estudo B", tipo: "sala_estudo",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { capacidade: 10, quadro_branco: true, tomadas: 12, tv: true },
    regras: ["Silêncio moderado"]
  },
  {
    _id: 5, unidade_id: 2, nome: "Quadra Society", tipo: "quadra",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { modalidades: ["society"], coberta: false, iluminacao: true, gramado: "sintético" },
    regras: ["Chuteira society obrigatória"]
  },
  {
    _id: 6, unidade_id: 2, nome: "Sala de Estudo Central", tipo: "sala_estudo",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { capacidade: 8, quadro_branco: true, tomadas: 10, tv: true },
    regras: []
  },
  {
    _id: 7, unidade_id: 3, nome: "Sala de Reunião Ipê", tipo: "sala_reuniao",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { capacidade: 12, videoconferencia: true, projetor: true, cafe_incluso: true },
    regras: ["Reserva vinculada ao convênio do coworking"]
  },
  {
    _id: 8, unidade_id: 3, nome: "Sala de Reunião Buriti", tipo: "sala_reuniao",
    funcionamento: { abertura: 8, fechamento: 20 },
    atributos: { capacidade: 4, videoconferencia: true, projetor: false },
    regras: ["Reserva vinculada ao convênio do coworking"]
  }
]);

db.espacos.createIndex({ unidade_id: 1, tipo: 1 });
