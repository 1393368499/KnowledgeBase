"""Download a fixed public ASR model snapshot, checking its published LFS hash."""
import hashlib
from pathlib import Path
import httpx

REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MODEL_SHA = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
BASE = f"https://huggingface.co/Systran/faster-whisper-small/resolve/{REVISION}"


def main():
    root = Path(__file__).resolve().parent.parent / "data/media/voice-models/small"
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name in ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"]:
            path = root / name
            if path.is_file() and (name != "model.bin" or hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA):
                continue
            part = path.with_suffix(".download")
            with client.stream("GET", BASE + "/" + name) as response:
                response.raise_for_status()
                with part.open("wb") as out:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024): out.write(chunk)
            if name == "model.bin" and hashlib.sha256(part.read_bytes()).hexdigest() != MODEL_SHA:
                raise SystemExit("Model checksum mismatch")
            part.replace(path)
            print("Downloaded", name, path.stat().st_size, flush=True)
    (root / "REVISION").write_text(REVISION + "\n" + MODEL_SHA + "\n")
    print("ASR model snapshot verified", REVISION)


if __name__ == "__main__":
    main()
