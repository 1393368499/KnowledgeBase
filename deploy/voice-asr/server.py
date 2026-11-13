import io
import os
import threading
import wave
from contextlib import asynccontextmanager

# These dependencies are installed in the ASR image from requirements.lock.
from fastapi import FastAPI, File, HTTPException, UploadFile  # pyright: ignore[reportMissingImports]
from faster_whisper import WhisperModel  # pyright: ignore[reportMissingImports]
from faster_whisper.vad import get_speech_timestamps, VadOptions  # pyright: ignore[reportMissingImports]
import numpy as np

model = None
lock = threading.Lock()



@asynccontextmanager
async def lifespan(app):
    global model
    model = WhisperModel(os.getenv("ASR_MODEL", "small"), device="cpu", compute_type="int8", download_root="/models")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"ready": model is not None}


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...)):
    raw = file.file.read(2_000_045)
    if not 44 <= len(raw) <= 2_000_044:
        raise HTTPException(400, "音频长度无效")
    try:
        with wave.open(io.BytesIO(raw)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError()
            if not 3200 <= wav.getnframes() <= 960000:
                raise ValueError()
            pcm = wav.readframes(wav.getnframes())
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    except (ValueError, EOFError, wave.Error):
        raise HTTPException(400, "需要 16kHz PCM16 WAV 音频")
    with lock:
        if model is None:
            raise HTTPException(503, "语音识别模型未就绪")
        speech = get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=200))
        if not speech:
            return {"text": "", "speech_detected": False}
        segments, info = model.transcribe(audio, language="zh", vad_filter=True, beam_size=5, condition_on_previous_text=False)
        segments = [s for s in segments if s.no_speech_prob < 0.7 and s.avg_logprob > -1.0]
        text = "".join(s.text for s in segments).strip()
    return {"text": text, "speech_detected": bool(text)}
