# meikipop/screenshot/screenmanager.py
import logging
import threading
import time

from PIL import Image

from meikipop.config.config import config, IS_WAYLAND
from meikipop.gui.region_selector import RegionSelector
from meikipop.screenshot import kwin_window

if IS_WAYLAND:
    from . import wayland_mss_shim

    mss = wayland_mss_shim.MSSModuleShim()
else:
    import mss

logger = logging.getLogger(__name__)  # Get the logger


# todo doesnt work when monitors change
class ScreenManager(threading.Thread):
    def __init__(self, shared_state, input_loop):
        super().__init__(daemon=True, name="ScreenManager")
        self.shared_state = shared_state
        self.monitor = None
        self.last_ocr_put_time = 0.0
        self.last_screenshot = None
        self.last_mouse_pos = None
        self.input_loop = input_loop
        self.window_uuid = None
        self.screen_bounds = None
        self.last_window_rebind_time = 0.0
        self.scan_area_lock = threading.Lock()
        if config.scan_region == "region":
            self.set_scan_region()
        elif config.scan_region == "window" and kwin_window.is_available():
            # Esc on the crosshair falls back to the last used window
            if not (self.set_scan_window() or self.rebind_saved_window()):
                logger.warning("No window selected, defaulting to screen 1.")
                self.set_scan_screen(1)
        else:
            try:
                screen_idx = int(config.scan_region)
                self.set_scan_screen(screen_idx)
            except (ValueError, IndexError):
                logger.warning(f"Invalid screen '{config.scan_region}' in config, defaulting to screen 1.")
                self.set_scan_screen(1)

    def run(self):
        logger.debug("Screenshot thread started.")
        while self.shared_state.running:
            try:
                if config.auto_scan_mode and not config.is_enabled:
                    logger.debug(f"paused while auto mode")
                    self._sleep_and_handle_loop_exit(1)
                    continue
                self.shared_state.screenshot_trigger_event.wait()
                self.shared_state.screenshot_trigger_event.clear()
                if not self.shared_state.running: break
                logger.debug("Screenshot: Triggered!")

                # prevent multiple ocr runs during auto_scan_interval_seconds
                seconds_since_last_ocr = time.perf_counter() - self.last_ocr_put_time
                if config.auto_scan_mode and seconds_since_last_ocr < config.auto_scan_interval_seconds:
                    logger.debug(
                        f"...{seconds_since_last_ocr:.2f}s since last ocr, sleeping for another {config.auto_scan_interval_seconds - seconds_since_last_ocr:.2f}s")
                    self._sleep_and_handle_loop_exit(config.auto_scan_interval_seconds - seconds_since_last_ocr)
                    continue

                # prevent ocr runs without mouse movements for auto-on-mouse-move mode
                if config.auto_scan_mode and config.auto_scan_on_mouse_move and self.last_mouse_pos == self.input_loop.get_mouse_pos():
                    continue
                self.last_mouse_pos = self.input_loop.get_mouse_pos()

                # query KWin outside screen_lock, since show_popup blocks the GUI thread on it
                if self.window_uuid and not self.update_window_geometry():
                    logger.debug("Bound window is minimized or off-screen, skipping screenshot")
                    self._sleep_and_handle_loop_exit(1)
                    continue

                logger.debug("screenmanager acquiring lock...")
                with self.shared_state.screen_lock:
                    logger.debug("...successfully acquired lock by screenmanager")
                    start_time = time.perf_counter()
                    screenshot = self.take_screenshot()
                logger.debug("...successfully released lock by screenmanager")
                processing_duration = time.perf_counter() - start_time
                logger.debug(f"Screenshot {screenshot.size} complete in {processing_duration:.2f}s")

                if self.last_screenshot and self.last_screenshot.raw == screenshot.raw:
                    logger.debug(f"Screen content didnt change... skipping ocr")
                    self._sleep_and_handle_loop_exit(0.1)
                    continue

                self.last_screenshot = screenshot
                self.last_mouse_pos = self.input_loop.get_mouse_pos()
                img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                self.shared_state.ocr_queue.put(img)
                self.last_ocr_put_time = time.perf_counter()
            except:
                logger.exception("An unexpected error occurred in the screenshot loop. Continuing...")
                self._sleep_and_handle_loop_exit(1)
        logger.debug("Screenshot thread stopped.")

    def take_screenshot(self):
        with mss.mss() as sct:
            sct_img = sct.grab(self.monitor)
            return sct_img

    def set_scan_region(self):
        scan_rect = RegionSelector.get_region()
        if scan_rect:
            logger.info(f"Set scan area to region {scan_rect}")
            with self.scan_area_lock:
                self.window_uuid = None
                self.monitor = {"top": scan_rect.y(), "left": scan_rect.x(), "width": scan_rect.width(),
                                "height": scan_rect.height()}
            return True
        else:
            logger.info("Region selection canceled.")
            return False

    def set_scan_window(self):
        info = kwin_window.pick_window()
        if not info:
            return False
        self._bind_window(info)
        config.scan_region = 'window'
        config.scan_window_class = info.get('resourceClass', '')
        config.scan_window_caption = info.get('caption', '')
        config.save()
        return True

    def rebind_saved_window(self):
        info = self._find_saved_window()
        if not info:
            return False
        self._bind_window(info)
        return True

    def _find_saved_window(self):
        return kwin_window.find_window(config.scan_window_class, config.scan_window_caption)

    def _bind_window(self, info):
        with mss.mss() as sct:
            self.screen_bounds = sct.monitors[0]
        rect = self._window_rect(info)
        with self.scan_area_lock:
            self.window_uuid = info['uuid']
            self.monitor = rect or self.monitor
        logger.info(f"Set scan area to window '{info.get('caption')}' ({info.get('resourceClass')})")

    def update_window_geometry(self):
        """Refreshes the scan area from the bound window, returns False if the window isn't visible."""
        uuid = self.window_uuid
        info = kwin_window.get_window_info(uuid)
        if not info:
            # window was closed (e.g. game restarted), try to find it again every few seconds
            if time.perf_counter() - self.last_window_rebind_time < 3:
                return False
            self.last_window_rebind_time = time.perf_counter()
            info = self._find_saved_window()
            if not info:
                return False
            logger.info(f"Rebound scan area to window '{info.get('caption')}'")
        rect = None if info.get('minimized') else self._window_rect(info)
        with self.scan_area_lock:
            if self.window_uuid != uuid:
                return True  # scan area was changed from the tray in the meantime
            self.window_uuid = info['uuid']
            if rect:
                self.monitor = rect
        return rect is not None

    def _window_rect(self, info):
        """Window rect clamped to the captured area, or None if the window is outside of it."""
        b = self.screen_bounds
        left = max(round(info['x']), b['left'])
        top = max(round(info['y']), b['top'])
        right = min(round(info['x'] + info['width']), b['left'] + b['width'])
        bottom = min(round(info['y'] + info['height']), b['top'] + b['height'])
        if right <= left or bottom <= top:
            return None
        return {"top": top, "left": left, "width": right - left, "height": bottom - top}

    def set_scan_screen(self, screen_index):
        with mss.mss() as sct:
            if screen_index < len(sct.monitors):
                logger.info(f"Set scan area to screen {screen_index}")
                with self.scan_area_lock:
                    self.window_uuid = None
                    self.monitor = sct.monitors[screen_index]
            else:
                logger.error(f"Cannot set scan screen: index {screen_index} is out of bounds.")

    def get_scan_geometry(self):
        if not self.monitor:
            return 0, 0, 0, 0
        return self.monitor["left"], self.monitor["top"], self.monitor["width"], self.monitor["height"]

    def force_screenshot_trigger(self):
        self.last_screenshot = None
        self.last_mouse_pos = None

    def _sleep_and_handle_loop_exit(self, interval):
        if config.auto_scan_mode:
            time.sleep(interval)
            self.shared_state.screenshot_trigger_event.set()
        else:
            self.shared_state.hit_scan_queue.trigger()

    @staticmethod
    def get_screens():
        with mss.mss() as sct:
            return sct.monitors