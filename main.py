"""Screen Shield release: Tk UI, restartable detector, tray and hotkey."""
import argparse
import logging
import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageDraw, ImageFont, ImageTk

from detector import DEFAULT_CONFIG, Detector, load_config
from overlay import Overlay


DEFAULT_HOTKEY = "ctrl+alt+s"


def parse_hotkey(text, keyboard):
    """Translate friendly key names and validate with pynput's own parser."""
    aliases = {"control": "ctrl", "option": "alt", "command": "cmd", "super": "cmd"}

    def parse(value):
        if not isinstance(value, str):
            raise ValueError("expected a string such as ctrl+alt+s")
        parts = [part.strip().lower() for part in value.split("+")]
        if not parts or any(not part for part in parts):
            raise ValueError("empty key in shortcut")
        for index, part in enumerate(parts):
            if "<" in part or ">" in part:
                if not (part.startswith("<") and part.endswith(">")
                        and "<" not in part[1:] and ">" not in part[:-1]):
                    raise ValueError("malformed key brackets")
                parts[index] = part[1:-1]
        parts = [aliases.get(part, part) for part in parts]
        formatted = "+".join(part if len(part) == 1 else f"<{part}>" for part in parts)
        keys = keyboard.HotKey.parse(formatted)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key in shortcut")
        return formatted

    try:
        return parse(text)
    except (TypeError, ValueError, KeyError) as exc:
        logging.error("Invalid hotkey %r: %s. Falling back to %s", text, exc, DEFAULT_HOTKEY)
        return parse(DEFAULT_HOTKEY)


def shield_image(enabled):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.polygon([(32, 3), (57, 13), (53, 39), (44, 52), (32, 61),
                  (20, 52), (11, 39), (7, 13)],
                 fill="#26a85b" if enabled else "#80858b", outline="white", width=3)
    return image


def detector_loop(config, stop, events, generation, paused=None,
                  preview_requested=None, preview_frames=None):
    """Own camera/model here, retry dead feeds, and never call Tk."""
    camera = detector = None
    paused = paused if paused is not None else threading.Event()

    def send(kind, value=None):
        events.put(("worker", (generation, kind, value)))

    def release_camera():
        nonlocal camera
        if camera is not None:
            try:
                camera.release()
            finally:
                camera = None

    def lost(message):
        # Unknown holds hysteresis, breaks pending presence, and cannot clear.
        detector.process_frame(None)
        send("camera_lost", message)

    try:
        if stop.is_set():
            return
        # ENABLED means detection is requested, including camera-retry periods.
        send("ready")
        previous_state, was_paused = "SAFE", False
        camera_lost = True
        interval = 1 / config["process_fps"]
        while not stop.is_set():
            if paused.is_set():
                was_paused = True
                stop.wait(0.1)
                continue
            started = time.monotonic()
            if detector is None:
                detector = Detector(config)
                cv2 = detector.cv2
                if stop.is_set():
                    break
                if paused.is_set():
                    was_paused = True
                    continue
            if was_paused:
                # Resume with an unknown observation so neither timer can
                # count unobserved snooze time. No inference occurred paused.
                detector.process_frame(None)
                was_paused = False
            if camera is None:
                try:
                    camera = cv2.VideoCapture(config["camera_index"])
                    if not camera.isOpened():
                        raise RuntimeError("device unavailable, in use, or camera permission denied")
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, config["camera_width"])
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, config["camera_height"])
                    camera.set(cv2.CAP_PROP_FPS, config["camera_fps"])
                except Exception as exc:
                    release_camera()
                    lost(f"Cannot open camera {config['camera_index']}: {exc}. "
                         f"Retrying in {config['camera_retry_seconds']:g}s; check device, permissions, and other camera apps.")
                    camera_lost = True
                    stop.wait(config["camera_retry_seconds"])
                    continue
            try:
                ok, frame = camera.read()
            except Exception as exc:
                ok, frame = False, None
                logging.warning("Camera read failed: %s", exc)
            if stop.is_set():
                break
            if paused.is_set():
                del frame
                was_paused = True
                continue
            if not ok or frame is None or not frame.size:
                release_camera()
                lost(f"Camera feed lost; holding detection state as unknown. "
                     f"Retrying in {config['camera_retry_seconds']:g}s.")
                camera_lost = True
                stop.wait(config["camera_retry_seconds"])
                continue
            if camera_lost:
                send("camera_restored")
                camera_lost = False
            state = detector.process_frame(frame)
            if state != previous_state:
                send("state", state)
                previous_state = state
            if preview_requested is not None and preview_requested.is_set() and preview_frames is not None:
                # One shared camera/model. Only the newest requested frame is
                # retained; Tk objects are created exclusively by the UI thread.
                packet = (generation, frame.copy(), detector.last_detection)
                try:
                    preview_frames.put_nowait(packet)
                except queue.Full:
                    try:
                        preview_frames.get_nowait()
                    except queue.Empty:
                        pass
                    preview_frames.put_nowait(packet)
                del packet
            del frame
            # Inference time counts toward the interval, avoiding extra delay
            # on slow hardware. Stop interrupts all pacing/retry sleeps.
            stop.wait(max(0, interval - (time.monotonic() - started)))
    except Exception as exc:
        send("error", str(exc))
    finally:
        try:
            release_camera()
        except Exception as exc:
            send("error", f"Camera cleanup failed: {exc}")
        finally:
            try:
                if detector is not None:
                    detector.close()
            except Exception as exc:
                send("error", f"Detector cleanup failed: {exc}")
            finally:
                send("stopped")


