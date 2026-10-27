# KnowledgeBase Note 4 voice terminal

Target: monochrome ZECTRIX NOTE4, ESP32-S3 N16R8. NOTE4C is not supported.

Board/display/audio sources are adapted from Zectrix Lab's reference demo,
commit `ca285c98ed0641f86780edb1f5ec77b0335fe649`.
Keep `LICENSE` and `THIRD_PARTY_NOTICES.md`; the ESP-IDF 6 adaptation changes
the EPD component's GPIO/SPI dependencies. This terminal builds the panel's
existing OTP black/white path only (`ZECTRIX_EPD_MONO_ONLY`); experimental gray
waveforms fail their upstream integrity assertions with this toolchain and are
excluded as a complete feature. Their checks are not removed or bypassed;
the 4bpp entry point returns ESP_ERR_NOT_SUPPORTED. Application code lives in `main/`.

## Build

Use the installed ESP-IDF 6.0.2 environment:

```powershell
$env:IDF_TOOLS_PATH = 'E:\Espressif\tools'
. E:\Espressif\esp-idf\export.ps1
python scripts/build_note4_status.py  # run from the KnowledgeBase root if regenerating screens
idf.py -C firmware/note4 set-target esp32s3
idf.py -C firmware/note4 build
```

The target is also pinned in CMakeLists.txt. Credentials are not compiled into
the application. Current partition layout uses NVS at 0x9000 (0x6000 bytes),
partition table at 0x8000 and the application at 0x10000. Always use the generated
flasher_args.json rather than assuming offsets for a different build.

## Provision and flash

1. Verify two complete 16 MiB factory backups and their SHA-256 hashes.
2. Deploy the voice backend and obtain a reachable HTTPS device URL.
3. Keep hotspot settings only in ignored `secrets/note4-wifi.json`.
4. Run `scripts/provision_note4.py --user <owner> --server https://<host>`
   from the project root. It adds one independent device on first use, reuses
   its local identity thereafter, and generates private NVS data without
   printing credentials.
5. Flash the application using its build manifest; flash the generated private
   `secrets/note4-nvs.bin` to 0x9000. Do not distribute that provisioning image.
6. Verify microphone capture, hotspot connection, server authentication and
   the actual turn state in `/app/devices/` before declaring the device ready.

Quick Tunnel hostnames change after recreation. Update provisioning when the
address changes. A stable endpoint remains a separate deployment requirement.

## Controls and current scope

- Hold the front button to record (LED on); release to upload and submit.
- Press UP during recording to cancel. Maximum recording duration: 60 seconds.
- Short front press switches between transcript and reply after a completed turn.
- UP / DOWN change pages directly from a bounded 64-page PSRAM cache. Nearby
  pages are fetched in verified batches of up to eight; uncached pages need
  connectivity. One in-flight voice request per device.
- Input uses one complete transcription after release. Incremental preview was
  withdrawn at the user's request.
- Monochrome transition refresh follows the first full refresh, with a full
  refresh every eight updates. The panel still needs about one second to update.
- Audio lives in PSRAM until accepted. Pending turn identity survives reset in
  NVS, but unuploaded audio does not survive power loss.
- Certificate and hostname verification remain enabled. Network time must be
  available. ESP-IDF's cross-signed certificate bundle verification is enabled
  for the HTTPS endpoint's Google Trust Services certificate chain.
- This first version keeps the MCU awake; measured battery optimization and
  safe OTA partitions are later work. USB remains the recovery/update path.

Factory restore is a separate, explicit recovery operation using this device's
verified full backup at address 0x0; never use another device's private backup.
