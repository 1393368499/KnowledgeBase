"""Offline native Android build using the installed official SDK/JDK only.

Signing identity stays in ignored secrets/ so later versions can update in place.
An optional public HTTPS endpoint can be included. Device tokens, Wi-Fi
credentials and audio are never packaged in APK.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    subprocess.run([str(x) for x in args], check=True, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", default="E:/tools/androidsdk")
    parser.add_argument("--jdk", default="E:/Tools/Java/JDK21")
    parser.add_argument("--endpoint-file", type=Path, help="Private local JSON containing the public server URL")
    args = parser.parse_args()
    sdk, jdk = Path(args.sdk), Path(args.jdk)
    tools = sdk / "build-tools/36.0.0"
    jar = sdk / "platforms/android-36/android.jar"
    output = ROOT / "artifacts/android-relay"
    classes, dex = output / "classes", output / "dex"
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(parents=True, exist_ok=True)
    private = ROOT / "secrets/android-relay"
    private.mkdir(parents=True, exist_ok=True)
    key, password = private / "signing.p12", private / "signing-password.txt"
    if key.exists() != password.exists():
        raise SystemExit("签名文件不完整，保留现有文件并检查备份")
    if not key.exists():
        import secrets
        password.write_text(secrets.token_urlsafe(32), encoding="ascii")
        run([jdk / "bin/keytool.exe", "-genkeypair", "-keystore", key,
             "-storepass:file", password, "-alias", "kb-relay", "-keyalg", "RSA",
             "-keysize", "3072", "-validity", "3650", "-dname", "CN=KnowledgeBase Local Relay"])
    source = ROOT / "android/relay"
    generated = output / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    run([tools / "aapt2.exe", "compile", "--dir", source / "res", "-o", output / "resources.zip"])
    run([tools / "aapt2.exe", "link", "-I", jar, "--manifest", source / "AndroidManifest.xml",
         output / "resources.zip", "--java", generated,
         "--version-code", "8", "--version-name", "1.3.1", "-o", output / "base.apk"])
    run([jdk / "bin/javac.exe", "--release", "17", "-encoding", "UTF-8", "-cp", jar,
         "-d", classes, *sorted((source / "src").rglob("*.java")), *sorted(generated.rglob("*.java"))])
    # Invoke the official D8 jar directly to avoid shell/batch quoting differences.
    run([jdk / "bin/java.exe", "-cp", tools / "lib/d8.jar", "com.android.tools.r8.D8",
         "--lib", jar, "--min-api", "33", "--output", dex, *sorted(classes.rglob("*.class"))])
    shutil.copyfile(output / "base.apk", output / "unsigned.apk")
    with zipfile.ZipFile(output / "unsigned.apk", "a", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in dex.glob("*.dex"):
            archive.write(item, item.name)
        if args.endpoint_file:
            import json
            from urllib.parse import urlsplit
            endpoint = json.loads(args.endpoint_file.read_text(encoding="utf-8-sig"))["server"]
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.port or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise SystemExit("Invalid HTTPS endpoint")
            archive.writestr("assets/endpoint.txt", endpoint.rstrip("/"))
    run([tools / "zipalign.exe", "-f", "-p", "4", output / "unsigned.apk", output / "aligned.apk"])
    apk = output / "KnowledgeBase-Relay.apk"
    run([jdk / "bin/java.exe", "-jar", tools / "lib/apksigner.jar", "sign", "--ks", key,
         "--ks-pass", "file:" + str(password), "--out", apk, output / "aligned.apk"])
    run([jdk / "bin/java.exe", "-jar", tools / "lib/apksigner.jar", "verify", "--verbose", apk])
    print("APK ready:", apk)


if __name__ == "__main__":
    main()
