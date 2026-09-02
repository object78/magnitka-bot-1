from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from .storage import Storage

log = logging.getLogger(__name__)


class TelegramGateway:
    def __init__(self, token: str, storage: Storage):
        self.token = token
        self.storage = storage
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=20.0)
        self.offset = int(storage.get_state("telegram_offset", 0) or 0)

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, chat_id: int, text: str) -> None:
        r = await self.client.post(
            f"{self.base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        r.raise_for_status()

    async def broadcast(self, text: str) -> None:
        for chat_id in self.storage.subscribers():
            try:
                await self.send(chat_id, text)
            except Exception as exc:  # noqa: BLE001
                log.error("Telegram send failed for %s: %s", chat_id, exc)

    async def poll_commands(self, status_provider: Callable[[], str], today_provider: Callable[[], str]) -> None:
        while True:
            try:
                r = await self.client.get(
                    f"{self.base}/getUpdates",
                    params={"offset": self.offset, "timeout": 25, "allowed_updates": '["message"]'},
                    timeout=35.0,
                )
                r.raise_for_status()
                data = r.json()
                for upd in data.get("result", []):
                    self.offset = max(self.offset, int(upd["update_id"]) + 1)
                    self.storage.set_state("telegram_offset", self.offset)
                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    chat_id = chat.get("id")
                    text = (msg.get("text") or "").strip().lower()
                    if not chat_id:
                        continue
                    if text.startswith("/start"):
                        self.storage.add_subscriber(int(chat_id))
                        await self.send(
                            int(chat_id),
                            "✅ Магнитка бот 1 подключён.\n\n"
                            "Я предупрежу перед нужным периодом, затем дам СТАВКА СЕЙЧАС или ОТМЕНА.\n"
                            "Команды: /status /today /mute /unmute",
                        )
                    elif text.startswith("/status"):
                        self.storage.add_subscriber(int(chat_id))
                        await self.send(int(chat_id), status_provider())
                    elif text.startswith("/today"):
                        self.storage.add_subscriber(int(chat_id))
                        await self.send(int(chat_id), today_provider())
                    elif text.startswith("/mute"):
                        self.storage.set_subscriber_enabled(int(chat_id), False)
                        await self.send(int(chat_id), "🔕 Уведомления выключены. /unmute — включить.")
                    elif text.startswith("/unmute"):
                        self.storage.set_subscriber_enabled(int(chat_id), True)
                        await self.send(int(chat_id), "🔔 Уведомления включены.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(3)