def model_face_annotation(face, detection):
    """Display existing decisions; do not add another set of detection rules."""
    if face.index == detection.owner_index:
        return "#26ce75", "Owner (estimate)"
    if face.index in detection.viewer_indices:
        return "#f4515c", "Viewer candidate"
    return "#f4c344", "Other face"


class ModelView:
    """Opt-in camera display, not a security cover. Tk main thread only."""
    def __init__(self, root, on_close, resume_enabled):
        self.window = tk.Toplevel(root)
        self.window.title("Screen Shield — Model view · cover OFF")
        self.window.geometry("860x700")
        self.window.minsize(640, 520)
        self.window.configure(background="#101b28")
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(2, weight=1)
        tk.Label(self.window, text="MODEL VIEW — PRIVACY COVER OFF", font=("Helvetica", 18, "bold"),
                 background="#101b28", foreground="#f4c344").grid(row=0, column=0, pady=(18, 6), padx=16)
        tk.Label(self.window, text="Green: owner estimate   ·   Yellow: other face   ·   Red: viewer candidate",
                 font=("Helvetica", 12), wraplength=600, background="#101b28", foreground="white").grid(row=1, column=0, padx=16, pady=(0, 12))
        self.canvas = tk.Canvas(self.window, background="black", highlightthickness=0, width=640, height=400)
        self.canvas.grid(row=2, column=0, sticky="nsew", padx=16)
        self.image_id = self.canvas.create_image(0, 0, anchor="center")
        self.message_id = self.canvas.create_text(320, 200, text="Waiting for the camera…", fill="white", font=("Helvetica", 15), width=570)
        self.status = tk.StringVar(value="This window never triggers a real screen cover.")
        tk.Label(self.window, textvariable=self.status, font=("Helvetica", 12), wraplength=600,
                 background="#101b28", foreground="#d8e3ec").grid(row=3, column=0, pady=10, padx=16)
        tk.Label(self.window, text="Colors show face/angle rules, not identity, eye gaze, or intent. No video is recorded.",
                 font=("Helvetica", 11), wraplength=600, background="#101b28", foreground="#a9bdcd").grid(row=4, column=0, padx=16)
        label = "Close & resume protection" if resume_enabled else "Close & release camera"
        tk.Button(self.window, text=label, command=on_close).grid(row=5, column=0, pady=(10, 18))
        self.window.protocol("WM_DELETE_WINDOW", on_close)
        self._photo = None
        self.font = ImageFont.load_default(size=15)

    def clear(self, message):
        self.canvas.itemconfigure(self.image_id, image="")
        self._photo = None
        self.canvas.itemconfigure(self.message_id, text=message)
        self.canvas.coords(self.message_id, max(1, self.canvas.winfo_width()) / 2,
                           max(1, self.canvas.winfo_height()) / 2)
        self.status.set(message + " Privacy cover remains OFF in model view.")

    def render(self, frame, detection):
        image = Image.fromarray(frame[:, :, ::-1])
        try:
            draw = ImageDraw.Draw(image)
            for face in detection.faces:
                color, label = model_face_annotation(face, detection)
                x1, y1, x2, y2 = face.box
                draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
                details = (f" yaw {face.yaw:+.0f}° pitch {face.pitch:+.0f}°"
                           if face.yaw is not None and face.pitch is not None else " pose unknown")
                text = label + details
                bounds = draw.textbbox((0, 0), text, font=self.font)
                tw, th = bounds[2] - bounds[0] + 10, bounds[3] - bounds[1] + 10
                left = max(0, min(x1, image.width - tw))
                top = max(0, y1 - th)
                draw.rectangle((left, top, left + tw, top + th), fill=color)
                draw.text((left + 5, top + 3 - bounds[1]), text, font=self.font, fill="#101b28")
            width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(image, master=self.window)
            self.canvas.coords(self.image_id, width / 2, height / 2)
            self.canvas.itemconfigure(self.image_id, image=self._photo)
            self.canvas.itemconfigure(self.message_id, text="")
            quality = ("Unknown: " + detection.reason.replace("_", " ")) if detection.viewer_present is None else f"{len(detection.faces)} faces · {len(detection.viewer_indices)} viewer candidates"
            owner_status = "Primary user present" if detection.owner_index is not None else "No primary user in position"
            self.status.set(f"{quality} · {owner_status} · Detector state: {detection.state} · Cover OFF")
        finally:
            image.close()

    def close(self):
        self.canvas.itemconfigure(self.image_id, image="")
        self._photo = None
        self.window.destroy()


