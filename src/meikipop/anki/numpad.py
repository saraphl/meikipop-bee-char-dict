# meikipop/anki/numpad.py
import logging
import selectors
import threading

from meikipop.config.config import IS_LINUX

logger = logging.getLogger(__name__)

RESCAN_INTERVAL_SECONDS = 5


class NumpadListener(threading.Thread):
    """
    Reports numpad digit presses (1-9, 0 as 10) from evdev devices, regardless of the focused window.
    Doesn't grab the devices, so the keys still reach the focused app. Requires read access to /dev/input.
    """

    def __init__(self, on_digit):
        super().__init__(daemon=True, name="NumpadListener")
        self.on_digit = on_digit

    def run(self):
        if not IS_LINUX:
            logger.warning("Anki: Numpad shortcuts are only supported on Linux.")
            return
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            logger.warning("Anki: python-evdev is not installed, numpad shortcuts are disabled.")
            return

        digit_codes = {getattr(ecodes, f'KEY_KP{n}'): (n or 10) for n in range(10)}
        selector = selectors.DefaultSelector()
        ignored_paths = set()  # readable devices without a numpad, e.g. mice

        def rescan():
            # picks up keyboards plugged in or re-enumerated (e.g. after suspend) since the last scan
            paths = set(evdev.list_devices())
            ignored_paths.intersection_update(paths)
            registered = {key.fileobj.path for key in selector.get_map().values()}
            for path in paths - registered - ignored_paths:
                try:
                    device = evdev.InputDevice(path)
                except OSError:
                    continue
                if ecodes.KEY_KP1 in device.capabilities().get(ecodes.EV_KEY, []):
                    selector.register(device, selectors.EVENT_READ)
                    logger.debug(f"Anki: Listening for numpad keys on '{device.name}'.")
                else:
                    ignored_paths.add(path)
                    device.close()

        rescan()
        if not selector.get_map():
            logger.warning("Anki: No readable keyboard with a numpad found in /dev/input "
                           "(is your user in the 'input' group?). Will keep looking.")

        while True:
            ready = selector.select(timeout=RESCAN_INTERVAL_SECONDS)
            if not ready:
                rescan()
                continue
            for key, _ in ready:
                device = key.fileobj
                try:
                    for event in device.read():
                        if event.type == ecodes.EV_KEY and event.value == 1 and event.code in digit_codes:
                            self.on_digit(digit_codes[event.code])
                except BlockingIOError:
                    continue
                except OSError:  # device unplugged
                    selector.unregister(device)
                    try:
                        device.close()
                    except OSError:
                        pass
                except Exception:
                    logger.exception("Anki: Error while handling a numpad key.")
