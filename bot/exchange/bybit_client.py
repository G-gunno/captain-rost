import os
import asyncio

from loguru import logger


class BybitClient:
    """Клиент Bybit API v5 через официальную библиотеку pybit."""

    def __init__(self):
        self.api_key = os.getenv("BYBIT_API_KEY", "").strip()
        self.api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
        self.testnet = os.getenv("BYBIT_TESTNET", "true").lower() == "true"
        self.base_url = (
            "https://api-testnet.bybit.com" if self.testnet else "https://api.bybit.com"
        )
        self.last_error = None

    def _session(self):
        from pybit.unified_trading import HTTP

        return HTTP(
            testnet=self.testnet,
            api_key=self.api_key,
            api_secret=self.api_secret,
            recv_window=5000,
            logging_level="ERROR",
        )

    async def get_wallet_balance(self, account_type: str):
        """Возвращает словарь кошелька или None."""
        self.last_error = None

        def _call():
            session = self._session()
            return session.get_wallet_balance(accountType=account_type)

        try:
            data = await asyncio.to_thread(_call)
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Bybit (pybit) ошибка: {e}")
            return None

        if isinstance(data, dict) and data.get("retCode") != 0:
            self.last_error = f"retCode={data.get('retCode')}, retMsg={data.get('retMsg')}"
            logger.error(f"Bybit API error: {data}")
            return None

        try:
            return data["result"]["list"][0]
        except Exception:
            self.last_error = f"неожиданный ответ: {str(data)[:200]}"
            logger.error(f"Bybit unexpected response: {data}")
            return None