class ScreenShield:
    """Main-thread lifecycle owner. Background callbacks only enqueue events."""
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.events = queue.Queue()
        self.overlay = Overlay(root, config)
        self.phase = "disabled"  # disabled -> starting -> enabled -> stopping
        self.state = "SAFE"
        self.generation = 0
        self.worker = None
        self.worker_stop = None
        self.paused = threading.Event()
        self.feed_lost = False
        self.accept_toggle_after = 0.0
        self.quitting = threading.Event()
        self.listener = None
        self.listener_failed = False
        self.tray = None
        self.tray_thread = None
        self.tray_ready = False
        self.exit_code = 0
        self.images = {True: shield_image(True), False: shield_image(False)}
        self.panel_status = None
        self.notice_text = ""
        self.model_view = None
        self.model_view_restore_disabled = False
        self.preview_requested = threading.Event()
        self.preview_frames = queue.Queue(maxsize=1)

    @property
    def enabled(self):
        return self.phase == "enabled"

    def request_toggle(self):
        self.events.put(("toggle", time.monotonic()))

    def request_quit(self, signum=None, frame=None):
        self.quitting.set()
        self.events.put(("quit", None))

    def start(self):
        self.root.protocol("WM_DELETE_WINDOW", self.request_quit)
        self.root.report_callback_exception = self.callback_error
        if sys.platform == "darwin":
            self.create_control_window()
        self.start_hotkey()
        self.start_tray()
        if self.config["start_enabled"]:
            self.enable()
        else:
            logging.info("Screen Shield DISABLED")
        self.root.after(50, self.poll)

    def create_control_window(self):
        """A visible Mac control/quit surface for Finder-launched app bundles."""
        root = self.root
        root.title("Screen Shield")
        root.configure(background="#101b28")
        root.geometry("480x570")
        root.resizable(False, False)
        self.panel_icon = ImageTk.PhotoImage(self.images[True], master=root)
        tk.Label(root, image=self.panel_icon, background="#101b28").pack(pady=(24, 8))
        tk.Label(root, text="Screen Shield", font=("Helvetica", 26, "bold"),
                 background="#101b28", foreground="white").pack()
        tk.Label(root, text="A little privacy, right on your Mac.", font=("Helvetica", 13),
                 background="#101b28", foreground="#a9bdcd").pack(pady=(6, 20))
        self.panel_status = tk.StringVar(value="Starting…")
        self.panel_detail = tk.StringVar(value="Preparing local protection.")
        tk.Label(root, textvariable=self.panel_status, font=("Helvetica", 18, "bold"),
                 background="#101b28", foreground="#66dda0").pack()
        tk.Label(root, textvariable=self.panel_detail, font=("Helvetica", 12), wraplength=410,
                 justify="center", background="#101b28", foreground="#d8e3ec").pack(pady=(8, 20))
        buttons = tk.Frame(root, background="#101b28")
        buttons.pack()
        self.panel_toggle = tk.Button(buttons, text="Disable", width=13, command=self.request_toggle)
        self.panel_toggle.grid(row=0, column=0, padx=5, pady=4)
        tk.Button(buttons, text="Snooze 5 min", width=13,
                  command=lambda: self.events.put(("snooze", None))).grid(row=0, column=1, padx=5, pady=4)
        tk.Button(buttons, text="Permissions…", width=13,
                  command=self.open_permissions).grid(row=1, column=0, padx=5, pady=4)
        tk.Button(buttons, text="Quit", width=13, command=self.request_quit).grid(row=1, column=1, padx=5, pady=4)
        self.panel_model_button = tk.Button(buttons, text="Open model view (testing)", width=29, command=self.toggle_model_view)
        self.panel_model_button.grid(row=2, column=0, columnspan=2, padx=5, pady=(10, 4))
        tk.Label(root, text="Model view shows live boxes with the privacy cover OFF.",
                 font=("Helvetica", 11), background="#101b28", foreground="#f4c344").pack(pady=(9, 0))
        tk.Label(root, text="Allow Camera and Screen Recording when asked.\nAccessibility / Input Monitoring enables the global shortcut.",
                 font=("Helvetica", 11), wraplength=420, justify="center",
                 background="#101b28", foreground="#a9bdcd").pack(pady=(20, 10))
        tk.Label(root, text="No uploads. No saved video. Camera released when disabled.",
                 font=("Helvetica", 10), background="#101b28", foreground="#a9bdcd").pack()
        root.bind("<Command-q>", self.request_quit)
        root.createcommand("tk::mac::Quit", self.request_quit)
        root.deiconify()
        root.lift()

    def open_permissions(self):
        try:
            subprocess.Popen(["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security"])
        except OSError:
            self.notice_text = "Open System Settings → Privacy & Security to allow permissions."

    def update_control_window(self):
        if self.panel_status is None:
            return
        if self.phase == "disabled":
            status, detail = "Disabled", "Your webcam is released. Enable when you want protection."
        elif self.phase in ("starting", "stopping"):
            status, detail = "Starting…" if self.phase == "starting" else "Stopping…", "Please wait a moment."
        elif self.model_view is not None:
            status, detail = "Model view — cover OFF", "Testing only. Close the model view to restore the previous protection setting."
        elif self.overlay.is_snoozed():
            status, detail = "Snoozed", "Protection is paused and will resume after the snooze."
        elif self.feed_lost:
            status, detail = "Camera unavailable", "Retrying automatically. Check Camera permission and close other camera apps."
        elif self.state == "INTRUDER":
            status, detail = "Screen covered", "Escape or three clicks on the cover will snooze protection."
        else:
            status, detail = "Protection enabled", "Monitoring locally. Your video stays on this Mac."
        self.panel_status.set(status)
        self.panel_detail.set(self.notice_text or detail)
        self.panel_toggle.configure(text="Disable" if self.enabled else "Enable",
                                    state="normal" if self.phase in ("enabled", "disabled") else "disabled")
        self.panel_model_button.configure(text="Close model view" if self.model_view is not None else "Open model view (testing)",
                                          state="normal" if self.phase in ("enabled", "disabled") else "disabled")

    def discard_preview_frames(self):
        while True:
            try:
                self.preview_frames.get_nowait()
            except queue.Empty:
                return

    def toggle_model_view(self):
        if self.model_view is not None:
            self.close_model_view()
            return
        if self.phase not in ("enabled", "disabled") or self.quitting.is_set():
            return
        self.model_view_restore_disabled = not self.enabled
        self.discard_preview_frames()
        self.model_view = ModelView(self.root, self.close_model_view, resume_enabled=self.enabled)
        self.overlay.hide()
        self.preview_requested.set()
        self.paused.clear()
        if self.phase == "disabled":
            self.enable()
        logging.info("Model view opened: privacy cover suppressed until the view is closed")

    def close_model_view(self, restore_prior_state=True):
        if self.model_view is None:
            return
        self.preview_requested.clear()
        view, self.model_view = self.model_view, None
        self.discard_preview_frames()
        view.close()
        restore_disabled, self.model_view_restore_disabled = self.model_view_restore_disabled, False
        if restore_prior_state and restore_disabled:
            self.disable()
        logging.info("Model view closed; prior enable/disable setting restored")

    def enable(self):
        if self.phase != "disabled" or self.quitting.is_set():
            return
        self.phase, self.state = "starting", "SAFE"
        self.notice_text = ""
        self.feed_lost = False
        self.paused.clear()
        if self.overlay.is_snoozed() and self.model_view is None:
            self.paused.set()
        self.generation += 1
        self.worker_stop = threading.Event()
        self.worker = threading.Thread(target=detector_loop,
            args=(self.config, self.worker_stop, self.events, self.generation, self.paused,
                  self.preview_requested, self.preview_frames),
            name=f"ScreenShieldDetector-{self.generation}")
        self.worker.start()
        self.refresh_tray()

    def disable(self):
        self.close_model_view(restore_prior_state=False)
        self.state = "SAFE"
        self.feed_lost = False
        self.overlay.hide()
        if self.worker is not None:
            self.phase = "stopping"
            self.worker_stop.set()
        else:
            self.phase = "disabled"
        self.refresh_tray()

    def reap_worker(self):
        if self.worker is None or self.worker.is_alive():
            return
        self.worker.join()
        self.worker = self.worker_stop = None
        self.phase, self.state = "disabled", "SAFE"
        self.close_model_view(restore_prior_state=False)
        self.feed_lost = False
        self.overlay.hide()
        self.accept_toggle_after = time.monotonic()
        logging.info("Screen Shield DISABLED")
        self.refresh_tray()

    def start_hotkey(self):
        try:
            from pynput import keyboard
            shortcut = parse_hotkey(self.config["hotkey"], keyboard)
            listener_class = keyboard.GlobalHotKeys
            if sys.platform == "darwin":
                # macOS 26 asserts if TIS/TSM keyboard-layout queries run on
                # pynput's listener thread. Capture its copied layout data on
                # Tk's main thread, then run only the event tap in background.
                from pynput.keyboard import _darwin
                from pynput._util.darwin import ListenerMixin
                with _darwin.keycode_context() as context:
                    layout_context = context

                class MacHotKeys(keyboard.GlobalHotKeys):
                    def _run(self):
                        self._context = layout_context
                        try:
                            ListenerMixin._run(self)
                        finally:
                            self._context = None

                listener_class = MacHotKeys
            self.listener = listener_class({shortcut: self.request_toggle})
            self.listener.start()
            print(f"Screen Shield hotkey: {shortcut}. Ctrl+C in the terminal quits.", flush=True)
        except Exception:
            self.notice_text = "Global shortcut unavailable. Use these buttons, or allow Accessibility / Input Monitoring and restart."
            logging.exception("Global hotkey unavailable. Check input/accessibility permissions; Ctrl+C still quits")

    def start_tray(self):
        if sys.platform == "darwin":
            print("Screen Shield: macOS tray skipped (Tk owns the main thread). Use the hotkey; Ctrl+C quits.", flush=True)
            return
        try:
            import pystray
            # Menu descriptors are replaced by the UI thread when state changes;
            # callbacks themselves never inspect or mutate Tk/camera state.
            self.tray = pystray.Icon("Screen Shield", self.images[False], "Screen Shield")
            self.tray.menu = self.tray_menu(pystray)
            if not self.tray.HAS_MENU:
                logging.warning("Tray backend has no menus; use the hotkey and Ctrl+C")
            finished = threading.Event()

            def setup(icon):
                if finished.is_set():
                    return
                try:
                    if self.quitting.is_set():
                        icon.stop()
                    else:
                        icon.visible = True
                        self.events.put(("tray_ready", None))
                except Exception as exc:
                    self.events.put(("tray_error", str(exc)))
                    icon.stop()

            def run():
                try:
                    self.tray.run(setup=setup)
                except Exception as exc:
                    self.events.put(("tray_error", str(exc)))
                finally:
                    finished.set()
                    # pystray 0.19.x starts a non-daemon setup thread before
                    # initializing its backend. If initialization fails before
                    # ready, stop() cannot wake that thread. Release its queue
                    # here so an unavailable tray cannot keep Python alive.
                    setup_queue = getattr(self.tray, "_Icon__queue", None)
                    setup_thread = getattr(self.tray, "_setup_thread", None)
                    if setup_queue is not None:
                        setup_queue.put(None)
                    if setup_thread is not None and setup_thread.ident is not None:
                        setup_thread.join()
                    self.events.put(("tray_stopped", None))

            self.tray_thread = threading.Thread(target=run, name="ScreenShieldTray")
            self.tray_thread.start()
        except Exception:
            logging.exception("Tray unavailable; use the hotkey and Ctrl+C")

    def tray_menu(self, pystray):
        return pystray.Menu(
            pystray.MenuItem("Disable" if self.enabled else "Enable",
                lambda icon, item: self.request_toggle(),
                enabled=self.phase in ("enabled", "disabled")),
            pystray.MenuItem("Snooze 5 min", lambda icon, item: self.events.put(("snooze", None))),
            pystray.MenuItem("Model view (testing)", lambda icon, item: self.events.put(("model_view", None))),
            pystray.MenuItem("Quit", lambda icon, item: self.events.put(("quit", None))))

    def refresh_tray(self):
        if self.tray is None or not self.tray_ready:
            return
        try:
            import pystray
            self.tray.icon = self.images[self.enabled]
            self.tray.menu = self.tray_menu(pystray)
            self.tray.update_menu()
        except Exception:
            logging.exception("Unable to update tray")

    def handle_event(self, kind, value):
        if kind == "quit":
            self.quitting.set()
        elif kind == "model_view":
            self.toggle_model_view()
        elif kind == "toggle":
            # Ignore queued keystrokes from a transition, even if they are
            # processed just after ready/cleanup. Never queue a restart.
            if (not self.quitting.is_set() and self.phase in ("enabled", "disabled")
                    and value >= self.accept_toggle_after):
                self.disable() if self.enabled else self.enable()
        elif kind == "snooze" and not self.quitting.is_set():
            self.overlay.snooze(5 * 60)
            self.paused.set()
            logging.info("Screen Shield SNOOZED for 5 min")
        elif kind == "worker":
            generation, action, payload = value
            if generation != self.generation:
                return
            if action == "ready" and self.phase == "starting" and not self.quitting.is_set():
                self.phase = "enabled"
                self.accept_toggle_after = time.monotonic()
                logging.info("Screen Shield ENABLED")
                self.refresh_tray()
            elif action == "state" and self.enabled and not self.quitting.is_set():
                self.state = payload
            elif action == "camera_lost" and self.enabled and not self.quitting.is_set():
                first_loss = not self.feed_lost
                self.feed_lost = True
                self.discard_preview_frames()
                if self.model_view is not None:
                    self.model_view.clear("Camera unavailable — retrying automatically.")
                logging.warning("%s", payload)
                if first_loss:
                    self.notify_camera_loss()
            elif action == "camera_restored" and self.enabled and not self.quitting.is_set():
                self.feed_lost = False
                logging.info("Camera feed available")
            elif action == "error":
                self.notice_text = f"Detection could not start: {payload}"
                logging.error("Detector failed: %s", payload)
                self.disable()
        elif kind == "tray_ready":
            self.tray_ready = True
            self.refresh_tray()
        elif kind in ("tray_error", "tray_stopped"):
            self.tray_ready = False
            if not self.quitting.is_set():
                logging.warning("Tray unavailable (%s); use the hotkey and Ctrl+C", value or "stopped")

    def notify_camera_loss(self):
        if self.tray is not None and self.tray_ready and self.tray.HAS_NOTIFICATION:
            try:
                self.tray.notify("Camera unavailable. Screen Shield will retry automatically.", "Screen Shield")
            except Exception:
                logging.info("Tray notification unavailable; camera status remains in console")

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle_event(kind, value)
        self.reap_worker()
        if self.listener is not None and not self.listener_failed and not self.listener.is_alive():
            self.listener_failed = True
            self.notice_text = "Global shortcut stopped. Use the buttons; check Accessibility / Input Monitoring and restart."
            logging.error("Global hotkey listener stopped. Check permissions; Ctrl+C still quits")
        if self.overlay.is_snoozed() and self.model_view is None:
            self.paused.set()
        else:
            self.paused.clear()
        try:
            generation, frame, detection = self.preview_frames.get_nowait()
        except queue.Empty:
            pass
        else:
            if (self.model_view is not None and self.enabled and not self.feed_lost
                    and generation == self.generation and not self.quitting.is_set()):
                self.model_view.render(frame, detection)
            del frame
        if self.quitting.is_set():
            self.disable()
            if self.worker is None:
                self.root.quit()
                return
        elif self.model_view is not None:
            if self.overlay.is_visible():
                self.overlay.hide()
        elif self.enabled and (self.state == "INTRUDER" or (self.config["fail_closed"] and self.feed_lost)):
            if not self.overlay.is_visible() and not self.overlay.is_snoozed():
                self.overlay.show()
        elif self.overlay.is_visible():
            self.overlay.hide()
        self.update_control_window()
        self.root.after(50, self.poll)

    def callback_error(self, exc_type, exc_value, traceback):
        logging.error("UI failed", exc_info=(exc_type, exc_value, traceback))
        self.exit_code = 1
        self.request_quit()
        self.root.quit()

    def close(self):
        """Stop sources first; join every thread before releasing Tk/images."""
        self.quitting.set()
        if self.worker_stop is not None:
            self.worker_stop.set()
        try:
            self.close_model_view(restore_prior_state=False)
            self.overlay.hide()
        finally:
            # Cleanup failures must not skip the remaining resources.
            for service in (self.listener, self.tray):
                if service is not None:
                    try:
                        service.stop()
                    except Exception:
                        logging.exception("Unable to stop desktop service")
            for thread in (self.worker, self.listener, self.tray_thread):
                if thread is not None and thread.ident is not None:
                    try:
                        thread.join()
                    except Exception:
                        logging.exception("Desktop service cleanup failed")
            self.worker = self.worker_stop = None
            self.discard_preview_frames()
            self.phase, self.state = "disabled", "SAFE"
            for image in self.images.values():
                image.close()



