# Screen Shield

Screen Shield is a local Python desktop app that uses a webcam to notice a second person facing your screen and covers the primary monitor with a frozen, strongly blurred screenshot until the viewer leaves. It runs entirely on your computer, offers enable/disable and snooze controls, and releases the webcam when disabled. Windows and Linux have a tray menu; macOS uses the global hotkey and console because Tkinter owns its main thread. This is a privacy aid, not a security boundary or a guarantee that nobody can see your screen.

## Policies, support, and acceptance (1.0.2)

The public Documentation link is replaced with `privacy.html` and `terms.html`.
Canonical text is in `policies.json`, also bundled into the app. The public
contact address is **screenshield.riverboat836@silomails.com**, a forwarding
alias; the private destination address is not included. Support email goes
through email providers and is not end-to-end encrypted merely by forwarding.

The page identifies Screen Shield as an independent student-built preview and
includes Apple's per-app trust steps: attempt opening, choose Done on the
notarization warning, then System Settings → Privacy & Security → Open Anyway,
authenticate, and confirm Open if the source is trusted. Never disable Gatekeeper
globally. These instructions do not remove the need for notarization for smooth
public distribution and do not treat other malware/damage alerts as equivalent.

Every normal download link opens a two-document review dialog until the current
policies have been accepted. Users must scroll through both, acknowledge the
Privacy Policy and separately agree to Terms; boxes are unchecked by default.
Cancel does not download. A version/hash/timestamp record is kept in browser
local storage if available, or only in memory for that page session.

The app independently requires the same review/acceptance on first run, before
starting camera, capture, hotkey, or tray services. It remembers document version,
SHA-256, timestamp, and both acknowledgments in `consent.json` under the user's
Screen Shield application-data folder. Changed documents or a missing/corrupt
record require renewed acceptance. Decline or closing the review quits. The
acceptance record is not uploaded. These are normal-user consent flows, not DRM;
they cannot verify comprehension or stop someone modifying open-source code.

Technical README documentation remains for developers. The policies describe
actual behavior and preserve MIT/third-party license rights; no legal-enforceability
or regulatory-compliance guarantee is claimed. The installed app's acceptance
has not been selected on the user's behalf during tests.

## Download page

Open `index.html` in Chrome or your default browser to read about Screen Shield,
download version 1.0.1, and see the installation steps. The download button
saves the DMG to Downloads or the location selected in the browser; users then
drag Screen Shield into Applications. A website cannot silently install it into
Applications. Keep `index.html` with the `dist/` installers, `README.md`, and
`LICENSE` so its relative links work. It contains no external scripts, fonts,
analytics, or camera access.

The page is local, not publicly hosted. To publish it later, host the page and
linked distribution files together. The preview installer still needs Developer
ID signing and notarization for a smooth public Mac download. Old 1.0.0
installers are retained under `dist/previous-1.0.0/` and are not linked by the page.

## Mac app installer (Apple Silicon preview)

**1.0.3 policy-screen fix:** the document reader now resizes inside a grid with a fixed footer. The **Continue to Screen Shield** button stays visible, enables only after both reviews and acknowledgments, and opens the normal app controls. Enter also continues when ready. Existing consent remains valid because policy text is unchanged.

**1.0.1 startup fix:** macOS 26 rejects pynput keyboard-layout queries from a
background thread. The Mac listener now captures layout data on the main Tk
thread before starting its background event tap. This fixes the
`TSMGetInputSourceProperty` / `dispatch_assert_queue` / SIGTRAP startup crash;
detection rules are unchanged. Use the 1.0.2 installer and replace the older app.


A self-contained **Screen Shield.app** and drag-to-Applications disk image are
prepared in `dist/`. This build is for **Apple Silicon (M1 or later), macOS 15
or later**; it is not an Intel or Windows build. Python, runtime libraries, and
the Face Landmarker model are bundled. No Terminal, pip, or separate model
installation is needed to use the app.

1. Download `Screen-Shield-1.0.3-macOS-arm64.dmg` in Chrome or another browser,
   or open the local disk image from this project's `dist` folder.
2. Open the disk image and drag **Screen Shield** into **Applications**.
3. Eject the disk image and launch **Screen Shield** from Applications.
4. Allow Camera and Screen Recording for Screen Shield. Allow Accessibility /
   Input Monitoring if you want the global shortcut, then relaunch if needed.
