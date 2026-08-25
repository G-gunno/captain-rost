import os
import time
import json
import hmac
import hashlib
from urllib.parse import urlencode

import httpx
from loguru import logger


class BybitClient:
    """Клиент Bybit API v5 (testnet / mainnet)."""

    def __init__(self):
        self.api_key = os.getenv("BYBIT_API_KEY", "").strip()
        self.api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
        testnet = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
        self.base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self.recv_window = "5000"

    def _sign(self, params_str: str, timestamp: str) -> str:
        payload = timestamp + self.api_key + self.recv_window + params_str
        return hmac.new(
            self.api_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _request(self, method: str, path: str, params: dict = None):
        params = params or {}
        timestamp = str(int(time.time() * 1000))

        if method == "GET":
            params_str = urlencode(params)
        else:
            params_str = json.dumps(params)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": self._sign(params_str, timestamp),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            if method == "GET":
                resp = await client.get(self.base_url + path, params=params, headers=headers)
            else:
                resp = await client.post(self.base_url + path, json=params, headers=headers)

        # Защита от не-JSON ответов (например 401 с пустым телом)
        try:
            data = resp.json()
        except Exception:
            logger.error(f"Bybit не-JSON ответ: status={resp.status_code}, body={resp.text[:200]}")
            return {"retCode": resp.status_code, "retMsg": f"HTTP {resp.status_code}"}

        if data.get("retCode") != 0:
            logger.error(f"Bybit API error: {data}")
        return data

    async def get_wallet_balance(self, account_type: str):
        """account_type: UNIFIED или FUND"""
        data = await self._request(
            "GET", "/v5/account/wallet-balance", {"accountType": account_type}
        )
        if data.get("retCode") == 0:
            return data["result"]["list"][0]
        return None
