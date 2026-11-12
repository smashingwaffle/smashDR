"""
tilecache.py - offline cache for the Leaflet maps (Aircraft, ISS, APRS).

Map tiles and the Leaflet library are loaded through our own URL scheme 'smtile://<host>/<path>'
instead of 'https://<host>/<path>'. The handler serves a file from user/cache/tiles/ when it has
one, otherwise fetches it over HTTPS, saves it, and serves it. So every tile you have looked at
is still there without internet. No bulk downloading: the tile providers' terms don't allow it,
the cache only keeps what you viewed (like a browser cache that never forgets).

    tilecache.register_scheme()        # before QApplication
    tilecache.install()                # after QApplication
    html = tilecache.rewrite(html)     # in each map page
"""

import hashlib
import mimetypes
import os
import re
import time

from PyQt6 import QtCore, QtNetwork

from smashdr.paths import USER

SCHEME = b"smtile"
HOSTS = ("server.arcgisonline.com", "basemaps.cartocdn.com", "tile.openstreetmap.org", "unpkg.com")
CACHE = USER / "cache" / "tiles"
REFRESH_DAYS = 60                 # tiles older than this are refreshed when online (kept if offline)

_handler = None
_registered = [False]


def rewrite(html: str) -> str:
    """Route the tile hosts and Leaflet itself through the cache."""
    if _handler is None:
        return html
    for h in HOSTS:
        html = html.replace(f"https://{h}", f"smtile://{h}")
    return html


def register_scheme():
    try:
        from PyQt6.QtWebEngineCore import QWebEngineUrlScheme
    except ImportError:
        return
    try:
        s = QWebEngineUrlScheme(SCHEME)
        s.setSyntax(QWebEngineUrlScheme.Syntax.Host)      # default port stays "unspecified"
        F = QWebEngineUrlScheme.Flag
        s.setFlags(F.SecureScheme | F.CorsEnabled | F.ContentSecurityPolicyIgnored)
        QWebEngineUrlScheme.registerScheme(s)
        _registered[0] = True
    except Exception as e:                                # maps then just load online as before
        print("[!] map cache not available:", e)


def _path_for(url: QtCore.QUrl):
    host = url.host()
    p = url.path().lstrip("/")
    safe = re.sub(r"[^A-Za-z0-9._/-]", "_", p)
    if not safe or ".." in safe or len(safe) > 180:
        safe = hashlib.sha1(p.encode()).hexdigest()
    ext = os.path.splitext(safe)[1]
    if not ext and "/tile/" in p:                  # Esri tiles have no extension
        safe += ".tile"
    return CACHE / host / safe


def _mime(path, data=b""):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return b"image/png"
    if data[:3] == b"\xff\xd8\xff":
        return b"image/jpeg"
    m, _ = mimetypes.guess_type(str(path))
    if str(path).endswith(".js"):
        m = "application/javascript"
    return (m or "application/octet-stream").encode()


def _make_handler():
    from PyQt6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

    class Handler(QWebEngineUrlSchemeHandler):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.net = QtNetwork.QNetworkAccessManager(self)
            self._keep = {}                       # job id -> (reply, buffer) while running
            self.stats = {"hit": 0, "fetched": 0, "offline": 0}

        def requestStarted(self, job):
            url = job.requestUrl()
            if url.host() not in HOSTS:
                job.fail(QWebEngineUrlRequestJob.Error.UrlInvalid)
                return
            path = _path_for(url)
            fresh = path.exists() and time.time() - path.stat().st_mtime < REFRESH_DAYS * 86400
            if fresh:
                self._serve(job, path.read_bytes(), path)
                self.stats["hit"] += 1
                return
            real = QtCore.QUrl(url)
            real.setScheme("https")
            req = QtNetwork.QNetworkRequest(real)
            req.setHeader(QtNetwork.QNetworkRequest.KnownHeaders.UserAgentHeader, "smashDR (map cache)")
            req.setAttribute(QtNetwork.QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QtNetwork.QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
            reply = self.net.get(req)
            key = id(job)
            self._keep[key] = (reply, job)
            job.destroyed.connect(lambda _o=None, k=key: self._drop(k))
            reply.finished.connect(lambda r=reply, k=key, p=path: self._done(r, k, p))

        def _drop(self, key):
            r = self._keep.pop(key, None)
            if r:
                r[0].abort()

        def _done(self, reply, key, path):
            held = self._keep.pop(key, None)
            reply.deleteLater()
            if held is None:                     # page went away
                return
            job = held[1]
            ok = reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else b""
            try:
                if ok and data:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(path.suffix + ".part")
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                    self.stats["fetched"] += 1
                    self._serve(job, data, path)
                elif path.exists():               # offline (or server error): use the old copy
                    self.stats["offline"] += 1
                    self._serve(job, path.read_bytes(), path)
                else:
                    job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            except (OSError, RuntimeError):
                pass

        def _serve(self, job, data, path):
            buf = QtCore.QBuffer(job)            # owned by the job, freed with it
            buf.setData(data)
            buf.open(QtCore.QIODevice.OpenModeFlag.ReadOnly)
            job.reply(_mime(path, data), buf)

    return Handler


def install():
    """Attach the cache to the default web profile (call once, after QApplication)."""
    global _handler
    try:
        from PyQt6.QtWebEngineCore import QWebEngineProfile
    except ImportError:
        return
    if _handler is not None or not _registered[0]:
        return
    try:
        Handler = _make_handler()
        h = Handler(QtCore.QCoreApplication.instance())
        QWebEngineProfile.defaultProfile().installUrlSchemeHandler(SCHEME, h)
        _handler = h
    except Exception as e:
        print("[!] map cache not available:", e)


def size_mb():
    total = 0
    for root, _d, files in os.walk(CACHE):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6
