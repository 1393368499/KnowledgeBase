"""Fetch the pinned Linux tunnel connector needed by its Docker build."""
import hashlib
from pathlib import Path
import httpx

VERSION = "2026.10.4"
SHA256 = "ae5e8add7884b0531d66bbca4570d1de491ff6da51e4ebe7eb160cdf241d9e42"
URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-linux-amd64"


def main():
    path = Path(__file__).resolve().parent.parent / "artifacts/voice-tools/cloudflared-linux-amd64"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        temporary = path.with_suffix(".download")
        with httpx.stream("GET", URL, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes(): output.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != SHA256:
            temporary.unlink(missing_ok=True)
            raise SystemExit("Tunnel connector checksum mismatch")
        temporary.replace(path)
    print("Verified cloudflared", VERSION, SHA256)


if __name__ == "__main__":
    main()
