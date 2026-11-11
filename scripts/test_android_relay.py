"""Compile an isolated, same-signer test APK and run real Android protocol checks."""
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path("E:/tools/androidsdk/build-tools/36.0.0")
JDK = Path("E:/Tools/Java/JDK21/bin")
JAR = Path("E:/tools/androidsdk/platforms/android-36/android.jar")


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], cwd=ROOT, check=True, **kwargs)


def main():
    build = ROOT / "artifacts/android-relay"
    output = build / "checks"
    classes, dex = output / "classes", output / "dex"
    classes.mkdir(parents=True, exist_ok=True)
    dex.mkdir(parents=True, exist_ok=True)
    tests = ROOT / "android/relay/tests"
    run([JDK / "javac.exe", "--release", "17", "-encoding", "UTF-8", "-cp", str(JAR) + ";" + str(build / "classes"), "-d", classes, tests / "RelayChecks.java"])
    run([JDK / "java.exe", "-cp", TOOLS / "lib/d8.jar", "com.android.tools.r8.D8", "--lib", JAR,
         "--classpath", build / "classes", "--min-api", "33", "--output", dex, *classes.rglob("*.class")])
    run([TOOLS / "aapt2.exe", "link", "-I", JAR, "--manifest", tests / "AndroidManifest.xml", "-o", output / "base.apk"])
    with zipfile.ZipFile(output / "base.apk", "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(dex / "classes.dex", "classes.dex")
    run([TOOLS / "zipalign.exe", "-f", "4", output / "base.apk", output / "aligned.apk"])
    private = ROOT / "secrets/android-relay"
    apk = output / "checks.apk"
    run([JDK / "java.exe", "-jar", TOOLS / "lib/apksigner.jar", "sign", "--ks", private / "signing.p12",
         "--ks-pass", "file:" + str(private / "signing-password.txt"), "--out", apk, output / "aligned.apk"])
    run(["adb", "install", "-r", apk])
    result = run(["adb", "shell", "am", "instrument", "-w", "cn.knowledgebase.relay.tests/cn.knowledgebase.relay.RelayChecks"], capture_output=True, text=True, encoding="utf-8")
    print(result.stdout)
    if "PASS 39 protocol checks" not in result.stdout:
        raise SystemExit("Android protocol checks did not pass")


if __name__ == "__main__":
    main()
