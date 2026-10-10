# meikipop/screenshot/kwin_window.py
# Window picking and geometry tracking via KWin's D-Bus API (KDE Plasma, Wayland and X11).
import logging
import threading

from meikipop.config.config import IS_LINUX

logger = logging.getLogger(__name__)

KWIN_SERVICE = 'org.kde.KWin'


def _bus():
    from gi.repository import Gio
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _call(path, interface, method, args, reply_type, timeout_ms=2000, service=KWIN_SERVICE):
    from gi.repository import GLib, Gio
    result = _bus().call_sync(service, path, interface, method, args,
                              GLib.VariantType.new(reply_type), Gio.DBusCallFlags.NONE, timeout_ms, None)
    return result.unpack()[0]


def is_available():
    if not IS_LINUX:
        return False
    try:
        from gi.repository import GLib
        return _call('/org/freedesktop/DBus', 'org.freedesktop.DBus', 'NameHasOwner',
                     GLib.Variant('(s)', (KWIN_SERVICE,)), '(b)', service='org.freedesktop.DBus')
    except Exception:
        return False


def get_window_info(uuid):
    """Returns KWin's window info dict, or None if the window no longer exists."""
    try:
        from gi.repository import GLib
        info = _call('/KWin', 'org.kde.KWin', 'getWindowInfo', GLib.Variant('(s)', (uuid,)), '(a{sv})')
    except Exception as e:
        logger.debug(f"getWindowInfo failed: {e}")
        return None
    return info or None


def pick_window():
    """Shows KWin's crosshair and returns info of the clicked window, or None if canceled."""
    from gi.repository import GLib
    from PyQt6.QtCore import QEventLoop, QTimer

    result = {}

    def worker():
        try:
            result['info'] = _call('/KWin', 'org.kde.KWin', 'queryWindowInfo', None, '(a{sv})',
                                   timeout_ms=GLib.MAXINT32)
        except Exception as e:
            logger.info(f"Window selection canceled: {e}")

    # run the blocking call in a thread so the Qt event loop stays responsive while the user picks
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(lambda: loop.quit() if not thread.is_alive() else None)
    timer.start(50)
    loop.exec()
    timer.stop()
    return result.get('info') or None


def find_window(resource_class, caption):
    """Finds an open window by resource class, preferring an exact caption match."""
    if not resource_class:
        return None
    try:
        from gi.repository import GLib
        matches = _call('/WindowsRunner', 'org.kde.krunner1', 'Match', GLib.Variant('(s)', (resource_class,)),
                        '(a(sssida{sv}))')
    except Exception as e:
        logger.debug(f"WindowsRunner query failed: {e}")
        return None

    candidates = []
    for match_id, *_ in matches:
        uuid = match_id.split('_', 1)[-1]
        info = get_window_info(uuid)
        if info and info.get('resourceClass') == resource_class:
            candidates.append(info)

    exact = [c for c in candidates if c.get('caption') == caption]
    if exact:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None

