# Telegram front-end for the SSAS/Qwen agent

This project combines the `qwen_agent.py` AI/SSAS logic and a Telegram front-end using long polling.

## what it does

- Reads the live SSAS schema once when the bot starts.
- Accepts questions from approved Telegram users only.
- Passes each question to `ask(question, model_schema)` in `qwen_agent.py`.
- Sends the resulting answer back to Telegram.
- Keeps database answers in private chats only.
- Re-reads `allowed_users.txt` on every request, so allowlist edits do not require a restart.
- Supports `/reloadschema` to refresh SSAS metadata without restarting the bot.

## 1. Create the Telegram bot

In Telegram, open the official **@BotFather** account, create a bot, and copy the bot token.

Do not paste the token into source code. Put it in `.env` as:

```text
TELEGRAM_BOT_TOKEN=your-token-here
```

## 2. Prepare `.env`

Keep using your existing `.env` with your Qwen/SSAS settings. Add this line:

```text
TELEGRAM_BOT_TOKEN=your-token-here
```

An `.env.example` file is included as a reference. The real `.env` is deliberately not included in this package.

## 3. Install the Telegram dependency

Install all listed dependencies:

```powershell
pip install -r requirements.txt
```

## 4. Edit the allowlist

Open `allowed_users.txt` in Notepad. One user goes on each line.

You can use a username:

```text
@username
```

Or, preferably, a stable numeric Telegram user ID:

```text
123456789
```

The bot reads this file on every request, so you can add or remove people while the bot is running.

### Why numeric IDs are better

Telegram usernames can be changed. Numeric user IDs are stable. A user can privately send `/whoami` to the bot to see their numeric ID; you can then put that number in `allowed_users.txt` and remove their username entry.

## 5. Start the bot on your laptop

Run:

```powershell
python telegram_bot.py
```

Or double-click `start_bot.bat`.

At startup, the bot first connects to SSAS and loads the semantic-model schema. If that succeeds, Telegram long polling begins.

Then open your bot in Telegram, press **Start**, and send a normal question.

## Commands

- `/start` - introduction
- `/help` - usage help
- `/whoami` - show your username and numeric Telegram user ID
- `/reloadschema` - reload the live SSAS schema

## Security behavior

- Only users in `allowed_users.txt` can ask questions or reload the schema.
- `/whoami` is available to a private-chat user so you can obtain their numeric ID before allowing them.
- Questions are accepted only in private chats. This prevents a database answer from accidentally being posted into a Telegram group.
- Full internal exceptions are printed to the laptop/server log; Telegram receives a generic error instead of SSAS/Qwen internals.

## Moving it to a server later

You can copy the same project folder to the server and run `python telegram_bot.py` there. You do **not** need to rewrite this as FastAPI just to move it to a server; long polling works there too.

The server must have:

1. Python and the required packages.
2. Your `.env` file stored securely.
3. Network access to the SSAS server.
4. ADOMD.NET/PyADOMD configured so `qwen_agent.py` works on that machine.
5. Outbound internet access to Telegram and Qwen.

If the eventual server is Windows, the current ADOMD.NET approach is the easiest path. If it is Linux, the SSAS connectivity layer will need separate consideration because the current agent relies on a Windows ADOMD.NET installation path.
