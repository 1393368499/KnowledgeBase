"""One bounded startup job, then exit. No Docker socket or credentials needed."""
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def gateway_ready(endpoint):
    try:
        with build_opener(NoRedirect).open(Request(endpoint + "/api/v1/voice/device/config/"), timeout=5):
            return False
    except HTTPError as error:
        return error.code == 401
    except OSError:
        return False


def save(path, endpoint):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"server": endpoint,
        "updated_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    os.replace(temporary, path)


def main():
    log = Path("/run/voice/startup.log")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                found = PATTERN.findall(stream.read(65536).decode("utf-8", "replace"))
            if found and gateway_ready(found[-1]):
                save(Path("/state/current.json"), found[-1])
                print("Voice endpoint saved; startup job finished", flush=True)
                return
        except OSError:
            pass
        time.sleep(3)
    print("Voice endpoint was not ready within startup budget; previous saved address retained", flush=True)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
