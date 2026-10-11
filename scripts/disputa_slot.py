"""Demo do seminário: N usuários disputando o MESMO slot ao mesmo tempo.

Reproduz o pico de abertura da agenda (Marco 1, seção 1.3) e mostra que
exatamente uma tentativa vence, e onde cada uma das outras foi barrada.

Uso (com o docker compose rodando):
    python scripts/disputa_slot.py --tentativas 200 --espaco 1 --horario 19:00
"""

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # acentos no console do Windows
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--tentativas", type=int, default=200)
    p.add_argument("--espaco", type=int, default=1)
    p.add_argument("--data", default=(date.today() + timedelta(days=1)).isoformat())
    p.add_argument("--horario", default="19:00")
    p.add_argument("--usuario-inicial", type=int, default=10_000)
    args = p.parse_args()

    with httpx.Client(base_url=args.url, timeout=30) as http:
        antes = http.get("/metricas").json()

        def tentar(usuario_id: int) -> int:
            corpo = {"usuario_id": usuario_id, "espaco_id": args.espaco, "data": args.data, "horario": args.horario}
            return http.post("/reservas", json=corpo).status_code

        usuarios = range(args.usuario_inicial, args.usuario_inicial + args.tentativas)
        inicio = time.perf_counter()
        with ThreadPoolExecutor(max_workers=min(args.tentativas, 100)) as pool:
            status = Counter(pool.map(tentar, usuarios))
        duracao = time.perf_counter() - inicio

        depois = http.get("/metricas").json()

    delta = {k: depois.get(k, 0) - antes.get(k, 0) for k in depois}
    print(f"\n{args.tentativas} tentativas no espaço {args.espaco}, {args.data} {args.horario} "
          f"em {duracao:.2f}s\n")
    print("Respostas HTTP:")
    for codigo, n in sorted(status.items()):
        print(f"  {codigo}: {n}")
    print("\nOnde cada tentativa parou:")
    for chave in ("barradas_no_lock", "barradas_na_cota", "rejeitadas_na_lwt", "confirmadas"):
        print(f"  {chave:<20} {delta.get(chave, 0)}")

    vencedores = status.get(201, 0)
    print(f"\n{'OK' if vencedores <= 1 else 'FALHOU'}: {vencedores} vencedor(es) para o slot.")


if __name__ == "__main__":
    main()
