"""First-use email signup. No password or mailbox verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from .auth import Access, AuthStore, RegistrationError

logger = logging.getLogger(__name__)
SIGNUP_TIMEOUT = 15 * 60


@dataclass
class SignupSession:
    stage: str = "idle"
    expires_at: float = 0

    def reset(self):
        self.stage = "idle"
        self.expires_at = 0


class SignupFlow:
    def __init__(self, store: AuthStore, clock=time.time):
        self.store = store
        self.clock = clock

    async def access(self, telegram_id: int, send) -> Access | None:
        try:
            return await asyncio.to_thread(self.store.access, telegram_id)
        except Exception as error:  # noqa: BLE001
            logger.error("Cannot check email authorization (%s).", type(error).__name__)
            await send("Account access is temporarily unavailable. Please contact the administrator.", None)
            return None

    async def start(self, telegram_id: int, state: SignupSession, send) -> bool:
        state.reset()
        access = await self.access(telegram_id, send)
        if access is None:
            return False
        if access.allowed:
            return True
        if access.account:
            await send("Your email is no longer approved to use the bot. Please contact the administrator.", None)
            return False
        state.stage = "email"
        state.expires_at = self.clock() + SIGNUP_TIMEOUT
        await send(
            "Welcome. To register, send your approved Gmail address (name@gmail.com).\n"
            "Use /cancel to stop signup.",
            None,
        )
        return False

    async def text(self, telegram_id: int, state: SignupSession, text: str, send) -> bool:
        access = await self.access(telegram_id, send)
        if access is None:
            state.reset()
            return False
        if access.allowed:
            state.reset()
            return True
        if access.account:
            state.reset()
            await send("Your email is no longer approved to use the bot. Please contact the administrator.", None)
            return False
        if state.stage == "idle":
            return await self.start(telegram_id, state, send)
        if self.clock() >= state.expires_at:
            state.reset()
            await send("Signup expired. Use /start to begin again.", None)
            return False
        try:
            if state.stage == "email":
                account = await asyncio.to_thread(self.store.register, telegram_id, text)
                state.reset()
                await send(
                    f"Registration complete for {account.email}.\n"
                    "This Telegram account is now linked. Next time, /start will recognize you automatically.",
                    None,
                )
                return True
        except RegistrationError as error:
            await send(str(error), None)
        except Exception as error:  # noqa: BLE001
            state.reset()
            logger.error("Signup failed (%s).", type(error).__name__)
            await send("Registration is temporarily unavailable. Please contact the administrator.", None)
        return False

    async def cancel(self, state: SignupSession, send):
        state.reset()
        await send("Signup cancelled. Use /start whenever you want to register.", None)
