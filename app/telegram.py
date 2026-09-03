from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .storage import Storage

log = logging.getLogger(__name__)


class TelegramGateway:
    def __init__(
        self,
        token: str,
        storage: Storage,
        owner_telegram_id: int | None = None,
        invite_valid_hours: int = 24,
        display_tz: ZoneInfo | None = None,
    ):
        self.token = token
        self.storage = storage
        self.owner_telegram_id = owner_telegram_id
        self.invite_valid_hours = invite_valid_hours
        self.display_tz = display_tz or ZoneInfo("Asia/Yekaterinburg")
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=20.0)
        self.offset = int(storage.get_state("telegram_offset", 0) or 0)
        self.bot_username: str | None = None
        if owner_telegram_id:
            self.storage.ensure_owner(owner_telegram_id)

    async def initialize(self) -> None:
        r = await self.client.get(f"{self.base}/getMe")
        r.raise_for_status()
        me = r.json().get("result", {})
        self.bot_username = me.get("username")
        log.info("Telegram bot initialized: @%s", self.bot_username or "?")
        if not self.owner_telegram_id:
            log.warning("OWNER_TELEGRAM_ID is not set. Use /myid, then add OWNER_TELEGRAM_ID in BotHost.")

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, chat_id: int, text: str) -> None:
        r = await self.client.post(
            f"{self.base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        r.raise_for_status()

    async def broadcast(self, text: str) -> None:
        for chat_id in self.storage.access_recipients():
            try:
                await self.send(chat_id, text)
            except Exception as exc:  # noqa: BLE001
                log.error("Telegram send failed for %s: %s", chat_id, exc)

    def _fmt_expiry(self, dt: datetime | None) -> str:
        if not dt:
            return "без срока"
        return dt.astimezone(self.display_tz).strftime("%d.%m.%Y %H:%M") + " (время Магнитогорска)"

    def _subscription_text(self, chat_id: int) -> str:
        info = self.storage.subscription_info(chat_id)
        if not info:
            return "🔒 Доступ не активирован. Нужна персональная ссылка-приглашение."
        if info["role"] == "owner":
            return "👑 OWNER — бессрочный полный доступ."
        if not info["active"]:
            return "🔒 Подписка неактивна или закончилась. Для продления обратитесь к владельцу."
        return f"✅ Подписка активна\nДоступ до: {self._fmt_expiry(info['expires_at'])}"

    def _admin_help(self) -> str:
        return (
            "👑 Админ-панель\n\n"
            "/invite7 — одноразовая ссылка на 7 дней\n"
            "/invite14 — на 14 дней\n"
            "/invite30 — на 30 дней\n"
            "/users — пользователи и сроки\n"
            "/extend ID 7|14|30 — продлить\n"
            "/revoke ID — отозвать доступ\n"
            "/stats — статистика 7/30 дней\n"
            "/status /today /debug — технические команды\n"
            "/myid — показать ваш Telegram ID"
        )

    async def _make_invite(self, chat_id: int, days: int) -> None:
        if not self.storage.is_owner(chat_id):
            await self.send(chat_id, "⛔ Команда доступна только владельцу.")
            return
        if not self.bot_username:
            await self.initialize()
        token = self.storage.create_invite(days, chat_id, self.invite_valid_hours)
        link = f"https://t.me/{self.bot_username}?start=inv_{token}"
        await self.send(
            chat_id,
            f"🔗 Приглашение на {days} дней\n\n{link}\n\n"
            f"Ссылка одноразовая и должна быть активирована в течение {self.invite_valid_hours} ч.\n"
            f"Срок подписки начнётся в момент активации.",
        )

    async def _users_text(self, chat_id: int) -> None:
        if not self.storage.is_owner(chat_id):
            await self.send(chat_id, "⛔ Команда доступна только владельцу.")
            return
        rows = self.storage.list_users()
        if not rows:
            await self.send(chat_id, "Пользователей пока нет.")
            return
        lines = ["👥 Пользователи"]
        now = datetime.now(timezone.utc)
        for r in rows[:80]:
            if r["role"] == "owner":
                status = "👑 OWNER"
            else:
                exp = datetime.fromisoformat(r["subscription_expires_at"]) if r["subscription_expires_at"] else None
                if exp and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                active = bool(r["enabled"] and exp and exp > now)
                status = "🟢" if active else "🔴"
                status += " до " + self._fmt_expiry(exp)
            name = r["first_name"] or ""
            user = f"@{r['username']}" if r["username"] else ""
            lines.append(f"{status}\n{name} {user}\nID: {r['chat_id']}")
        await self.send(chat_id, "\n\n".join(lines))

    async def _maintenance(self) -> None:
        # 24-hour reminder.
        for r in self.storage.expiring_users(0, 24):
            expiry = r["subscription_expires_at"]
            key = f"expiry24:{r['chat_id']}:{expiry}"
            if self.storage.notice_sent(key):
                continue
            try:
                await self.send(
                    int(r["chat_id"]),
                    "⏳ До окончания подписки осталось меньше 24 часов.\n"
                    f"Доступ до: {self._fmt_expiry(datetime.fromisoformat(expiry))}\n"
                    "Для продления обратитесь к владельцу.",
                )
                self.storage.mark_notice_sent(key, int(r["chat_id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("Expiry reminder failed for %s: %s", r["chat_id"], exc)

        # Expire and notify. Broadcast already stops at the exact expiry timestamp.
        for r in self.storage.expired_enabled_users():
            expiry = r["subscription_expires_at"] or ""
            key = f"expired:{r['chat_id']}:{expiry}"
            if not self.storage.notice_sent(key):
                try:
                    await self.send(
                        int(r["chat_id"]),
                        "🔒 Срок доступа истёк. Сигналы больше не отправляются.\n"
                        "Для продления подписки обратитесь к владельцу.",
                    )
                    self.storage.mark_notice_sent(key, int(r["chat_id"]))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Expiry notification failed for %s: %s", r["chat_id"], exc)
            self.storage.set_subscriber_enabled(int(r["chat_id"]), False)

    async def poll_commands(
        self,
        status_provider: Callable[[], str],
        today_provider: Callable[[], str],
        debug_provider: Callable[[], str],
    ) -> None:
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
                    user = msg.get("from") or {}
                    chat_id = chat.get("id")
                    raw = (msg.get("text") or "").strip()
                    if not chat_id:
                        continue
                    chat_id = int(chat_id)
                    username = user.get("username")
                    first_name = user.get("first_name")
                    self.storage.touch_user(chat_id, username, first_name)
                    if self.owner_telegram_id and chat_id == self.owner_telegram_id:
                        self.storage.ensure_owner(chat_id, username, first_name)

                    parts = raw.split()
                    command = parts[0].split("@")[0].lower() if parts else ""

                    if command == "/myid":
                        await self.send(chat_id, f"Ваш Telegram ID: {chat_id}")
                        continue

                    if command == "/start":
                        payload = parts[1] if len(parts) > 1 else ""
                        if payload.startswith("inv_"):
                            ok, message, expiry = self.storage.redeem_invite(payload[4:], chat_id, username, first_name)
                            if ok:
                                await self.send(
                                    chat_id,
                                    f"✅ {message}\nДоступ до: {self._fmt_expiry(expiry)}\n\n"
                                    "Вы будете получать сигналы автоматически.\n"
                                    "/stats — результаты за 7 и 30 дней\n"
                                    "/subscription — срок вашей подписки",
                                )
                                for owner_id in self.storage.owner_ids():
                                    if owner_id != chat_id:
                                        await self.send(
                                            owner_id,
                                            "✅ Активировано приглашение\n"
                                            f"{first_name or ''} @{username or '-'}\nID: {chat_id}\n"
                                            f"Доступ до: {self._fmt_expiry(expiry)}",
                                        )
                            else:
                                await self.send(chat_id, f"⛔ {message}")
                        elif self.storage.is_owner(chat_id):
                            await self.send(chat_id, "👑 Магнитка бот 1 — режим владельца.\n\n" + self._admin_help())
                        elif self.storage.has_active_access(chat_id):
                            await self.send(chat_id, "✅ Доступ активен. Сигналы будут приходить автоматически.\n\n" + self._subscription_text(chat_id))
                        else:
                            await self.send(chat_id, "🔒 Доступ к боту только по персональному приглашению владельца.")
                        continue

                    # Owner-only commands.
                    if self.storage.is_owner(chat_id):
                        if command in ("/admin", "/help"):
                            await self.send(chat_id, self._admin_help())
                        elif command in ("/invite7", "/invite14", "/invite30"):
                            await self._make_invite(chat_id, int(command.replace("/invite", "")))
                        elif command == "/invite" and len(parts) >= 2 and parts[1] in {"7", "14", "30"}:
                            await self._make_invite(chat_id, int(parts[1]))
                        elif command == "/users":
                            await self._users_text(chat_id)
                        elif command == "/extend" and len(parts) >= 3:
                            try:
                                uid, days = int(parts[1]), int(parts[2])
                                expiry = self.storage.extend_subscription(uid, days)
                                if not expiry:
                                    await self.send(chat_id, "Не удалось продлить: пользователь не найден или это OWNER.")
                                else:
                                    await self.send(chat_id, f"✅ ID {uid} продлён на {days} дней.\nДо: {self._fmt_expiry(expiry)}")
                                    try:
                                        await self.send(uid, f"✅ Подписка продлена на {days} дней.\nДоступ до: {self._fmt_expiry(expiry)}")
                                    except Exception:  # noqa: BLE001
                                        pass
                            except (ValueError, TypeError):
                                await self.send(chat_id, "Формат: /extend TELEGRAM_ID 7|14|30")
                        elif command == "/revoke" and len(parts) >= 2:
                            try:
                                uid = int(parts[1])
                                ok = self.storage.revoke_subscription(uid)
                                await self.send(chat_id, "✅ Доступ отозван." if ok else "Не удалось отозвать доступ.")
                                if ok:
                                    try:
                                        await self.send(uid, "🔒 Владелец отозвал доступ к сигналам.")
                                    except Exception:  # noqa: BLE001
                                        pass
                            except ValueError:
                                await self.send(chat_id, "Формат: /revoke TELEGRAM_ID")
                        elif command == "/stats":
                            await self.send(chat_id, self.storage.stats_text())
                        elif command == "/subscription":
                            await self.send(chat_id, self._subscription_text(chat_id))
                        elif command == "/status":
                            await self.send(chat_id, status_provider())
                        elif command == "/today":
                            await self.send(chat_id, today_provider())
                        elif command == "/debug":
                            await self.send(chat_id, debug_provider())
                        elif command == "/mute":
                            self.storage.set_subscriber_enabled(chat_id, False)
                            await self.send(chat_id, "🔕 Уведомления владельца выключены. /unmute — включить.")
                        elif command == "/unmute":
                            self.storage.ensure_owner(chat_id, username, first_name)
                            await self.send(chat_id, "🔔 Уведомления владельца включены.")
                        else:
                            await self.send(chat_id, self._admin_help())
                        continue

                    # Active paid user: read-only access only.
                    if self.storage.has_active_access(chat_id):
                        if command == "/stats":
                            await self.send(chat_id, self.storage.stats_text())
                        elif command == "/subscription":
                            await self.send(chat_id, self._subscription_text(chat_id))
                        else:
                            await self.send(
                                chat_id,
                                "✅ Подписка активна. Управление стратегиями недоступно.\n"
                                "/stats — статистика\n/subscription — срок доступа",
                            )
                    else:
                        await self.send(chat_id, "🔒 Доступ к боту только по персональному приглашению владельца.")

                await self._maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram polling error: %s", exc)
                await asyncio.sleep(3)
