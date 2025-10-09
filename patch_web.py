import asyncio
from webbrain import inject_web_context
import main

orig_text = main.on_text
orig_voice = main.on_voice

async def patched_text(update, ctx):
    msg = update.message.text or ""
    ctx_data = await inject_web_context(msg)
    if ctx_data:
        msg += f"\n\n🧠 Интернет-сводка:\n{ctx_data}"
        update.message.text = msg
    await orig_text(update, ctx)

async def patched_voice(update, ctx):
    msg = getattr(update.message, "text", "") or ""
    ctx_data = await inject_web_context(msg)
    if ctx_data:
        msg += f"\n\n🧠 Интернет-сводка:\n{ctx_data}"
        update.message.text = msg
    await orig_voice(update, ctx)

main.on_text = patched_text
main.on_voice = patched_voice
