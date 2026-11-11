"""Manually verify the voice tunnel and update authorized USB phones once."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[1]
ADB = Path("E:/tools/androidsdk/platform-tools/adb.exe")
ENDPOINT = ROOT / "secrets/note4-current-endpoint.json"
STATUS = ROOT / "artifacts/voice-connection/status.txt"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def command(args):
    return subprocess.run([str(x) for x in args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=25,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def current_endpoint():
    result = command(["docker", "compose", "-f", "docker-compose.yml", "-f",
                      "deploy/docker-compose.voice.yml", "logs", "--no-color", "--tail", "1600", "voice-tunnel"])
    matches = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", result.stdout + result.stderr)
    return matches[-1] if matches else None


def verify(endpoint):
    device = json.loads((ROOT / "secrets/note4-device.json").read_text(encoding="utf-8-sig"))
    req = Request(endpoint + "/api/v1/voice/device/config/",
                  headers={"Authorization": "Bearer " + device["token"]})
    with build_opener(NoRedirect).open(req, timeout=15) as response:
        config = json.loads(response.read(16384))
    if str(config.get("id")) != str(device["id"]):
        raise ValueError("Device identity mismatch")


def sync(sent):
    endpoint = current_endpoint()
    if not endpoint:
        return "Waiting for the voice tunnel"
    if sent.get("verified") != endpoint:
        verify(endpoint)
        temporary = ENDPOINT.with_suffix(".tmp")
        temporary.write_text(json.dumps({"server": endpoint}), encoding="utf-8")
        os.replace(temporary, ENDPOINT)
        sent["verified"] = endpoint
    lines = command([ADB, "devices"]).stdout.splitlines()[1:]
    peers = [line.split()[0] for line in lines if len(line.split()) == 2 and line.split()[1] == "device"]
    for peer in peers:
        if sent.get(peer) == endpoint:
            continue
        result = command([ADB, "-s", peer, "shell", "am", "broadcast", "-n",
            "cn.knowledgebase.relay/.EndpointReceiver", "-a", "cn.knowledgebase.relay.SET_ENDPOINT", "--es", "server", endpoint])
        if result.returncode == 0 and re.search(r"result=1\b", result.stdout):
            sent[peer] = endpoint
    for peer in list(sent):
        if peer != "verified" and peer not in peers:
            del sent[peer]
    return ("Remote entry verified; phone updated" if any(sent.get(p) == endpoint for p in peers)
            else "Remote entry verified; waiting for an authorized USB phone with App 1.3.0")


def main():
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = sync({})
    except Exception as exc:
        status = "Connection check failed: " + type(exc).__name__
    # Manual invocation only. No background loop or automatic startup.
    print(status, flush=True)
    STATUS.write_text(status, encoding="utf-8")


if __name__ == "__main__":
    main()
