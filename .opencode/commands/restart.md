---
description: Restart the Telegram bot (media_bot) and its supervisor/watcher process.
---

Restart the Telegram bot and supervisor process. Run these steps:

1. Inform the user the bot is restarting.
2. Find and kill the bot process (`python3 -m media_bot`) with SIGTERM.
3. Find and kill the supervisor process (`python3 supervisor.py`) with SIGTERM.
4. Start the supervisor in the background with `nohup`.
5. The supervisor will automatically start the bot.

Execute this bash command:

```bash
echo "🔄 Restarting bot and supervisor..."
pkill -f "python3 -m media_bot" 2>/dev/null; sleep 1
pkill -f "python3 supervisor.py" 2>/dev/null; sleep 1
nohup python3 supervisor.py > /tmp/supervisor.log 2>&1 &
echo "✅ Bot restarted (PID $(jobs -p 2>/dev/null || echo 'started'))."
```
