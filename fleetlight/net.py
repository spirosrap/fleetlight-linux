"""One TLS context for every HTTPS request Fleetlight makes.

urllib builds a fresh context for each connection unless it is given one, which loads the system
certificate store every time. That memory is not returned, so a check every minute grew the app by
hundreds of megabytes over a day or two.
"""
import ssl
import threading
import urllib.request

_lock = threading.Lock()
_context = None
_installed = False


def context():
    global _context
    with _lock:
        if _context is None:
            _context = ssl.create_default_context()
        return _context


def opener(*handlers):
    """An opener that verifies certificates with the shared context, plus any extra handlers."""
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context()), *handlers)


def install():
    """Make plain urllib.request.urlopen calls in this process use the shared context."""
    global _installed
    if not _installed:
        urllib.request.install_opener(opener())
        _installed = True
