"""Provision from local ignored secrets; never print Wi-Fi passwords or tokens."""
import argparse
import csv
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "secrets"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="admin")
    parser.add_argument("--server", required=True)
    parser.add_argument("--transport", choices=["ble", "wifi"], default="ble")
    parser.add_argument("--idf-root", default="E:/Espressif/esp-idf")
    parser.add_argument("--idf-python", default="E:/Espressif/tools/python_env/idf6.0_py3.13_env/Scripts/python.exe")
    args = parser.parse_args()
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", args.server):
        raise SystemExit("需要有效 HTTPS 服务地址，不含路径")
    PRIVATE.mkdir(exist_ok=True)
    device_file = PRIVATE / "note4-device.json"
    if device_file.exists():
        data = json.loads(device_file.read_text(encoding="utf-8"))
        if data["username"] != args.user:
            raise SystemExit("已有设备属于其他账号；请明确处理现有设备后重试")
    else:
        code = (
            "import os,json;os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');"
            "import django;django.setup();"
            "from django.contrib.auth import get_user_model;"
            "from apps.voice.services import add_device;"
            f"u=get_user_model().objects.get(username={args.user!r},is_active=True);"
            "d,t=add_device(u,{'name':'Note 4 语音入口'});"
            "print(json.dumps({'id':str(d.pk),'token':t,'username':u.username,'conversation_id':d.conversation_id}))"
        )
        result = subprocess.run(["docker", "compose", "exec", "-T", "web", "python", "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise SystemExit("创建设备失败，请先确认应用及迁移已部署")
        data = json.loads(result.stdout.strip().splitlines()[-1])
    data["server"] = args.server
    device_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    values = {"server": args.server, "token": data["token"], "transport": args.transport}
    if args.transport == "wifi":
        wifi = json.loads((PRIVATE / "note4-wifi.json").read_text(encoding="utf-8-sig"))
        if not 1 <= len(wifi["ssid"].encode("utf-8")) <= 32:
            raise SystemExit("热点名称必须不超过 32 个 UTF-8 字节")
        values.update(ssid=wifi["ssid"], password=wifi["password"])
    target = PRIVATE / "note4-nvs.csv"
    with target.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["key", "type", "encoding", "value"])
        writer.writerow(["kbvoice", "namespace", "", ""])
        for key, value in values.items():
            writer.writerow([key, "data", "string", value])
    generator = Path(args.idf_root) / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    result = subprocess.run([args.idf_python, str(generator), "generate", str(target), str(PRIVATE / "note4-nvs.bin"), "0x6000"], capture_output=True)
    if result.returncode:
        raise SystemExit("NVS 配置镜像生成失败（未输出凭据）")
    print("Device added/configured:", data["id"], "conversation", data["conversation_id"])
    print("Private provisioning image ready: secrets/note4-nvs.bin")


if __name__ == "__main__":
    main()
