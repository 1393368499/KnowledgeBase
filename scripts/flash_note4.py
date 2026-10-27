"""Flash only after checking the target, local provisioning and factory backups."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--config-only", action="store_true", help="Update private NVS without rewriting application")
    mode.add_argument("--app-only", action="store_true", help="Update application, preserving provisioned NVS and pending identity")
    parser.add_argument("--idf-python", default="E:/Espressif/tools/python_env/idf6.0_py3.13_env/Scripts/python.exe")
    args = parser.parse_args()
    backup = ROOT / "secrets/note4-backup"
    hashes = []
    for name in ("factory-a.bin", "factory-b.bin"):
        p = backup / name
        if not p.exists() or p.stat().st_size != 0x1000000:
            raise SystemExit("Missing complete factory backup")
        hashes.append(hashlib.sha256(p.read_bytes()).hexdigest())
    if hashes[0] != hashes[1]:
        raise SystemExit("Factory backup hashes do not match")
    build = ROOT / "firmware/note4/build"
    description = json.loads((build / "project_description.json").read_text())
    if description.get("target") != "esp32s3":
        raise SystemExit("Wrong firmware chip target")
    manifest = json.loads((build / "flasher_args.json").read_text())
    nvs = ROOT / "secrets/note4-nvs.bin"
    if not nvs.exists() or nvs.stat().st_size != 0x6000:
        raise SystemExit("Missing device provisioning image")
    cmd = [args.idf_python, "-m", "esptool", "--chip", "esp32s3", "--port", args.port,
           "--baud", "921600", "write-flash"]
    if not args.config_only:
        for offset, relative in sorted(manifest["flash_files"].items(), key=lambda pair: int(pair[0], 16)):
            if args.app_only and int(offset, 16) != 0x10000:
                continue
            cmd += [offset, str(build / relative)]
    if not args.app_only:
        cmd += ["0x9000", str(nvs)]
    # Never erase the full device or modify eFuses. esptool verifies each write.
    subprocess.run(cmd, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
