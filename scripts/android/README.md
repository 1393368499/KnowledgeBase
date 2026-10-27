# iQOO Neo8 / OriginOS 6 hotspot diagnostics

This phone's normal Vivo hotspot screen did not expose the frequency selector.
The installed Settings package also provides the stock Android hotspot screen:

```powershell
adb shell "am start -W -n 'com.android.settings/.Settings`$WifiTetherSettingsActivity'"
```

When invoking adb through Python, pass the remote component in single quotes
so the remote shell preserves the literal `$`:

```python
subprocess.run([
    "adb", "shell",
    "am start -W -n 'com.android.settings/.Settings$WifiTetherSettingsActivity'",
], check=True)
```

On the tested V2301A / Android 16 / OriginOS 6 phone, choose
**速度和兼容性 → 2.4 GHz**. Inspect the current page before interacting; screen
positions may change. Other phones or software versions may differ.

`HotspotConfig.java` is an optional read-only diagnostic utility executed with
the owner's authorized Android USB debugging shell. It is not an installed app
and does not request root. It supports `read`, `backup`, and `verify`; frequency
changes use the phone's Settings UI. A direct OS setter was not usable on the
tested phone and is intentionally absent from this utility.

Compile against Android API 36 with Java 17 bytecode, convert to dex with D8,
then push to `/data/local/tmp/kb-note4-hotspot.dex`:

```powershell
adb shell 'CLASSPATH=/data/local/tmp/kb-note4-hotspot.dex app_process /system/bin HotspotConfig read'
```

`backup` stores a private Parcelable snapshot on the phone; `verify` checks that
the SSID, password, security mode, client limit and shutdown settings still match
that snapshot. No identifiers or passwords are printed. Band `1` means 2.4 GHz;
the phone originally reported `3` (2.4/5 GHz automatic selection). Keep backups
and raw phone diagnostics in the ignored `secrets/` directory.