def policy_acceptance_current(path, digest, version):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(record, dict) and record.get("policy_hash") == digest
                and record.get("version") == version
                and record.get("privacy_acknowledged") is True
                and record.get("terms_accepted") is True)
    except (OSError, ValueError, UnicodeError):
        return False


def require_policy_acceptance(root):
    """Review gate before camera, capture, tray, or hotkey services start."""
    bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        raw = (bundle / "policies.json").read_bytes()
        docs = json.loads(raw)
        assert isinstance(docs, dict) and isinstance(docs["version"], str) and isinstance(docs["effective_date"], str)
        digest = hashlib.sha256(raw).hexdigest()
        for key in ("privacy", "terms"):
            assert isinstance(docs[key]["intro"], str) and docs[key]["sections"]
            for section in docs[key]["sections"]:
                assert isinstance(section["title"], str) and isinstance(section["body"], str)
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        messagebox.showerror("Screen Shield", "Required Privacy Policy / Terms files are missing or invalid. Please reinstall the app; monitoring will not start.", parent=root)
        return False
    if sys.platform == "darwin":
        support = Path.home() / "Library" / "Application Support" / "Screen Shield"
    elif sys.platform == "win32":
        support = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Screen Shield"
    else:
        support = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "Screen Shield"
    consent_path = support / "consent.json"
    if policy_acceptance_current(consent_path, digest, docs["version"]):
        return True

    root.title("Screen Shield — Review before use")
    root.geometry("760x680")
    root.minsize(600, 580)
    root.configure(background="#f6f5ef")
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(1, weight=1, minsize=120)
    header = ttk.Frame(frame)
    header.grid(row=0, column=0, sticky="ew")
    ttk.Label(header, text="Before you use Screen Shield", font=("Helvetica", 21, "bold")).pack(anchor="w")
    ttk.Label(header, text="An independent student-built preview. Review both documents before accepting.\nNo camera, screen capture, or shortcut listener starts before acceptance.", wraplength=545).pack(anchor="w", pady=(8, 12))
    notebook = ttk.Notebook(frame)
    notebook.grid(row=1, column=0, sticky="nsew")
    # Reserve footer space before the reader gets any expandable height.
    footer = ttk.Frame(frame)
    footer.grid(row=2, column=0, sticky="ew")
    viewed = set()
    checks = {key: tk.BooleanVar(value=False) for key in ("privacy", "terms")}
    readers = {}
    checkbox_widgets = {}
    decision = tk.BooleanVar(value=False)
    status = tk.StringVar(value="Scroll to the end of both documents to unlock the acknowledgments.")
    controls = {}

    def refresh():
        for key, widget in checkbox_widgets.items():
            widget.configure(state="normal" if key in viewed else "disabled")
        if "accept" in controls:
            enabled = len(viewed) == 2 and all(value.get() for value in checks.values())
            controls["accept"].configure(state="normal" if enabled else "disabled")
        if len(viewed) == 2 and all(value.get() for value in checks.values()):
            status.set("Ready — click Continue to Screen Shield below.")
        elif len(viewed) == 2:
            status.set("Both documents reviewed. Select both acknowledgments, then click Continue.")
        else:
            status.set("Privacy: " + ("reviewed" if "privacy" in viewed else "scroll to end")
                       + "  ·  Terms: " + ("reviewed" if "terms" in viewed else "scroll to end"))

    for key in ("privacy", "terms"):
        tab = ttk.Frame(notebook, padding=10)
        notebook.add(tab, text=docs[key]["title"])
        text = tk.Text(tab, height=12, wrap="word", font=("Helvetica", 12), background="white",
                       foreground="#142d2b", relief="flat", padx=14, pady=12)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        content = docs[key]["title"] + "\nEffective " + docs["effective_date"] + "\n\n" + docs[key]["intro"]
        for section in docs[key]["sections"]:
            content += "\n\n" + section["title"] + "\n\n" + section["body"]
        text.insert("1.0", content)
        text.configure(state="disabled")
        readers[key] = text
    ttk.Label(footer, textvariable=status, wraplength=545).pack(anchor="w", pady=(10, 8))
    for key, label in (("privacy", "I have read and acknowledge the Privacy Policy."),
                       ("terms", "I have read and agree to the Terms of Service.")):
        widget = ttk.Checkbutton(footer, text=label, variable=checks[key], command=refresh, state="disabled")
        widget.pack(anchor="w", pady=4)
        checkbox_widgets[key] = widget
    ttk.Label(footer, text="Acceptance is remembered on this Mac only. You can decline and quit.\nSupport: screenshield.riverboat836@silomails.com", wraplength=545).pack(anchor="w", pady=(10, 10))
    buttons = ttk.Frame(footer)
    buttons.pack(fill="x")

    def accept():
        if len(viewed) != 2 or not all(value.get() for value in checks.values()):
            return
        record = {"version": docs["version"], "policy_hash": digest,
                  "accepted_at": datetime.now(timezone.utc).isoformat(),
                  "privacy_acknowledged": True, "terms_accepted": True}
        try:
            support.mkdir(parents=True, exist_ok=True)
            consent_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except OSError:
            logging.warning("Could not save policy acceptance; it applies for this session only")
        decision.set(True)

    ttk.Button(buttons, text="Decline & Quit", command=lambda: decision.set(False)).pack(side="left")
    controls["accept"] = ttk.Button(buttons, text="Continue to Screen Shield →", command=accept, state="disabled", default="active")
    controls["accept"].pack(side="right")
    root.protocol("WM_DELETE_WINDOW", lambda: decision.set(False))
    root.bind("<Escape>", lambda event: decision.set(False))
    root.bind("<Return>", lambda event: accept())
    if sys.platform == "darwin":
        root.createcommand("tk::mac::Quit", lambda: decision.set(False))
    timer = [None]

    def observe_scroll():
        for key, text in readers.items():
            if text.winfo_ismapped() and text.yview()[1] >= 0.999:
                viewed.add(key)
        refresh()
        timer[0] = root.after(120, observe_scroll)

    root.deiconify()
    root.lift()
    timer[0] = root.after(120, observe_scroll)
    try:
        root.wait_variable(decision)
        return bool(decision.get())
    finally:
        if timer[0] is not None:
            root.after_cancel(timer[0])
        root.unbind("<Escape>")
        root.unbind("<Return>")
        frame.destroy()
        root.withdraw()
        root.minsize(1, 1)


