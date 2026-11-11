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
3. BLE phone relay is the default. Only `--transport wifi` needs hotspot settings
   in ignored `secrets/note4-wifi.json`.
4. Run `scripts/provision_note4.py --user <owner> --server https://<host>`
   from the project root. It adds one independent device on first use, reuses
   its local identity thereafter, and generates private NVS data without
   printing credentials.
5. Flash the application using its build manifest; flash the generated private
   `secrets/note4-nvs.bin` to 0x9000. Do not distribute that provisioning image.
6. Verify microphone capture, encrypted phone connection, server authentication and
   the actual turn state in `/app/devices/` before declaring the device ready.

Quick Tunnel hostnames change after recreation. The Android App can override the
public HTTPS endpoint without changing credentials. A stable endpoint remains
a separate deployment requirement. See ../../android/relay/README.md.

## Controls and current scope

- Version 2.5-power-restart adds locally read battery percentage and a charging
  marker in the calendar home header, preserving the date/time. The percentage
  is the board driver's voltage estimate, not a fuel-gauge measurement. Missing
  readings show `--`; no-battery/fault states are marked rather than inventing a
  full charge. It works without phone connectivity and does not fetch a screen.
- Hold UP for five seconds to restart. Local pending audio, capturing, sending,
  or an unconfirmed task selection blocks restart with a visible explanation.
  Normal short UP paging and recording cancellation keep their existing controls.
  NVS pairing, device identity and server-side tasks survive restart; existing
  audio-loss safeguards remain in force.

- Hold the front button to record (LED on); release to upload and submit.
- Two short front presses within 350 ms request cancellation. Release starts an
  800 ms upload grace period; a recording release is not itself a short click.
  The server only confirms cancellation before AI starts; later requests get a
  clear "too late" screen and do not roll back completed operations.
- Press UP during recording to cancel. Maximum recording duration: 60 seconds.
- Short front press switches between transcript and reply immediately when no
  send is pending. The double-click window is retained while a send is pending.
- Ten seconds without input shows the calendar and real processing tasks.
  Recording and pairing retain their dedicated screens. A side key first wakes
  the previous content; hold-to-record works from standby. New answers wake the
  reply. This is a display mode, not MCU deep sleep.
- BLE is the default transport. Wi-Fi is never initialized in this mode. Phone
  pairing and credentials survive application-only firmware updates in NVS.
- Hold DOWN for 0.8 seconds to open the task selector; UP/DOWN choose, front
  confirms, and another long DOWN returns. A task is one conversation, with
  ordered follow-up recordings. Different tasks may run concurrently (four
  maximum); the microphone becomes available after upload acknowledgement.
- Task selection is captured at recording start. A selection change during
  recording cannot redirect that recording. Selecting a different task clears
  unrelated cached pages; deleted-task URLs return 404 and cannot revive jobs.
- The first eight task choices are warmed in bounded idle passes. Cached menu
  movements and reply pages use the local display queue, with immediate key LED
  feedback. A confirmed selection reuses its verified title bitmap for the home
  screen; selection itself still needs a successful server response. Uncached
  choices require connectivity. Rapid inputs keep only the latest queued frame.
- Task confirmation shows a waiting screen. Disconnected confirmation does not
  lock the menu or silently queue a change. A BLE selection response waits at
  most 12 seconds; failures show a retry message, unlock selection and restore
  the cached menu. Successful selection still requires server acknowledgement.
- UP / DOWN change pages directly from a bounded 64-page PSRAM cache. Nearby
  pages are fetched in verified batches of up to eight; uncached pages need
  connectivity. One in-flight voice request per device.
- Version 2.4-report-reader opens the actual saved report body for document
  tasks. Tables become labeled vertical text, with every value retained.
  The first report page is fetched separately; subsequent pages use batches.
  A changed answer revision clears stale reply pages without clearing transcript
  pages. Large page batches have a 90-second response budget; cached key actions
  remain local. Selecting an existing task opens its latest readable reply.
- Input uses one complete transcription after release. Incremental preview was
  withdrawn at the user's request.
- Monochrome transition refresh follows the first full refresh, with a full
  refresh every eight updates. The panel still needs about one second to update.
- Audio lives in PSRAM until accepted. Pending turn identity survives reset in
  NVS, but unuploaded audio does not survive power loss.
- Firmware 2.3.2 requires Android relay 1.2.1. Metadata and audio streams wait
  for verified phone receipts every eight fragments or at stream end. Receipt
  waits are bounded to eight seconds, and connection loss wakes both response
  and receipt waits. Stream pacing always yields at least one RTOS tick.
  Retries retain the original UUID. A final confirmed missing turn displays
  "recording not delivered" instead of directing the user to an absent web chat.
- Certificate and hostname verification remain enabled in the phone HTTPS
  client. The authenticated phone supplies board time. Legacy Wi-Fi uses SNTP
  and ESP-IDF's verified cross-signed certificate bundle.
- This first version keeps the MCU awake; measured battery optimization and
  safe OTA partitions are later work. USB remains the recovery/update path.

Factory restore is a separate, explicit recovery operation using this device's
verified full backup at address 0x0; never use another device's private backup.