5. The control window provides **Enable/Disable**, **Snooze 5 min**,
   **Permissions…**, and **Quit**. Closing the window or Command+Q also quits.
   Escape/three clicks on the cover remains the emergency snooze.

The Mac control window is also available when running `python main.py` from
source. There is still no macOS tray icon. The desktop app keeps preferences at
`~/Library/Application Support/Screen Shield/config.json` and resolves the
bundled model relative to its current app location, so moving the app does not
break its default model. The model itself is read from the bundle, not copied
into recordings or uploaded anywhere. Source/CLI runs still use the project's
`config.json`; explicit `--config` continues to work.

**Signing status:** this is a locally built, ad-hoc-signed preview. No Developer
ID signing identity was available on the build Mac, so it has **not been Apple
notarized**. A local build may open normally, but Chrome/internet downloads on
another Mac can be blocked by Gatekeeper. Do not market this build as Apple
verified or a friction-free public release. Public distribution should first be
signed with a Developer ID Application certificate, notarized with Apple's
notary service, and stapled. Do not disable Gatekeeper globally. See
[Apple's distribution guidance](https://developer.apple.com/macos/distribution/).

A disk image and ZIP are distribution artifacts, not website hosting. A public
Chrome download URL requires uploading the signed release to a download host.
The build has not been published to Netlify or any other service.

The bundle includes the MIT project license and dependency license notices in
`Contents/Resources/third-party-licenses`. Native app startup/quit and bundled
model initialization are tested without reading a real webcam. Actual viewer
accuracy, permissions, and the screen cover still need hands-on validation.

## How it works

1. **Owner:** score face area with a small center bonus. Keep the previous owner when position, overlap, and size still match; other faces are candidate viewers. This is geometric tracking, not identity recognition.
2. **Distance:** ignore candidate landmark boxes smaller than `min_face_width_ratio` of frame width.
3. **Head pose:** accept only candidates within yaw/pitch limits, using the facial transformation matrix or a six-point solvePnP fallback. Head direction estimates orientation, not eye gaze.
4. **Time:** trigger after continuous qualifying presence; clear after known absence. Timing uses monotonic seconds live and `frame_index / FPS` for recorded clips.
5. **Unknown frames:** no-face/dark/unreadable frames cannot trigger or clear the state. They hold state, break pending presence, and pause clearing. A dead feed also holds state while the app retries.

The cover captures once with mss **before its window appears**, blurs in memory, and stays frozen until hidden. Capture failure produces a solid black cover. Tk runs on the main thread; camera/model, hotkey, and supported tray services use separate threads with queued control/status events. Capture is requested at 640×480; inference is capped at 10 FPS by default. No camera reads or inference occur while snoozed (an in-flight call can finish). Snooze keeps an already-open camera allocated; disabling closes it and destroys detector state. Re-enabling starts fresh.

## Install and run

Use **Python 3.11** and a local desktop session. Keep the virtual environment and test footage outside this directory. The application needs OpenCV, MediaPipe Face Landmarker, mss, Pillow, pystray, pynput, and Python's Tkinter. All runtime pip dependencies are already listed in `requirements.txt`.

### macOS

Install Python 3.11 with Tk support, open Terminal, then:

```sh
cd /Users/kaanand/Documents/ChatGPT/Code/Screen-Shield
python3.11 -m venv ../screen-shield-venv
source ../screen-shield-venv/bin/activate
python -m pip install -r requirements.txt
```

### Windows (Command Prompt)

Install Python 3.11 including Tcl/Tk, open Command Prompt, and change into this project:

```bat
cd C:\path\to\Screen-Shield
py -3.11 -m venv ..\screen-shield-venv
..\screen-shield-venv\Scripts\activate
python -m pip install -r requirements.txt
```

### Linux

Use a Python 3.11 installation with its matching Tk package (often supplied by the distribution as `python3-tk` or a versioned equivalent). Use an X11 desktop for mss/pynput; native Wayland can restrict screen capture and global keys. Tray menus require a supported desktop/backend, such as an existing AppIndicator-capable environment.

```sh
cd /path/to/Screen-Shield
python3.11 -m venv ../screen-shield-venv
source ../screen-shield-venv/bin/activate
python -m pip install -r requirements.txt
```

### Model setup (all OSes, once)

Run this from the activated environment. Dependencies and Google's model weights need internet **during setup only**. Runtime never downloads the model automatically.

```sh
python -c "from pathlib import Path; from urllib.request import urlretrieve; p=Path.home()/'.screen-shield'/'face_landmarker.task'; p.parent.mkdir(parents=True, exist_ok=True); urlretrieve('https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task', str(p))"
python -m tkinter
python main.py
```

`python -m tkinter` opens a small Tk check window; close it before launching. If setup reports a certificate error, repair that Python installation's certificate store or download the same official model with your browser to the configured local path. Do not disable TLS verification.

Use `python main.py --config /absolute/path/config.json` for an external configuration. Missing/corrupt/non-object configuration files are recreated with defaults when writable. Missing keys, wrong types, and out-of-range values warn and use defaults in memory; individually invalid entries are not automatically written back. If writing is denied, the app continues with in-memory defaults and a warning. Unknown keys warn and are ignored. Configuration recovery does not supply a missing model; finish model setup first.

## Permissions and platform behavior

- **macOS:** allow Camera, Screen Recording (or Screen & System Audio Recording), Accessibility, and where requested Input Monitoring for the Terminal/Python launcher. Restart that launcher after changing permissions. The tray is deliberately skipped: pystray and Tkinter both require the main thread on macOS. Use the hotkey and Ctrl+C instead.
- **Windows:** allow camera access for desktop apps in Privacy & security → Camera. Close other camera users if the device is exclusive.
- **Linux:** permit access to the webcam device, usually `/dev/video0`, and the graphical session. Some Wayland environments block capture/key hooks; fallback black covers and unavailable-service warnings are expected in unsupported environments.

A local development test under a restricted macOS command sandbox aborted inside MediaPipe's `DrishtiMetalHelper`/`TensorsToDetectionsCalculator` with `SIGABRT`; the same generated-frame test passed outside that sandbox. Dismiss a leftover test crash dialog with **Ignore/OK** and launch from ordinary Terminal. No administrator privileges are required. Python cannot catch this native abort with `try/except`. If ordinary Terminal also crashes, stop retrying and inspect the newest Python report in `~/Library/Logs/DiagnosticReports/` before diagnosing that installed Python/MediaPipe/macOS combination. Do not disable crash reporting.

## Controls

| Action | Control |
| --- | --- |
| Enable/disable | **Ctrl+Alt+S** globally (Alt is Option on macOS), or the Windows/Linux tray Enable/Disable item. |
| Tray status | Green shield means detection requested; gray means disabled/transitioning. ENABLED includes camera-retry periods, so check outage logs. |
| Snooze cover and processing | Escape while the cover is focused, or click it three times: `snooze_seconds` (10 s). Windows/Linux tray **Snooze 5 min**: 300 s. |
| Quit | Tray **Quit** on Windows/Linux, or **Ctrl+C in the launching terminal** on every OS. On macOS, the control window also has Quit and supports Command+Q. |
| Detector-only diagnostic | `python detector.py`; `q` in its terminal or Ctrl+C exits. This diagnostic exits on repeated camera failures; the desktop launcher retries. |

Snooze preserves the current detection state and an already-open camera, but skips camera reads and model inference. It also postpones reopen attempts while the worker is paused. The cover can return at expiry if INTRUDER or fail-closed outage persists. Disabled always hides the cover, releases the webcam completely, and resets SAFE. Snooze survives a toggle and never enables a disabled app. Toggles during start/stop transitions are ignored; queued old-run events cannot affect a new detector. The app joins owned threads on shutdown.

Change `hotkey`, for example to `ctrl+shift+s`, and restart. Aliases include control/ctrl, option/alt, command/cmd, and super/cmd; `<ctrl>+<alt>+s` and named keys such as `f8` are accepted. Invalid syntax/duplicate keys log an error and use Ctrl+Alt+S. Unavailable key or tray services are reported to console; Ctrl+C remains available.

## Camera loss and fail-safe policy

Camera-open failure or the first unreadable/dead frame releases the capture handle and starts retries every `camera_retry_seconds` (5 s). The console explains likely causes: another app, denied permission, disconnected device, or wrong index. A supported tray may show one notification per outage. Retry/pacing waits are interruptible by disable/quit. The existing detector stays alive during a camera outage so INTRUDER is not accidentally reset to SAFE. Recovery is announced only after a valid frame is read, then normal detection resumes.

- **`fail_closed: false` (default):** keep the detection state unchanged during the outage. A previously active cover stays up; a SAFE state does not invent a viewer or force a cover.
- **`fail_closed: true`:** additionally force the cover during a camera outage while enabled, even when the held detection state is SAFE. Remove that forced condition after a valid frame returns. Escape/three clicks, tray snooze, and disabling still dismiss it. This favors covering sensitive content but can interrupt work when a camera is busy or disconnected.

This policy does not change face detection and does not force a cover merely because a valid feed has no faces or poor lighting. Model/dependency errors are logged and disable detection; they are not retried as camera failures. Native drivers can block inside an open/read call; such OS-level stalls cannot be cancelled safely by Python threads, so shutdown timing needs live hardware validation.

## Configuration reference

Defaults are shared by all entry points in `detector.DEFAULT_CONFIG`. Timings are seconds. Numeric fractions are 0–1 unless stated otherwise; positive time settings accept 0.001–86400. Booleans must be JSON `true`/`false`, not strings. Integer settings reject decimal/string values. Edit and restart to apply changes.

| Key | Default | Purpose |
| --- | --- | --- |
| `model_path` | `~/.screen-shield/face_landmarker.task` | Local Face Landmarker model. `~` expands to home; relative paths use the config folder. |
| `camera_index` | `0` | OpenCV webcam device index (integer 0–1024). |
| `camera_width` | `640` | Requested capture width (1–16384); the driver may adjust it. |
| `camera_height` | `480` | Requested capture height (1–16384); the driver may adjust it. |
| `camera_fps` | `10` | Requested camera FPS (0.1–240), independent of processing cap. |
| `process_fps` | `10` | Live inference cap (0.1–120 FPS); no throttling in offline evaluation. |
| `camera_retry_seconds` | `5.0` | Wait after a camera failure before retrying; stop/disable interrupts this wait. |
| `max_faces` | `4` | MediaPipe face limit (integer 2–4). |
| `min_face_detection_confidence` | `0.65` | Detection confidence threshold (0–1). Higher may miss more faces. |
| `min_face_presence_confidence` | `0.65` | Landmark presence threshold (0–1). |
| `min_tracking_confidence` | `0.65` | Tracking confidence threshold (0–1). |
| `owner_centrality_weight` | `0.1` | Central owner area bonus (0–1); 0 uses largest area only. |
| `owner_max_center_distance` | `0.12` | Maximum normalized center movement for retaining the owner (0–1). |
| `owner_min_iou` | `0.15` | Minimum box intersection/union for owner matching (0–1). |
| `owner_min_size_ratio` | `0.5` | Minimum width/height similarity for owner matching (0–1). |
| `owner_memory_seconds` | `2.0` | Maximum time to remember an unmatched owner position. |
| `min_face_width_ratio` | `0.12` | Reject candidate landmark boxes narrower than this fraction of frame width (0–1). |
| `max_yaw_deg` | `35` | Maximum absolute candidate yaw (0–90 degrees). |
| `max_pitch_deg` | `30` | Maximum absolute candidate pitch (0–90 degrees). |
| `pnp_focal_length_ratio` | `1.0` | Fallback camera focal length / frame width (0.01–100). |
| `pnp_max_reprojection_error_ratio` | `0.03` | Maximum fallback reprojection error / frame width (0–1). |
| `min_frame_brightness` | `35.0` | Minimum mean frame grayscale intensity (0–255); lower is unknown. |
| `min_face_brightness` | `40.0` | Minimum candidate box mean grayscale intensity (0–255). |
| `trigger_seconds` | `0.7` | Continuous viewer evidence needed for INTRUDER. |
| `clear_seconds` | `2.0` | Known absence needed to return SAFE; unknown observations pause clearing. |
| `max_frame_gap_seconds` | `0.5` | Larger sampling gaps break pending presence and do not advance clearing. |
| `max_camera_failures` | `20` | Only for standalone `detector.py`: failed reads before diagnostic exit. The desktop app retries immediately on feed loss. |
| `debug` | `false` | Per-frame numeric diagnostics. Evaluator `--debug` overrides this for failing pairs only. |
| `blur_radius` | `40` | Gaussian blur radius in displayed pixels (0.1–1000). |
| `snooze_seconds` | `10` | Escape/three-click snooze duration. Tray snooze is always five minutes. |
| `show_message` | `true` | Show the centered warning; the failsafe hint remains visible. |
| `hotkey` | `ctrl+alt+s` | Global enable/disable shortcut. Invalid syntax falls back to Ctrl+Alt+S. |
| `start_enabled` | `true` | Start with detection requested. False keeps the camera unopened until enabled. |
| `fail_closed` | `false` | Show the cover during a camera outage while enabled; snooze/disable can dismiss it. |

Very low `process_fps` can exceed `max_frame_gap_seconds` and prevent sustained-presence evidence. Requested capture modes depend on the driver. Reducing capture resolution can change face quality; re-evaluate sensitivity on the deployed setup. No user-tuned numbers were supplied at release prep, so width/yaw/pitch and trigger/clear defaults remain unchanged.

## Evaluation

Record webcam clips with another camera app, then close it before live use. Keep clips outside the repository, for example `~/ScreenShield-clips`. Use the deployed camera position, resolution, lighting, and preferably constant-frame-rate video. Name negatives `alone_far_01.mp4` (must never trigger) and positives `intruder_behind_02.mp4` (must trigger at least once). Include angled/distant passersby, movement, occlusion, and changing light. Let positives contain sustained viewer presence beyond the trigger delay. Scenarios preserve the text after the prefix but strip a final numeric take suffix: `intruder_person_behind_02.mp4` → `person_behind`. Files directly in the folder with .mp4/.mov/.avi/.mkv extensions are evaluated, including uppercase variants; other prefixes warn and are skipped. Subfolders are not scanned.

```sh
python evaluate.py ~/ScreenShield-clips
python evaluate.py ~/ScreenShield-clips --sweep
python evaluate.py ~/ScreenShield-clips --debug
```

On Windows, use a quoted local folder path. Each clip has a fresh detector and in-memory config. The clock is `frame_index / video_fps`, not processing wall time. Variable-frame-rate video uses this approximation; constant-FPS footage gives more reliable timing. Offline evaluation ignores live `process_fps`, snooze, enable/disable, camera retry, and fail-closed UI policy. It does not open the webcam, Tk, tray, or global key hooks.

The table reports PASS, FALSE POSITIVE, MISS, or ERROR. Time means first false trigger for an alone clip or detection latency **from clip start** for an intruder clip, not from viewer arrival. Average latency includes detected intruder clips only. Summary rates use the appropriate class denominator, and absent classes show n/a. Scenario rows show class-aware counts/rates. Unknown frame percentage is weighted across decoded frames; no-face is a subset of unknown, separate from the held SAFE/INTRUDER state.

Unreadable/empty clips, invalid FPS, and early EOF relative to the decoder's reported frame count are ERRORs excluded from scoring; a decodable all-unknown intruder clip is still a MISS. Exit codes: 0 for completed measurements (including FP/misses), 1 for input/processing errors, 130 for cancellation.

`--sweep` tests widths 0.08/0.10/0.12/0.15/0.18 × yaw limits 25/30/35/45. It runs inference once per clip and caches **only numeric face observations in memory**, replaying identical owner/filter/timing logic with fresh state per combination. Only one clip cache is retained at a time. Rank by fewest false positives, then misses, with printed grid order breaking exact ties. It never changes config automatically. Use held-out clips before adopting settings. `--debug` prints each frame's candidate diagnostics only for failing clip/config pairs; combining it with `--sweep` can produce substantial console output.

Programmatic use: `Detector(config, overrides={"max_yaw_deg": 30})` creates private config and fresh state. Pass clip seconds to `process_frame(frame, timestamp=...)`, and call `close()` in `finally`. `load_model=False` is for numeric `process_observation` replay only. Changing model, lighting, or owner-score settings requires fresh inference.

### Evaluation results — fill with your measurements

No real labeled-clip scores or tuned values were supplied. Synthetic/mock checks are not accuracy measurements.

| Dataset/run | Alone clips | Intruder clips | False positives | Misses | Accuracy | FP rate | Detection rate | Mean latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline | [count] | [count] | [FP] | [misses] | [accuracy] | [rate] | [rate] | [seconds] |
| Tuned / held-out | [count] | [count] | [FP] | [misses] | [accuracy] | [rate] | [rate] | [seconds] |

| Tuned setting | Value |
| --- | --- |
| min_face_width_ratio | [not supplied; 0.12 retained] |
| max_yaw_deg | [not supplied; 35 retained] |
| max_pitch_deg | [not supplied; 30 retained] |
| trigger_seconds | [not supplied; 0.7 retained] |
| clear_seconds | [not supplied; 2.0 retained] |

## Limitations

- Cannot see someone outside the webcam view, a phone camera pointed at the screen, or software screenshots/screen recording.
- Reflections, photos, displays showing faces, partial occlusion, and very low light can confuse the model; unknown intervals intentionally hold state.
- A closer/more central person, overlapping/crossing faces, or a lone non-owner can confuse the geometric owner heuristic. No identity or eye-gaze recognition is performed.
- Maximum four faces. The landmark box is smaller than a full head box, and solvePnP uses an approximate face/camera model.
- Coverage is the primary monitor, with placement/scaling dependent on the window manager. OS secure screens and other macOS fullscreen Spaces may sit outside the cover.
- The cover freezes a screenshot; it does not lock the OS or prevent capture by other software. A busy/suspended process, blocked driver, denied permission, or native library crash can interrupt protection.
- Windows/Linux tray, device permissions, native hotkey delivery, camera-disconnect behavior, mixed-DPI coverage, and packaged builds need validation on the target machine. No cross-platform live accuracy is claimed.

## Privacy and repository hygiene

Inference and controls are fully local. Runtime has no analytics/upload/network client. Webcam frames and screenshots are held in memory only and are never saved or transmitted by Screen Shield. The overlay image is released when hidden; Python/OS memory handling does not guarantee secure erasure. Disabling stops the detector and fully releases the webcam; snoozing skips processing but may keep the camera light on because the handle is still allocated. The global listener recognizes a shortcut without recording/transmitting keystrokes. Logs go to console at INFO level with warnings/errors and optional numeric face diagnostics; no frames/images are logged or saved, and no file logger is installed.

`.gitignore` excludes video extensions, `clips/`, bytecode, `.venv/`, logs, `dist/`, `build/`, and PyInstaller specs. Keep footage outside the repository anyway. Ignore rules do not untrack existing commits or override `git add -f`; inspect `git status` before publishing. The project includes the application files, `index.html`, `.gitignore`, and `LICENSE`; distribution artifacts are under the ignored `dist/` folder. No Git repository was initialized or published during prep.

## Optional PyInstaller packaging (not a runtime dependency)

If you already have PyInstaller in a **separate build environment**, first test a console build on the target OS. It is not added to `requirements.txt` and no installer is required for normal use. PyInstaller builds are OS-specific. The prepared Apple Silicon build uses a windowed `.app` plus a visible control window, bundled model, icon, permission descriptions, and external preferences; the generic commands below do not reproduce all of that configuration.

```sh
pyinstaller --onefile main.py
```

Keep config and model external and pass `--config` to a writable config file. For example, on macOS/Linux from the project directory: `dist/main --config "$PWD/config.json"`. On Windows: `dist\main.exe --config "%CD%\config.json"`. Do not rely on a frozen program's temporary `__file__` directory for persistent config. MediaPipe/Tk/native backends may require additional collection/build adjustments; diagnose with the console build.

On Windows/Linux **after verifying a working tray Quit**, the requested optional console-free build is:

```sh
pyinstaller --onefile --noconsole main.py
```

On macOS a console build remains useful for diagnosing errors. The prepared windowed bundle now has a control window with Quit, so it does not rely on Terminal or a tray icon to exit. A console-free build also hides permission/camera warnings and startup config recovery messages. Test enabled/disabled/snoozed quit and permission failures before distributing any build.

## License

MIT; see `LICENSE`. Model weights and third-party dependencies have their own licenses.