def main():
    parser = argparse.ArgumentParser(description="Screen Shield (Ctrl+C or tray Quit to exit)")
    packaged_mac = getattr(sys, "frozen", False) and sys.platform == "darwin"
    support = Path.home() / "Library" / "Application Support" / "Screen Shield"
    default_config = support / "config.json" if packaged_mac else Path(__file__).with_name("config.json")
    parser.add_argument("--config", type=Path, default=default_config)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = load_config(args.config)
    if packaged_mac:
        # Persist preferences outside the read-only .app; resolve bundled
        # weights at each launch so moving the app does not break the model.
        os.environ["MPLCONFIGDIR"] = str(support / "cache" / "matplotlib")
        bundled_model = Path(sys._MEIPASS) / "face_landmarker.task"
        if config["model_path"] == str(Path(DEFAULT_CONFIG["model_path"]).expanduser()) and bundled_model.is_file():
            config["model_path"] = str(bundled_model)
    root = app = None
    previous_handler = signal.getsignal(signal.SIGINT)
    exit_code = 0
    try:
        root = tk.Tk()
        root.withdraw()
        if not require_policy_acceptance(root):
            return 0
        app = ScreenShield(root, config)
        signal.signal(signal.SIGINT, app.request_quit)
        app.start()
        root.mainloop()
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.exception("Screen Shield failed")
        exit_code = 1
    finally:
        try:
            if app is not None:
                app.close()
        finally:
            try:
                if root is not None:
                    root.destroy()
            finally:
                signal.signal(signal.SIGINT, previous_handler)
    return exit_code or (app.exit_code if app is not None else 0)


if __name__ == "__main__":
    raise SystemExit(main())
