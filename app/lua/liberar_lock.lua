-- Libera o lock do slot apenas se ele ainda pertence a quem o pegou (Marco 1, 2.2a).
-- KEYS[1] = lock:slot:<espaco>:<data>:<hora>
-- ARGV[1] = token do dono (reserva_id)
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
