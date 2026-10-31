import ssl
import sys
import urllib.error
import urllib.request

from .config import USER_AGENT


def ssl_context():
    # System CA paths differ between distros, so packaged builds carry their own.
    if not getattr(sys, "frozen", False):
        return None
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = ssl_context()


def http_get(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)
