# Telegram bot setup (Bot API, no personal-account access)

The bot only sees what Telegram delivers to it. It never logs in as you and never reads channels it
is not a member of.

## How signals reach the bot
| Path | You do | Bot treats source as |
|---|---|---|
| A. Channel you control or administer | Add the bot to the channel (as an admin, posting rights not needed) | that channel |
| B. Channel you *don't* control | Forward its posts into your **private intake group** where the bot is a member | the **original channel** (from Telegram's `forward_origin`), with the **original post time** |
| C. Your private group | Type a signal yourself | the private group |

Rejected automatically: messages in chats not on your intake list; posts/forwards by anyone not on your
trusted-user list; forwards from individual users or hidden users (can't be attributed to a channel);
**edited** messages (a signal changed after posting is audited, never executed); and everything the
signal validator rejects (stale, duplicate, no SL, ambiguous, etc.). The same channel post received via
path A and forwarded via path B counts as one signal.

## Steps
1. In Telegram, open **@BotFather** → `/newbot` → choose a name. BotFather shows a token. **Do not paste it into chat with me, into the repo, or into screenshots.**
2. Store the token on your Windows PC (stored in your user environment in the registry; anyone with access to your Windows account can read it):
   ```powershell
   $t = Read-Host "Paste bot token"; [Environment]::SetEnvironmentVariable("AEGIS_TELEGRAM_BOT_TOKEN", $t, "User"); Remove-Variable t
   ```
   Then reopen PowerShell. (Optional, stronger: Windows Credential Manager via the `keyring` package. Ask me to add it; it needs your OK as a new dependency.)
3. Group setup (path B/C): create a **private** group, add the bot. Either make the bot an admin or, in BotFather, `/setprivacy` → *Disable*, otherwise the bot won't see ordinary group messages.
4. Post or forward one harmless message in each chat, then run `uv run aegis telegram-check`. It lists chat IDs and sender IDs from pending updates **without consuming them**.
5. Set allowlists (numeric IDs, comma-separated):
   ```powershell
   [Environment]::SetEnvironmentVariable("AEGIS_TELEGRAM_INTAKE_CHATS", "-100...,-100...", "User")   # chats the bot listens in
   [Environment]::SetEnvironmentVariable("AEGIS_TELEGRAM_SOURCES", "-100...", "User")                 # channels/groups whose signals you trust
   [Environment]::SetEnvironmentVariable("AEGIS_TELEGRAM_TRUSTED_USERS", "123456", "User")           # your own user ID
   ```
6. Run (paper trading with live MT5 demo prices — no broker orders):
   `uv run aegis telegram-run --broker paper`
   Only after a paper run looks right: `uv run aegis telegram-run --broker mt5-demo` (demo account orders; refuses non-demo).

## Notes
- Delivery is at-most-once: if Aegis crashes mid-message, that signal is skipped rather than risk a double trade.
- Queued messages older than `max_signal_age_seconds` (default 120 s) are rejected as stale on startup.
- Respect the source channel's terms; forwarding paid-channel content may breach them.
