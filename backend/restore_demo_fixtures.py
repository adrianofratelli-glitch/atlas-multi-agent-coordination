"""Restaura os saldos que a própria demo/eval consome (idempotente, só as contas de fidelidade da demo).

O eval live resgata um voucher (500 pontos) da `carla` a cada rodada; sem saldo, `carla-loyalty-redemption`
falha por "saldo insuficiente". Rode antes de um eval/demo se o saldo estiver baixo.

    python restore_demo_fixtures.py
"""

import asyncio
import sys

from app.config import get_settings
from app.database import DataStore
from app.seed_data import seed_documents


async def restore(store: DataStore) -> dict[str, int]:
    restored = {}
    for account in seed_documents()["loyalty_accounts"]:
        current = await store.find_one("loyalty_accounts", {"customer_key": account["customer_key"]})
        if current and current["points"] != account["points"]:
            await store.update_one("loyalty_accounts", {"customer_key": account["customer_key"]}, {"$set": {"points": account["points"]}})
            restored[account["customer_key"]] = account["points"]
    return restored


async def main() -> None:
    store = DataStore(get_settings())
    await store.connect()
    try:
        if store.memory:
            sys.exit("DEMO_MODE/sem MONGODB_URI: o seed em memória já nasce com os saldos.")
        print("saldos restaurados:", await restore(store) or "nenhum (já estavam corretos)")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
