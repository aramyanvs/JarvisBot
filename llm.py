import io
import tempfile
from openai import AsyncOpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL
aclient = AsyncOpenAI(api_key=OPENAI_API_KEY, max_retries=3, timeout=30)

def sys_prompt(persona: str, lang: str) -> str:
    base = "Отвечай кратко и по делу."
    if persona == "professor":
        base = "Объясняй подробно, по шагам, приводя примеры и уточнения."
    if persona == "sarcastic":
        base = "Отвечай с лёгкой ироничностью, но оставайся полезным и доброжелательным."
    return f"{base} Язык ответа: {lang}. Если пользователь явно просит другой язык — следуй ему. Если дан URL или вопрос о текущих событиях — можешь использовать предоставленный веб-контент ниже."

async def empathize(text: str, lang: str) -> str:
    try:
        r = await aclient.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "system", "content": "Определи настроение пользователя: neutral, positive, stressed, sad, angry. Верни одно слово."}, {"role": "user", "content": text}],
            temperature=0.2,
            max_tokens=5,
        )
        mood = (r.choices[0].message.content or "neutral").strip().lower()
    except Exception:
        mood = "neutral"
    if lang.startswith("ru"):
        d = {"positive": "Рад это слышать!", "stressed": "Понимаю. Давай разгрузим голову — я рядом.", "sad": "Сочувствую. Готов поддержать.", "angry": "Понимаю злость. Постараюсь помочь конструктивно.", "neutral": "Принято."}
    else:
        d = {"positive": "Glad to hear!", "stressed": "I get it. I'm here to help.", "sad": "Sorry to hear that.", "angry": "I understand. Let's fix it.", "neutral": "Got it."}
    return d.get(mood, "Got it.")

async def llm(messages, sys):
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": sys}] + messages,
        temperature=0.6,
        max_tokens=1000,
    )
    return r.choices[0].message.content

async def to_tts(text: str, voice: str = "alloy") -> bytes:
    resp = await aclient.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(resp.read())
    tmp.close()
    with open(tmp.name, "rb") as f:
        return f.read()

async def transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = await aclient.audio.transcriptions.create(model="whisper-1", file=f, language="auto")
    return r.text

async def translate_text(text: str, to_lang: str) -> str:
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": f"Переведи текст на {to_lang}. Сохраняй смысл и тон."}, {"role": "user", "content": text}],
        temperature=0.2,
        max_tokens=1000,
    )
    return r.choices[0].message.content

async def summarize_text(text: str, lang: str) -> str:
    r = await aclient.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": f"Суммируй кратко на {lang}."}, {"role": "user", "content": text}],
        temperature=0.3,
        max_tokens=600,
    )
    return r.choices[0].message.content

async def openai_image(prompt: str) -> bytes:
    im = await aclient.images.generate(model="gpt-image-1", prompt=prompt, size="1024x1024", response_format="b64_json")
    import base64
    return base64.b64decode(im.data[0].b64_json)
