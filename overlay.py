"""Primary-monitor frozen blur overlay. All methods run on Tk's main thread.

Screenshots exist only in memory. No capture happens while visible or snoozed.
"""
import logging
import threading
import time
import tkinter as tk

import mss
from PIL import Image, ImageFilter, ImageTk


class Overlay:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self._window = None
        self._photo = None
        self._clicks = 0
        self._snoozed_until = 0.0

    @staticmethod
    def _assert_main_thread():
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Overlay must be accessed from the main thread")

    def is_visible(self):
        return self._window is not None

    def is_snoozed(self):
        return time.monotonic() < self._snoozed_until

    def show(self):
        self._assert_main_thread()
        if self.is_visible() or self.is_snoozed():
            return
        # Tk's default screen is the primary target. Use Tk dimensions for the
        # displayed image to accommodate Retina/scaled displays. No Toplevel
        # exists until the screenshot has been captured and blurred.
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        blurred = None
        try:
            with mss.mss() as capture:
                monitors = capture.monitors[1:]
                if not monitors:
                    raise RuntimeError("No screen capture monitors available")
                monitor = next((m for m in monitors if m.get("is_primary")), None)
                if monitor is None:
                    monitor = next((m for m in monitors if m["left"] == 0 and m["top"] == 0), monitors[0])
                shot = capture.grab(monitor)
                raw = Image.frombytes("RGB", shot.size, shot.rgb)
                try:
                    # Blur in displayed pixels, so radius 40 stays strong on
                    # both normal and high-density monitors.
                    scaled = raw.resize((width, height), Image.Resampling.LANCZOS)
                    try:
                        blurred = scaled.filter(ImageFilter.GaussianBlur(self.config["blur_radius"]))
                    finally:
                        scaled.close()
                finally:
                    raw.close()
                    del shot
        except Exception:
            if blurred is not None:
                blurred.close()
                blurred = None
            logging.exception("Screen capture/blur failed; using a solid black overlay")
        window = None
        try:
            window = tk.Toplevel(self.root)
            window.withdraw()
            window.title("Screen Shield")
            window.configure(background="black")
            window.overrideredirect(True)
            window.geometry(f"{width}x{height}+0+0")
            window.attributes("-topmost", True)
            window.attributes("-fullscreen", True)
            canvas = tk.Canvas(window, background="black", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            if blurred is not None:
                try:
                    self._photo = ImageTk.PhotoImage(blurred, master=window)
                    canvas.create_image(width // 2, height // 2, image=self._photo)
                except Exception:
                    self._photo = None
                    logging.exception("Blurred image display failed; using a solid black overlay")
            if self.config["show_message"]:
                # A black panel keeps the warning readable over any capture.
                canvas.create_rectangle(width * .1, height / 2 - 65,
                                        width * .9, height / 2 + 65, fill="black", outline="")
                canvas.create_text(width / 2, height / 2,
                                   text="Screen Shield: someone may be viewing your screen",
                                   fill="white", font=("TkDefaultFont", 20), width=int(width * .75))
            canvas.create_text(width / 2, height - 50,
                               text="Escape or click three times to snooze",
                               fill="white", font=("TkDefaultFont", 14))
            window.bind("<Escape>", self._snooze)
            window.bind("<Button-1>", self._clicked)
            window.protocol("WM_DELETE_WINDOW", self._snooze)
            self._clicks = 0
            self._window = window
            window.deiconify()
            window.lift()
            window.focus_force()
        except Exception:
            self._window = None
            self._photo = None
            if window is not None:
                window.destroy()
            raise
        finally:
            if blurred is not None:
                blurred.close()

    def hide(self):
        self._assert_main_thread()
        if not self.is_visible():
            return
        window, self._window = self._window, None
        try:
            window.withdraw()
            window.destroy()
        finally:
            self._photo = None
            self._clicks = 0

    def _snooze(self, event=None):
        self.snooze(self.config["snooze_seconds"])
        return "break"

    def snooze(self, seconds):
        """Main-thread dismissal used by the tray and overlay failsafes."""
        self._assert_main_thread()
        self._snoozed_until = time.monotonic() + seconds
        self.hide()

    def _clicked(self, event=None):
        self._clicks += 1
        if self._clicks >= 3:
            return self._snooze()
        return "break"
