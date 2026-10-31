import asyncio
from decimal import Decimal, localcontext


class RPCProvider:
    """Tracked asset balances only. No whole-portfolio or transaction classification claim."""

    def __init__(self, client, endpoints, rps=3):
        self.client, self.endpoints = client, endpoints
        self.interval = 1 / max(0.1, rps)
        self.gate = asyncio.Lock()
        self.last_request = 0.0
        self.request_cache = {}

    def begin_cycle(self):
        self.request_cache = {}

    async def cached_rpc(self, chain, method, params):
        import json

        key = (chain, method, json.dumps(params, sort_keys=True))
        if key not in self.request_cache:
            self.request_cache[key] = asyncio.create_task(self.rpc(chain, method, params))
        return await self.request_cache[key]

    async def rpc(self, chain, method, params):
        endpoint = self.endpoints.get(chain)
        if not endpoint:
            raise ValueError("RPC endpoint not configured")
        for attempt in range(3):
            async with self.gate:
                loop = asyncio.get_running_loop()
                await asyncio.sleep(max(0, self.last_request + self.interval - loop.time()))
                self.last_request = loop.time()
            await self.before_request()
            response = await self.client.post(
                endpoint, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            if response.status_code == 429 or response.status_code >= 500:
                await asyncio.sleep(2**attempt)
                continue
            response.raise_for_status()
            body = response.json()
            if "error" in body or "result" not in body:
                raise ValueError("RPC returned an error or incomplete data")
            return body["result"]
        raise ValueError("RPC unavailable after retries")

    async def before_request(self):
        """Subclass hook: meter every actual attempt, including transport failures."""

    async def balance(self, row):
        with localcontext() as context:
            context.prec = 100
            return await self._balance(row)

    async def _balance(self, row):
        if row["group"].startswith("Futures"):
            raise ValueError("Futures platform adapter pending PDF source integration")
        chain, asset, address = row["chain"], row["asset"], row["address"]
        if chain == "solana":
            if asset == "native":
                raw = await self.rpc(chain, "getBalance", [address, {"commitment": "finalized"}])
                return Decimal(raw["value"]) / Decimal(10**9)
            result = await self.rpc(
                chain,
                "getTokenAccountsByOwner",
                [address, {"mint": asset}, {"encoding": "jsonParsed", "commitment": "finalized"}],
            )
            total = Decimal(0)
            for item in result["value"]:
                token = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += Decimal(token["amount"]) / Decimal(10 ** int(token["decimals"]))
            return total
        if asset == "native":
            raw = await self.rpc(chain, "eth_getBalance", [address, "finalized"])
            return Decimal(int(raw, 16)) / Decimal(10**18)
        block = await self.cached_rpc(chain, "eth_getBlockByNumber", ["finalized", False])
        height = block["number"]
        decimals = await self.cached_rpc(
            chain, "eth_call", [{"to": asset, "data": "0x313ce567"}, height]
        )
        if int(decimals, 16) != row["decimals"]:
            raise ValueError("Token decimals differ from Excel metadata")
        raw = await self.rpc(
            chain,
            "eth_call",
            [{"to": asset, "data": "0x70a08231" + address[2:].rjust(64, "0")}, height],
        )
        return Decimal(int(raw, 16)) / Decimal(10 ** row["decimals"])
