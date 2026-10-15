-- Verifica e consome a cota de reservas futuras do usuário de forma atômica (RF06, Marco 1, 2.2c).
-- O score é o início do slot: reservas que já aconteceram saem do conjunto sozinhas.
-- KEYS[1] = ativas:usuario:<id>
-- ARGV = agora (epoch s), inicio_slot (epoch s), reserva_id, limite
redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", ARGV[1])
if redis.call("ZCARD", KEYS[1]) >= tonumber(ARGV[4]) then
  return 0
end
redis.call("ZADD", KEYS[1], ARGV[2], ARGV[3])
return 1
