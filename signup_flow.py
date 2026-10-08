"""First-use email/password signup. No email delivery or verification step."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time

from email_auth import Access, AuthStore, RegistrationError, hash_password, password_matches


logger = logging.getLogger(__name__)
SIGNUP_TIMEOUT = 15 * 60


@dataclass
class SignupSession:
    stage: str = "idle"
    email: str | None = None
    password_hash: str | None = field(default=None, repr=False)
    expires_at: float = 0

    def reset(self):
        self.stage = "idle"
        self.email = None
        self.password_hash = None
        self.expires_at = 0


class SignupFlow:
    def __init__(self, store: AuthStore, clock=time.time):
        self.store = store
        self.clock = clock

    async def access(self, telegram_id: int, send) -> Access | None:
        try:
            return await asyncio.to_thread(self.store.access, telegram_id)
        except Exception as error:
            # Never log the Telegram update, message text, or credentials.
            logger.error("Cannot check email authorization (%s). Check the account file and email allowlist.",
                         type(error).__name__)
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
        await send("Welcome. To register, send your Technolife work email (name@technolife.com).\n"
                   "Use /cancel to stop signup.", None)
        return False

    async def text(self, telegram_id: int, state: SignupSession, text: str, send) -> bool:
        """Consume signup text. True means registration/access is now complete."""
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
                state.email = await asyncio.to_thread(self.store.available_email, text)
                state.stage = "password"
                await send("Email approved. Set a new password for this bot (12–128 characters).\n"
                           "Choose a separate password; do not send your work email password.\n"
                           "I will try to delete your password messages after receiving them.", None)
            elif state.stage == "password":
                # Recheck approval immediately before requesting/saving a hash.
                await asyncio.to_thread(self.store.available_email, state.email)
                state.password_hash = await asyncio.to_thread(hash_password, text)
                state.stage = "confirm"
                await send("Send the same bot password again to confirm it.", None)
            elif state.stage == "confirm":
                matches = await asyncio.to_thread(password_matches, text, state.password_hash)
                if not matches:
                    await send("The passwords do not match. Try the confirmation again, or use /start to restart signup.", None)
                    return False
                account = await asyncio.to_thread(self.store.register, telegram_id, state.email, state.password_hash)
                state.reset()
                await send(f"Registration complete for {account.email}.\n"
                           "This Telegram account is now linked. Next time, /start will recognize you automatically.", None)
                return True
        except RegistrationError as error:
            # Only controlled, non-secret messages from our validation code.
            await send(str(error), None)
        except Exception as error:
            state.reset()
            logger.error("Signup failed (%s). No signup message content was logged.", type(error).__name__)
            await send("Registration is temporarily unavailable. Please contact the administrator.", None)
        return False

    async def cancel(self, state: SignupSession, send):
        state.reset()
        await send("Signup cancelled. Use /start whenever you want to register.", None)
