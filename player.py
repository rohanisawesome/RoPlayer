import sys
import os

# Work around a known upstream bug (protobuf issue #22067): protobuf's
# compiled "upb" C extension (_message.abi3.so) can segfault during
# CPython's own interpreter shutdown/GC on Python 3.13+, completely
# independent of whether it was ever actually used for anything. pychromecast
# pulls in protobuf just by being imported below, so this must be set before
# that import happens. Forcing the pure-Python implementation avoids loading
# the buggy extension at all - encode/decode is a hair slower, but at the
# tiny message volume a Cast connection uses, that's not noticeable.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import re
import html
import base64
import binascii
import time
import random
import math
import hashlib
import webbrowser
from typing import Optional
import urllib.request
import urllib.parse
import json
import threading
import socket
import http.server

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, QHBoxLayout, QWidget,
    QSlider, QLabel, QFileDialog, QListWidget, QListWidgetItem,
    QAbstractItemView, QLineEdit, QGraphicsDropShadowEffect, QMenu,
    QStackedWidget, QGraphicsOpacityEffect, QDialog, QFormLayout,
    QDialogButtonBox, QCheckBox, QMessageBox, QProxyStyle, QStyle, QProgressBar,
    QScrollArea, QFrame
)

from PyQt6.QtCore import (
    Qt, QUrl, QSize, QRect, QRectF, QPointF, QThread, pyqtSignal, QObject,
    QSettings, QPropertyAnimation, QVariantAnimation, QEasingCurve,
    pyqtProperty, QTimer, QPoint, QStandardPaths, QEvent
)

from PyQt6.QtGui import (
    QPixmap, QIcon, QColor, QImage, QPainter, QPainterPath, QShortcut, 
    QKeySequence, QAction, QPen, QFont, QCursor, QFontMetrics, QLinearGradient
)

from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from mutagen import File as MutagenFile

# MPRIS2 (Linux "now playing" desktop integration - media keys, system tray
# widgets, lock screen info) is built on D-Bus, which only really exists on
# Linux/BSD desktops. Built on dbus-python + PyGObject rather than PyQt6's
# own QtDBus bindings - QtDBus's property/adaptor system turned out to have
# multiple real marshaling and notification bugs for building a D-Bus
# *service* (as opposed to being a client), serious enough that desktop
# widgets couldn't reliably read the data even once it was proven correct
# via busctl/playerctl and KDE's own engine. dbus-python is the same,
# far more battle-tested library Tauon (and most other Python media
# players) use for exactly this. Guard the import so RoPlayer still runs
# fine without these installed - see _setup_mpris() for what happens then.
try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
    HAS_DBUS_PYTHON = True
except ImportError:
    HAS_DBUS_PYTHON = False

# Chromecast
import pychromecast
from pychromecast.controllers.media import MediaController

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".wav", ".wma", ".aac"}
COVER_SIZE = 125
CARD_SIZE = QSize(139, 207)  # +16px over the original 191 - room for a title to wrap to 2 lines
GRID_SIZE = QSize(151, 217)  # grown by the same +16px, keeping the original 10px spacing buffer
# Bigger, more prominent cards for Home shelves specifically, matching the
# YT Music reference - Library/Pinned/search results all keep using the
# full CARD_SIZE above unchanged. AlbumCardWidget scales its cover/text/
# badges proportionally from whatever size it's given (~1.58x here).
HOME_CARD_SIZE = QSize(220, 327)
HOME_GRID_SIZE = QSize(232, 337)  # same 12px spacing buffer relationship as CARD_SIZE -> GRID_SIZE
# A fixed ~12% bump over CARD_SIZE/GRID_SIZE for Artist view specifically -
# tried computing an exact per-window-width stretch instead (scaling tiles
# to fill the row's width to the pixel), but that meant rebuilding the grid
# (and re-kicking the background photo fetcher) on every tab visit and
# window resize, which was fragile enough to be worth abandoning for
# something this simple. Doesn't perfectly eliminate the rightmost margin
# on every window size, just narrows it - see AlbumCardWidget, which scales
# everything inside a card (cover, fonts, badge) proportionally from
# whatever size it's given.
ARTIST_CARD_SIZE = QSize(156, 232)
ARTIST_GRID_SIZE = QSize(169, 243)
ARTIST_COVER_SIZE = 140  # matches the ~1.12x scale AlbumCardWidget derives from ARTIST_CARD_SIZE.width()
ARTIST_DETAIL_PHOTO_SIZE = 110  # the bigger photo shown in the artist detail panel above the filtered album grid
# How long cached artist-detail data is trusted before re-fetching -
# separate windows since listener/scrobble counts climb daily and are
# worth checking often, while a bio is close to static and re-fetching it
# on the same short cycle would just be a wasted network call almost every
# time. Same underlying artist.getinfo call returns both together
# regardless (see ArtistInfoFetcher) - these just control *when* that call
# is worth making and *which* half of a stale cache entry actually gets
# overwritten with the fresh response, not two separate requests.
ARTIST_STATS_MAX_AGE_SECONDS = 1 * 86400
ARTIST_BIO_MAX_AGE_SECONDS = 90 * 86400
ORG_NAME = "RohanApps"
APP_NAME = "AdaptiveMusicPlayer"

# --------------------------------------------------------------------------
# Last.fm application identity.
#
# This identifies THIS APPLICATION to Last.fm, not any individual user -
# it's normal and expected for open-source scrobblers to ship these in
# public source (Quod Libet, Clementine, mpdscribble, etc. all do this).
# Per-user auth happens separately: each person authorizes via their
# browser and gets their own private session key (stored locally via
# QSettings, never committed to source).
#
# Get your own pair at: https://www.last.fm/api/account/create
# --------------------------------------------------------------------------
LASTFM_API_KEY = "903a1f689f6f7eca4c254a6efc3e7806"
LASTFM_API_SECRET = "3bf3a47d2e3c16f06c1129fb5a665b93"


# --------------------------------------------------------------------------
# Tag / cover-art helpers
# --------------------------------------------------------------------------
def extract_cover_bytes(raw_audio) -> Optional[bytes]:
    if raw_audio is None:
        return None
    try:
        for key in raw_audio.keys():
            if key.startswith("APIC"):
                return raw_audio[key].data
        if "covr" in raw_audio:
            return bytes(raw_audio["covr"][0])
        pictures = getattr(raw_audio, "pictures", None)
        if pictures:
            return pictures[0].data
    except Exception:
        pass
    return None

def read_track_tags(path: str):
    artist, album, year, cover_bytes = "Unknown Artist", None, 9999, None
    try:
        raw_audio = MutagenFile(path)
        cover_bytes = extract_cover_bytes(raw_audio)
        easy_audio = MutagenFile(path, easy=True)
        tags = easy_audio.tags if easy_audio and easy_audio.tags else {}
        if "artist" in tags:
            artist = str(tags["artist"][0])
        if "album" in tags:
            album = str(tags["album"][0])
        date_value = tags.get("date") or tags.get("originaldate")
        if date_value:
            match = re.search(r"\d{4}", str(date_value[0]))
            if match:
                year = int(match.group(0))
    except Exception:
        pass
    return artist.strip(), (album.strip() if album else None), year, cover_bytes

def read_track_title(path: str) -> Optional[str]:
    # Lighter than read_track_tags() - just the embedded title tag,
    # skipping cover art extraction (which needs a separate, heavier
    # MutagenFile(path) call) since this runs once per TRACK during a
    # scan, not once per album. Falls back to the filename (handled by
    # the caller) for files with no title tag - e.g. downloaded/renamed
    # files that were never actually tagged with one.
    try:
        easy_audio = MutagenFile(path, easy=True)
        tags = easy_audio.tags if easy_audio and easy_audio.tags else {}
        if "title" in tags:
            title = str(tags["title"][0]).strip()
            if title:
                return title
    except Exception:
        pass
    return None

# Downloaders (yt-dlp and similar) often swap characters that Windows forbids in
# filenames - < > : " / \ | ? * - for visually-similar fullwidth Unicode punctuation
# so the title can still round-trip through a filename. Most UI fonts don't ship a
# glyph for those fullwidth forms, so they render as a missing-glyph box even though
# the "real" character is right there. Map them back to normal ASCII for display.
_FILENAME_SAFE_CHAR_MAP = str.maketrans({
    "\uFF1C": "<",   # fullwidth less-than sign
    "\uFF1E": ">",   # fullwidth greater-than sign
    "\uFF1A": ":",   # fullwidth colon
    "\uFF02": "\"",  # fullwidth quotation mark
    "\uFF0F": "/",   # fullwidth solidus
    "\uFF3C": "\\",  # fullwidth reverse solidus
    "\uFF5C": "|",   # fullwidth vertical line
    "\uFF1F": "?",   # fullwidth question mark
    "\uFF0A": "*",   # fullwidth asterisk
})

def clean_track_name(filename: str) -> str:
    name, _ = os.path.splitext(filename)
    name = re.sub(r"^\d+[\s\-_.]*", "", name)
    name = name.translate(_FILENAME_SAFE_CHAR_MAP)
    return name.replace("_", " ").strip() or filename

def simplify_multi_artist_credit(artist: str, max_before_simplifying: int = 4, keep: int = 2) -> str:
    # Used for the album grid display only (not scrobbling, MPRIS, or
    # anything else that cares about the exact real credit) - a tag with
    # a long list of featured/collaborating artists was a common cause of
    # album cards needing to wrap/truncate at all, so simplifying it here
    # reduces how often that's ever needed in the first place.
    #
    # "feat."/"ft."/"featuring" reliably marks a guest credit rather than
    # being part of the main artist's own name, so it's always safe to
    # treat as a separate name. Splitting on commas/slashes/semicolons/
    # ampersands too, but only actually simplifying once there are QUITE
    # a few segments (more than max_before_simplifying) - a single comma
    # or ampersand is more likely to just be part of one band's own name
    # (e.g. "Earth, Wind & Fire") than a genuine list, so this stays
    # deliberately conservative about how eagerly it splits.
    if not artist:
        return artist
    parts = re.split(r"\s*(?:,|/|;|&|\bfeat\.?\b|\bfeaturing\b|\bft\.?\b)\s*", artist, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > max_before_simplifying:
        return ", ".join(parts[:keep])
    return artist

# \b before the group, not just after - without a LEADING boundary too,
# "ft" (or "feat") matches as a mere substring anywhere it's followed by a
# word boundary, not just when it's actually its own standalone word. That
# silently truncated any artist name ending in "ft" - "Taylor Swift" became
# "Taylor Swi", "Microsoft" became "Microso", "Croft" became "Cro" - since
# the end of the string right after "...ft" also satisfies a trailing \b.
_FEATURED_CREDIT_SPLIT_RE = re.compile(r"\s*[,\(\[]*\s*\b(?:feat\.?|featuring|ft\.?)\b", re.IGNORECASE)

def primary_artist_name(artist: str) -> str:
    # Buckets a featured-artist credit ("Baby Keem feat. Brent Faiyaz")
    # under the main artist ("Baby Keem") - used for Artist view grouping
    # specifically. Deliberately narrower than simplify_multi_artist_credit()
    # above (display-only, and conservative about splitting on plain
    # commas/ampersands too, since those are often just part of one act's
    # own name - "Earth, Wind & Fire" being the classic example) - this
    # only ever splits on an explicit "feat."/"featuring"/"ft." credit,
    # which reliably marks a guest rather than co-billing, so it's always
    # safe to group under regardless of how many segments there are.
    if not artist:
        return artist
    primary = _FEATURED_CREDIT_SPLIT_RE.split(artist, maxsplit=1)[0].strip()
    return primary or artist.strip()

_TRACK_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

def normalize_track_title(title: str) -> str:
    # Used to match a track name from Last.fm's global top-tracks list
    # against this person's own local library - the two rarely agree on
    # punctuation/casing/parenthetical tags exactly ("Happier" vs
    # "Happier (Bonus Track)", curly vs straight apostrophes, etc.), so
    # comparing on a stripped-down "letters and numbers only" form is far
    # more forgiving than an exact string match while still being
    # specific enough not to conflate two different songs.
    if not title:
        return ""
    stripped = re.sub(r"\(.*?\)|\[.*?\]", "", title.lower())
    return _TRACK_TITLE_NORMALIZE_RE.sub("", stripped)

def format_time(ms: int) -> str:
    seconds = int((ms / 1000) % 60)
    minutes = int(ms / 60000)
    return f"{minutes}:{seconds:02d}"


def format_stat_count(raw: str) -> str:
    # Last.fm's stats come back as plain digit strings ("3300000") -
    # rendered compactly ("3.3M") to match how Last.fm's own site shows
    # them, rather than a long unbroken run of digits.
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class ClickAnywhereSliderStyle(QProxyStyle):
    # By default, QSlider only starts a drag if you click exactly on the
    # handle - clicking elsewhere on the groove just jumps the handle to
    # that spot, but doesn't let you continue dragging from there. This
    # makes a left-click anywhere on the slider both jump AND immediately
    # grab the handle for dragging, like most volume/seek sliders behave.
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return int(Qt.MouseButton.LeftButton.value)
        return super().styleHint(hint, option, widget, returnData)



# --------------------------------------------------------------------------
# Background Worker Engines (Scanner & Last.FM)
# --------------------------------------------------------------------------
import json
import time
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from PyQt6.QtCore import QObject, QThread, pyqtSignal, QSettings

class LibraryScanner(QObject):
    progress = pyqtSignal(int)

    def __init__(self, *args, **kwargs):
        super().__init__()
        # ... Put the rest of your original library scanner __init__ code here ...


class LastfmWorker(QThread):
    status_msg = pyqtSignal(str)
    task_finished = pyqtSignal(bool, str)

    def __init__(self, method_type, params, secret, session_key=None):
        super().__init__()
        self.method_type = method_type  # "scrobble", "nowplaying", etc.
        self.initial_params = params if isinstance(params, dict) else {}
        self.secret = secret
        self.session_key = session_key

    def run(self):
        settings = QSettings("RohanApps", "AdaptiveMusicPlayer")
        api_key = LASTFM_API_KEY
        api_secret = LASTFM_API_SECRET
        saved_sk = settings.value("lfm_sk", "").strip()

        if not api_key or not api_secret or api_secret == "PUT_YOUR_LASTFM_API_SECRET_HERE":
            self.task_finished.emit(False, "Missing Last.fm API Key or Secret in source configuration.")
            return

        params = self.initial_params.copy()
        params["api_key"] = api_key
        params["format"] = "json"

        # --- Map the API method to a friendly action name for logging/dispatch ---
        method = params.get("method", "")
        action_map = {
            "auth.getToken": "get_token",
            "auth.getSession": "get_session",
            "auth.getMobileSession": "auth",
            "track.updateNowPlaying": "now_playing",
            "track.scrobble": "scrobble",
        }
        action_name = action_map.get(method, self.method_type)

        # Only calls that act on behalf of an already-authorized user need a session key.
        # get_token / get_session are part of establishing that session in the first place.
        if action_name in ("now_playing", "scrobble") and "sk" not in params and saved_sk:
            params["sk"] = saved_sk

        # Generate API signature protocols: sort, concatenate, append secret.
        # CRITICAL: Exclude 'format' and 'callback' from the signature calculation!
        sig_params = {k: v for k, v in params.items() if k not in ["format", "callback"]}
        sig_str = "".join(f"{k}{sig_params[k]}" for k in sorted(sig_params.keys())) + api_secret
        params["api_sig"] = hashlib.md5(sig_str.encode("utf-8")).hexdigest()

        try:
            data = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request("https://ws.audioscrobbler.com/2.0/", data=data)
            req.add_header("User-Agent", "AdaptiveMusicPlayer/2.0")

            with urllib.request.urlopen(req, timeout=6) as response:
                resp_data = json.loads(response.read().decode("utf-8"))

                if "error" in resp_data:
                    error_msg = resp_data.get("message", "API Error")
                    print(f"[Last.fm Error] {action_name} failed: {error_msg}")
                    self.task_finished.emit(False, error_msg)
                else:
                    if action_name == "get_token":
                        token = resp_data.get("token", "")
                        if token:
                            print("[Last.fm Log] Request token obtained.")
                            self.task_finished.emit(True, token)
                        else:
                            self.task_finished.emit(False, "Failed to obtain a request token.")
                    elif action_name in ("auth", "get_session"):
                        sk = resp_data.get("session", {}).get("key", "")
                        if sk:
                            print("[Last.fm Log] Session key received and verified successfully!")
                            self.task_finished.emit(True, sk)
                        else:
                            self.task_finished.emit(False, "Failed to parse session key.")
                    else:
                        print(f"[Last.fm Log] Successfully executed API call: {action_name}")
                        self.task_finished.emit(True, action_name)

        except urllib.error.HTTPError as e:
            try:
                error_body = json.loads(e.read().decode("utf-8"))
                error_msg = error_body.get("message", f"HTTP {e.code}: {e.reason}")
                error_code = error_body.get("error", "?")
                print(f"[Last.fm Error] {action_name} failed (code {error_code}): {error_msg}")
            except Exception:
                error_msg = f"HTTP {e.code}: {e.reason}"
                print(f"[Last.fm Error] {action_name} failed: {error_msg}")
            self.task_finished.emit(False, error_msg)
        except Exception as e:
            print(f"[Last.fm Exception] Network error during {action_name}: {str(e)}")
            self.task_finished.emit(False, str(e))

# --------------------------------------------------------------------------
# Artist photo lookup for Artist view - a real photo reads much better on
# an artist tile than reusing one of their album covers (which is what
# every other "grabber" in this file, e.g. the cover-art extraction above,
# is already keyed off). Uses Deezer's public search API, which needs no
# API key/auth for this - same spirit as the Last.fm identity notice near
# LASTFM_API_KEY above, just a different, key-less service.
# --------------------------------------------------------------------------
class ArtistImageFetcher(QThread):
    image_fetched = pyqtSignal(str, bytes)  # artist name, raw image bytes

    def __init__(self, artist_names: list):
        super().__init__()
        self.artist_names = artist_names
        self._stop_requested = False

    def stop(self):
        # Best-effort - an in-flight urlopen() for the current artist
        # still runs to completion (nothing clean to interrupt it with),
        # but every artist still queued behind it is skipped. Used when
        # the library changes again before a previous run has finished,
        # so two overlapping fetch runs can't both be writing/emitting at
        # once.
        self._stop_requested = True

    def run(self):
        for name in self.artist_names:
            if self._stop_requested:
                return
            image_bytes = self._fetch_one(name)
            if image_bytes and not self._stop_requested:
                self.image_fetched.emit(name, image_bytes)

    def _fetch_one(self, name: str):
        try:
            query = urllib.parse.quote(name)
            # limit=10, not 1 - Deezer's search ranking isn't always the
            # correct artist first (this is what was showing up as
            # completely wrong photos for some artists - a same-named or
            # loosely-matched entry outranking the actual one). Asking for
            # several candidates and picking the best match ourselves
            # (exact name match, then most fans - i.e. "the one people
            # have actually heard of") is far more reliable than trusting
            # whichever one the search put first.
            search_url = f"https://api.deezer.com/search/artist?q={query}&limit=10"
            req = urllib.request.Request(search_url, headers={"User-Agent": "RoPlayer/1.0"})
            with urllib.request.urlopen(req, timeout=6) as response:
                data = json.loads(response.read().decode("utf-8"))
            results = data.get("data") or []
            if not results:
                return None

            exact_matches = [r for r in results if r.get("name", "").strip().lower() == name.strip().lower()]
            candidates = exact_matches or results
            best = max(candidates, key=lambda r: r.get("nb_fan", 0) or 0)

            # "big" (500x500), not "medium" (250x250) - this same cached
            # photo now also backs the larger artist-detail panel photo,
            # not just the small grid tile, so it needs more resolution
            # headroom before scaling starts looking soft.
            picture_url = best.get("picture_big") or best.get("picture_medium") or best.get("picture")
            if not picture_url:
                return None
            img_req = urllib.request.Request(picture_url, headers={"User-Agent": "RoPlayer/1.0"})
            with urllib.request.urlopen(img_req, timeout=6) as response:
                return response.read()
        except Exception:
            # Non-fatal by design - worst case this one artist just keeps
            # showing its generated initial-avatar placeholder, same
            # tolerance _log_play/_save_library_cache use for their own
            # best-effort I/O.
            return None

# --------------------------------------------------------------------------
# Artist bio + public listener/scrobble stats, and (optionally) top tracks
# by global scrobble count, for the Artist detail panel - both Last.fm
# artist.getinfo and artist.gettoptracks are plain read-only calls. Unlike
# LastfmWorker above (which handles the *authenticated* scrobbling
# endpoints - session key, request signing, all of it), this only ever
# needs the same api_key already used for scrobbling and nothing else, so
# it doesn't share that machinery.
# --------------------------------------------------------------------------
class ArtistInfoFetcher(QThread):
    info_fetched = pyqtSignal(str, str, str, str)  # artist name, bio text, listeners, playcount
    top_tracks_fetched = pyqtSignal(str, list)  # artist name, [{"title": str, "playcount": str}, ...]

    def __init__(self, artist_name: str, fetch_top_tracks: bool = False):
        super().__init__()
        self.artist_name = artist_name
        self.fetch_top_tracks = fetch_top_tracks

    def run(self):
        try:
            query = urllib.parse.quote(self.artist_name)
            url = (
                "https://ws.audioscrobbler.com/2.0/"
                f"?method=artist.getinfo&artist={query}&api_key={LASTFM_API_KEY}"
                "&format=json&autocorrect=1"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "RoPlayer/1.0"})
            with urllib.request.urlopen(req, timeout=6) as response:
                data = json.loads(response.read().decode("utf-8"))
            artist_data = data.get("artist")
            if artist_data:
                stats = artist_data.get("stats", {}) or {}
                listeners = stats.get("listeners", "") or ""
                playcount = stats.get("playcount", "") or ""
                bio_html = (artist_data.get("bio", {}) or {}).get("summary", "") or ""
                bio_text = self._clean_bio(bio_html)
                self.info_fetched.emit(self.artist_name, bio_text, listeners, playcount)
        except Exception:
            # Non-fatal - the detail panel just keeps showing no bio/stats
            # for this artist, same tolerance ArtistImageFetcher._fetch_one
            # uses above for a missing/unreachable photo.
            pass

        if not self.fetch_top_tracks:
            return
        try:
            query = urllib.parse.quote(self.artist_name)
            url = (
                "https://ws.audioscrobbler.com/2.0/"
                f"?method=artist.gettoptracks&artist={query}&api_key={LASTFM_API_KEY}"
                "&format=json&autocorrect=1&limit=15"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "RoPlayer/1.0"})
            with urllib.request.urlopen(req, timeout=6) as response:
                data = json.loads(response.read().decode("utf-8"))
            raw_tracks = ((data.get("toptracks", {}) or {}).get("track", [])) or []
            tracks = [
                {"title": t.get("name", ""), "playcount": t.get("playcount", "") or ""}
                for t in raw_tracks if t.get("name")
            ]
            if tracks:
                self.top_tracks_fetched.emit(self.artist_name, tracks)
        except Exception:
            # Non-fatal, same as above - Top Songs just stays empty/stale
            # for this artist rather than the whole fetch failing.
            pass

    @staticmethod
    def _clean_bio(bio_html: str) -> str:
        # Last.fm's summary field is HTML, and always ends with a
        # "<a href=...>Read more on Last.fm</a>" sentence - strip that and
        # every other tag rather than showing raw markup in a QLabel.
        text = re.sub(r"<a[^>]*>.*?</a>", "", bio_html, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()

# Last.FM Connection Management Dialog UI
# --------------------------------------------------------------------------
class LastfmLoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Last.fm Scrobbler")
        self.setFixedSize(380, 260)
        self.settings = QSettings(ORG_NAME, APP_NAME)

        # Frameless dark design styling override
        self.setStyleSheet("""
            QDialog { background-color: #1e222b; border: 1px solid rgba(255,255,255,0.1); }
            QLabel { color: #FFFFFF; font-weight: 600; font-size: 11px; }
            QLabel#hint { color: #AAAAAA; font-weight: 400; font-size: 10px; }
            QPushButton { background-color: rgba(255,255,255,0.1); color: white; border-radius: 6px; padding: 8px; }
            QPushButton:hover { background-color: rgba(255,255,255,0.2); }
            QPushButton:disabled { color: rgba(255,255,255,0.4); }
            QCheckBox { color: #FFFFFF; font-size: 11px; }
        """)

        layout = QVBoxLayout(self)

        # --- Dynamic Status Row ---
        status_row = QHBoxLayout()
        status_title = QLabel("Status:", self)
        self.status_lbl = QLabel(self)
        self.status_lbl.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.status_lbl.setWordWrap(True)
        status_row.addWidget(status_title)
        status_row.addWidget(self.status_lbl, 1)
        layout.addLayout(status_row)

        hint = QLabel(
            "Click Connect, authorize this app on Last.fm in your browser, "
            "then come back and click Continue.", self
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # --- Browser-auth flow buttons ---
        self.connect_btn = QPushButton("Connect with Last.fm", self)
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.clicked.connect(self.start_authorization)
        layout.addWidget(self.connect_btn)

        self.confirm_btn = QPushButton("I've Authorized \u2014 Continue", self)
        self.confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_btn.setStyleSheet("background-color: #2ECC71; color: white;")
        self.confirm_btn.clicked.connect(self.confirm_authorization)
        self.confirm_btn.setVisible(False)
        layout.addWidget(self.confirm_btn)

        layout.addStretch()

        # --- Dynamic Interactive Checkbox & Logout Layout controls ---
        self.toggle_cb = QCheckBox("Enable Scrobbling", self)
        self.toggle_cb.stateChanged.connect(self.handle_toggle_changed)
        layout.addWidget(self.toggle_cb)

        self.logout_btn = QPushButton("Logout from Last.fm", self)
        self.logout_btn.setStyleSheet("background-color: #c0392b; color: white; padding: 5px;")
        self.logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_btn.clicked.connect(self.process_logout)
        layout.addWidget(self.logout_btn)

        # Build button matrix manually to control exactly when the window hides
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.button_box.rejected.connect(self.reject)
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        close_button.clicked.connect(self.reject)
        layout.addWidget(self.button_box)

        self.worker = None
        self.pending_token = None
        self._auth_in_progress = False

        # Populate initial UI constraints safely
        self.refresh_ui_state()

    def refresh_ui_state(self):
        """Updates color indicators and active states safely without breaking the layout context."""
        session_key = self.settings.value("lfm_sk", None)

        if not session_key:
            self.status_lbl.setText("Not connected")
            self.status_lbl.setStyleSheet("color: #888888; font-weight: bold;")
            self.connect_btn.setVisible(True)
            self.connect_btn.setEnabled(True)
            self.confirm_btn.setVisible(False)
            self.toggle_cb.setEnabled(False)
            self.logout_btn.setEnabled(False)
        else:
            self.connect_btn.setVisible(False)
            self.confirm_btn.setVisible(False)
            self.toggle_cb.setEnabled(True)
            self.logout_btn.setEnabled(True)

            is_enabled = self.settings.value("lfm_scrobble_enabled", "true") == "true"

            self.toggle_cb.blockSignals(True)
            self.toggle_cb.setChecked(is_enabled)
            self.toggle_cb.blockSignals(False)

            if is_enabled:
                self.status_lbl.setText("Scrobbling enabled")
                self.status_lbl.setStyleSheet("color: #2ECC71; font-weight: bold;")
            else:
                self.status_lbl.setText("Scrobbling disabled")
                self.status_lbl.setStyleSheet("color: #E74C3C; font-weight: bold;")

    def handle_toggle_changed(self, state):
        try:
            is_checked = self.toggle_cb.isChecked()
            self.settings.setValue("lfm_scrobble_enabled", "true" if is_checked else "false")
            QTimer.singleShot(0, self.refresh_ui_state)
        except Exception as e:
            print(f"Error handling toggle: {e}")

    def process_logout(self):
        self.settings.remove("lfm_sk")
        self.pending_token = None
        QTimer.singleShot(0, self.refresh_ui_state)

    def start_authorization(self):
        """Step 1: fetch a request token, then send the user to Last.fm to authorize it."""
        self.status_lbl.setText("Requesting authorization link...")
        self.status_lbl.setStyleSheet("color: #FFFFFF; font-weight: bold;")
        self.connect_btn.setEnabled(False)
        self._auth_in_progress = True

        params = {"method": "auth.getToken"}
        self.worker = LastfmWorker("get_token", params, LASTFM_API_SECRET)
        self.worker.task_finished.connect(self.on_token_received)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def on_token_received(self, success, response_data):
        self._auth_in_progress = False
        self.connect_btn.setEnabled(True)

        if success:
            self.pending_token = str(response_data).strip()
            auth_url = f"https://www.last.fm/api/auth/?api_key={LASTFM_API_KEY}&token={self.pending_token}"
            webbrowser.open(auth_url)

            self.status_lbl.setText("Authorize in your browser, then click Continue")
            self.status_lbl.setStyleSheet("color: #FFFFFF; font-weight: bold;")
            self.connect_btn.setVisible(False)
            self.confirm_btn.setVisible(True)
            self.confirm_btn.setEnabled(True)
        else:
            self.status_lbl.setText(f"Error: {response_data}")
            self.status_lbl.setStyleSheet("color: #E74C3C; font-weight: bold;")

    def confirm_authorization(self):
        """Step 2: once the user has clicked Allow on Last.fm, trade the token for a session key."""
        if not self.pending_token:
            self.status_lbl.setText("No pending authorization \u2014 click Connect again")
            self.status_lbl.setStyleSheet("color: #E74C3C; font-weight: bold;")
            return

        self.status_lbl.setText("Verifying authorization...")
        self.status_lbl.setStyleSheet("color: #FFFFFF; font-weight: bold;")
        self.confirm_btn.setEnabled(False)
        self._auth_in_progress = True

        params = {"method": "auth.getSession", "token": self.pending_token}
        self.worker = LastfmWorker("get_session", params, LASTFM_API_SECRET)
        self.worker.task_finished.connect(self.on_lastfm_finished)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def on_lastfm_finished(self, success, response_data):
        """Slot that catches the custom task_finished signal from LastfmWorker."""
        self._auth_in_progress = False
        self.confirm_btn.setEnabled(True)

        if success:
            # Save the raw session token safely
            session_key = str(response_data).strip()
            self.settings.setValue("lfm_sk", session_key)
            self.settings.setValue("lfm_scrobble_enabled", "true")
            self.pending_token = None

            self.refresh_ui_state()

            # Dismiss the dialog only now that the token is committed
            self.accept()
        else:
            # Display any server exceptions or bad inputs directly into UI
            self.status_lbl.setText(
                f"Error: {response_data}. Make sure you clicked Allow Access, then try Continue again."
            )
            self.status_lbl.setStyleSheet("color: #E74C3C; font-weight: bold;")

    def closeEvent(self, event):
        if self._auth_in_progress:
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self):
        if self._auth_in_progress:
            return
        super().reject()


# --------------------------------------------------------------------------
# Smooth Scrolling List View Helper
# --------------------------------------------------------------------------
class ControlledScrollListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_target = None
        self._scroll_animation = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        slow_delta = int(delta * 0.25)
        if abs(slow_delta) < 1 and delta != 0:
            slow_delta = 1 if delta > 0 else -1

        bar = self.verticalScrollBar()
        # Accumulate onto a running target rather than the bar's current
        # (possibly mid-animation) value, so scrolling several times in
        # quick succession keeps building smoothly on top of itself
        # instead of the animation restarting from wherever it happens to
        # currently be.
        base = self._scroll_target if self._scroll_target is not None else bar.value()
        target = max(bar.minimum(), min(bar.maximum(), base - slow_delta))
        self._scroll_target = target

        if self._scroll_animation is not None and self._scroll_animation.state() == QPropertyAnimation.State.Running:
            self._scroll_animation.stop()

        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.finished.connect(self._on_scroll_animation_finished)
        self._scroll_animation = anim
        anim.start()

    def _on_scroll_animation_finished(self):
        self._scroll_target = None


class ControlledScrollArea(QScrollArea):
    # Same smooth animated wheel-scroll as ControlledScrollListWidget above -
    # duplicated rather than shared via a mixin, since QScrollArea and
    # QListWidget don't share a convenient common base beyond
    # QAbstractScrollArea (which is what verticalScrollBar() actually comes
    # from), and PyQt multiple inheritance across two different QWidget
    # subclasses isn't worth the risk for ~20 lines of logic.
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_target = None
        self._scroll_animation = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        slow_delta = int(delta * 0.25)
        if abs(slow_delta) < 1 and delta != 0:
            slow_delta = 1 if delta > 0 else -1

        bar = self.verticalScrollBar()
        base = self._scroll_target if self._scroll_target is not None else bar.value()
        target = max(bar.minimum(), min(bar.maximum(), base - slow_delta))
        self._scroll_target = target

        if self._scroll_animation is not None and self._scroll_animation.state() == QPropertyAnimation.State.Running:
            self._scroll_animation.stop()

        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.finished.connect(self._on_scroll_animation_finished)
        self._scroll_animation = anim
        anim.start()

    def _on_scroll_animation_finished(self):
        self._scroll_target = None


class CenteredColumnWidget(QWidget):
    # Centers its content within a target maximum width by computing
    # explicit left/right margins on every resize, rather than the more
    # "normal" Qt approach of sandwiching a max-width child between two
    # stretch spacers in an outer QHBoxLayout - that relies on the child's
    # sizeHint/size-policy correctly reporting "wants to be as wide as
    # possible", which in practice wasn't producing symmetric margins
    # here. Explicit margin math is simpler to reason about and verify.
    def __init__(self, target_width: int, parent=None):
        super().__init__(parent)
        self.target_width = target_width

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_margins()

    def update_margins(self):
        layout = self.layout()
        if layout is None or self.width() <= 0:
            return
        margins = layout.contentsMargins()
        side_margin = max(20, (self.width() - self.target_width) // 2)
        layout.setContentsMargins(side_margin, margins.top(), side_margin, margins.bottom())


class CenteredGridColumnWidget(CenteredColumnWidget):
    # Same margin-centering idea as CenteredColumnWidget above, but for a
    # card grid rather than a fixed-width column of text/content - the
    # "ideal" width isn't one constant, it's however many whole columns of
    # cell_width actually fit in the current width, recomputed the same
    # way on every resize (a wider window centers a wider, more-columns
    # block, rather than being capped at some fixed max column count
    # forever). Used for Artist view specifically: tried actually
    # stretching the cards themselves to fill the row exactly instead of
    # centering the leftover space, but that meant recomputing card sizes
    # (and everything that depends on them - font sizes, the background
    # photo fetcher) on a schedule tied to widget geometry timing that
    # turned out to be genuinely unreliable. This is pure layout math with
    # no such side effects, so it can safely just recompute on every
    # resize without any of that risk.
    def __init__(self, cell_width: int, spacing: int, parent=None):
        super().__init__(target_width=cell_width, parent=parent)
        self.cell_width = cell_width
        self.spacing = spacing

    def update_margins(self):
        layout = self.layout()
        if layout is None or self.width() <= 0:
            return
        columns = max(1, (self.width() + self.spacing) // (self.cell_width + self.spacing))
        content_width = columns * self.cell_width + (columns - 1) * self.spacing
        margins = layout.contentsMargins()
        side_margin = max(0, (self.width() - content_width) // 2)
        layout.setContentsMargins(side_margin, margins.top(), side_margin, margins.bottom())


class AutoHeightIconGrid(ControlledScrollListWidget):
    # A QListWidget in icon-grid mode that sizes itself to fit exactly as
    # many rows as its content needs, instead of either scrolling (like
    # the main album_grid) or expanding to fill whatever space the layout
    # gives it. Used for the pinned-albums strip: it should only be as
    # tall as it needs to be, with the *main* library grid underneath it
    # taking all the remaining space. Column count depends on available
    # width, so the height gets recomputed on every resize - same reason
    # TwoLineHeadingLabel recomputes its wrap on resize elsewhere in this
    # file.
    def __init__(self, parent=None, max_rows: Optional[int] = None):
        super().__init__(parent)
        # Caps how tall this can grow - beyond that it scrolls internally
        # (via the wheelEvent already inherited from
        # ControlledScrollListWidget) instead of pushing the real library
        # grid further and further down as more things get pinned.
        self.max_rows = max_rows

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_height()

    def update_height(self):
        count = self.count()
        if count == 0:
            self.setFixedHeight(0)
            return

        available_width = self.viewport().width()
        if available_width <= 0:
            # Not laid out yet - this runs the first time during startup,
            # before the window has ever been shown/sized, so the real
            # width isn't known. Computing against a stale/zero width here
            # would badly undercount columns (often down to just 1),
            # wildly overcounting rows and swallowing most of the panel's
            # height until the next real resize happened to fix it -
            # defer instead of guessing wrong.
            QTimer.singleShot(0, self.update_height)
            return

        grid = self.gridSize()
        spacing = self.spacing()
        columns = max(1, (available_width + spacing) // (grid.width() + spacing))
        rows = -(-count // columns)  # ceil division, no float rounding surprises
        if self.max_rows is not None:
            rows = min(rows, self.max_rows)
        self.setFixedHeight(rows * grid.height() + spacing)

    def wheelEvent(self, event):
        # This widget is sized to exactly fit its own content (see
        # update_height above), so it normally has nothing of its own to
        # scroll - the *shared* scroll area wrapping it and album_grid
        # (see _build_album_panel) is what's actually meant to scroll.
        # ControlledScrollListWidget.wheelEvent (inherited) always accepts
        # the event though, which - with nothing to scroll - was silently
        # swallowing every wheel event over the album cards themselves
        # instead of letting it bubble up to that outer scroll area.
        #
        # Gated on max_rows being set, not just "is bar.maximum() nonzero" -
        # only a deliberately-capped grid (the pinned-albums strip) is ever
        # meant to have genuine internal overflow. An auto-height grid
        # (max_rows=None) is sized to fit ALL its content via
        # update_height(), so it should never have real overflow of its
        # own - but a few pixels of rounding mismatch between that manual
        # column/row math and Qt's own internal layout engine can still
        # leave its scrollbar with a tiny (a handful of px) nonzero range.
        # Checking bar.maximum() > bar.minimum() alone was true for that
        # leftover sliver too, so the grid was quietly consuming (and
        # animating!) a couple of invisible pixels of its own hidden-
        # scrollbar "scroll" on every wheel tick, and since it always
        # accepted the event once that fired, the event never reached the
        # real scroll area beneath it - which read on screen as "scrolls
        # about 2px, then does nothing further."
        bar = self.verticalScrollBar()
        if self.max_rows is not None and bar.maximum() > bar.minimum():
            super().wheelEvent(event)
        else:
            event.ignore()


class HorizontalShelfList(ControlledScrollListWidget):
    # A single row of cards that scrolls horizontally rather than
    # wrapping onto further rows - used for Home shelves. Wrapping (like
    # AutoHeightIconGrid, which Pinned Albums uses) doesn't read as a
    # "shelf" the way a real horizontal strip does; this is deliberately
    # a different, simpler widget rather than adding a wrap/no-wrap mode
    # to that one, since the two behave quite differently (fixed-height
    # multi-row vs. fixed-height single-row+horizontal-scroll).
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setGridSize(HOME_GRID_SIZE)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(12)
        self.setWordWrap(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(HOME_GRID_SIZE.height() + 6)
        self._page_anim = None

    def wheelEvent(self, event):
        # Deliberately does nothing - only the paging arrows (page_by)
        # move a shelf horizontally now. This used to redirect vertical
        # wheel motion into horizontal scrolling, but that meant it
        # accepted (swallowed) every wheel event over any shelf, so
        # scrolling down the Home page never reached the outer scroll
        # area wrapping the whole page - which, since shelves cover most
        # of the visible area, made it hard to scroll down at all.
        # Ignoring it here lets it propagate up to that outer scroll area
        # instead, same fix as AutoHeightIconGrid.wheelEvent uses for the
        # same underlying reason.
        event.ignore()

    def page_by(self, direction: int):
        # direction: -1 for the previous page, +1 for the next.
        bar = self.horizontalScrollBar()
        page = max(HOME_GRID_SIZE.width(), self.viewport().width())
        target = max(bar.minimum(), min(bar.maximum(), bar.value() + direction * page))

        if self._page_anim is not None and self._page_anim.state() == QPropertyAnimation.State.Running:
            self._page_anim.stop()

        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(280)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.start()
        self._page_anim = anim  # keep a reference so it isn't GC'd mid-animation


class NowPlayingListWidget(ControlledScrollListWidget):
    # Draws its own "now playing" pill highlight behind whichever track
    # row is currently playing, and bolds that row's text - instead of
    # relying on native Qt selection styling for either. QSS can't animate
    # a background-color change, so native ::item:selected could only ever
    # jump the highlight instantly; this is what lets it glide smoothly
    # down to the next track when playback advances instead.
    BASE_PIXEL_SIZE = 13  # pinned so the whole list has a known, consistent text size

    def __init__(self, parent=None):
        super().__init__(parent)
        self._now_playing_row = -1
        self._highlight_visible = False
        self._highlight_y = 0.0
        self._highlight_anim: Optional[QVariantAnimation] = None
        self._swept_rows: list[int] = []

    def clear(self):
        # A clear() means whatever row indices we were tracking no longer
        # mean anything (the list is about to be repopulated with a
        # different album's tracks) - reset so a stale highlight can't
        # carry over onto the wrong row.
        self._now_playing_row = -1
        self._highlight_visible = False
        self._swept_rows = []
        if self._highlight_anim is not None and self._highlight_anim.state() == QVariantAnimation.State.Running:
            self._highlight_anim.stop()
        super().clear()

    def _set_bold(self, row: int, is_bold: bool):
        item = self.item(row) if 0 <= row < self.count() else None
        if item is None:
            return
        font = item.font()
        font.setWeight(QFont.Weight.Bold if is_bold else QFont.Weight.Normal)
        item.setFont(font)

    def set_now_playing_row(self, row: int, animate: bool = True):
        previous_row = self._now_playing_row
        previous_item = self.item(previous_row) if 0 <= previous_row < self.count() else None
        # Capture where the highlight actually is on screen right now
        # (before anything changes) as the animation's start point, rather
        # than trusting a possibly-stale cached position.
        start_y = self.visualItemRect(previous_item).y() if previous_item is not None else None

        if self._highlight_anim is not None and self._highlight_anim.state() == QVariantAnimation.State.Running:
            self._highlight_anim.stop()

        # A previous sweep might have left a row bold partway through it
        # (e.g. skipping tracks quickly) - clear anything left over from
        # that before starting a new one, other than whatever's about to
        # become the new now-playing row.
        for r in self._swept_rows:
            if r != row:
                self._set_bold(r, False)
        self._swept_rows = []

        self._now_playing_row = row
        new_item = self.item(row) if 0 <= row < self.count() else None

        if new_item is None:
            self._set_bold(previous_row, False)
            self._highlight_visible = False
            self.viewport().update()
            return

        self.scrollToItem(new_item, QAbstractItemView.ScrollHint.EnsureVisible)
        target_y = float(self.visualItemRect(new_item).y())

        if not animate or start_y is None or previous_row == row:
            self._set_bold(previous_row, False)
            self._set_bold(row, True)
            self._highlight_y = target_y
            self._highlight_visible = True
            self.viewport().update()
            return

        # Every row between the old and new now-playing row - not just
        # those two - gets its bold moment as the highlight visually
        # sweeps across it, same rule applied consistently regardless of
        # how many tracks are being skipped over at once.
        lo, hi = (previous_row, row) if previous_row < row else (row, previous_row)
        affected_rows = [r for r in range(lo, hi + 1) if 0 <= r < self.count()]
        self._swept_rows = affected_rows

        row_height = self.visualItemRect(new_item).height() or 1
        lo_item = self.item(lo)
        lo_row_y = self.visualItemRect(lo_item).y() if lo_item is not None else start_y

        self._highlight_visible = True
        anim = QVariantAnimation(self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def _apply(t, start_y=start_y, target_y=target_y, lo=lo, hi=hi,
                   lo_row_y=lo_row_y, row_height=row_height, affected_rows=affected_rows):
            self._highlight_y = start_y + (target_y - start_y) * t
            self.viewport().update()

            # Which row is the highlight currently centered over? Bold
            # that one, un-bold every other row in the swept range - so a
            # multi-track skip lights up each row in turn on the way past,
            # the same as a single adjacent transition just does it once.
            virtual_row = lo + (self._highlight_y - lo_row_y) / row_height
            current_row = max(lo, min(hi, round(virtual_row)))

            for r in affected_rows:
                self._set_bold(r, r == current_row)

        def _on_finished():
            # Safety net so every swept row lands on its exact final
            # state even if a rounding quirk left one slightly off after
            # the last tweened frame.
            for r in affected_rows:
                self._set_bold(r, r == row)
            self._swept_rows = []

        anim.valueChanged.connect(_apply)
        anim.finished.connect(_on_finished)
        self._highlight_anim = anim
        anim.start()

    def paintEvent(self, event):
        if self._highlight_visible and 0 <= self._now_playing_row < self.count():
            item = self.item(self._now_playing_row)
            if item is not None:
                row_rect = self.visualItemRect(item)
                is_animating = (
                    self._highlight_anim is not None
                    and self._highlight_anim.state() == QVariantAnimation.State.Running
                )
                # Outside of an active transition, always draw from the
                # item's real current position (so scrolling the list keeps
                # the highlight glued to the right row) - only use the
                # animated value while actually sliding between rows.
                y = self._highlight_y if is_animating else row_rect.y()

                painter = QPainter(self.viewport())
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 36))
                highlight_rect = QRectF(row_rect.x(), y, row_rect.width(), row_rect.height())
                painter.drawRoundedRect(highlight_rect, 8, 8)
                painter.end()
        super().paintEvent(event)


class LyricsListWidget(ControlledScrollListWidget):
    # The lyrics view always scrolls to center the *currently playing*
    # line in the middle of the box - so whatever leftover space is left
    # above/below it essentially never divides evenly into whole extra
    # lines (made worse by lines that wrap to two lines, and by the
    # active line itself being visibly bigger than the rest), routinely
    # leaving a line at the very top/bottom edge only partially visible,
    # cut off mid-character.
    #
    # Fixes this by actually shrinking the box's real height to the
    # nearest whole multiple of one line's height, rather than trying to
    # visually mask/fade the ragged edge (tried and abandoned - overlay
    # widgets never reliably repositioned themselves, and a fade still
    # left a visible partial line). body.addWidget() below passes
    # AlignVCenter so the layout centers this (now smaller than its full
    # allocated row) rather than pinning it to the top.
    def __init__(self, parent=None):
        super().__init__(parent)
        self._top_overhead = 0

    def set_top_overhead(self, px: int):
        # Height of whatever sits above this box within the same page
        # (the back-button row + layout spacing) - see resize_to_whole_lines.
        self._top_overhead = px

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resize_to_whole_lines()

    def resize_to_whole_lines(self):
        # Deliberately measured from the *page's* height, not this
        # widget's own current height - once this box's height is fixed
        # below, its own size stops reflecting "how much space is
        # actually available" and becomes a moving target chasing its own
        # tail. The page's size is governed by the surrounding
        # QStackedWidget instead, independent of anything done here.
        page = self.parentWidget()
        if page is None:
            return
        available = page.height() - self._top_overhead
        if available <= 0:
            return

        unit = QFontMetrics(QFont("SF Pro Text", 16, QFont.Weight.Medium)).height() + 12  # matches ::item margin-bottom
        if unit <= 0:
            return

        usable = (available // unit) * unit
        if usable <= 0:
            return
        self.setFixedHeight(usable)


# --------------------------------------------------------------------------
# Immediate Jump & Draggable Seek Slider
# --------------------------------------------------------------------------
class JumpSeekSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            click_ratio = event.position().x() / self.width()
            val = self.minimum() + ((self.maximum() - self.minimum()) * click_ratio)
            val = max(self.minimum(), min(self.maximum(), int(val)))
            self.setValue(val)
            self.sliderMoved.emit(val)
            event.accept()
        super().mousePressEvent(event)


# --------------------------------------------------------------------------
# Manual corner resize grip
# --------------------------------------------------------------------------
class ManualSizeGrip(QWidget):
    # A from-scratch replacement for Qt's built-in QSizeGrip. That widget
    # tries to hand resizing off to the platform's native window-resize
    # support, which isn't reliably available for a frameless top-level
    # window on every Wayland/X11 compositor combination - when it isn't,
    # QSizeGrip just silently does nothing. This computes the new window
    # geometry by hand from raw mouse-drag deltas instead, so it works
    # identically regardless of what the compositor supports.
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._drag_start_mouse = None
        self._drag_start_geometry = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(255, 255, 255, 110))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        w, h = self.width(), self.height()
        for offset in (4, 8, 12):
            painter.drawLine(w - offset, h - 2, w - 2, h - offset)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            self._drag_start_mouse = event.globalPosition().toPoint()
            self._drag_start_geometry = window.geometry()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_start_mouse is None:
            return
        window = self.window()
        delta = event.globalPosition().toPoint() - self._drag_start_mouse
        new_width = max(window.minimumWidth(), self._drag_start_geometry.width() + delta.x())
        new_height = max(window.minimumHeight(), self._drag_start_geometry.height() + delta.y())
        window.resize(new_width, new_height)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_mouse = None
            self._drag_start_geometry = None
            event.accept()


# --------------------------------------------------------------------------
# Custom frameless title bar
# --------------------------------------------------------------------------
class CustomTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CustomTitleBar")
        self.setFixedHeight(46)
        self._drag_active = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window_handle = self.window().windowHandle()
            if window_handle is not None:
                window_handle.startSystemMove()
                self._drag_active = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_active = False
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
        super().mouseDoubleClickEvent(event)


# --------------------------------------------------------------------------
# Fading Stacked Widget for Page Transitions
# --------------------------------------------------------------------------
class FadingStackedWidget(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fade_duration = 250  
        self._is_animating = False
        self._old_idx = -1
        self._new_idx = -1
        
        self.current_eff = None
        self.next_eff = None
        self.anim_current = None
        self.anim_next = None

    def currentIndex(self) -> int:
        # While a crossfade is running, the *real* QStackedWidget index
        # doesn't switch over until _animation_done() (right at the end of
        # the fade) - see the comment in setCurrentIndex() below for why
        # that's deliberate for the widget swap itself. But anything that
        # asks "which page is current?" right after calling
        # setCurrentIndex() - e.g. code that switches pages and then
        # immediately populates/queries that page - means the page it just
        # asked for, not whatever was showing before the fade started.
        # Reporting the intended target here (rather than the stale old
        # value) is what that calling code actually wants.
        if self._is_animating:
            return self._new_idx
        return super().currentIndex()

    def setCurrentIndex(self, index: int):
        if self.currentIndex() == index or self._is_animating:
            return
            
        self._old_idx = self.currentIndex()
        self._new_idx = index
        self._is_animating = True
        
        old_widget = self.widget(self._old_idx)
        if old_widget:
            self.current_eff = QGraphicsOpacityEffect(old_widget)
            old_widget.setGraphicsEffect(self.current_eff)
            self.anim_current = QPropertyAnimation(self.current_eff, b"opacity")
            self.anim_current.setDuration(self.fade_duration)
            self.anim_current.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.anim_current.setStartValue(1.0)
            self.anim_current.setEndValue(0.0)
            
        new_widget = self.widget(self._new_idx)
        if new_widget:
            # QStackedLayout only ever gives real geometry to whichever page
            # is officially the *current* widget - every other page just
            # sits at whatever (essentially top-left/default) geometry it
            # had when added, until it becomes current for the first time.
            # We deliberately delay calling the real setCurrentIndex() until
            # the crossfade finishes (see _animation_done below), so the old
            # page can keep visibly fading out underneath the new one - but
            # that means a page's very first-ever fade-in shows it at that
            # stale geometry for the whole animation, then it visibly snaps
            # into place once _animation_done() finally makes it current.
            # Force it into the right spot up front so even the first time
            # looks correct.
            new_widget.setGeometry(self.rect())

            self.next_eff = QGraphicsOpacityEffect(new_widget)
            new_widget.setGraphicsEffect(self.next_eff)
            
            self.anim_next = QPropertyAnimation(self.next_eff, b"opacity")
            self.anim_next.setDuration(self.fade_duration)
            self.anim_next.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.anim_next.setStartValue(0.0)
            self.anim_next.setEndValue(1.0)
            
            new_widget.show()
            new_widget.raise_()
            
        if self.anim_current:
            self.anim_current.start()
        if self.anim_next:
            self.anim_next.finished.connect(self._animation_done)
            self.anim_next.start()
        else:
            self._animation_done()

    def _animation_done(self):
        super().setCurrentIndex(self._new_idx)
        
        old_widget = self.widget(self._old_idx)
        if old_widget:
            old_widget.setGraphicsEffect(None)
            
        new_widget = self.widget(self._new_idx)
        if new_widget:
            new_widget.setGraphicsEffect(None)
            
        self.current_eff = None
        self.next_eff = None
        self.anim_current = None
        self.anim_next = None
        self._is_animating = False


# --------------------------------------------------------------------------
# Album card widget
# --------------------------------------------------------------------------
class AlbumCardWidget(QWidget):
    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()
    rightClicked = pyqtSignal()

    def __init__(self, pixmap: QPixmap, title: str, artist: str, parent=None, card_size: QSize = CARD_SIZE, is_mix: bool = False):
        super().__init__(parent)
        self.setFixedSize(card_size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self.mode = "album"
        self.album_key: Optional[str] = None
        self.track_path: Optional[str] = None
        # Set True by refresh_home_shelves() for cards living in a Home
        # shelf - Home has no track-list panel of its own (that lives on
        # the Library page), so a single click plays immediately instead
        # of just updating an off-screen "browsing" selection. See
        # handle_card_clicked/handle_card_double_clicked.
        self.plays_immediately_on_click = False

        self._selected = False
        self._hovered = False
        self._pinned = False
        # Separate from _selected below - _selected is "you're currently
        # browsing this album" (one at a time, follows clicks), while
        # this is "this album is what's actually playing" (follows
        # active_playing_album_key, can be a totally different album
        # than whatever you're currently browsing/clicked into).
        self._now_playing = False
        self._accent = QColor("#FFFFFF")

        # Scales proportionally from the default CARD_SIZE - Home shelves
        # use a smaller card_size than Library/Pinned/search results do,
        # and everything below (cover, text width, font sizes, badge)
        # needs to shrink with it rather than staying fixed and looking
        # oversized/cramped on a smaller card.
        scale = card_size.width() / CARD_SIZE.width()
        cover_size = max(24, round(COVER_SIZE * scale))
        margin = max(4, round(7 * scale))
        title_font_px = max(9, round(11 * scale))
        artist_font_px = max(8, round(10 * scale))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(max(3, round(6 * scale)))

        self.cover_label = QLabel(self)
        self.cover_label.setFixedSize(cover_size, cover_size)
        self.cover_label.setScaledContents(True)
        self.cover_label.setPixmap(pixmap)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.cover_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Text that's too wide for the card used to just get silently
        # clipped by the layout (not wrapped - word wrap was never on -
        # just cut off). Text that fits (on one line, or wrapped onto two)
        # is centered and completely static. Text that's still too long
        # even wrapped onto two lines shows a truncated, static ("...")
        # form by default - the full text is only ever revealed via a
        # slow scroll while actually hovering this specific card, not as
        # an ambient animation running on every long title all the time.
        text_viewport_width = card_size.width() - margin * 2

        # setPixelSize(), not the QFont(family, pointSize, weight)
        # constructor - pointSize and CSS's font-size:Npx are different
        # units (points vs. pixels, not interchangeable), and passing a
        # pixel value in as if it were a point size was quietly rendering
        # every title/artist label larger than intended, which is why
        # Library's wrapping broke too even though nothing about its own
        # sizing was supposed to change.
        title_font = QFont("SF Pro Text")
        title_font.setPixelSize(title_font_px)
        title_font.setWeight(QFont.Weight.Bold)
        title_style = "color: #FFFFFF; background: transparent;"
        title_viewport, self.title_lbl = self._make_title_label(title, title_style, text_viewport_width, title_font)
        layout.addWidget(title_viewport, alignment=Qt.AlignmentFlag.AlignHCenter)

        artist_font = QFont("SF Pro Text")
        artist_font.setPixelSize(artist_font_px)
        artist_font.setWeight(QFont.Weight.Normal)
        subtitle_style = "color: rgba(255,255,255,0.5); background: transparent;"
        if is_mix:
            # A mix/playlist's "artist" slot holds a description, not a
            # real name - unlike an actual artist credit, which reads
            # fine squeezed onto one truncated line, a description needs
            # to actually be readable. Same wrap-then-shorten tiers the
            # title uses (1 line if it fits, up to 2 if it doesn't, only
            # truncated with an ellipsis if even that isn't enough room),
            # just styled to match the dimmer, non-bold subtitle look
            # instead of the title's bold white.
            subtitle_viewport, self.artist_lbl = self._make_title_label(
                artist, subtitle_style, text_viewport_width, artist_font,
            )
            layout.addWidget(subtitle_viewport, alignment=Qt.AlignmentFlag.AlignHCenter)
        else:
            self.artist_lbl = self._make_artist_label(artist, subtitle_style, text_viewport_width, artist_font)
            layout.addWidget(self.artist_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)

        # A real child widget, not something drawn in paintEvent() - cover_label
        # above is also a child widget occupying this exact corner, and
        # child widgets always paint on top of their parent's own
        # paintEvent drawing, so a badge painted there would just get
        # silently covered up by the cover art underneath it. Created last
        # (after cover_label) so normal Qt sibling stacking order puts it
        # on top; raise_() below is just extra insurance.
        badge_size = max(14, round(20 * scale))
        self._pin_badge = QLabel("\u2605", self)
        self._pin_badge.setFixedSize(badge_size, badge_size)
        self._pin_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_badge.setStyleSheet(
            f"background-color: rgba(0, 0, 0, 150); color: #FFD166; "
            f"border-radius: {badge_size // 2}px; font-size: {max(8, round(11 * scale))}pt;"
        )
        self._pin_badge.move(self.width() - badge_size - 6, 5)
        self._pin_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._pin_badge.setVisible(False)
        self._pin_badge.raise_()

        self._pulse = 0.0
        self._pulse_anim = QPropertyAnimation(self, b"pulseValue", self)
        self._pulse_anim.setDuration(260)
        self._pulse_anim.setStartValue(1.0)
        self._pulse_anim.setEndValue(0.0)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _make_artist_label(self, text: str, style: str, viewport_width: int, font: QFont) -> QLabel:
        # Simple static single line, ellipsized if it's too long to fit -
        # no animation. With simplify_multi_artist_credit() already
        # trimming down overly-long multi-artist credits before this ever
        # runs, actual overflow here should be rare.
        label = QLabel(self)
        label.setFont(font)
        label.setStyleSheet(style)
        label.setWordWrap(False)
        label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, viewport_width))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFixedWidth(viewport_width)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return label

    def _make_title_label(self, text: str, style: str, viewport_width: int, font: QFont):
        # Two tiers, in order of preference:
        #   1. Fits on one line - fully static.
        #   2. Doesn't fit on one line - wrapped onto (at most) two lines
        #      and, if even that isn't enough room, shortened word by
        #      word with an ellipsis until what's left fits. Always fully
        #      static either way - no scrolling/sliding reveal of any
        #      kind. That was tried (both as a permanent effect and as a
        #      hover-only one) and consistently caused more problems than
        #      it solved, so the full title just isn't shown in the grid
        #      for the rare title long enough to need this - it's still
        #      visible in full wherever the album is actually opened.
        #
        # Measures using the actual label that will be displayed (via its
        # own heightForWidth()), not a separate approximate probe object -
        # measuring with anything other than the exact widget that's
        # actually shown risked the two disagreeing, which is what caused
        # a few titles to wrap oddly before.
        #
        # Font set explicitly via setFont(), not embedded in the style
        # string as font-size/font-weight CSS - measuring immediately
        # afterward via fontMetrics()/heightForWidth() needs the font to
        # have unambiguously taken effect right away, and a real QFont
        # object removes any possible doubt about that the way relying on
        # Qt's stylesheet cascade to have already resolved a CSS font-size
        # by the time we measure does not. This was very likely the real
        # reason Home's much-larger font was measuring so unreliably
        # compared to Library's font size, which is close enough to
        # Qt's/the widget's own default that any such gap barely showed.
        #
        # Shared by every card size (Library, Pinned, search, and Home's
        # bigger cards) rather than each having its own copy - same
        # algorithm everywhere is what guarantees the same look
        # everywhere, rather than two implementations quietly drifting
        # apart from each other.
        label = QLabel(text)
        label.setFont(font)
        label.setStyleSheet(style)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        line_height = label.fontMetrics().height()
        # Buffer scaled proportionally to line_height rather than a flat
        # +6 - at Library's ~14px line height this computes to exactly 6
        # (identical to a flat +6, so no behavior change there at all);
        # it only grows once the font is meaningfully bigger, which is
        # what Home's cards need to avoid the fits-in-two-lines check
        # being disproportionately strict at that size.
        two_line_height = line_height * 2 + max(6, round(line_height * 0.4))

        viewport = QWidget(self)
        viewport.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label.setParent(viewport)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        if label.heightForWidth(viewport_width) <= line_height + 1:
            # Tier 1: fits on one line.
            label.setWordWrap(False)
            label.setFixedSize(viewport_width, line_height)
            viewport.setFixedSize(viewport_width, line_height)
            label.move(0, 0)
            return viewport, label

        # Tier 2: wrap onto two lines, shortening with an ellipsis first
        # if even two lines isn't enough room (checked against this same
        # real label each time, so what's finally shown is guaranteed to
        # actually fit).
        label.setFixedWidth(viewport_width)
        display_text = text
        while label.heightForWidth(viewport_width) > two_line_height + 2 and " " in display_text.strip():
            display_text = display_text.rsplit(" ", 1)[0].rstrip(",.;:- ")
            label.setText(display_text + "\u2026")

        actual_height = label.heightForWidth(viewport_width)
        label.setFixedSize(viewport_width, actual_height)
        viewport.setFixedSize(viewport_width, actual_height)
        label.move(0, 0)

        return viewport, label

    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, value: float):
        self._pulse = value
        self.update()

    pulseValue = pyqtProperty(float, fget=_get_pulse, fset=_set_pulse)

    def set_selected(self, selected: bool, accent: Optional[QColor] = None):
        self._selected = selected
        if accent is not None:
            self._accent = accent
        self.update()

    def set_pinned(self, pinned: bool):
        self._pinned = pinned
        self._pin_badge.setVisible(pinned)

    def set_now_playing(self, is_playing: bool):
        if self._now_playing == is_playing:
            return
        self._now_playing = is_playing
        self.update()

    def set_accent(self, accent: QColor):
        self._accent = accent
        if self._selected or self._now_playing:
            self.update()

    def play_click_pulse(self):
        self._pulse_anim.stop()
        self._pulse = 1.0
        self._pulse_anim.start()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hovered and not self._selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 18))
            painter.drawRoundedRect(self.rect(), 10, 10)
        if self._pulse > 0.0:
            alpha = int(70 * self._pulse)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, alpha))
            painter.drawRoundedRect(self.rect(), 10, 10)
        if self._selected or self._now_playing:
            # One ring, not two - _selected (you're browsing this album)
            # and _now_playing (this album is what's actually playing)
            # are tracked separately since they can point at different
            # albums, but when they're the same album there's no need to
            # actually draw the outline twice.
            pen = QPen(self._accent)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rect = self.rect().adjusted(1, 1, -1, -1)
            painter.drawRoundedRect(rect, 10, 10)
        painter.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_click_pulse()
            self.clicked.emit()
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.rightClicked.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class MorphingPlayPauseButton(QPushButton):
    # A single continuous glyph that morphs between a play triangle and a
    # pause icon (two bars), instead of just swapping unicode characters.
    # The button's own background/hover circle still comes from Qt/QSS as
    # normal (object name "PlayButton" is unchanged) - this class only
    # takes over painting the icon on top of it.
    #
    # The icon is two independently-animated quadrilaterals ("bars"). At
    # t=0 (paused - showing the play triangle) bar A's own four corners
    # already form the whole triangle, and bar B is fully collapsed to a
    # single point at the triangle's tip (so it's invisible). At t=1
    # (playing - showing pause) each bar's corners spread out into its own
    # vertical bar. Animating t smoothly "splits" the triangle's tip apart
    # into two bars, and merges them back the same way in reverse.
    _ICON_BOX = 24.0  # coordinates below are in a 24x24 virtual icon grid

    # Each bar is (top-left, top-right, bottom-right, bottom-left).
    _BAR_A_PLAY = [(7, 5), (18, 12), (18, 12), (7, 19)]
    _BAR_A_PAUSE = [(7, 5), (11, 5), (11, 19), (7, 19)]
    _BAR_B_PLAY = [(18, 12), (18, 12), (18, 12), (18, 12)]
    _BAR_B_PAUSE = [(13, 5), (17, 5), (17, 19), (13, 19)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("")
        self._t = 0.0  # 0 = play icon, 1 = pause icon
        self._anim: Optional[QVariantAnimation] = None

    def set_playing(self, is_playing: bool, animate: bool = True):
        target = 1.0 if is_playing else 0.0
        if self._anim is not None and self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()

        if not animate:
            self._t = target
            self.update()
            return

        anim = QVariantAnimation(self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self._t)
        anim.setEndValue(target)
        anim.valueChanged.connect(self._on_anim_value)
        self._anim = anim
        anim.start()

    def _on_anim_value(self, value):
        self._t = value
        self.update()

    @staticmethod
    def _lerp(start, end, t):
        return [(sx + (ex - sx) * t, sy + (ey - sy) * t) for (sx, sy), (ex, ey) in zip(start, end)]

    def paintEvent(self, event):
        super().paintEvent(event)  # background/hover circle, drawn via QSS

        side = min(self.width(), self.height()) * 0.42
        scale = side / self._ICON_BOX
        offset_x = (self.width() - self._ICON_BOX * scale) / 2
        offset_y = (self.height() - self._ICON_BOX * scale) / 2

        bar_a = self._lerp(self._BAR_A_PLAY, self._BAR_A_PAUSE, self._t)
        bar_b = self._lerp(self._BAR_B_PLAY, self._BAR_B_PAUSE, self._t)

        path = QPainterPath()
        for bar in (bar_a, bar_b):
            points = [QPointF(offset_x + x * scale, offset_y + y * scale) for x, y in bar]
            path.moveTo(points[0])
            for pt in points[1:]:
                path.lineTo(pt)
            path.closeSubpath()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, QColor("#000000"))
        painter.end()


class MorphingLoopButton(QPushButton):
    # Repeat/shuffle cycles through 4 modes (off / repeat playlist / repeat
    # song / shuffle). The "repeat" modes share one glyph and the shuffle
    # mode is a totally different shape (crossing arrows) - there's no
    # sensible shared vertex topology to path-morph between those two like
    # MorphingPlayPauseButton does, so instead every visual piece (the
    # on/off background pill, the loop glyph, the small "repeat one" badge,
    # and the shuffle glyph) is its own independently-animated opacity
    # channel. Changing mode crossfades all four channels toward that
    # mode's target values at once, which reads as a smooth, continuous
    # transition between every mode rather than a set of hard snaps.
    _MODE_TARGETS = {
        # mode: (pill/"on" progress, loop-glyph opacity, repeat-one badge, shuffle-glyph opacity)
        0: (0.0, 1.0, 0.0, 0.0),
        1: (1.0, 1.0, 0.0, 0.0),
        2: (1.0, 1.0, 1.0, 0.0),
        3: (1.0, 0.0, 0.0, 1.0),
    }
    _TOOLTIPS = ["Repeat off", "Repeat playlist", "Repeat song", "Shuffle"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("")
        self._mode = 0
        self._current = list(self._MODE_TARGETS[0])
        self._anim: Optional[QVariantAnimation] = None
        self.setToolTip(self._TOOLTIPS[0])

    def set_mode(self, mode: int, animate: bool = True):
        self._mode = mode
        target = self._MODE_TARGETS[mode]
        self.setToolTip(self._TOOLTIPS[mode])

        if self._anim is not None and self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()

        if not animate:
            self._current = list(target)
            self.update()
            return

        start = list(self._current)
        anim = QVariantAnimation(self)
        anim.setDuration(220)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def _apply(t, start=start, target=target):
            self._current = [s + (e - s) * t for s, e in zip(start, target)]
            self.update()

        anim.valueChanged.connect(_apply)
        self._anim = anim
        anim.start()

    def _paint_shuffle_glyph(self, painter: QPainter, color: QColor, rect: QRectF):
        size = min(rect.width(), rect.height()) * 0.75
        s = size / 24.0
        ox = rect.x() + (rect.width() - size) / 2
        oy = rect.y() + (rect.height() - size) / 2

        def pt(x, y):
            return QPointF(ox + x * s, oy + y * s)

        pen = QPen(color, max(1.3, 1.5 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Two crossing diagonal arrows
        painter.drawLine(pt(5, 7), pt(19, 17))
        painter.drawLine(pt(19, 17), pt(14.5, 17))
        painter.drawLine(pt(19, 17), pt(19, 12.5))

        painter.drawLine(pt(5, 17), pt(19, 7))
        painter.drawLine(pt(19, 7), pt(14.5, 7))
        painter.drawLine(pt(19, 7), pt(19, 11.5))

    def paintEvent(self, event):
        on_p, loop_p, badge_p, shuffle_p = self._current
        rect = QRectF(self.rect())

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background pill - always at least a faint circle so the button
        # reads as a button even at rest (matching the lyrics/mic button
        # next to it), brightening into the full "on" pill as on_p rises.
        rest_bg, on_bg = 20, 41
        rest_border, on_border = 24, 51
        bg_alpha = rest_bg + (on_bg - rest_bg) * on_p
        border_alpha = rest_border + (on_border - rest_border) * on_p

        painter.setPen(QPen(QColor(255, 255, 255, int(border_alpha)), 1))
        painter.setBrush(QColor(255, 255, 255, int(bg_alpha)))
        radius = min(rect.width(), rect.height()) / 2
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        icon_alpha = 102 + (255 - 102) * on_p  # dim gray at "off" -> full white "on"

        if loop_p > 0.003:
            font = QFont(self.font())
            # Fixed size and weight rather than animated - setPixelSize()
            # only takes whole pixels, and bold vs normal are genuinely
            # different glyph shapes with different ink bounds, so
            # animating either one meant a discrete jump partway through
            # the transition once combined with the ink-bounds centering
            # below (most noticeably right where the weight flipped).
            # Brightness (icon_alpha, animated smoothly) alone now carries
            # the on/off distinction for this glyph.
            font.setPixelSize(16)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, int(icon_alpha * loop_p)))
            # AlignCenter centers based on the font's line-height metrics,
            # not the glyph's actual visible ink - for an asymmetric symbol
            # like this one that reliably lands off-center. Center on its
            # real bounding box instead.
            glyph = "\u21bb"
            bounds = painter.fontMetrics().tightBoundingRect(glyph)
            draw_x = rect.center().x() - bounds.width() / 2 - bounds.left()
            draw_y = rect.center().y() - bounds.height() / 2 - bounds.top()
            painter.drawText(QPointF(draw_x, draw_y), glyph)

        if badge_p > 0.01:
            # "Repeat one" indicator - a small badge that pops in at the
            # bottom-right corner rather than the cramped superscript "1"
            # the old text glyph used.
            diameter = 14 * badge_p
            cx, cy = rect.width() * 0.74, rect.height() * 0.74
            badge_rect = QRectF(cx - diameter / 2, cy - diameter / 2, diameter, diameter)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int(255 * badge_p)))
            painter.drawEllipse(badge_rect)
            if badge_p > 0.5:
                badge_font = QFont(self.font())
                badge_font.setPixelSize(9)
                badge_font.setWeight(QFont.Weight.Bold)
                painter.setFont(badge_font)
                painter.setPen(QColor(0, 0, 0, int(255 * badge_p)))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "1")
            painter.setBrush(Qt.BrushStyle.NoBrush)

        if shuffle_p > 0.003:
            self._paint_shuffle_glyph(painter, QColor(255, 255, 255, int(icon_alpha * shuffle_p)), rect)

        painter.end()


# --------------------------------------------------------------------------
# MPRIS2 (Linux desktop "now playing" integration)
# --------------------------------------------------------------------------
# Lets the system show track/artist/art and respond to media keys / system
# tray "now playing" widgets, the same way Tauon, VLC, Spotify etc. do on
# Linux. Spec: https://specifications.freedesktop.org/mpris-spec/latest/
#
# Built on dbus-python + PyGObject, not PyQt6's QtDBus - see the comment
# near the import at the top of this file for why. dbus-python needs its
# own GLib main loop, which is NOT the same loop Qt runs (QApplication.exec()
# for the rest of this app) - so the whole service runs in a background
# thread with its own GLib.MainLoop(). Two consequences of that:
#   - Incoming control calls (Next/Previous/Play/Pause/...) arrive on that
#     background thread. They're relayed to the GUI thread via the
#     _MPRISBridge QObject's signals below - Qt automatically makes a
#     cross-thread signal emission a *queued* (thread-safe) delivery
#     whenever the receiving object lives on a different thread than
#     whichever thread called .emit(), which is exactly this situation.
#   - Outgoing updates (new track, volume changed, etc, happening on the
#     GUI thread) can't safely call straight into the dbus-python service's
#     methods, since those aren't thread-safe to call from outside the
#     GLib loop that owns them. GLib.idle_add() is the documented,
#     thread-safe way to schedule a callback to run *on* that loop from
#     any other thread - see _mpris_notify()/_mpris_notify_seeked() on the
#     main window.
if HAS_DBUS_PYTHON:

    class _MPRISBridge(QObject):
        nextRequested = pyqtSignal()
        previousRequested = pyqtSignal()
        pauseRequested = pyqtSignal()
        playRequested = pyqtSignal()
        playPauseRequested = pyqtSignal()
        stopRequested = pyqtSignal()
        seekRequested = pyqtSignal(int)          # microsecond offset
        setPositionRequested = pyqtSignal(int)   # absolute microseconds
        raiseRequested = pyqtSignal()
        quitRequested = pyqtSignal()
        setVolumeRequested = pyqtSignal(float)
        setLoopStatusRequested = pyqtSignal(str)
        setShuffleRequested = pyqtSignal(bool)


    class _MPRISService(dbus.service.Object):
        # Maintains its own cached property dicts (pushed to by the GUI
        # thread via apply_update(), rather than pulling live state itself)
        # - the same pattern Tauon's own t_dbus.py uses.
        def __init__(self, bus, object_path, bridge: "_MPRISBridge"):
            self.bridge = bridge
            bus_name = dbus.service.BusName("org.mpris.MediaPlayer2.roplayer", bus)
            self._bus_name = bus_name  # must be kept alive for the name registration to stick
            dbus.service.Object.__init__(self, bus, object_path, bus_name=bus_name)

            self.root_properties = {
                "CanQuit": True,
                "CanRaise": True,
                "HasTrackList": False,
                "Identity": "RoPlayer",
                "DesktopEntry": "roplayer",
                "SupportedUriSchemes": dbus.Array([dbus.String("file")], signature="s"),
                "SupportedMimeTypes": dbus.Array(
                    [dbus.String(t) for t in (
                        "audio/mpeg", "audio/flac", "audio/mp4", "audio/ogg",
                        "audio/x-wav", "audio/x-ms-wma", "audio/aac",
                    )], signature="s"
                ),
            }
            self.player_properties = {
                "PlaybackStatus": "Stopped",
                "LoopStatus": "None",
                "Rate": 1.0,
                "Shuffle": False,
                "Volume": 1.0,
                "Position": dbus.Int64(0),
                "MinimumRate": 1.0,
                "MaximumRate": 1.0,
                "CanGoNext": True,
                "CanGoPrevious": True,
                "CanPlay": True,
                "CanPause": True,
                "CanSeek": True,
                "CanControl": True,
                "Metadata": dbus.Dictionary({}, signature="sv"),
            }

        # --- org.freedesktop.DBus.Properties ---
        @dbus.service.method(dbus_interface=dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface_name, property_name):
            if interface_name == "org.mpris.MediaPlayer2":
                return self.root_properties[property_name]
            if interface_name == "org.mpris.MediaPlayer2.Player":
                return self.player_properties[property_name]
            raise dbus.exceptions.DBusException("Unknown interface: " + interface_name)

        @dbus.service.method(dbus_interface=dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface_name):
            if interface_name == "org.mpris.MediaPlayer2":
                return self.root_properties
            if interface_name == "org.mpris.MediaPlayer2.Player":
                return self.player_properties
            raise dbus.exceptions.DBusException("Unknown interface: " + interface_name)

        @dbus.service.method(dbus_interface=dbus.PROPERTIES_IFACE, in_signature="ssv")
        def Set(self, interface_name, property_name, value):
            if interface_name != "org.mpris.MediaPlayer2.Player":
                return
            if property_name == "Volume":
                self.bridge.setVolumeRequested.emit(float(value))
            elif property_name == "LoopStatus":
                self.bridge.setLoopStatusRequested.emit(str(value))
            elif property_name == "Shuffle":
                self.bridge.setShuffleRequested.emit(bool(value))

        @dbus.service.signal(dbus_interface=dbus.PROPERTIES_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface_name, changed, invalidated):
            pass

        # --- org.mpris.MediaPlayer2 ---
        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2")
        def Raise(self):
            self.bridge.raiseRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2")
        def Quit(self):
            self.bridge.quitRequested.emit()

        # --- org.mpris.MediaPlayer2.Player ---
        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def Next(self):
            self.bridge.nextRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def Previous(self):
            self.bridge.previousRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def Pause(self):
            self.bridge.pauseRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def PlayPause(self):
            self.bridge.playPauseRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def Stop(self):
            self.bridge.stopRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player")
        def Play(self):
            self.bridge.playRequested.emit()

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player", in_signature="x")
        def Seek(self, offset):
            self.bridge.seekRequested.emit(int(offset))

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player", in_signature="ox")
        def SetPosition(self, track_id, position):
            self.bridge.setPositionRequested.emit(int(position))

        @dbus.service.method(dbus_interface="org.mpris.MediaPlayer2.Player", in_signature="s")
        def OpenUri(self, uri):
            pass  # RoPlayer only plays from its own scanned library

        @dbus.service.signal(dbus_interface="org.mpris.MediaPlayer2.Player", signature="x")
        def Seeked(self, position):
            pass

        # --- called via GLib.idle_add() from the GUI thread ---
        def apply_update(self, changed_root, changed_player):
            changed = {}
            for key, value in (changed_root or {}).items():
                self.root_properties[key] = value
            for key, value in (changed_player or {}).items():
                self.player_properties[key] = value
                changed[key] = value
            if changed:
                try:
                    self.PropertiesChanged("org.mpris.MediaPlayer2.Player", changed, [])
                except Exception as e:
                    print(f"[MPRIS] PropertiesChanged failed: {e}")  # Debug

        def emit_seeked(self, position_us):
            try:
                self.Seeked(dbus.Int64(int(position_us)))
            except Exception as e:
                print(f"[MPRIS] Seeked signal failed: {e}")  # Debug

        def set_position_cache(self, position_us):
            # Deliberately not sent via PropertiesChanged (see the comment
            # on _mpris_position_timer) - just keeps Get("Position") /
            # GetAll() accurate for clients that poll it directly instead
            # of calculating it locally from elapsed time.
            self.player_properties["Position"] = dbus.Int64(int(position_us))


class TwoLineHeadingLabel(QLabel):
    # Same static, no-scroll wrap-then-shorten approach used for album
    # grid card titles (see AlbumCardWidget._make_title_label): fits on
    # one line where possible, otherwise wraps onto (at most) two lines,
    # shortening word-by-word with a trailing ellipsis if even two lines
    # isn't enough room. The grid card version gets away with computing
    # this once because CARD_SIZE is a fixed width - this label lives in
    # a resizable panel, so the same measurement has to be redone on
    # every resize rather than just once at construction time.
    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._full_text = ""
        self.setWordWrap(True)
        if text:
            self.setText(text)

    def setText(self, text: str):
        self._full_text = text
        self._relayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self):
        width = self.width()
        if width <= 0:
            # Not laid out yet (e.g. text set before the widget has a
            # real size) - show the full text for now, resizeEvent will
            # redo this properly once real geometry is known.
            super().setText(self._full_text)
            return

        line_height = self.fontMetrics().height()
        two_line_height = line_height * 2 + 6  # small buffer, see AlbumCardWidget for why

        display_text = self._full_text
        super().setText(display_text)

        if self.heightForWidth(width) <= line_height + 1:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            return  # fits on one line, nothing further to do

        while self.heightForWidth(width) > two_line_height + 2 and " " in display_text.strip():
            display_text = display_text.rsplit(" ", 1)[0].rstrip(",.;:- ")
            super().setText(display_text + "\u2026")

        # Pin the height so wrapping to a second line grows the label
        # downward by exactly one line - not an unbounded amount - and
        # so it doesn't jitter as text changes between tracks/albums.
        self.setFixedHeight(two_line_height)


class TopSongRow(QWidget):
    # One row in the artist detail panel's Top Songs list - rank, title,
    # a global Last.fm playcount, and (if this track is actually in the
    # person's own library, not just popular on Last.fm) a click to play
    # it. Deliberately a plain QWidget with its own mousePressEvent rather
    # than a QPushButton - a button's built-in styling fights with wanting
    # three independently-aligned pieces of text (rank/title/count) in one
    # row rather than a single centered label.
    clicked = pyqtSignal()

    def __init__(self, rank: int, title: str, playcount_text: str, playable: bool, parent=None):
        super().__init__(parent)
        self.playable = playable
        self.setObjectName("TopSongRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(10)

        rank_label = QLabel(str(rank), self)
        rank_label.setFixedWidth(18)
        rank_label.setObjectName("TopSongRank")
        layout.addWidget(rank_label)

        title_label = QLabel(title, self)
        title_label.setObjectName("TopSongTitle" if playable else "TopSongTitleMuted")
        layout.addWidget(title_label, 1)

        count_label = QLabel(playcount_text, self)
        count_label.setObjectName("TopSongCount")
        layout.addWidget(count_label)

        if playable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def mousePressEvent(self, event):
        if self.playable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class AdaptiveMusicPlayer(QMainWindow):
    chromecast_devices_found = pyqtSignal(list)
    chromecast_scan_failed = pyqtSignal(str)
    chromecast_connected = pyqtSignal(object)
    chromecast_connect_failed = pyqtSignal(str)
    chromecast_media_status = pyqtSignal(str)
    mprisArtReady = pyqtSignal()  # fires whenever a background art write finishes
    mprisServiceReady = pyqtSignal()

    # view_stack page indices. Home/Library/Artists are real peer tabs -
    # switching between them updates active_top_level_tab (see
    # switch_top_level_tab). Showcase/Lyrics are detail views you drop
    # into and back out of, not tabs - "back" (close_current_view)
    # returns to whichever of the three tabs you were actually on, not
    # unconditionally to one fixed page.
    TAB_HOME = 0
    TAB_LIBRARY = 1
    TAB_ARTISTS = 2
    VIEW_SHOWCASE = 3
    VIEW_LYRICS = 4
    TAB_NAMES = {TAB_HOME: "Home", TAB_LIBRARY: "Library", TAB_ARTISTS: "Artists"}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RoPlayer")
        self.scrobble_timer = QTimer(self)
        self.setGeometry(100, 100, 1150, 750)  # default for first-ever launch
        self.setMinimumSize(820, 560)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        app_icon_pixmap = self._build_app_icon()
        self.setWindowIcon(QIcon(app_icon_pixmap))
        self._install_desktop_icon(app_icon_pixmap)
        self.settings = QSettings(ORG_NAME, APP_NAME)

        # Restores size/position and whether the window was
        # maximized/full-screen when it was last closed - saveGeometry()
        # captures all of that in one blob, written back out in
        # closeEvent(). Falls through to the setGeometry() default above on
        # first-ever launch (no saved value) or if the saved blob is no
        # longer valid (e.g. that monitor isn't connected anymore).
        saved_geometry = self.settings.value("window_geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)

        # --- Audio engine -----------------------------------------------
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        
        self.player.mediaStatusChanged.connect(self.handle_media_status)
        self.player.positionChanged.connect(self.update_timeline_position)
        self.player.durationChanged.connect(self.update_timeline_duration)
        self.player.errorOccurred.connect(self.handle_player_error)
        
        # Remembers the volume from last session (defaults to 30% the very
        # first time the app runs, before anything's been saved) instead of
        # always resetting to a fixed level on launch.
        try:
            self.initial_volume_pct = max(0, min(100, int(self.settings.value("volume_pct", 30))))
        except (TypeError, ValueError):
            self.initial_volume_pct = 30
        self.audio_output.setVolume(self.initial_volume_pct / 100.0)

        # Pinned albums stay at the front of the grid regardless of sort
        # order, so a favorite never ends up buried a long scroll away.
        # Persisted the same way as everything else in `settings`.
        try:
            self.pinned_album_keys: list[str] = json.loads(self.settings.value("pinned_albums", "[]") or "[]")
            if not isinstance(self.pinned_album_keys, list):
                self.pinned_album_keys = []
        except Exception:
            self.pinned_album_keys = []

        # Resume-last-session state - see _save_resume_state() (writes it)
        # and _try_resume_last_session() (reads it back on launch).
        # Position is saved periodically while playing (this timer) and
        # immediately on pause/close, rather than only on a clean exit -
        # a crash or force-quit shouldn't lose more than ~10s of progress.
        self._resume_attempted = False
        self._pending_resume_position_ms = 0
        self.resume_save_timer = QTimer(self)
        self.resume_save_timer.setInterval(10000)
        self.resume_save_timer.timeout.connect(self._save_resume_state)

        # --- State ---------------------------------------------------------
        # Combined repeat/shuffle button state: 0=off, 1=repeat playlist, 2=repeat song, 3=shuffle
        self.playback_mode = 0
        self.user_is_dragging_slider = False
        self.music_folder = ""
        
        self.album_tracks: dict[str, list[str]] = {}
        # path -> the real, scanned album_key it belongs to (never a
        # synthetic "__mix__..." key) - built alongside album_tracks in
        # on_scan_complete(). Exists so a play can always be attributed to
        # its actual album even when it was pressed play on from a smart
        # mix shelf (Replay Mix, Morning Mix, etc.), whose own key would
        # otherwise get logged instead - see _maybe_log_play().
        self.track_to_album_key: dict[str, str] = {}
        self.album_display_meta: dict[str, dict] = {}
        self.sorted_album_keys: list[str] = []
        
        self.browsing_tracks: list[str] = []
        self.browsing_album_key: Optional[str] = None
        self.grid_mode = "album"
        # Set while the Library page's album grid is showing one
        # artist's albums only (via Artist view -> click an artist), so
        # it can be restored/cleared correctly (see
        # filter_library_by_artist/clear_artist_filter). None the rest
        # of the time.
        self.artist_filter_name: Optional[str] = None
        # normalize_track_title(title) -> (album_key, track_path) for the
        # artist currently showing in the detail panel - rebuilt fresh
        # each time by filter_library_by_artist(); see
        # _render_artist_top_songs.
        self._artist_top_songs_track_index: dict = {}
        # The background ArtistImageFetcher currently populating Artist
        # view's tiles, if one is running - see _kick_off_artist_image_fetch().
        self._artist_image_fetcher: Optional[ArtistImageFetcher] = None
        # A fetcher that's been told to stop() (because a newer one is
        # replacing it) but may still be mid-request for its current
        # artist - stop() just sets a flag checked between artists, not
        # an instant interrupt, so dropping the only Python reference to
        # a QThread while its thread is still actually running is unsafe.
        # Held here until its own `finished` signal confirms run() has
        # actually returned - see _kick_off_artist_image_fetch().
        self._retiring_artist_fetchers: list = []
        # The background ArtistInfoFetcher currently populating the
        # artist detail panel's bio/stats, if one is running - see
        # _kick_off_artist_info_fetch(). Same retiring-list treatment as
        # _retiring_artist_fetchers above, for the same reason.
        self._artist_info_fetcher: Optional[ArtistInfoFetcher] = None
        self._retiring_artist_info_fetchers: list = []
        # Which top-level tab "back" (close_current_view) returns to after
        # a detour into Showcase/Lyrics, and the tab the app opens on at
        # launch.
        self.active_top_level_tab = self.TAB_HOME
        # A pinned album has two on-screen card widgets (one in the
        # pinned strip, one in its normal alphabetical spot) - both
        # should show the selection highlight together, so this tracks
        # every currently-selected widget, not just one.
        self.selected_cards: list[AlbumCardWidget] = []
        self._current_accent = QColor("#FFFFFF")
        
        self.active_playing_tracks: list[str] = []
        self.active_playing_album_key: Optional[str] = None
        # Cache for the "now playing" outline - which cards currently have
        # it (self._now_playing_card_widgets) and which album key that
        # cache reflects (self._now_playing_outline_key), so
        # _refresh_now_playing_outlines() can skip its full grid walk
        # whenever the playing album hasn't actually changed. See that
        # method for why this matters.
        self._now_playing_card_widgets: list[AlbumCardWidget] = []
        self._now_playing_outline_key: Optional[str] = None
        self.current_track_index = -1
        self.track_titles: dict[str, str] = {}
        
        # Lyric Timestamps State
        self.lyric_timestamps: list[int] = []  
        self.last_active_lyric_index = -1
        self.last_scrolled_lyric_index = -1
        # (timestamp, text) pairs for the current track, parsed once per
        # track change rather than once per lyrics-view-open - see
        # _load_lyrics_for_track(). Lets sync_lyrics_scroll() keep tracking
        # the active line continuously even while the lyrics view is
        # closed, so opening it doesn't need to scroll up from scratch.
        self._parsed_lyric_lines: list[tuple[int, str]] = []
        self._lyrics_have_real_data = False
        
        # Smooth Lyric Animation handle
        self.lyric_scroll_anim = None
        self.lyric_item_anims = {}  # lyric line index -> QVariantAnimation, for the bold/fade highlight transition
        
        self.is_playing = False
        self.scanner: Optional[LibraryScanner] = None

        # --- Last.fm Tracker Variables --------------------------------------
        self.lfm_async_threads = []
        self.current_scrobbled = False  # Track if the current track play instance has scrobbled

        # --- Local play-history log ------------------------------------------
        # Backend-only for now (no UI reads from this yet) - the shared
        # foundation Recently Played, Most Played/On Repeat, and
        # recommendations will all read from later, rather than each
        # bolting on a slightly different tracker of its own. See
        # _maybe_log_play()/_log_play() for the write side and
        # get_recent_plays()/get_recently_played_albums()/
        # get_album_play_counts() for reading it back.
        self._play_logged_for_current_track = False
        # Set on every track change (see play_track_at) - lets
        # Showcase/Lyrics show a mix track's actual embedded art instead
        # of a virtual mix's generic tile, without re-reading the file
        # themselves. See _current_display_cover_bytes().
        self._current_track_cover_bytes = None

        # === CHROMECAST SUPPORT ===
        self.chromecast_device = None
        self.cast_media_controller = None
        self.chromecast_browser = None
        self.local_http_server = None
        self.local_server_thread = None
        self.local_server_port = 8010
        self.chromecast_devices_found.connect(self._show_chromecast_device_menu)
        self.chromecast_scan_failed.connect(self._handle_chromecast_scan_failure)
        self.chromecast_connected.connect(self._handle_chromecast_connected)
        self.chromecast_connect_failed.connect(self._handle_chromecast_connect_failed)
        self.chromecast_media_status.connect(self._handle_chromecast_media_status)

        # --- Chromecast lyric-sync delay compensation -----------------------
        # Our local QMediaPlayer keeps decoding (muted) alongside the cast so
        # its positionChanged signal can drive the lyric highlight/scroll -
        # but that local position is essentially "real time", while the
        # Chromecast has to fetch the file over HTTP, buffer it, and decode
        # it before any sound actually comes out of the speaker. That gap
        # means lyrics highlight noticeably ahead of what's audible.
        #
        # We periodically compare the cast device's own reported playback
        # position (MediaStatus.adjusted_current_time, which already accounts
        # for time elapsed since the receiver's last status push) against our
        # local position to estimate that gap, smooth it over a few samples,
        # and subtract it before feeding the position into sync_lyrics_scroll
        # - only for the lyric view, never for the seek bar/local playback.
        self.chromecast_lyric_delay_ms = 0
        self._chromecast_delay_timer = QTimer(self)
        self._chromecast_delay_timer.setInterval(1000)
        self._chromecast_delay_timer.timeout.connect(self._measure_chromecast_delay)

        # Debounces rapid/repeated seeks before actually telling the cast
        # device to seek - see on_timeline_released() for why.
        self._pending_cast_seek_ms = 0
        self._chromecast_seek_debounce_timer = QTimer(self)
        self._chromecast_seek_debounce_timer.setSingleShot(True)
        self._chromecast_seek_debounce_timer.timeout.connect(self._perform_debounced_chromecast_seek)
        # =========================


        self.init_ui()
        # Must run before restore_last_folder() below: resuming a saved
        # session (see on_scan_complete/_resolve_resume_target) can call
        # play_track_at() synchronously during startup, which pings MPRIS -
        # self._mpris_service has to already exist by then, not just get
        # created later.
        self._setup_mpris()
        self.restore_last_folder()

        # Restoring a maximized/full-screen window above doesn't need a
        # resize handle - set the size grip's initial visibility to match
        # whatever state we actually launched into.
        self._update_size_grip_visibility()

        # Frameless windows don't get mouse-move events for hover-only
        # (no button held) unless tracking is explicitly enabled - needed
        # so the resize cursor can update just from hovering near an edge,
        # not only while dragging. The event filter is installed on the
        # whole application (not just this window) so edge detection
        # still works even when the cursor is technically over a child
        # widget near the border, not the bare window background.
        self.setMouseTracking(True)
        self._resize_cursor_active = None
        self._resize_active_edges = Qt.Edge(0)
        self._resize_start_mouse = None
        self._resize_start_geometry = None
        QApplication.instance().installEventFilter(self)

    def init_ui(self):
        master_layout = QVBoxLayout()
        master_layout.setContentsMargins(0, 0, 0, 0)
        master_layout.setSpacing(0)

        self.title_bar = self._build_custom_title_bar()
        master_layout.addWidget(self.title_bar)

        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(20, 14, 20, 20)
        body_layout.setSpacing(14)

        self.nav_tab_bar = self._build_nav_tab_bar()
        body_layout.addWidget(self.nav_tab_bar)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14) 

        self.album_panel_widget = self._build_album_panel()
        self.track_panel_widget = self._build_track_panel()

        content_layout.addWidget(self.album_panel_widget, stretch=2)
        content_layout.addWidget(self.track_panel_widget, stretch=1)

        library_page = QWidget()
        library_page.setLayout(content_layout)

        home_page = self._build_home_view()
        artists_page = self._build_artists_view()
        self.showcase_page = self._build_showcase_view()
        self.lyrics_page = self._build_lyrics_view()

        self.view_stack = FadingStackedWidget()
        self.view_stack.addWidget(home_page)       # TAB_HOME = 0
        self.view_stack.addWidget(library_page)    # TAB_LIBRARY = 1
        self.view_stack.addWidget(artists_page)    # TAB_ARTISTS = 2
        self.view_stack.addWidget(self.showcase_page)  # VIEW_SHOWCASE = 3
        self.view_stack.addWidget(self.lyrics_page)    # VIEW_LYRICS = 4
        # The base class's setCurrentIndex, deliberately - this is the
        # startup default, not a real navigation event, so it shouldn't
        # crossfade through the Home placeholder on every single launch
        # the way FadingStackedWidget's own override would.
        QStackedWidget.setCurrentIndex(self.view_stack, self.active_top_level_tab)

        body_layout.addWidget(self.view_stack, stretch=1)
        body_layout.addWidget(self._build_transport_panel())

        master_layout.addLayout(body_layout, stretch=1)

        container = QWidget()
        container.setLayout(master_layout)
        self.setCentralWidget(container)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.toggle_play)
        QShortcut(QKeySequence(Qt.Key.Key_Tab), self, activated=self.toggle_showcase_view)
        
        self.apply_theme(QColor(18, 20, 24))

    def _load_bundled_icon_pixmap(self) -> Optional[QPixmap]:
        # When installed via the Arch package, a proper hand-designed icon
        # ships alongside the app at a known system path - prefer that
        # over the plain programmatically-drawn circle below. Also checks
        # next to the script itself, for running from source with icon.png
        # placed alongside player.py.
        candidates = [
            "/usr/share/pixmaps/roplayer.png",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                pixmap = QPixmap(candidate)
                if not pixmap.isNull():
                    return pixmap
        return None

    def _build_app_icon(self) -> QPixmap:
        bundled = self._load_bundled_icon_pixmap()
        if bundled is not None:
            return bundled

        size = 128
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        path = QPainterPath()
        path.addEllipse(0, 0, size, size)
        painter.setClipPath(path)
        painter.fillRect(0, 0, size, size, QColor(70, 78, 200))
        painter.setPen(QColor(255, 255, 255))
        font = QFont()
        font.setPixelSize(int(size * 0.5))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "\u266a")
        painter.end()
        return pixmap

    def _install_desktop_icon(self, pixmap: QPixmap):
        # roplayer.desktop (see earlier setup) currently points Icon= at a
        # generic system icon name - now that setDesktopFileName() ties
        # this window firmly to that .desktop entry, KDE's taskbar appears
        # to prefer *that* file's icon over setWindowIcon(). Installing our
        # actual icon under the standard XDG hicolor theme location lets
        # Icon=roplayer resolve to it - written fresh on every launch so it
        # always matches whatever this method currently draws.
        try:
            icon_dir = os.path.expanduser("~/.local/share/icons/hicolor/128x128/apps")
            os.makedirs(icon_dir, exist_ok=True)
            pixmap.save(os.path.join(icon_dir, "roplayer.png"), "PNG")
        except Exception as e:
            print(f"[Icon] Could not install desktop icon: {e}")  # Debug

    def _build_mic_icon(self, color: QColor, size: int = 22) -> QIcon:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        s = size / 24.0  # design on a 24x24 grid, then scale

        # Mic capsule (head)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        capsule = QRectF(9 * s, 3 * s, 6 * s, 11 * s)
        painter.drawRoundedRect(capsule, 3 * s, 3 * s)

        # Stand (the U-shaped cage below the capsule) + stem + base
        pen = QPen(color, max(1.4, 1.6 * s))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        stand_rect = QRectF(6 * s, 9 * s, 12 * s, 10 * s)
        painter.drawArc(stand_rect, 180 * 16, 180 * 16)

        painter.drawLine(QPointF(12 * s, 15 * s), QPointF(12 * s, 19 * s))
        painter.drawLine(QPointF(8.5 * s, 20.5 * s), QPointF(15.5 * s, 20.5 * s))

        painter.end()
        return QIcon(pixmap)

    def _build_custom_title_bar(self) -> QWidget:
        bar = CustomTitleBar()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 6, 10, 6)
        layout.setSpacing(10)

        logo_mark = QLabel("\u266a", bar)
        logo_mark.setObjectName("LogoMark")
        logo_mark.setFixedSize(26, 26)
        logo_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title_lbl = QLabel("RoPlayer<sup>\u2122</sup>", bar)
        title_lbl.setObjectName("AppTitle")
        title_lbl.setTextFormat(Qt.TextFormat.RichText)

        layout.addWidget(logo_mark)
        layout.addWidget(title_lbl)
        layout.addStretch(1)

        self.options_btn = QPushButton("Options", bar)
        self.options_btn.setObjectName("OptionsButton")
        self.options_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.options_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.options_menu = QMenu(self)
        self.choose_folder_action = QAction("Choose Library Folder...", self)
        self.choose_folder_action.triggered.connect(self.choose_folder)
        self.options_menu.addAction(self.choose_folder_action)

        # Library is loaded from a cache on launch (see restore_last_folder)
        # so startup doesn't re-scan the whole folder every time - this is
        # the manual way to pick up files added/removed/retagged on disk
        # since the last real scan.
        self.rescan_folder_action = QAction("Rescan Folder", self)
        self.rescan_folder_action.triggered.connect(self.rescan_current_folder)
        self.options_menu.addAction(self.rescan_folder_action)
        
        # New Integration Action Node
        self.lastfm_action = QAction("\ud83d\udcca Last.fm Login Setup...", self)
        self.lastfm_action.triggered.connect(self.open_lastfm_configuration)
        self.options_menu.addAction(self.lastfm_action)
        

        # === CHROMECAST MENU ITEM ===
        self.chromecast_action = QAction("🎥 Chromecast to Speaker...", self)
        self.chromecast_action.triggered.connect(self.show_chromecast_menu)
        self.options_menu.addAction(self.chromecast_action)
        # ============================



        self.options_menu.addSeparator()
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.close)
        self.options_menu.addAction(self.exit_action)
        self.options_btn.setMenu(self.options_menu)

        self.folder_label = QLabel("Library", bar)
        self.folder_label.setObjectName("FolderLabel")

        self.search_box = QLineEdit(bar)
        self.search_box.setPlaceholderText("Search (min 3 chars)...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.filter_library)
        self.search_box.setFixedWidth(180)
        self.search_box.setObjectName("SearchBox")
        self.search_box.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # Enter should "confirm and dismiss" like a normal search field -
        # otherwise the box just stays focused forever afterward (see the
        # click-outside handling in eventFilter() for the other half of
        # this), silently swallowing keys like Space that are meant to
        # hit the global play/pause shortcut instead of getting typed in.
        self.search_box.returnPressed.connect(self.search_box.clearFocus)

        layout.addWidget(self.options_btn)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.search_box)
        layout.addSpacing(16)

        self.min_btn = QPushButton("\u2013", bar)
        self.min_btn.setObjectName("WinControlButton")
        self.min_btn.setFixedSize(34, 28)
        self.min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.min_btn.clicked.connect(self.showMinimized)

        self.max_btn = QPushButton("\u25a2", bar)
        self.max_btn.setObjectName("WinControlButton")
        self.max_btn.setFixedSize(34, 28)
        self.max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.max_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.max_btn.clicked.connect(self.toggle_maximize)

        self.close_btn = QPushButton("\u00d7", bar)
        self.close_btn.setObjectName("WinCloseButton")
        self.close_btn.setFixedSize(34, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.clicked.connect(self.close)

        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)

        return bar

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ------------------------------------------------------ Top-level tabs --
    def _build_nav_tab_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.tab_buttons = {}
        for index in (self.TAB_HOME, self.TAB_LIBRARY, self.TAB_ARTISTS):
            btn = QPushButton(self.TAB_NAMES[index], bar)
            btn.setObjectName("NavTabButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setProperty("active", "1" if index == self.active_top_level_tab else "0")
            btn.clicked.connect(lambda checked=False, i=index: self.switch_top_level_tab(i))
            layout.addWidget(btn)
            self.tab_buttons[index] = btn

        layout.addStretch(1)

        self._nav_tab_bar_effect = QGraphicsOpacityEffect(bar)
        self._nav_tab_bar_effect.setOpacity(1.0)
        bar.setGraphicsEffect(self._nav_tab_bar_effect)

        return bar

    def _set_nav_tab_bar_visible(self, visible: bool):
        # Opacity, not setVisible() - setVisible removes the bar from
        # body_layout's space calculations entirely, which resizes
        # view_stack at the exact moment a Showcase/Lyrics crossfade
        # animation is starting (or ending) - two competing visual
        # changes landing on the same frame is what was making entering/
        # leaving those views feel janky instead of a clean fade. This
        # keeps the bar's layout footprint constant either way, so
        # view_stack's size never changes - only the crossfade itself is
        # visible.
        self._nav_tab_bar_effect.setOpacity(1.0 if visible else 0.0)
        self.nav_tab_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not visible)

    def switch_top_level_tab(self, index: int):
        self.active_top_level_tab = index
        self.view_stack.setCurrentIndex(index)
        self._refresh_nav_tab_buttons()
        if index == self.TAB_HOME:
            self.refresh_home_shelves()
        # Artist view isn't refreshed here on every switch, unlike Home
        # above - grouping only changes when the library itself changes
        # (a scan/rescan), which already calls refresh_artist_grid()
        # directly (see on_scan_complete). That method retries itself a
        # bounded number of times on its own if the page's real width
        # isn't knowable yet, rather than depending on some later,
        # unrelated event like a tab switch to ever correct it.

    def _refresh_nav_tab_buttons(self):
        for index, btn in self.tab_buttons.items():
            btn.setProperty("active", "1" if index == self.active_top_level_tab else "0")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _build_home_shelf(self, title: str):
        # A heading row (title + prev/next paging arrows) above a real
        # horizontally-scrolling single-row strip of cards. Returns
        # (container, grid); container is what gets added to the page
        # layout, and is what refresh_home_shelves() shows/hides based on
        # whether the shelf actually has anything in it.
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        heading = QLabel(title, container)
        heading.setObjectName("HomeShelfHeading")
        header_row.addWidget(heading)
        header_row.addStretch(1)

        prev_btn = QPushButton("\u2039", container)
        prev_btn.setObjectName("ShelfNavButton")
        prev_btn.setFixedSize(26, 26)
        prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header_row.addWidget(prev_btn)

        next_btn = QPushButton("\u203a", container)
        next_btn.setObjectName("ShelfNavButton")
        next_btn.setFixedSize(26, 26)
        next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header_row.addWidget(next_btn)

        container_layout.addLayout(header_row)

        grid = HorizontalShelfList(container)
        grid.setObjectName("HomeShelfGrid")
        container_layout.addWidget(grid)

        prev_btn.clicked.connect(lambda: grid.page_by(-1))
        next_btn.clicked.connect(lambda: grid.page_by(1))

        return container, grid

    def _build_home_view(self) -> QWidget:
        page = QWidget()
        page.setObjectName("HomePage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        # Margins computed explicitly on resize (see CenteredColumnWidget)
        # to keep the shelf column centered at a comfortable width rather
        # than left-hugging the full window with all the leftover space
        # dumped on one side. 1500px comfortably fits several more
        # full-size cards per row than the previous 1120px cap did.
        scroll_content = CenteredColumnWidget(target_width=1500, parent=page)
        self.home_scroll_content = scroll_content
        content_layout = QVBoxLayout(scroll_content)
        content_layout.setContentsMargins(20, 4, 20, 20)
        content_layout.setSpacing(26)

        # (shelf id, heading text) - refresh_home_shelves() decides what
        # actually goes in each one and hides any shelf with nothing to
        # show (e.g. Favorites before anything's ever been pinned).
        self.home_shelves = {}
        for shelf_id, title in (
            ("jump_back_in", "Jump Back In"),
            ("made_for_you", "Made For You"),
            ("recently_played", "Recently Played"),
            ("recently_added", "Recently Added"),
            ("most_played", "Most Played"),
            ("favorites", "Favorites"),
        ):
            container, grid = self._build_home_shelf(title)
            content_layout.addWidget(container)
            self.home_shelves[shelf_id] = (container, grid)

        self.home_empty_label = QLabel(
            "Nothing to show yet - play a few albums and pin your favorites, and they'll start showing up here.",
            scroll_content,
        )
        self.home_empty_label.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.5);")
        self.home_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_empty_label.setWordWrap(True)
        self.home_empty_label.setVisible(False)
        content_layout.addWidget(self.home_empty_label)

        content_layout.addStretch(1)

        scroll_area = ControlledScrollArea(page)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setWidget(scroll_content)

        page_layout.addWidget(scroll_area)
        return page

    REPLAY_MIX_KEY = "__mix__replay"
    ON_REPEAT_KEY = "__mix__on_repeat"
    FORGOTTEN_FAVORITES_KEY = "__mix__forgotten_favorites"
    NIGHT_OWL_KEY = "__mix__night_owl"
    MORNING_MIX_KEY = "__mix__morning_mix"
    WEEKEND_MIX_KEY = "__mix__weekend_mix"
    ALBUM_REWIND_KEY = "__mix__album_rewind"
    MONTH_REWIND_KEY = "__mix__month_rewind"

    # Shared cap on how many tracks any single generated smart mix can
    # hold - keeps them feeling like a curated mix rather than "your
    # entire history for this window" once play counts grow.
    MIX_TRACK_LIMIT = 50

    # Play counts below this don't really mean "used to love it" yet -
    # applies to both Forgotten Favorites (tracks) and Album Rewind
    # (albums) so a track/album played twice ever doesn't qualify just
    # because it also happens to have 0 recent plays.
    _FORGOTTEN_MIN_ALL_TIME_PLAYS = 3
    _FORGOTTEN_RECENT_WINDOW_DAYS = 30
    _FORGOTTEN_RECENT_MAX_PLAYS = 1

    # Jump Back In looks at real, recent listening (not the generated
    # mixes below) over this window to decide what counts as "recent".
    JUMP_BACK_IN_WINDOW_DAYS = 7
    JUMP_BACK_IN_LIMIT = 15
    # A track needs at least this many plays in the window to surface on
    # its own as a singular-track card (as opposed to just being part of
    # an album that's shown as a whole).
    _JUMP_BACK_IN_MIN_TRACK_PLAYS = 3
    # An album counts as "played track-by-track" if at least this large a
    # fraction of its tracks were played in the window (with a floor of
    # 3, so a 2-track single doesn't count as "the whole album" off one
    # play of each song).
    _JUMP_BACK_IN_ALBUM_MIN_FRACTION = 0.6
    _JUMP_BACK_IN_ALBUM_MIN_TRACKS = 3

    def get_jump_back_in_entries(self) -> list:
        # Builds the mixed track/album Jump Back In bar. A singular track
        # you've been playing on repeat surfaces as its own track card; an
        # album you've been working through track-by-track surfaces as an
        # album card instead. Both kinds are ranked together by recent
        # play count (ties broken by recency) so the bar reads left to
        # right as "what you're most into right now" regardless of
        # whether that's one song or a whole record.
        #
        # Returns a list of dicts: {"mode": "track"|"album", "album_key",
        # "track_path" (only for "track")}.
        since_ts = int(time.time()) - self.JUMP_BACK_IN_WINDOW_DAYS * 86400

        # album_key -> {track_path: [play ts, ...]} for real, still-
        # scanned albums only - a play logged under a synthetic mix key
        # (Replay Mix etc.) has no stable "album" to attribute it to, so
        # those are excluded here rather than recursively feeding a smart
        # mix's plays back into Jump Back In.
        album_track_plays: dict[str, dict[str, list]] = {}
        for record in self.get_recent_plays(since_ts=since_ts):
            path = record.get("path")
            album_key = record.get("album_key")
            ts = record.get("ts")
            if not path or ts is None or not os.path.exists(path):
                continue
            if not album_key or album_key.startswith("__mix__") or album_key not in self.album_tracks:
                continue
            album_track_plays.setdefault(album_key, {}).setdefault(path, []).append(ts)

        # --- Album candidates: played through track-by-track --------------
        album_candidates = []
        for album_key, per_track in album_track_plays.items():
            total_tracks = len(self.album_tracks.get(album_key, []))
            if total_tracks == 0:
                continue
            required = max(
                self._JUMP_BACK_IN_ALBUM_MIN_TRACKS,
                math.ceil(self._JUMP_BACK_IN_ALBUM_MIN_FRACTION * total_tracks),
            )
            if len(per_track) < required:
                continue
            album_candidates.append({
                "mode": "album",
                "album_key": album_key,
                "track_path": None,
                "score": sum(len(ts_list) for ts_list in per_track.values()),
                "recent_ts": max(max(ts_list) for ts_list in per_track.values()),
            })

        # --- Track candidates: one singular track played a lot ------------
        # At most one per album - otherwise a heavily-replayed album can
        # crowd the bar with several near-duplicate single-track cards in
        # a row, which is exactly what this caps.
        best_track_per_album: dict[str, tuple] = {}
        for album_key, per_track in album_track_plays.items():
            for path, ts_list in per_track.items():
                count = len(ts_list)
                if count < self._JUMP_BACK_IN_MIN_TRACK_PLAYS:
                    continue
                most_recent = max(ts_list)
                current = best_track_per_album.get(album_key)
                if current is None or (count, most_recent) > (current[1], current[2]):
                    best_track_per_album[album_key] = (path, count, most_recent)

        track_candidates = [
            {
                "mode": "track",
                "album_key": album_key,
                "track_path": path,
                "score": count,
                "recent_ts": most_recent,
            }
            for album_key, (path, count, most_recent) in best_track_per_album.items()
        ]

        combined = album_candidates + track_candidates
        combined.sort(key=lambda e: (e["score"], e["recent_ts"]), reverse=True)
        return combined[:self.JUMP_BACK_IN_LIMIT]

    def _set_mix(self, key: str, title: str, subtitle: str, style: str, track_paths: list):
        # Shared plumbing behind every synthetic "smart mix" playlist
        # (Replay Mix, On Repeat, etc.) - stored in album_tracks/
        # album_display_meta under a special __mix__ key, exactly like a
        # real scanned album, which is what lets every existing piece of
        # album-handling code (play_track_at, the track panel, scrobbling,
        # etc.) treat it correctly without any special-casing.
        self.album_tracks.pop(key, None)
        self.album_display_meta.pop(key, None)
        if not track_paths:
            return

        self.album_tracks[key] = track_paths
        self.album_display_meta[key] = {
            "title": title,
            "artist": subtitle,
            "icon": QIcon(),
            "pixmap": self.generate_mix_cover_pixmap(style=style),
            "cover_bytes": None,
            "added_ts": 0,
            "is_mix": True,
            "mix_style": style,
        }

    def refresh_replay_mix(self):
        # Deliberately track-level, not album-level - two favorite tracks
        # from two different albums both count on equal footing, unlike
        # the Most Played shelf above.
        since_ts = int(time.time()) - 14 * 86400
        track_paths = self.get_top_tracks(since_ts=since_ts, limit=self.MIX_TRACK_LIMIT)
        self._set_mix(self.REPLAY_MIX_KEY, "Replay Mix", "Your top tracks, last 2 weeks", "replay", track_paths)

    def refresh_on_repeat(self):
        # Same idea as Replay Mix but tighter - last 7 days instead of 2
        # weeks, so this reads as "what you're currently obsessed with"
        # rather than a broader recent snapshot.
        since_ts = int(time.time()) - 7 * 86400
        track_paths = self.get_top_tracks(since_ts=since_ts, limit=self.MIX_TRACK_LIMIT)
        self._set_mix(self.ON_REPEAT_KEY, "On Repeat", "What you're playing right now", "on_repeat", track_paths)

    def refresh_forgotten_favorites(self):
        # Tracks with a high all-time play count but almost none in the
        # last 30 days - a diff between an all-time and a recent-window
        # top-tracks count, no new data needed.
        all_time_counts = self.get_track_play_counts()
        recent_counts = self.get_track_play_counts(
            since_ts=int(time.time()) - self._FORGOTTEN_RECENT_WINDOW_DAYS * 86400
        )
        candidates = [
            path for path, count in all_time_counts.items()
            if count >= self._FORGOTTEN_MIN_ALL_TIME_PLAYS
            and recent_counts.get(path, 0) <= self._FORGOTTEN_RECENT_MAX_PLAYS
        ]
        candidates.sort(key=lambda p: all_time_counts[p], reverse=True)
        self._set_mix(
            self.FORGOTTEN_FAVORITES_KEY, "Forgotten Favorites",
            "Old favorites you've drifted away from", "forgotten_favorites",
            candidates[:self.MIX_TRACK_LIMIT],
        )

    def _track_counts_in_hours(self, hours: set) -> dict:
        # path -> all-time play count restricted to plays that happened
        # during one of the given hours-of-day (0-23, local time). Shared
        # by Night Owl and Morning Mix - each just passes a different
        # set of hours.
        counts: dict = {}
        for record in self.get_recent_plays():
            path = record.get("path")
            ts = record.get("ts")
            if not path or ts is None or not os.path.exists(path):
                continue
            if time.localtime(ts).tm_hour not in hours:
                continue
            counts[path] = counts.get(path, 0) + 1
        return counts

    def refresh_night_owl_mix(self):
        # Whatever you tend to reach for late at night, bucketed by
        # hour-of-day from the existing play-record timestamps.
        counts = self._track_counts_in_hours(set(range(22, 24)) | set(range(0, 5)))
        ranked = sorted(counts.keys(), key=lambda p: counts[p], reverse=True)
        self._set_mix(self.NIGHT_OWL_KEY, "Night Owl", "What you reach for late at night", "night_owl", ranked[:self.MIX_TRACK_LIMIT])

    def refresh_morning_mix(self):
        # Same idea as Night Owl, first-thing-in-the-morning hours.
        counts = self._track_counts_in_hours(set(range(5, 10)))
        ranked = sorted(counts.keys(), key=lambda p: counts[p], reverse=True)
        self._set_mix(self.MORNING_MIX_KEY, "Morning Mix", "Your first-thing-in-the-morning tracks", "morning_mix", ranked[:self.MIX_TRACK_LIMIT])

    def refresh_weekend_mix(self):
        # Same idea as Night Owl/Morning Mix, but bucketed by
        # day-of-week instead of time-of-day - tm_wday is 0=Monday, so
        # weekend is 5 (Saturday) and 6 (Sunday).
        counts: dict = {}
        for record in self.get_recent_plays():
            path = record.get("path")
            ts = record.get("ts")
            if not path or ts is None or not os.path.exists(path):
                continue
            if time.localtime(ts).tm_wday not in (5, 6):
                continue
            counts[path] = counts.get(path, 0) + 1
        ranked = sorted(counts.keys(), key=lambda p: counts[p], reverse=True)
        self._set_mix(self.WEEKEND_MIX_KEY, "Weekend Mix", "Your Saturday & Sunday soundtrack", "weekend_mix", ranked[:self.MIX_TRACK_LIMIT])

    def refresh_album_rewind(self):
        # The album-level version of Forgotten Favorites, using
        # get_album_play_counts() instead of track-level counts. The mix
        # itself is still a flat track playlist (same __mix__ machinery
        # as everything else) - just built from a handful of tracks per
        # forgotten album rather than one track at a time, so one very
        # long-forgotten album doesn't crowd out the rest of the mix.
        all_time_counts = self.get_album_play_counts()
        recent_counts = self.get_album_play_counts(
            since_ts=int(time.time()) - self._FORGOTTEN_RECENT_WINDOW_DAYS * 86400
        )
        candidates = [
            key for key, count in all_time_counts.items()
            if count >= self._FORGOTTEN_MIN_ALL_TIME_PLAYS
            and recent_counts.get(key, 0) <= self._FORGOTTEN_RECENT_MAX_PLAYS
            and key in self.album_tracks
        ]
        candidates.sort(key=lambda k: all_time_counts[k], reverse=True)
        track_paths = []
        for key in candidates[:10]:
            track_paths.extend(self.album_tracks[key][:4])
        self._set_mix(
            self.ALBUM_REWIND_KEY, "Album Rewind",
            "Whole albums you used to spin and stopped", "album_rewind",
            track_paths[:self.MIX_TRACK_LIMIT],
        )

    def _previous_month_bounds(self):
        # (since_ts, until_ts, month-name label) for the most recently
        # completed calendar month - e.g. running this in July returns
        # all of June. until_ts is exclusive, matching get_recent_plays.
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon - 1
        if month == 0:
            month, year = 12, year - 1
        since_ts = int(time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1)))
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        until_ts = int(time.mktime((next_year, next_month, 1, 0, 0, 0, 0, 0, -1)))
        label = time.strftime("%B", (year, month, 1, 0, 0, 0, 0, 0, -1))
        return since_ts, until_ts, label

    def refresh_month_rewind(self):
        # A Wrapped-style retrospective for last calendar month - just
        # windowed top-tracks aggregation using get_recent_plays' since/
        # until bounds together instead of an open-ended floor.
        since_ts, until_ts, label = self._previous_month_bounds()
        track_paths = self.get_top_tracks(since_ts=since_ts, until_ts=until_ts, limit=self.MIX_TRACK_LIMIT)
        self._set_mix(
            self.MONTH_REWIND_KEY, f"{label} Rewind",
            f"Your top tracks from {label}", "month_rewind",
            track_paths,
        )

    def _home_card_pixmap(self, meta: dict) -> QPixmap:
        # Home's cards are meaningfully bigger than Library's (see
        # HOME_CARD_SIZE) - reusing the small pre-decoded pixmap built for
        # Library-sized cards would mean upscaling it ~1.6x, which reads
        # as noticeably soft/blurry compared to a properly decoded cover
        # at the actual size it's being displayed at. cover_bytes (the
        # raw original) is already cached per-album from the scan, so
        # this just re-decodes at the right size - no disk I/O needed.
        home_cover_size = round(COVER_SIZE * (HOME_CARD_SIZE.width() / CARD_SIZE.width()))
        cover_bytes = meta.get("cover_bytes")
        if cover_bytes:
            return self.cover_bytes_to_pixmap(cover_bytes, size=home_cover_size)
        # Generated mix covers (Replay Mix etc.) have no real cover_bytes -
        # regenerated at the right size for the same reason real covers
        # are above, rather than upscaling the small one built for
        # Library-sized cards (a smooth gradient scales up fine, but the
        # glyph text on top of it turned out to show the same softness a
        # photo would).
        if meta.get("is_mix"):
            return self.generate_mix_cover_pixmap(style=meta.get("mix_style", "replay"), size=home_cover_size)
        return meta.get("pixmap")

    def refresh_home_shelves(self):
        if not hasattr(self, "home_shelves"):
            return

        if hasattr(self, "home_scroll_content"):
            self.home_scroll_content.update_margins()

        self.refresh_replay_mix()
        self.refresh_on_repeat()
        self.refresh_month_rewind()
        self.refresh_night_owl_mix()
        self.refresh_morning_mix()
        self.refresh_weekend_mix()
        self.refresh_forgotten_favorites()
        self.refresh_album_rewind()

        jump_back_in_entries = self.get_jump_back_in_entries()
        jump_back_in_album_keys = {e["album_key"] for e in jump_back_in_entries}
        recently_played_keys = [k for k in self.get_recently_played_albums(limit=12) if k not in jump_back_in_album_keys]
        # Ordered roughly "freshest/most relevant first" - each one is
        # only included if it actually had enough play history to build
        # from (see the individual refresh_* methods / _set_mix).
        candidate_mix_keys = [
            key for key in (
                self.REPLAY_MIX_KEY, self.ON_REPEAT_KEY, self.MONTH_REWIND_KEY,
                self.NIGHT_OWL_KEY, self.MORNING_MIX_KEY, self.WEEKEND_MIX_KEY,
                self.FORGOTTEN_FAVORITES_KEY, self.ALBUM_REWIND_KEY,
            )
            if key in self.album_display_meta
        ]
        # Early on (a young/small play-history log), different windows can
        # legitimately end up drawing from the exact same handful of plays
        # - e.g. "last 14 days" and "last 7 days" cover identical records,
        # or every recent play happened to land in the 5-10am bucket. Two
        # cards with literally the same tracks in the same order is just
        # confusing, so once a track-set has already been claimed by an
        # earlier (higher-priority) mix, later mixes with that exact same
        # set are dropped from the shelf rather than shown again. The data
        # itself is untouched - this only affects what's displayed here.
        seen_track_sets = []
        made_for_you_keys = []
        for key in candidate_mix_keys:
            track_set = frozenset(self.album_tracks.get(key, []))
            if track_set and track_set in seen_track_sets:
                continue
            seen_track_sets.append(track_set)
            made_for_you_keys.append(key)
        shelf_key_lists = {
            # jump_back_in is handled separately below - its entries are
            # {"mode", "album_key", "track_path"} dicts, not plain keys,
            # since it can mix real tracks and real albums.
            "made_for_you": made_for_you_keys,
            "recently_played": recently_played_keys,
            "recently_added": self.get_recently_added_albums(limit=12),
            "most_played": self.get_most_played_albums(limit=12),
            "favorites": [k for k in self.pinned_album_keys if k in self.album_display_meta],
        }

        any_visible = False

        jump_container, jump_grid = self.home_shelves["jump_back_in"]
        jump_grid.clear()
        valid_entries = [e for e in jump_back_in_entries if e["album_key"] in self.album_display_meta]
        has_content = bool(valid_entries)
        jump_container.setVisible(has_content)
        any_visible = any_visible or has_content
        for entry in valid_entries:
            meta = self.album_display_meta[entry["album_key"]]
            if entry["mode"] == "track":
                card = self._add_card(
                    entry["album_key"], "track", self.get_track_title(entry["track_path"]), meta["artist"],
                    self._home_card_pixmap(meta), track_path=entry["track_path"],
                    target_grid=jump_grid, card_size=HOME_CARD_SIZE,
                )
                # plays_immediately_on_click deliberately left False (its
                # default) - single click just selects/cues the card like
                # everywhere else on Home now, double-click actually
                # plays it (see handle_card_clicked/
                # handle_card_double_clicked/_browse_to_track).
            else:
                card = self._add_card(
                    entry["album_key"], "album", meta["title"], meta["artist"], self._home_card_pixmap(meta),
                    target_grid=jump_grid, card_size=HOME_CARD_SIZE, is_mix=meta.get("is_mix", False),
                )
                # Double-click to play here, same as Library - see the
                # shelf loop below for why this one isn't set to True.

        for shelf_id, keys in shelf_key_lists.items():
            container, grid = self.home_shelves[shelf_id]
            grid.clear()
            valid_keys = [k for k in keys if k in self.album_display_meta]
            has_content = bool(valid_keys)
            container.setVisible(has_content)
            any_visible = any_visible or has_content
            for key in valid_keys:
                meta = self.album_display_meta[key]
                card = self._add_card(
                    key, "album", meta["title"], meta["artist"], self._home_card_pixmap(meta),
                    target_grid=grid, card_size=HOME_CARD_SIZE, is_mix=meta.get("is_mix", False),
                )
                # plays_immediately_on_click deliberately left False (its
                # default) - single click just selects/browses the card
                # like Library does, double-click actually plays it (see
                # handle_card_clicked/handle_card_double_clicked). This
                # shelf used to force single-click-to-play here, which is
                # the "why did that just start playing" behavior that
                # didn't match Library's double-click convention.

        self.home_empty_label.setVisible(not any_visible)
        self._refresh_now_playing_outlines(force=True)

    def _build_artists_view(self) -> QWidget:
        page = QWidget()
        page.setObjectName("ArtistsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        heading = QLabel("Artists", page)
        heading.setObjectName("ArtistsHeading")
        layout.addWidget(heading)

        # Shown instead of the grid when the library has no albums yet
        # (matches home_empty_label's role on the Home page) - the grid
        # itself is always built (see refresh_artist_grid), just empty.
        self.artists_empty_label = QLabel(
            "Artists will show up here once your library's been scanned.", page
        )
        self.artists_empty_label.setObjectName("ArtistsEmptyLabel")
        self.artists_empty_label.setStyleSheet("font-size: 13px; color: rgba(255,255,255,0.5);")
        self.artists_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.artists_empty_label.setWordWrap(True)
        self.artists_empty_label.setVisible(False)
        layout.addWidget(self.artists_empty_label)

        # Same AlbumCardWidget component the Library/Home grids use -
        # "mode" is set to "artist" per-card in refresh_artist_grid() and
        # wired directly to handle_artist_card_clicked() below rather
        # than through handle_card_clicked/_add_card, since an artist
        # tile's click behavior (jump straight to a filtered album grid)
        # doesn't fit the play/browse semantics those share for actual
        # album and track cards.
        self.artist_grid = AutoHeightIconGrid(self)
        self.artist_grid.setObjectName("AlbumGrid")  # same visual language as the album grid
        self.artist_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.artist_grid.setGridSize(ARTIST_GRID_SIZE)
        self.artist_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.artist_grid.setMovement(QListWidget.Movement.Static)
        self.artist_grid.setSpacing(12)
        self.artist_grid.setWordWrap(False)
        self.artist_grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.artist_grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.artist_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.artist_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Centers the grid's content (however many whole columns fit)
        # within the page, splitting any leftover width evenly across
        # both margins - see CenteredGridColumnWidget. Still a plain,
        # non-scrollable wrapper as far as the scroll area below is
        # concerned (it's not itself a QAbstractScrollArea), so it
        # doesn't reintroduce the nested-scroll-area wheel issue
        # CenteredColumnWidget's sibling comment below describes.
        scroll_content = CenteredGridColumnWidget(cell_width=ARTIST_GRID_SIZE.width(), spacing=12)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.addWidget(self.artist_grid)
        # Same top-anchoring fix as _build_album_panel's scroll_layout -
        # see the comment there for why this is needed at all.
        scroll_layout.addStretch(1)

        scroll_area = ControlledScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Wrapped in scroll_content rather than handed to the scroll area
        # directly - artist_grid is itself a QAbstractScrollArea (it's a
        # QListWidget), and nesting one scroll area straight into
        # another's setWidget() breaks the "ignore, let it bubble to the
        # parent" chain AutoHeightIconGrid's wheelEvent depends on (see
        # that class) to hand scrolling off to this outer scroll area - a
        # plain, non-scrollable wrapper widget in between (exactly what
        # _build_album_panel does for album_grid) is what lets that
        # bubbling actually reach here.
        scroll_area.setWidget(scroll_content)
        layout.addWidget(scroll_area)

        return page

    def _artist_groups(self) -> list:
        # [{"name": display name, "album_keys": [...]}], alphabetical by
        # name. Grouped case-insensitively (so e.g. "kendrick lamar" and
        # "Kendrick Lamar" tag-casing variants merge into one tile rather
        # than becoming two) via primary_artist_name(), so a featured-
        # artist credit buckets under the main artist instead of becoming
        # a one-album "artist" of its own. Built fresh each call from
        # sorted_album_keys (which already excludes smart mixes - see
        # on_scan_complete) rather than cached, mirroring how
        # rebuild_album_grid() itself always re-derives from current data.
        groups: dict = {}
        for key in self.sorted_album_keys:
            meta = self.album_display_meta.get(key)
            if not meta:
                continue
            primary = primary_artist_name(meta["artist"])
            norm = primary.lower()
            bucket = groups.setdefault(norm, {"name": primary, "album_keys": []})
            bucket["album_keys"].append(key)
        return sorted(groups.values(), key=lambda g: g["name"].lower())

    def refresh_artist_grid(self):
        if not hasattr(self, "artist_grid"):
            return
        self.artist_grid.clear()
        groups = self._artist_groups()
        self.artists_empty_label.setVisible(not groups)
        for group in groups:
            name = group["name"]
            album_count = len(group["album_keys"])
            subtitle = f"{album_count} album{'s' if album_count != 1 else ''}"
            # Prefer an already-cached real photo (from a previous
            # fetch) so a returning artist doesn't flash the placeholder
            # avatar before the network catches up - only artists that
            # come up empty here get queued in _kick_off_artist_image_fetch.
            image_bytes = self._load_cached_artist_image(name)
            pixmap = self.artist_image_bytes_to_pixmap(image_bytes, name, size=ARTIST_COVER_SIZE)

            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setSizeHint(ARTIST_CARD_SIZE)
            self.artist_grid.addItem(item)

            card = AlbumCardWidget(pixmap, name, subtitle, card_size=ARTIST_CARD_SIZE)
            card.mode = "artist"
            card.album_key = name  # repurposed here as "which artist this tile is" - see _on_artist_image_fetched
            card.clicked.connect(lambda n=name: self.handle_artist_card_clicked(n))
            self.artist_grid.setItemWidget(item, card)
        self.artist_grid.update_height()
        self._kick_off_artist_image_fetch(groups)

    def handle_artist_card_clicked(self, artist_name: str):
        self.filter_library_by_artist(artist_name)
        self.switch_top_level_tab(self.TAB_LIBRARY)

    def filter_library_by_artist(self, artist_name: str):
        # Same album grid Library normally shows, just restricted to one
        # artist's albums - mirrors filter_library()'s search-mode grid
        # takeover (hide Pinned, clear + repopulate album_grid with
        # "album" mode cards) but grouped by primary_artist_name()
        # instead of a text query.
        self.pinned_heading.setVisible(False)
        self.pinned_grid.setVisible(False)
        self.album_grid.clear()
        self._clear_card_selection()
        self.grid_mode = "artist"
        self.artist_filter_name = artist_name

        # normalize_track_title(track title) -> (album_key, track_path) for
        # every track this artist actually has in the library - built
        # alongside the album cards below so _render_artist_top_songs can
        # cheaply check "is this Last.fm top track something I actually
        # own" without re-scanning album_tracks itself.
        self._artist_top_songs_track_index = {}
        norm = artist_name.lower()
        for key in self.sorted_album_keys:
            meta = self.album_display_meta.get(key)
            if not meta or primary_artist_name(meta["artist"]).lower() != norm:
                continue
            self._add_card(key, "album", meta["title"], meta["artist"], meta["pixmap"])
            for path in self.album_tracks.get(key, []):
                normalized = normalize_track_title(self.get_track_title(path))
                if normalized:
                    self._artist_top_songs_track_index[normalized] = (key, path)
        self.album_grid.update_height()
        self._refresh_now_playing_outlines(force=True)

        self.artist_filter_label.setText(artist_name)
        # Reuses whatever Artist view's own photo cache already has for
        # this artist (same cache, same fetcher - see
        # _kick_off_artist_image_fetch) rather than fetching a second time
        # - if it's not cached yet either, this shows the same generated
        # initial-avatar placeholder Artist view's tiles fall back to.
        cached_photo = self._load_cached_artist_image(artist_name)
        photo_pixmap = self.artist_image_bytes_to_pixmap(
            cached_photo, artist_name, size=ARTIST_DETAIL_PHOTO_SIZE
        )
        self.artist_detail_photo.setPixmap(photo_pixmap)

        # Blank/hidden rather than "Loading..." placeholders - these
        # populate asynchronously via _on_artist_info_fetched, and a
        # missing bio/stat isn't unusual enough (an obscure or newly-
        # added artist, Last.fm being unreachable, etc.) to need an
        # explicit "still loading" state that might just sit there
        # forever instead.
        self.artist_detail_listeners_label.setText("")
        self.artist_detail_scrobbles_label.setText("")
        self.artist_detail_bio_label.setText("")
        self._render_artist_top_songs([])

        cached_info = self._load_cached_artist_info(artist_name)
        if cached_info:
            self._apply_artist_info(
                artist_name, cached_info.get("bio", ""), cached_info.get("listeners", ""), cached_info.get("playcount", "")
            )
            self._render_artist_top_songs(cached_info.get("top_tracks", []))
        stats_stale, bio_stale, top_tracks_needed = self._artist_info_staleness(cached_info)
        if stats_stale or bio_stale or top_tracks_needed:
            self._kick_off_artist_info_fetch(
                artist_name, update_stats=stats_stale, update_bio=bio_stale, fetch_top_tracks=top_tracks_needed
            )

        self.artist_filter_banner.setVisible(True)

    def clear_artist_filter(self):
        self.artist_filter_name = None
        self.rebuild_album_grid()

    # -------------------------------------------------------- Artist photos --
    def _artist_image_cache_dir(self) -> str:
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not data_dir:
            data_dir = os.path.expanduser(os.path.join("~", ".cache", APP_NAME))
        # "_v3", not "artist_images" - bumped once already so already-
        # cached photos fetched under the old "just take Deezer's first
        # search result" logic (which is what produced completely wrong
        # photos for some artists) get transparently treated as uncached
        # and re-fetched under the corrected exact-name/most-fans
        # matching in ArtistImageFetcher._fetch_one. Bumped again here
        # since that fetcher now also asks for a higher resolution photo
        # (see the picture_big comment there) - existing v2 photos are
        # correctly-matched but lower-res, and this saves them from
        # sticking around blurry until someone manually clears the cache.
        cache_dir = os.path.join(data_dir, "artist_images_v3")
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def _artist_image_cache_path(self, artist_name: str) -> str:
        # Hashed rather than the raw name as the filename - artist names
        # can contain slashes, quotes, or other characters that aren't
        # safe as a filename on every platform.
        digest = hashlib.sha1(artist_name.lower().encode("utf-8")).hexdigest()
        return os.path.join(self._artist_image_cache_dir(), f"{digest}.jpg")

    def _load_cached_artist_image(self, artist_name: str) -> Optional[bytes]:
        try:
            with open(self._artist_image_cache_path(artist_name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def _save_cached_artist_image(self, artist_name: str, image_bytes: bytes):
        try:
            with open(self._artist_image_cache_path(artist_name), "wb") as f:
                f.write(image_bytes)
        except OSError:
            pass  # non-fatal - worst case this artist just gets re-fetched next launch

    # ---------------------------------------------------- Artist bio/stats --
    def _artist_info_cache_path(self, artist_name: str) -> str:
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not data_dir:
            data_dir = os.path.expanduser(os.path.join("~", ".cache", APP_NAME))
        cache_dir = os.path.join(data_dir, "artist_info_v1")
        os.makedirs(cache_dir, exist_ok=True)
        digest = hashlib.sha1(artist_name.lower().encode("utf-8")).hexdigest()
        return os.path.join(cache_dir, f"{digest}.json")

    def _load_cached_artist_info(self, artist_name: str) -> Optional[dict]:
        try:
            with open(self._artist_info_cache_path(artist_name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _save_cached_artist_info(self, artist_name: str, info: dict):
        try:
            with open(self._artist_info_cache_path(artist_name), "w", encoding="utf-8") as f:
                json.dump(info, f)
        except OSError:
            pass  # non-fatal - worst case this artist's bio/stats just get re-fetched next time

    def _artist_info_staleness(self, cached_info: Optional[dict]) -> tuple:
        # (stats_stale, bio_stale, top_tracks_needed) - the first two are
        # independent time-based checks, since stats and bio are allowed
        # to go stale at different rates (see
        # ARTIST_STATS_MAX_AGE_SECONDS/ARTIST_BIO_MAX_AGE_SECONDS). No
        # cache at all counts as everything being needed - there's nothing
        # yet to even consider fresh.
        #
        # top_tracks_needed isn't purely time-based like the other two -
        # it's also true whenever the cache simply has no "top_tracks" key
        # at all yet, regardless of how fresh stats otherwise are. Without
        # that, an artist whose stats were already cached before Top Songs
        # existed would just never get one fetched for it until the next
        # time stats happen to go stale on their own, which could be up to
        # a full day away.
        if not cached_info:
            return True, True, True
        now = time.time()
        stats_stale = (now - cached_info.get("stats_updated_at", 0)) > ARTIST_STATS_MAX_AGE_SECONDS
        bio_stale = (now - cached_info.get("bio_updated_at", 0)) > ARTIST_BIO_MAX_AGE_SECONDS
        top_tracks_needed = stats_stale or "top_tracks" not in cached_info
        return stats_stale, bio_stale, top_tracks_needed

    def _kick_off_artist_info_fetch(self, artist_name: str, update_stats: bool, update_bio: bool, fetch_top_tracks: bool):
        if self._artist_info_fetcher is not None:
            old_fetcher = self._artist_info_fetcher
            # Same keep-alive-until-actually-finished handling
            # _kick_off_artist_image_fetch uses for the photo fetcher - a
            # QThread whose run() might still be executing shouldn't have
            # its last Python reference dropped just because a newer
            # request superseded it.
            self._retiring_artist_info_fetchers.append(old_fetcher)
            old_fetcher.finished.connect(lambda f=old_fetcher: self._retiring_artist_info_fetchers.remove(f))
            self._artist_info_fetcher = None
        fetcher = ArtistInfoFetcher(artist_name, fetch_top_tracks=fetch_top_tracks)
        # update_stats/update_bio say which half of the cache this
        # particular request is actually allowed to overwrite once it
        # comes back - captured here rather than threaded through the
        # fetcher/signal itself, since which fields were due is a
        # decision _kick_off_artist_info_fetch's caller already made and
        # the fetcher itself doesn't need to know or care about.
        fetcher.info_fetched.connect(
            lambda name, bio, listeners, playcount, us=update_stats, ub=update_bio:
                self._on_artist_info_fetched(name, bio, listeners, playcount, us, ub)
        )
        fetcher.top_tracks_fetched.connect(self._on_artist_top_tracks_fetched)
        fetcher.finished.connect(self._on_artist_info_fetch_finished)
        self._artist_info_fetcher = fetcher
        fetcher.start()

    def _on_artist_info_fetched(self, artist_name: str, bio: str, listeners: str, playcount: str, update_stats: bool, update_bio: bool):
        # Re-loads whatever's already cached and only overlays the
        # field(s) this particular request was actually meant to refresh -
        # e.g. stats going stale (weekly) fetches this same response, but
        # shouldn't reset the bio's own 90-day clock (and overwrite it
        # with what's presumably the same text anyway) unless the bio
        # itself was also due.
        info = self._load_cached_artist_info(artist_name) or {}
        now = time.time()
        if update_stats:
            info["listeners"] = listeners
            info["playcount"] = playcount
            info["stats_updated_at"] = now
        if update_bio:
            info["bio"] = bio
            info["bio_updated_at"] = now
        self._save_cached_artist_info(artist_name, info)
        # Guards against a slow response for an artist the person has
        # since clicked away from (or past, to a different one) landing
        # on the wrong panel - only apply it if it's still the one
        # actually showing.
        if self.artist_filter_name == artist_name:
            self._apply_artist_info(artist_name, info.get("bio", ""), info.get("listeners", ""), info.get("playcount", ""))

    def _on_artist_top_tracks_fetched(self, artist_name: str, tracks: list):
        info = self._load_cached_artist_info(artist_name) or {}
        info["top_tracks"] = tracks
        self._save_cached_artist_info(artist_name, info)
        if self.artist_filter_name == artist_name:
            self._render_artist_top_songs(tracks)

    def _on_artist_info_fetch_finished(self):
        self._artist_info_fetcher = None

    def _apply_artist_info(self, artist_name: str, bio: str, listeners: str, playcount: str):
        self.artist_detail_listeners_label.setText(
            f"{format_stat_count(listeners)} listeners" if listeners else ""
        )
        self.artist_detail_scrobbles_label.setText(
            f"{format_stat_count(playcount)} scrobbles" if playcount else ""
        )
        self.artist_detail_bio_label.setText(bio)

    def _render_artist_top_songs(self, tracks: list):
        # Clears and rebuilds the row widgets from Last.fm's top-tracks
        # response, keeping only whichever ones are also actually in this
        # person's own library (see the _artist_top_songs_track_index this
        # was built alongside in filter_library_by_artist) - the whole
        # section just stays hidden if there's no overlap at all, rather
        # than showing a list of songs that can't actually be played here.
        while self.artist_top_songs_rows_layout.count():
            item = self.artist_top_songs_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        rank = 0
        for track in tracks:
            match = self._artist_top_songs_track_index.get(normalize_track_title(track.get("title", "")))
            if not match:
                continue
            rank += 1
            album_key, track_path = match
            playcount_text = f"{format_stat_count(track.get('playcount', ''))} plays"
            row = TopSongRow(rank, track.get("title", ""), playcount_text, playable=True, parent=self.artist_top_songs_section)
            row.clicked.connect(lambda ak=album_key, tp=track_path: self._browse_to_track(ak, tp, autoplay=True))
            self.artist_top_songs_rows_layout.addWidget(row)
            if rank >= 10:
                break

        self.artist_top_songs_section.setVisible(rank > 0)

    def generate_artist_avatar_pixmap(self, name: str, size: int = COVER_SIZE) -> QPixmap:
        # Placeholder shown before a real photo's been fetched (or if the
        # grabber couldn't find/reach one) - a colored circle with the
        # artist's initial. Colored via a stable hash of their name so
        # the same artist always gets the same color rather than it
        # shuffling between launches/relaunches.
        digest = hashlib.sha1(name.lower().encode("utf-8")).hexdigest()
        hue = int(digest[:8], 16) % 360
        color_a = QColor.fromHsv(hue, 130, 210)
        color_b = QColor.fromHsv((hue + 40) % 360, 150, 120)
        pixmap = QPixmap(size, size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, color_a)
        gradient.setColorAt(1.0, color_b)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRect(0, 0, size, size)
        painter.setPen(QColor(255, 255, 255, 225))
        font = QFont("SF Pro Text", int(size * 0.38), QFont.Weight.Bold)
        painter.setFont(font)
        initial = (name.strip()[:1] or "?").upper()
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, initial)
        painter.end()
        # Circular, not rounded-square - a person's photo reads as a
        # "who", which a circle communicates at a glance better than the
        # square/rounded-square every actual album cover already uses,
        # so an artist tile doesn't get mistaken for one more album.
        return self.make_pixmap_rounded(pixmap, radius=size // 2)

    def artist_image_bytes_to_pixmap(self, image_bytes: Optional[bytes], name: str, size: int = COVER_SIZE) -> QPixmap:
        if image_bytes:
            pixmap = QPixmap()
            if pixmap.loadFromData(image_bytes):
                square = pixmap.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ).copy(0, 0, size, size)
                return self.make_pixmap_rounded(square, radius=size // 2)
        return self.generate_artist_avatar_pixmap(name, size=size)

    def _kick_off_artist_image_fetch(self, groups: list):
        # Only actually reaches the network for artists that don't
        # already have a cached photo on disk - most launches after the
        # first should need to fetch nothing at all.
        if self._artist_image_fetcher is not None:
            old_fetcher = self._artist_image_fetcher
            old_fetcher.stop()
            self._retiring_artist_fetchers.append(old_fetcher)
            old_fetcher.finished.connect(lambda f=old_fetcher: self._retiring_artist_fetchers.remove(f))
            self._artist_image_fetcher = None

        names_needing_fetch = [
            g["name"] for g in groups if self._load_cached_artist_image(g["name"]) is None
        ]
        if not names_needing_fetch:
            return
        fetcher = ArtistImageFetcher(names_needing_fetch)
        fetcher.image_fetched.connect(self._on_artist_image_fetched)
        fetcher.finished.connect(self._on_artist_image_fetch_finished)
        self._artist_image_fetcher = fetcher
        fetcher.start()

    def _on_artist_image_fetched(self, name: str, image_bytes: bytes):
        self._save_cached_artist_image(name, image_bytes)
        # Update just this one tile in place, if it's still on screen -
        # rebuilding the whole grid for one photo landing would be
        # wasteful, and could fight with whatever the person's doing on
        # the page right now (e.g. mid-scroll).
        pixmap = self.artist_image_bytes_to_pixmap(image_bytes, name, size=ARTIST_COVER_SIZE)
        for i in range(self.artist_grid.count()):
            widget = self.artist_grid.itemWidget(self.artist_grid.item(i))
            if isinstance(widget, AlbumCardWidget) and widget.mode == "artist" and widget.album_key == name:
                widget.cover_label.setPixmap(pixmap)
                break
        # The (bigger) artist detail panel on Library also shows this same
        # photo - if it's currently displaying this exact artist, refresh
        # it too rather than leaving it stuck on the generated avatar
        # placeholder until the next time the filter's re-applied.
        if self.artist_filter_name == name:
            detail_pixmap = self.artist_image_bytes_to_pixmap(image_bytes, name, size=ARTIST_DETAIL_PHOTO_SIZE)
            self.artist_detail_photo.setPixmap(detail_pixmap)

    def _on_artist_image_fetch_finished(self):
        self._artist_image_fetcher = None

    def _build_album_panel(self) -> QWidget:
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # Pinned albums get their own labeled section above the main
        # library grid, rather than just being reordered to the front of
        # it - both start hidden and only appear once something's
        # actually pinned (see rebuild_pinned_grid()). This section and
        # the main grid both live inside one shared scroll area (built at
        # the bottom of this method) - so pinned albums are the first
        # thing you see, but scroll away naturally along with everything
        # else as you scroll down, rather than staying fixed on screen no
        # matter how far into the library you've scrolled.
        self.pinned_heading = QLabel("Pinned Albums", self)
        self.pinned_heading.setObjectName("PinnedSectionHeading")
        self.pinned_heading.setVisible(False)

        # Shown only while the grid below is filtered to one artist (via
        # Artist view -> click an artist - see filter_library_by_artist).
        # "Clear" returns to the normal, unfiltered library grid. Built up
        # front here; populated per-artist by filter_library_by_artist()
        # and the async fetch callbacks below it.
        self.artist_filter_banner = QWidget(self)
        artist_detail_layout = QVBoxLayout(self.artist_filter_banner)
        artist_detail_layout.setContentsMargins(2, 2, 2, 16)
        artist_detail_layout.setSpacing(10)

        detail_top_row = QHBoxLayout()
        detail_top_row.setSpacing(16)

        self.artist_detail_photo = QLabel(self)
        self.artist_detail_photo.setFixedSize(ARTIST_DETAIL_PHOTO_SIZE, ARTIST_DETAIL_PHOTO_SIZE)
        self.artist_detail_photo.setScaledContents(True)
        detail_top_row.addWidget(self.artist_detail_photo, alignment=Qt.AlignmentFlag.AlignTop)

        detail_info_col = QVBoxLayout()
        detail_info_col.setSpacing(4)

        detail_name_row = QHBoxLayout()
        self.artist_filter_label = QLabel("", self)
        self.artist_filter_label.setObjectName("ArtistDetailName")
        artist_filter_clear_btn = QPushButton("\u2715 Clear", self)
        artist_filter_clear_btn.setObjectName("ArtistFilterClearButton")
        artist_filter_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        artist_filter_clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        artist_filter_clear_btn.clicked.connect(self.clear_artist_filter)
        detail_name_row.addWidget(self.artist_filter_label)
        detail_name_row.addStretch(1)
        detail_name_row.addWidget(artist_filter_clear_btn, alignment=Qt.AlignmentFlag.AlignTop)
        detail_info_col.addLayout(detail_name_row)

        # Public listener/scrobble counts from Last.fm - same kind of
        # numbers Last.fm's own artist pages show, not anything tied to
        # this person's own listening. Hidden until a fetch actually
        # returns something (see _on_artist_info_fetched) rather than
        # showing "—" the whole time it's loading.
        detail_stats_row = QHBoxLayout()
        detail_stats_row.setSpacing(14)
        self.artist_detail_listeners_label = QLabel("", self)
        self.artist_detail_listeners_label.setObjectName("ArtistDetailStat")
        self.artist_detail_scrobbles_label = QLabel("", self)
        self.artist_detail_scrobbles_label.setObjectName("ArtistDetailStat")
        detail_stats_row.addWidget(self.artist_detail_listeners_label)
        detail_stats_row.addWidget(self.artist_detail_scrobbles_label)
        detail_stats_row.addStretch(1)
        detail_info_col.addLayout(detail_stats_row)

        self.artist_detail_bio_label = QLabel("", self)
        self.artist_detail_bio_label.setObjectName("ArtistDetailBio")
        self.artist_detail_bio_label.setWordWrap(True)
        # Capped at ~4 lines worth of height - a full Last.fm bio can run
        # to several paragraphs, which would push the album grid below
        # much further down than makes sense for what's meant to be a
        # short "mini biography", not the whole thing.
        self.artist_detail_bio_label.setMaximumHeight(78)
        detail_info_col.addWidget(self.artist_detail_bio_label)

        detail_top_row.addLayout(detail_info_col, stretch=1)
        artist_detail_layout.addLayout(detail_top_row)
        self.artist_filter_banner.setVisible(False)

        self.pinned_grid = AutoHeightIconGrid(self, max_rows=2)
        self.pinned_grid.setObjectName("AlbumGrid")  # same look as the main grid
        self.pinned_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.pinned_grid.setGridSize(GRID_SIZE)
        self.pinned_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.pinned_grid.setMovement(QListWidget.Movement.Static)
        self.pinned_grid.setSpacing(12)
        self.pinned_grid.setWordWrap(False)
        self.pinned_grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # Capped at 2 rows (max_rows above) - pinning enough albums to
        # exceed that scrolls *within* this strip via the wheelEvent it
        # already inherits, same hidden-but-functional scrollbar approach
        # used throughout this file, rather than this section growing
        # forever.
        self.pinned_grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pinned_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pinned_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pinned_grid.setVisible(False)

        # AutoHeightIconGrid, not a self-scrolling grid - the shared
        # scroll area below is what actually scrolls now, so this just
        # needs to accurately report however tall its real content is
        # (see AutoHeightIconGrid for the startup-timing fix that makes
        # that reliable even before the window's ever been shown).
        self.album_grid = AutoHeightIconGrid(self)
        self.album_grid.setObjectName("AlbumGrid")
        self.album_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.album_grid.setGridSize(GRID_SIZE)
        self.album_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.album_grid.setMovement(QListWidget.Movement.Static)
        self.album_grid.setSpacing(12)
        self.album_grid.setWordWrap(False)
        self.album_grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.album_grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.album_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.album_grid.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setVisible(False)
        # Notification banner UI removed per request. The rest of the app still
        # calls status_label.setText(...)/.setVisible(True) in various places
        # (Last.fm, library scanning, playback errors, etc.) - that logic is left
        # untouched, we just make sure the widget itself never actually shows.
        self.status_label.setVisible = lambda *args, **kwargs: None

        # Unlike status_label above, this one is meant to be seen - it's the
        # only visible feedback while a (re)scan is running, since a full
        # library scan can take a couple of seconds with no other UI change
        # to show for it.
        self.scan_progress_bar = QProgressBar(self)
        self.scan_progress_bar.setObjectName("LibraryScanProgress")
        self.scan_progress_bar.setTextVisible(True)
        self.scan_progress_bar.setFixedHeight(18)
        self.scan_progress_bar.setVisible(False)

        # Shown only alongside artist_filter_banner above, in the same
        # artist-filtered mode - the artist detail panel's "Top Songs"
        # list (see filter_library_by_artist/_render_artist_top_songs).
        # Only ever contains tracks that are both one of this artist's
        # Last.fm top tracks by global scrobble count AND actually in
        # this person's own library - Last.fm's ranking has no idea what
        # anyone actually owns, and a row that can't be clicked to play
        # anything doesn't belong in a local player the way it might on a
        # streaming service's artist page.
        self.artist_top_songs_section = QWidget(self)
        top_songs_layout = QVBoxLayout(self.artist_top_songs_section)
        top_songs_layout.setContentsMargins(2, 20, 2, 4)
        top_songs_layout.setSpacing(6)
        top_songs_heading = QLabel("Top Songs", self)
        top_songs_heading.setObjectName("PinnedSectionHeading")
        top_songs_layout.addWidget(top_songs_heading)
        self.artist_top_songs_rows_layout = QVBoxLayout()
        self.artist_top_songs_rows_layout.setSpacing(0)
        top_songs_layout.addLayout(self.artist_top_songs_rows_layout)
        self.artist_top_songs_section.setVisible(False)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.addWidget(self.artist_filter_banner)
        scroll_layout.addWidget(self.pinned_heading)
        scroll_layout.addWidget(self.pinned_grid)
        scroll_layout.addWidget(self.status_label)
        scroll_layout.addWidget(self.scan_progress_bar)
        scroll_layout.addWidget(self.album_grid)
        scroll_layout.addWidget(self.artist_top_songs_section)
        # Claims any leftover vertical space itself, rather than leaving
        # it split above/below the content - setWidgetResizable(True)
        # below stretches scroll_content to fill the whole viewport even
        # when there's not enough here to need scrolling (e.g. an artist
        # filter with only a handful of albums), and without something
        # here to absorb that extra space, everything above gets pushed
        # down and away from the top instead of staying anchored there.
        # Never actually visible itself once there's enough content to
        # fill/overflow the viewport (the normal, unfiltered library case).
        scroll_layout.addStretch(1)

        scroll_area = ControlledScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setWidget(scroll_content)

        left_layout.addWidget(scroll_area)
        return left_widget

    def _build_track_panel(self) -> QWidget:
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        self.album_heading = TwoLineHeadingLabel("Select an album", self)
        self.album_heading.setObjectName("AlbumHeading")
        
        self.song_list_widget = NowPlayingListWidget(self)
        self.song_list_widget.itemClicked.connect(self.handle_song_list_item_clicked)
        self.song_list_widget.itemDoubleClicked.connect(self.play_item)
        self.song_list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.song_list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.song_list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Pinned to a known pixel size (keeping whatever font family was
        # already inherited) so the now-playing grow/shrink animation has
        # a fixed, known baseline to animate from instead of guessing at
        # an inherited default.
        list_font = self.song_list_widget.font()
        list_font.setPixelSize(NowPlayingListWidget.BASE_PIXEL_SIZE)
        self.song_list_widget.setFont(list_font)
        
        right_layout.addWidget(self.album_heading)
        right_layout.addWidget(self.song_list_widget)
        return right_widget

    def _build_showcase_view(self) -> QWidget:
        page = QWidget()
        page.setObjectName("ShowcasePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        top_row = QHBoxLayout()
        self.showcase_back_btn = QPushButton("\u2190  Library", page)
        self.showcase_back_btn.setObjectName("ShowcaseBackButton")
        self.showcase_back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.showcase_back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.showcase_back_btn.clicked.connect(self.close_current_view)
        top_row.addWidget(self.showcase_back_btn)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        body = QHBoxLayout()
        body.setContentsMargins(30, 0, 30, 0)
        body.setSpacing(40)
        body.addStretch(1)

        self.showcase_art = QLabel(page)
        self.showcase_art.setFixedSize(340, 340)
        self.showcase_art.setObjectName("ShowcaseArt")
        self.showcase_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.showcase_art)

        text_col = QVBoxLayout()
        text_col.setSpacing(8)
        text_col.addStretch(1)

        self.showcase_artist_lbl = QLabel("", page)
        self.showcase_artist_lbl.setObjectName("ShowcaseArtist")

        self.showcase_title_lbl = QLabel("", page)
        self.showcase_title_lbl.setObjectName("ShowcaseTitle")
        self.showcase_title_lbl.setWordWrap(True)

        text_col.addWidget(self.showcase_artist_lbl)
        text_col.addWidget(self.showcase_title_lbl)
        text_col.addStretch(1)

        body.addLayout(text_col, stretch=1)
        body.addStretch(1)
        layout.addLayout(body, stretch=1)

        return page

    def _build_lyrics_view(self) -> QWidget:
        page = QWidget()
        page.setObjectName("LyricsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        top_row = QHBoxLayout()
        self.lyrics_back_btn = QPushButton("\u2190  Library", page)
        self.lyrics_back_btn.setObjectName("ShowcaseBackButton")
        self.lyrics_back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lyrics_back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lyrics_back_btn.clicked.connect(self.close_current_view)
        top_row.addWidget(self.lyrics_back_btn)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        body = QHBoxLayout()
        body.setContentsMargins(30, 0, 30, 0)
        body.setSpacing(50)

        left_col = QVBoxLayout()
        left_col.setSpacing(0)
        left_col.addStretch(1)
        
        self.lyrics_art = QLabel(page)
        self.lyrics_art.setFixedSize(340, 340)
        self.lyrics_art.setObjectName("ShowcaseArt")
        self.lyrics_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_col.addWidget(self.lyrics_art, alignment=Qt.AlignmentFlag.AlignCenter)
        
        left_col.addSpacing(20)
        
        self.lyrics_title_lbl = QLabel("", page)
        self.lyrics_title_lbl.setStyleSheet("font-size: 26px; font-weight: 800; color: #FFFFFF;")
        self.lyrics_title_lbl.setWordWrap(True)
        self.lyrics_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_col.addWidget(self.lyrics_title_lbl)
        
        left_col.addSpacing(6)
        
        self.lyrics_artist_lbl = QLabel("", page)
        self.lyrics_artist_lbl.setStyleSheet("font-size: 14px; font-weight: 500; color: rgba(255,255,255,0.6);")
        self.lyrics_artist_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_col.addWidget(self.lyrics_artist_lbl)
        left_col.addStretch(1)
        
        body.addLayout(left_col, stretch=2)

        self.lyrics_box = LyricsListWidget(page)
        self.lyrics_box.setObjectName("LyricsBox")
        self.lyrics_box.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.lyrics_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lyrics_box.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.lyrics_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lyrics_box.setWordWrap(True)
        self.lyrics_box.setStyleSheet("""
            QListWidget#LyricsBox {
                background: transparent;
                border: none;
            }
            QListWidget#LyricsBox::item {
                background: transparent;
                border: none;
                margin-bottom: 12px;
            }
        """)
        
        body.addWidget(self.lyrics_box, stretch=3, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(body, stretch=1)
        self.lyrics_box.set_top_overhead(self.lyrics_back_btn.sizeHint().height() + layout.spacing())
        return page

    def _build_transport_panel(self) -> QWidget:
        bottom_panel = QWidget()
        bottom_panel.setObjectName("BottomPanel")
        bottom_layout = QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(20, 14, 20, 14)
        bottom_layout.setSpacing(8)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, -2)
        shadow.setColor(QColor(0, 0, 0, 110))
        bottom_panel.setGraphicsEffect(shadow)

        timeline_layout = QHBoxLayout()
        self.time_elapsed_lbl = QLabel("0:00", self)
        self.time_elapsed_lbl.setObjectName("TimeLabel")
        self.time_elapsed_lbl.setFixedWidth(40)
        self.time_elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        
        self.timeline_slider = JumpSeekSlider(Qt.Orientation.Horizontal, self)
        self.timeline_slider.setObjectName("TimelineSlider")
        self.timeline_slider.setRange(0, 0)
        self.timeline_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timeline_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        self.timeline_slider.sliderPressed.connect(self.on_timeline_pressed)
        self.timeline_slider.sliderReleased.connect(self.on_timeline_released)
        self.timeline_slider.sliderMoved.connect(self.on_timeline_moved)
        
        self.time_total_lbl = QLabel("0:00", self)
        self.time_total_lbl.setObjectName("TimeLabel")
        self.time_total_lbl.setFixedWidth(40)
        
        timeline_layout.addWidget(self.time_elapsed_lbl)
        timeline_layout.addWidget(self.timeline_slider)
        timeline_layout.addWidget(self.time_total_lbl)
        bottom_layout.addLayout(timeline_layout)

        controls_row_layout = QHBoxLayout()
        controls_row_layout.setContentsMargins(0, 0, 0, 0)

        now_playing_layout = QHBoxLayout()
        now_playing_layout.setSpacing(12)

        self.now_playing_art = QLabel(self)
        self.now_playing_art.setFixedSize(60, 60) 
        self.now_playing_art.setObjectName("NowPlayingArt")
        
        text_stack_layout = QVBoxLayout()
        text_stack_layout.setContentsMargins(0, 2, 0, 2)
        text_stack_layout.setSpacing(2)

        self.now_playing_label = QLabel("Nothing playing", self)
        self.now_playing_label.setObjectName("NowPlayingLabel")
        
        self.now_playing_artist_label = QLabel("", self)
        self.now_playing_artist_label.setObjectName("NowPlayingArtistLabel")
        
        text_stack_layout.addWidget(self.now_playing_label)
        text_stack_layout.addWidget(self.now_playing_artist_label)

        now_playing_layout.addWidget(self.now_playing_art)
        now_playing_layout.addLayout(text_stack_layout)
        controls_row_layout.addLayout(now_playing_layout, stretch=3)
        
        controls_row_layout.addStretch(1)

        media_buttons_layout = QHBoxLayout()
        media_buttons_layout.setSpacing(10)
        media_buttons_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.prev_btn = QPushButton("\u23ee", self) 
        self.prev_btn.setObjectName("PrevButton")
        self.prev_btn.setFixedSize(40, 40)
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.prev_btn.clicked.connect(self.play_previous)
        media_buttons_layout.addWidget(self.prev_btn)

        self.play_btn = MorphingPlayPauseButton(self) 
        self.play_btn.setObjectName("PlayButton")
        self.play_btn.setFixedSize(46, 46) 
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.clicked.connect(self.toggle_play)
        media_buttons_layout.addWidget(self.play_btn)

        self.next_btn = QPushButton("\u23ed", self) 
        self.next_btn.setObjectName("NextButton")
        self.next_btn.setFixedSize(40, 40)
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.next_btn.clicked.connect(self.play_next)
        media_buttons_layout.addWidget(self.next_btn)

        controls_row_layout.addLayout(media_buttons_layout)
        
        controls_row_layout.addStretch(1)

        volume_container = QWidget()
        volume_layout = QHBoxLayout(volume_container)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(6)
        volume_layout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.loop_btn = MorphingLoopButton(self)
        self.loop_btn.setObjectName("LoopButton")
        self.loop_btn.setFixedSize(36, 36)
        self.loop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.loop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.loop_btn.clicked.connect(self.cycle_playback_mode)
        volume_layout.addWidget(self.loop_btn)

        self._lyrics_icon_inactive = self._build_mic_icon(QColor(255, 255, 255, 140))
        self._lyrics_icon_active = self._build_mic_icon(QColor(255, 255, 255, 255))

        self.lyrics_btn = QPushButton("", self)
        self.lyrics_btn.setObjectName("LyricsButton")
        self.lyrics_btn.setFixedSize(36, 36)
        self.lyrics_btn.setIcon(self._lyrics_icon_inactive)
        self.lyrics_btn.setIconSize(QSize(17, 17))
        self.lyrics_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lyrics_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lyrics_btn.clicked.connect(self.toggle_lyrics_view)
        self.lyrics_btn.setProperty("active", "0")
        volume_layout.addWidget(self.lyrics_btn)

        volume_layout.addSpacing(8)
        
        self.volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.volume_slider.setObjectName("VolumeSlider")
        self._volume_slider_style = ClickAnywhereSliderStyle(self.volume_slider.style())
        self.volume_slider.setStyle(self._volume_slider_style)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self.initial_volume_pct)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.valueChanged.connect(self.change_volume)

        self.volume_label = QLabel(f"Vol: {self.initial_volume_pct}%", self)
        self.volume_label.setObjectName("VolumeLabel")
        # Fix the width to fit the widest possible value ("Vol: 100%") so the
        # label doesn't grow/shrink as the digit count changes (e.g. 9% vs
        # 15% vs 100%) - that reflow was what made the slider next to it
        # appear to jitter/move while dragging.
        self.volume_label.setFixedWidth(self.volume_label.fontMetrics().horizontalAdvance("Vol: 100%") + 4)

        # Dims slightly at rest and brightens to full opacity (plus bold
        # text) while the slider is actively being adjusted - see
        # _set_volume_label_adjusting(), wired to sliderPressed/Released
        # below. Animated via a per-widget stylesheet (alpha channel of the
        # text color) rather than a QGraphicsOpacityEffect - this label's
        # text also changes constantly while dragging (change_volume() calls
        # setText() on every tick), and combining that with an active
        # opacity-compositing effect made the label flicker out entirely
        # mid-drag instead of fading smoothly.
        self._volume_label_alpha = 0.7
        self._volume_label_fade_anim: Optional[QVariantAnimation] = None

        self.volume_slider.sliderPressed.connect(lambda: self._set_volume_label_adjusting(True))
        self.volume_slider.sliderReleased.connect(lambda: self._set_volume_label_adjusting(False))
        
        volume_layout.addWidget(self.volume_slider)
        volume_layout.addWidget(self.volume_label)

        self.size_grip = ManualSizeGrip(self)
        volume_layout.addWidget(self.size_grip)

        controls_row_layout.addWidget(volume_container, stretch=3, alignment=Qt.AlignmentFlag.AlignRight)
        bottom_layout.addLayout(controls_row_layout)

        return bottom_panel

    # ------------------------------------------------------------- Theme --
    def apply_theme(self, base_color: QColor):
        bg_hex = base_color.name()
        panel_bg = "rgba(255, 255, 255, 0.04)"
        panel_hover = "rgba(255, 255, 255, 0.08)"
        accent = base_color.lighter(200).name()
        self._current_accent = QColor(accent)

        if self.selected_cards:
            for card in self.selected_cards:
                card.set_accent(self._current_accent)
        self._push_accent_to_now_playing_cards()

        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {bg_hex}; }}

            QWidget#CustomTitleBar {{
                background-color: rgba(255, 255, 255, 0.03);
                border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            }}
            QLabel#LogoMark {{
                background-color: {accent};
                border-radius: 7px;
                font-size: 13px;
                font-weight: 700;
                color: rgba(255,255,255,0.85);
            }}
            QLabel#AppTitle {{ font-size: 13px; font-weight: 700; color: rgba(255,255,255,0.85); }}
            QLabel#FolderLabel {{ font-weight: 600; color: rgba(255,255,255,0.6); font-size: 12px; }}
            QLabel#PinnedSectionHeading {{
                font-weight: 700; font-size: 12px; color: rgba(255,255,255,0.6);
                padding: 2px 2px 8px 2px;
            }}
            QLabel#HomeShelfHeading {{
                font-weight: 800; font-size: 22px; color: rgba(255,255,255,0.95);
                padding: 2px 2px 6px 2px;
            }}
            QLabel#ArtistsHeading {{
                font-weight: 800; font-size: 24px; color: rgba(255,255,255,0.95);
                padding: 2px 2px 12px 2px;
            }}
            QLabel#ArtistDetailName {{
                font-weight: 800; font-size: 22px; color: rgba(255,255,255,0.95);
            }}
            QLabel#ArtistDetailStat {{
                font-weight: 600; font-size: 12px; color: rgba(255,255,255,0.55);
            }}
            QLabel#ArtistDetailBio {{
                font-weight: 400; font-size: 12px; color: rgba(255,255,255,0.6);
            }}
            QPushButton#ArtistFilterClearButton {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 10px;
            }}
            QWidget#TopSongRow:hover {{
                background-color: rgba(255, 255, 255, 0.05);
                border-radius: 6px;
            }}
            QLabel#TopSongRank {{
                font-weight: 600; font-size: 12px; color: rgba(255,255,255,0.4);
            }}
            QLabel#TopSongTitle {{
                font-weight: 600; font-size: 13px; color: rgba(255,255,255,0.9);
            }}
            QLabel#TopSongTitleMuted {{
                font-weight: 600; font-size: 13px; color: rgba(255,255,255,0.4);
            }}
            QLabel#TopSongCount {{
                font-weight: 500; font-size: 11px; color: rgba(255,255,255,0.4);
            }}

            QProgressBar#LibraryScanProgress {{
                background-color: rgba(255, 255, 255, 0.06);
                border: none;
                border-radius: 9px;
                color: rgba(255,255,255,0.85);
                font-size: 10px;
                font-weight: 600;
                text-align: center;
                margin-bottom: 6px;
            }}
            QProgressBar#LibraryScanProgress::chunk {{
                background-color: {accent};
                border-radius: 9px;
            }}

            QPushButton#OptionsButton {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 5px 26px 5px 12px;
                font-size: 12px;
                font-weight: 600;
                color: rgba(255,255,255,0.85);
            }}
            QPushButton#OptionsButton:hover {{
                background-color: rgba(255, 255, 255, 0.1);
            }}
            QPushButton#OptionsButton::menu-indicator {{
                subcontrol-origin: padding;
                subcontrol-position: right center;
                right: 10px;
                width: 10px;
            }}
            QPushButton#WinControlButton {{
                background-color: transparent;
                border: none;
                border-radius: 6px;
                padding: 0px;
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton#WinControlButton:hover {{ background-color: rgba(255, 255, 255, 0.12); }}
            QPushButton#WinCloseButton {{
                background-color: transparent;
                border: none;
                border-radius: 6px;
                padding: 0px;
                font-size: 15px;
                font-weight: 600;
            }}
            QPushButton#WinCloseButton:hover {{ background-color: #E5484D; color: #FFFFFF; }}

            QMenu {{ background-color: #252830; border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 4px; }}
            QMenu::item {{ color: #FFFFFF; padding: 6px 20px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: rgba(255,255,255,0.1); }}
            
            QLabel {{ color: #FFFFFF; font-family: 'SF Pro Text', -apple-system, BlinkMacSystemFont, sans-serif; }}
            QLabel#AlbumHeading {{ font-size: 14px; font-weight: 700; padding: 2px 4px 8px 4px; color: rgba(255,255,255,0.9); }}
            QLabel#StatusLabel {{ font-size: 12px; color: rgba(255,255,255,0.6); padding: 2px 4px; }}
            QLabel#TimeLabel {{ color: rgba(255, 255, 255, 0.6); font-size: 11px; font-weight: 600; }}
            QLabel#VolumeLabel {{ color: rgba(255, 255, 255, 0.7); font-size: 11px; font-weight: 600; margin-right: 4px; }}
            
            QLabel#NowPlayingLabel {{ font-size: 14px; font-weight: 700; color: rgba(255,255,255,0.95); }}
            QLabel#NowPlayingArtistLabel {{ font-size: 12px; font-weight: normal; color: rgba(255,255,255,0.6); }}
            QLabel#NowPlayingArt {{ background-color: rgba(255,255,255,0.05); border-radius: 10px; }}

            QWidget#ShowcasePage, QWidget#LyricsPage {{ background: transparent; }}
            QPushButton#ShowcaseBackButton {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
            }}
            QLabel#ShowcaseArt {{ background-color: rgba(255,255,255,0.04); border-radius: 16px; }}
            QLabel#ShowcaseArtist {{ font-size: 13px; font-weight: 700; color: rgba(255,255,255,0.5); }}
            QLabel#ShowcaseTitle {{ font-size: 34px; font-weight: 800; color: #FFFFFF; }}
            
            QPushButton {{
                background-color: rgba(255, 255, 255, 0.07);
                color: #FFFFFF;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 14px;
                padding: 6px 16px;
                font-weight: 600;
                font-size: 11px;
            }}
            QPushButton:hover {{ 
                background-color: rgba(255, 255, 255, 0.15); 
                border: 1px solid rgba(255, 255, 255, 0.15);
            }}
            
            #BottomPanel QPushButton#PrevButton, #BottomPanel QPushButton#NextButton {{
                border-radius: 20px;
                padding: 0px;
                font-size: 16px;
                color: rgba(255, 255, 255, 0.7);
            }}
            #BottomPanel QPushButton#LoopButton, #BottomPanel QPushButton#LyricsButton {{
                border-radius: 18px;
                padding: 0px;
                font-size: 15px;
                color: rgba(255, 255, 255, 0.7);
            }}
            #BottomPanel QPushButton#LoopButton:hover, #BottomPanel QPushButton#LyricsButton:hover {{
                color: #FFFFFF;
            }}
            #BottomPanel QPushButton#PlayButton {{ 
                background-color: #FFFFFF; 
                color: #000000; 
                border: none;
                border-radius: 23px;
                padding: 0px;
                font-size: 18px;
            }}
            #BottomPanel QPushButton#PlayButton:hover {{ background-color: rgba(255, 255, 255, 0.88); }}
            
            QPushButton#LyricsButton[active="1"] {{
                background-color: rgba(255, 255, 255, 0.16);
                border: 1px solid rgba(255, 255, 255, 0.2);
                color: #FFFFFF;
            }}

            QPushButton#NavTabButton {{
                background-color: transparent;
                border: 1px solid transparent;
                color: rgba(255, 255, 255, 0.55);
                border-radius: 14px;
                padding: 6px 16px;
                font-weight: 700;
                font-size: 13px;
            }}
            QPushButton#NavTabButton:hover {{
                background-color: rgba(255, 255, 255, 0.08);
                color: rgba(255, 255, 255, 0.85);
            }}
            QPushButton#NavTabButton[active="1"] {{
                background-color: rgba(255, 255, 255, 0.14);
                border: 1px solid rgba(255, 255, 255, 0.18);
                color: #FFFFFF;
            }}
            QWidget#HomePage, QWidget#ArtistsPage {{ background: transparent; }}
            QPushButton#ShelfNavButton {{
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 13px;
                padding: 0px;
                font-size: 15px;
                font-weight: 700;
                color: rgba(255, 255, 255, 0.7);
            }}
            QPushButton#ShelfNavButton:hover {{
                background-color: rgba(255, 255, 255, 0.14);
                color: #FFFFFF;
            }}
            
            QListWidget {{
                background-color: {panel_bg};
                border: none;
                border-radius: 12px;
                color: #FFFFFF;
                padding: 10px;
            }}
            QListWidget::item {{ color: #FFFFFF; border: none; border-radius: 8px; padding: 6px; }}
            QListWidget::item:hover {{ background-color: {panel_hover}; }}

            QListWidget#AlbumGrid::item {{
                padding: 0px;
                margin: 0px;
                border: none;
                background: transparent;
            }}
            QListWidget#AlbumGrid::item:hover {{ background: transparent; }}
            QListWidget#AlbumGrid::item:selected {{ background: transparent; }}

            QListWidget#HomeShelfGrid {{
                background: transparent;
                border: none;
                border-radius: 0px;
                padding: 0px;
            }}
            QListWidget#HomeShelfGrid::item {{
                padding: 0px;
                margin: 0px;
                border: none;
                background: transparent;
            }}
            QListWidget#HomeShelfGrid::item:hover {{ background: transparent; }}
            QListWidget#HomeShelfGrid::item:selected {{ background: transparent; }}
            
            QWidget#BottomPanel {{
                background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(40, 44, 54, 0.35), stop:1 rgba(20, 22, 28, 0.65));
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: 16px;
            }}
            
            QSlider#TimelineSlider {{
                background: transparent;
                height: 16px;
            }}
            QSlider#TimelineSlider::groove:horizontal {{
                border: none;
                background: rgba(255, 255, 255, 0.08);
                height: 6px;
                border-radius: 3px;
            }}
            QSlider#TimelineSlider::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {accent}, stop:1 #FFFFFF);
                border-radius: 3px;
            }}
            QSlider#TimelineSlider::handle:horizontal {{
                background: #FFFFFF;
                border: none;
                width: 6px;
                height: 14px;
                margin: -4px 0px;
                border-radius: 3px;
            }}
            
            QSlider#VolumeSlider::groove:horizontal {{
                border: none;
                background: rgba(255, 255, 255, 0.12);
                height: 4px;
                border-radius: 2px;
            }}
            QSlider#VolumeSlider::sub-page:horizontal {{
                background: rgba(255, 255, 255, 0.6);
                border-radius: 2px;
            }}
            QSlider#VolumeSlider::handle:horizontal {{
                background: #FFFFFF;
                width: 8px;
                height: 8px;
                margin: -2px 0px;
                border-radius: 4px;
            }}

            QLineEdit#SearchBox {{
                background-color: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 6px;
                padding: 3px 8px;
                color: #FFFFFF;
                font-size: 11px;
            }}
            QLineEdit#SearchBox:focus {{
                background-color: rgba(255, 255, 255, 0.12);
                border: 1px solid rgba(255, 255, 255, 0.25);
            }}
        """)

    # ------------------------------------------------------- Image helpers --
    def make_pixmap_rounded(self, src_pixmap: QPixmap, radius: int = 10) -> QPixmap:
        if src_pixmap.isNull():
            return src_pixmap
        size = src_pixmap.size()
        rounded_image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        rounded_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded_image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addRoundedRect(0, 0, size.width(), size.height(), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, src_pixmap)
        painter.end()
        return QPixmap.fromImage(rounded_image)

    def cover_bytes_to_pixmap(self, cover_bytes: Optional[bytes], size: int = COVER_SIZE, radius: int = 8) -> QPixmap:
        if cover_bytes:
            pixmap = QPixmap()
            if pixmap.loadFromData(cover_bytes):
                square = pixmap.scaled(
                    size, size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ).copy(0, 0, size, size)
                return self.make_pixmap_rounded(square, radius=radius)
        placeholder = QPixmap(size, size)
        placeholder.fill(QColor(38, 42, 50))
        return self.make_pixmap_rounded(placeholder, radius=radius)

    # (glyph, gradient-top-left, gradient-bottom-right) per smart mix - each
    # one gets its own little "theme" so the Made For You shelf doesn't turn
    # into a wall of identical purple tiles once there's more than one
    # generated mix living on it. Picked to loosely evoke each mix's idea:
    # Replay Mix's original violet stays as the default/fallback look.
    MIX_STYLES = {
        "replay":              ("\u266B", QColor(96, 68, 158), QColor(42, 46, 96)),    # music note - violet
        "on_repeat":           ("\u21BB", QColor(190, 54, 68), QColor(70, 18, 26)),    # repeat arrow - crimson
        "forgotten_favorites": ("\u2661", QColor(150, 126, 84), QColor(58, 46, 34)),   # heart outline - faded sepia
        "night_owl":           ("\u263E", QColor(28, 32, 74), QColor(8, 9, 20)),       # crescent moon - midnight blue
        "morning_mix":         ("\u2600", QColor(255, 170, 66), QColor(255, 92, 74)),  # sun - sunrise orange
        "weekend_mix":         ("\u2726", QColor(0, 160, 150), QColor(0, 78, 110)),    # sparkle - teal
        "album_rewind":        ("\u27F2", QColor(150, 96, 56), QColor(64, 38, 28)),    # circular arrow - vinyl brown
        "month_rewind":        ("\u2605", QColor(186, 58, 186), QColor(58, 20, 88)),   # star - wrapped magenta
    }

    def generate_mix_cover_pixmap(self, style: str = "replay", size: int = COVER_SIZE, radius: int = 8) -> QPixmap:
        # A generated cover for smart-mix "albums" that aren't real
        # scanned folders (e.g. Replay Mix) - a gradient tile with a
        # glyph, visually distinct enough to read as "this one's
        # different" from a real album cover. `style` looks up
        # MIX_STYLES so every mix gets its own glyph/gradient rather than
        # sharing one look.
        glyph, color_a, color_b = self.MIX_STYLES.get(style, self.MIX_STYLES["replay"])
        pixmap = QPixmap(size, size)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, size, size)
        gradient.setColorAt(0.0, color_a)
        gradient.setColorAt(1.0, color_b)
        painter.fillRect(0, 0, size, size, gradient)
        painter.setPen(QColor(255, 255, 255, 210))
        font = QFont("SF Pro Text", int(size * 0.4), QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        return self.make_pixmap_rounded(pixmap, radius=radius)

    def _current_display_cover_bytes(self) -> Optional[bytes]:
        # A virtual mix (Replay Mix etc.) has no single real cover of its
        # own - album_display_meta's cover_bytes for it is just None - so
        # Showcase/Lyrics need this specific track's own embedded art
        # instead (cached in play_track_at, not re-read here). Regular
        # albums just use the album's own cover_bytes as before.
        is_virtual_mix = bool(self.active_playing_album_key) and self.active_playing_album_key.startswith("__mix__")
        if is_virtual_mix:
            return self._current_track_cover_bytes
        meta = self.album_display_meta.get(self.active_playing_album_key)
        return meta.get("cover_bytes") if meta else None

    def compute_average_color(self, pixmap: QPixmap) -> QColor:
        if not pixmap or pixmap.isNull():
            return QColor(18, 20, 24)
        tiny = pixmap.toImage().scaled(
            1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        base = QColor(tiny.pixel(0, 0))
        return QColor(int(base.red() * 0.28), int(base.green() * 0.30), int(base.blue() * 0.34))

    # -------------------------------------------------------- Library scan --
    def restore_last_folder(self):
        last_folder = self.settings.value("music_folder", "")
        if last_folder and os.path.isdir(last_folder):
            self.music_folder = last_folder
            self.folder_label.setText(os.path.basename(last_folder))

            cached_albums = self._load_library_cache(last_folder)
            if cached_albums is not None:
                # Instant load from the last scan's cache - no "Scanning
                # library..." wait on every launch. Use Options > Rescan
                # Folder to pick up changes made on disk since then.
                self.on_scan_complete(cached_albums)
            else:
                self.start_scan(last_folder)

    def _library_cache_path(self) -> str:
        cache_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not cache_dir:
            cache_dir = os.path.expanduser(os.path.join("~", ".cache", APP_NAME))
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, "library_cache.json")

    def _load_library_cache(self, folder: str) -> Optional[dict]:
        try:
            with open(self._library_cache_path(), "r", encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            return None

        # Cache is keyed to a single folder - if the person picked a
        # different library since it was written, it doesn't apply here.
        if cached.get("folder") != folder:
            return None

        try:
            albums = {}
            for key, record in cached.get("albums", {}).items():
                cover_b64 = record.get("cover_bytes")
                albums[key] = {
                    "title": record["title"],
                    "artist": record["artist"],
                    "year": record["year"],
                    "paths": record["paths"],
                    "cover_bytes": base64.b64decode(cover_b64) if cover_b64 else None,
                    "track_titles": record.get("track_titles", {}),
                    "added_ts": record.get("added_ts", 0),
                }
            return albums
        except (KeyError, ValueError, TypeError, binascii.Error):
            # Malformed/corrupt cache - fall back to a real scan rather
            # than crash on launch.
            return None

    def _save_library_cache(self, folder: str, albums: dict):
        try:
            serializable_albums = {}
            for key, record in albums.items():
                cover_bytes = record.get("cover_bytes")
                serializable_albums[key] = {
                    "title": record["title"],
                    "artist": record["artist"],
                    "year": record["year"],
                    "paths": record["paths"],
                    "cover_bytes": base64.b64encode(cover_bytes).decode("ascii") if cover_bytes else None,
                    "track_titles": record.get("track_titles", {}),
                    "added_ts": record.get("added_ts", 0),
                }
            with open(self._library_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"folder": folder, "albums": serializable_albums}, f)
        except OSError:
            # Non-fatal - worst case we just re-scan next launch instead of
            # loading from cache.
            pass

    def rescan_current_folder(self):
        if not self.music_folder or not os.path.isdir(self.music_folder):
            self.status_label.setText("No music folder selected yet.")
            self.status_label.setVisible(True)
            return
        self.start_scan(self.music_folder)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if folder:
            self.music_folder = folder
            self.folder_label.setText(os.path.basename(folder))
            self.settings.setValue("music_folder", folder)
            self.start_scan(folder)

    def start_scan(self, folder: str):
        if self.scanner and self.scanner.isRunning():
            return
        self.album_grid.clear()
        self.album_grid.update_height()
        self._clear_card_selection()
        self.song_list_widget.clear()
        self.status_label.setText("Scanning library\u2026")
        self.status_label.setVisible(True)

        # Total album count isn't known until the folder walk finishes, so
        # start in Qt's built-in "busy" mode (range 0,0 animates a moving
        # bar with no fixed endpoint) and switch to a real N/total bar as
        # soon as the first progress signal gives us a total.
        self.scan_progress_bar.setRange(0, 0)
        self.scan_progress_bar.setFormat("Scanning library\u2026")
        self.scan_progress_bar.setVisible(True)

        self.rescan_folder_action.setEnabled(False)
        self.choose_folder_action.setEnabled(False)

        self.scanner = LibraryScanner(folder)
        self.scanner.progress.connect(self.on_scan_progress)
        self.scanner.scan_complete.connect(self.on_scan_complete)
        self.scanner.scan_failed.connect(self.on_scan_failed)
        self.scanner.start()

    def on_scan_progress(self, done: int, total: int):
        if total:
            self.status_label.setText(f"Scanning library\u2026 {done}/{total} albums")
            if self.scan_progress_bar.maximum() != total:
                self.scan_progress_bar.setRange(0, total)
            self.scan_progress_bar.setValue(done)
            self.scan_progress_bar.setFormat(f"Scanning\u2026 {done}/{total} albums")

    def _finish_scan_ui(self):
        self.scan_progress_bar.setVisible(False)
        self.rescan_folder_action.setEnabled(True)
        self.choose_folder_action.setEnabled(True)

    def on_scan_failed(self, message: str):
        self.status_label.setText(f"Couldn't read folder: {message}")
        self._finish_scan_ui()

    def on_scan_complete(self, albums: dict):
        self.album_tracks = {}
        self.track_to_album_key = {}
        self.album_display_meta = {}
        self.sorted_album_keys = []
        self.track_titles = {}
        if not albums:
            self.status_label.setText("No supported audio files found.")
            self._finish_scan_ui()
            return
        self.status_label.setVisible(False)
        self._finish_scan_ui()

        if self.music_folder:
            self._save_library_cache(self.music_folder, albums)
        
        sortable = []
        for key, record in albums.items():
            self.album_tracks[key] = record["paths"]
            for path in record["paths"]:
                self.track_to_album_key[path] = key
            self.track_titles.update(record.get("track_titles", {}))
            pixmap = self.cover_bytes_to_pixmap(record["cover_bytes"])
            self.album_display_meta[key] = {
                "title": record["title"],
                "artist": record["artist"],
                "icon": QIcon(pixmap),
                "pixmap": pixmap,
                "cover_bytes": record["cover_bytes"],
                "added_ts": record.get("added_ts", 0),
            }
            sortable.append((record["artist"].lower(), record["year"], record["title"].lower(), key))
        
        sortable.sort(key=lambda row: (row[0], row[1], row[2]))
        self.sorted_album_keys = [row[3] for row in sortable]

        # Resolved *before* the grid is (re)built below, on the very first
        # library load this launch only - pre-setting browsing_album_key
        # here means the resumed album's card picks up its selection
        # highlight through the exact same mechanism a normal click uses
        # (see _add_card), rather than needing separate lookup/selection
        # code after the fact.
        resume_album_key, resume_row, resume_position_ms = (None, -1, 0)
        if not self._resume_attempted:
            resume_album_key, resume_row, resume_position_ms = self._resolve_resume_target()
            if resume_album_key:
                self.browsing_album_key = resume_album_key

        self.rebuild_album_grid()

        # start_scan() clears the right-hand song list up front (there's
        # nothing to show mid-scan), and rebuild_album_grid() above only
        # restores which *card* is highlighted - it doesn't touch the song
        # list widget. Without this, whatever album you had open goes
        # blank after every scan even though it's still selected. Re-fill
        # it now that album_tracks is populated again - if the album
        # doesn't exist anymore, this just leaves the list empty. This
        # also doubles as the resumed album's track list on first launch,
        # since browsing_album_key may have just been set above.
        if self.browsing_album_key is not None:
            self.display_album_tracks_by_key(self.browsing_album_key)

        # Actually load (but don't auto-play) the resumed track, now that
        # the library/UI around it is all in place. Only on the very first
        # library load each launch - not on a manual rescan/folder change
        # later, which shouldn't yank whatever the person is actively
        # doing back to some old saved track.
        if not self._resume_attempted:
            self._resume_attempted = True
            if resume_album_key:
                self._start_playing_browsed_album()
                self.play_track_at(resume_row, autoplay=False, resume_position_ms=resume_position_ms)

        self.refresh_home_shelves()
        self.refresh_artist_grid()

    def get_track_title(self, path: str) -> str:
        # Prefers the real embedded title tag (e.g. from Picard) over the
        # filename - a tag with a properly clean title is exactly what
        # was missing before, when everything was derived from the
        # filename regardless of what the file was actually tagged with.
        # Falls back to the filename for tracks with no title tag at all.
        return self.track_titles.get(path) or clean_track_name(os.path.basename(path))

    def _start_playing_browsed_album(self):
        # Every place playback kicks off from whatever's currently being
        # browsed used to repeat these same two lines independently, which
        # meant the "now playing" outline had no single place to hook into
        # and could silently drift out of sync with active_playing_album_key.
        # Centralized here instead.
        self.active_playing_tracks = list(self.browsing_tracks)
        self.active_playing_album_key = self.browsing_album_key
        self._refresh_now_playing_outlines()

    # ---------------------------------------------------------- Card grid --
    def _add_card(self, key: str, mode: str, title: str, artist: str, pixmap: QPixmap,
                   track_path: Optional[str] = None, target_grid: Optional[QListWidget] = None,
                   card_size: QSize = CARD_SIZE, is_mix: bool = False):
        target_grid = target_grid if target_grid is not None else self.album_grid

        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(card_size)
        target_grid.addItem(item)

        card = AlbumCardWidget(pixmap, title, simplify_multi_artist_credit(artist), card_size=card_size, is_mix=is_mix)
        card.mode = mode
        card.album_key = key
        card.track_path = track_path
        card.clicked.connect(lambda c=card: self.handle_card_clicked(c))
        card.doubleClicked.connect(lambda c=card: self.handle_card_double_clicked(c))
        card.rightClicked.connect(lambda c=card: self.show_album_card_context_menu(c))
        target_grid.setItemWidget(item, card)

        if mode == "album":
            card.set_pinned(key in self.pinned_album_keys)
            if key == self.browsing_album_key:
                self.set_selected_card(card)
            # Covers every grid an album card can land in - Library,
            # Pinned, and every Home shelf - so a freshly (re)built card
            # picks up the outline immediately without needing a separate
            # pass afterward. "track" mode cards (individual songs, even
            # ones from this same album) are never touched here.
            if key == self.active_playing_album_key:
                card.set_accent(self._current_accent)
                card.set_now_playing(True)

        return card

    def rebuild_album_grid(self):
        self.rebuild_pinned_grid()

        self.album_grid.clear()
        self._clear_card_selection()
        self.grid_mode = "album"
        self.artist_filter_name = None
        self.artist_filter_banner.setVisible(False)
        self.artist_top_songs_section.setVisible(False)
        # Pinned albums also get a shortcut copy in their own section
        # above (see rebuild_pinned_grid) - but stay right here too, in
        # their normal alphabetical spot, rather than being pulled out of
        # the library entirely. Both copies pick up the star badge
        # automatically via _add_card's set_pinned() call below.
        for key in self.sorted_album_keys:
            meta = self.album_display_meta[key]
            self._add_card(key, "album", meta["title"], meta["artist"], meta["pixmap"])
        self.album_grid.update_height()
        self._refresh_now_playing_outlines(force=True)

    def rebuild_pinned_grid(self):
        self.pinned_grid.clear()
        self._clear_card_selection()
        pinned_keys = [k for k in self.pinned_album_keys if k in self.album_display_meta]
        has_pinned = bool(pinned_keys)
        self.pinned_heading.setVisible(has_pinned)
        self.pinned_grid.setVisible(has_pinned)
        for key in pinned_keys:
            meta = self.album_display_meta[key]
            self._add_card(key, "album", meta["title"], meta["artist"], meta["pixmap"], target_grid=self.pinned_grid)
        self.pinned_grid.update_height()
        self._refresh_now_playing_outlines(force=True)

    def show_album_card_context_menu(self, card: AlbumCardWidget):
        if card.mode != "album" or not card.album_key or card.album_key.startswith("__mix__"):
            return
        is_pinned = card.album_key in self.pinned_album_keys
        menu = QMenu(self)
        action = QAction("Unpin" if is_pinned else "Pin to top", self)
        action.triggered.connect(lambda checked=False, key=card.album_key: self.toggle_album_pin(key))
        menu.addAction(action)
        menu.exec(QCursor.pos())

    def toggle_album_pin(self, key: str):
        if key in self.pinned_album_keys:
            self.pinned_album_keys.remove(key)
        else:
            self.pinned_album_keys.append(key)
        self.settings.setValue("pinned_albums", json.dumps(self.pinned_album_keys))
        # Only actually rebuilds the grids while browsing albums normally -
        # pinning from a search result shouldn't yank the view out of
        # search mode.
        if self.grid_mode == "album":
            self.rebuild_album_grid()
        self.refresh_home_shelves()

    # ------------------------------------------------------- Search Engine --
    def filter_library(self, text: str):
        query = text.strip().lower()
        if len(query) < 3:
            if self.grid_mode != "album":
                self.rebuild_album_grid()
            return

        # The pinned section is a library-browsing affordance - it'd just
        # compete with actual search results for attention, so it hides
        # for the duration of a search rather than sitting above them.
        self.pinned_heading.setVisible(False)
        self.pinned_grid.setVisible(False)
        self.artist_filter_name = None
        self.artist_filter_banner.setVisible(False)
        self.artist_top_songs_section.setVisible(False)

        self.album_grid.clear()
        self._clear_card_selection()
        self.grid_mode = "search"
        
        for album_key in self.sorted_album_keys:
            meta = self.album_display_meta[album_key]
            tracks = self.album_tracks[album_key]
            
            for path in tracks:
                track_title = self.get_track_title(path)
                if query in track_title.lower() or query in meta["artist"].lower():
                    card = self._add_card(album_key, "track", track_title, meta["artist"], meta["pixmap"], track_path=path)
                    # Search results keep the single-click-to-play
                    # convention they've always had - only Home's track
                    # cards (see refresh_home_shelves) need the
                    # double-click gate.
                    card.plays_immediately_on_click = True
        self.album_grid.update_height()
        self._refresh_now_playing_outlines(force=True)

    # -------------------------------------------------------- Card actions --
    def _cards_for_album(self, album_key: str) -> list:
        # Both album_grid and pinned_grid can contain a card for the same
        # album (a pinned album shows in both) - "the card for this
        # album" can mean more than one on-screen widget, so this finds
        # all of them.
        matches = []
        for grid in (self.album_grid, self.pinned_grid):
            for i in range(grid.count()):
                widget = grid.itemWidget(grid.item(i))
                if isinstance(widget, AlbumCardWidget) and widget.mode == "album" and widget.album_key == album_key:
                    matches.append(widget)
        return matches

    def _all_grids_with_cards(self) -> list:
        # Every grid that can hold an AlbumCardWidget - unlike
        # _cards_for_album() above (Library + Pinned only, used for the
        # browsing-selection outline, which only ever needs to live on
        # the page it was clicked from), the "now playing" outline should
        # show up wherever this album's card happens to be on screen,
        # Home shelves included.
        grids = [self.album_grid, self.pinned_grid]
        if hasattr(self, "home_shelves"):
            grids.extend(grid for _container, grid in self.home_shelves.values())
        return grids

    def _refresh_now_playing_outlines(self, force: bool = False):
        # A full walk of every grid (Library alone can mean the whole
        # library) only actually needs to happen when the playing album
        # has changed since last time, or right after a grid holding
        # cards gets rebuilt from scratch (force=True, used by the
        # rebuild_*/refresh_home_shelves methods below, since that's the
        # only time old widget references can go stale). Skipping it
        # otherwise matters because this used to run unconditionally on
        # every single play_track_at() call - including replaying another
        # track from the same album, by far the most common case during
        # normal playback - which is what made rapid/repeated clicks
        # visibly stutter.
        if not force and self._now_playing_outline_key == self.active_playing_album_key:
            return
        self._now_playing_outline_key = self.active_playing_album_key

        for card in self._now_playing_card_widgets:
            try:
                card.set_now_playing(False)
            except RuntimeError:
                pass  # already destroyed by a grid rebuild since the last scan

        matches = []
        if self.active_playing_album_key:
            # Only "album" mode cards are eligible - a "track" mode card
            # (a single song on a Home shelf or in search results) isn't
            # itself playing just because the album it's from happens to
            # be.
            for grid in self._all_grids_with_cards():
                for i in range(grid.count()):
                    widget = grid.itemWidget(grid.item(i))
                    if isinstance(widget, AlbumCardWidget) and widget.mode == "album" and widget.album_key == self.active_playing_album_key:
                        widget.set_now_playing(True)
                        widget.set_accent(self._current_accent)
                        matches.append(widget)

        self._now_playing_card_widgets = matches
        for grid in self._all_grids_with_cards():
            grid.viewport().update()

    def _push_accent_to_now_playing_cards(self):
        # Reuses whichever cards _refresh_now_playing_outlines() last
        # found instead of re-walking every grid again - accent updates
        # happen once per *track* (even two tracks on the same album can
        # have different cover art), which is far more often than the
        # playing album actually changes, so this path needs to stay
        # cheap rather than repeating that full-grid walk on every track.
        for card in self._now_playing_card_widgets:
            try:
                card.set_accent(self._current_accent)
            except RuntimeError:
                pass  # already destroyed by a grid rebuild since the last scan

    def _clear_card_selection(self):
        # Unlike a bare `self.selected_cards = []`, this actually tells
        # each widget to visually deselect itself first - just dropping
        # the references would leave whichever card(s) they pointed to
        # permanently stuck showing their selected border, with nothing
        # left able to find and turn it back off.
        for card in self.selected_cards:
            card.set_selected(False)
        self.selected_cards = []

    def set_selected_card(self, card: AlbumCardWidget):
        self._clear_card_selection()
        # Select every on-screen copy of this album together, not just
        # the specific widget this call happened to be about - otherwise,
        # for a pinned album, only one of its two copies ever lit up
        # (whichever was created most recently), and since the pinned
        # strip is the one place that's always on screen without
        # scrolling, that made it look like the highlight was missing
        # entirely even when the other copy still had it.
        matches = self._cards_for_album(card.album_key) if card.album_key else []
        if card not in matches:
            matches.append(card)
        for widget in matches:
            widget.set_selected(True, self._current_accent)
        self.selected_cards = matches
        self.album_grid.viewport().update()
        self.pinned_grid.viewport().update()

    def handle_card_clicked(self, card: AlbumCardWidget):
        self.set_selected_card(card)
        if card.plays_immediately_on_click:
            # Home has no track-list panel of its own (that lives on the
            # Library page) - a single click here plays right away
            # instead of just updating an off-screen selection, and
            # stays on Home rather than navigating to Library. Search
            # results (also "track" mode cards) opt into this too, for
            # the same single-click-to-play convention search results
            # elsewhere already use.
            self._play_card(card)
            return
        if card.mode == "track":
            self._browse_to_track(card.album_key, card.track_path, autoplay=False)
        else:
            self.display_album_tracks_by_key(card.album_key)

    def _browse_to_track(self, album_key: Optional[str], track_path: Optional[str], autoplay: bool = True):
        # Opens a track's underlying album in the track panel with that
        # specific track cued up - used for search results, single-track
        # "smart" cards (e.g. a Jump Back In track card), and the artist
        # detail panel's Top Songs list, so clicking one continues
        # naturally into the rest of the album it's from. autoplay=False
        # just cues up the browsing state without starting playback - used
        # by handle_card_clicked above for cards that need a second
        # click/double-click to actually play.
        self.browsing_album_key = album_key
        self.browsing_tracks = self.album_tracks.get(album_key, [])

        meta = self.album_display_meta.get(album_key, {})
        self.album_heading.setText(f"{meta.get('title', '')} \u2014 {meta.get('artist', '')}")

        self.song_list_widget.clear()
        target_row = 0
        for i, path in enumerate(self.browsing_tracks):
            self.song_list_widget.addItem(self.get_track_title(path))
            if path == track_path:
                target_row = i

        if autoplay:
            self._start_playing_browsed_album()
            self.play_track_at(target_row)

    def _play_card(self, card: AlbumCardWidget):
        # Shared by Home's "single click plays immediately" cards and
        # Library's double-click-to-play - jumps straight into playback
        # rather than just updating the browsing selection.
        if card.mode == "track":
            self._browse_to_track(card.album_key, card.track_path, autoplay=True)
        else:
            self.display_album_tracks_by_key(card.album_key)
            if self.browsing_tracks:
                self._start_playing_browsed_album()
                self.play_track_at(0)

    def handle_card_double_clicked(self, card: AlbumCardWidget):
        if card.plays_immediately_on_click:
            return  # already started playing on the single click above
        self._play_card(card)

# ------------------------------------------------------------- Track panel --
    def display_album_tracks_by_key(self, key: Optional[str]):
        self.song_list_widget.clear()
        self.browsing_album_key = key
        self.browsing_tracks = self.album_tracks.get(key, [])

        meta = self.album_display_meta.get(key, {})
        self.album_heading.setText(f"{meta.get('title', '')} \u2014 {meta.get('artist', '')}")
        for path in self.browsing_tracks:
            self.song_list_widget.addItem(self.get_track_title(path))

        if self.browsing_album_key == self.active_playing_album_key and self.current_track_index != -1:
            self.song_list_widget.set_now_playing_row(self.current_track_index, animate=False)

    def handle_song_list_item_clicked(self, item: QListWidgetItem):
        row = self.song_list_widget.row(item)
        is_now_playing_row = (
            self.browsing_album_key == self.active_playing_album_key
            and row == self.current_track_index
            and self.is_playing
        )
        if is_now_playing_row:
            self.open_showcase_view()

    def play_item(self, item: QListWidgetItem):
        row = self.song_list_widget.row(item)
        self._start_playing_browsed_album()
        self.play_track_at(row)

    def play_track_at(self, row: int, autoplay: bool = True, resume_position_ms: int = 0):
        if not (0 <= row < len(self.active_playing_tracks)):
            return
        path = self.active_playing_tracks[row]
        if not os.path.exists(path):
            self.status_label.setText(f"File missing, skipping: {os.path.basename(path)}")
            self.status_label.setVisible(True)
            self.play_track_at(row + 1, autoplay=autoplay)
            return

        self.current_track_index = row
        self._load_lyrics_for_track(path)

        if self.browsing_album_key == self.active_playing_album_key:
            self.song_list_widget.set_now_playing_row(row, animate=autoplay)

        self.player.setSource(QUrl.fromLocalFile(path))
        if resume_position_ms > 0:
            # Best-effort immediate seek - harmless if the backend isn't
            # ready to honor it yet, since handle_media_status() re-applies
            # this once LoadedMedia actually confirms the track is ready.
            self.player.setPosition(resume_position_ms)
            self._pending_resume_position_ms = resume_position_ms
        if autoplay:
            self.player.play()
        self.play_btn.set_playing(autoplay)
        self.is_playing = autoplay
        self._mpris_notify({"Metadata": self.mpris_metadata(), "PlaybackStatus": "Playing" if autoplay else "Paused"})
        if HAS_DBUS_PYTHON:
            if autoplay:
                self._mpris_position_timer.start()
            # Extra insurance alongside _on_mpris_art_ready: some widgets
            # have proven picky about exactly when they first read a
            # property relative to their own startup/subscription timing
            # (same class of issue Volume had). A follow-up push a moment
            # later costs nothing and catches art that finished writing
            # just after the track started.
            QTimer.singleShot(1500, lambda: self._mpris_notify({"Metadata": self.mpris_metadata()}))

        # If we're connected to a Chromecast, push this track to it too -
        # casting was previously only triggered once, right after connecting,
        # so any track played afterwards never made it to the speaker.
        if autoplay and self.chromecast_device and self.cast_media_controller:
            self.cast_current_track()

        track_title = self.get_track_title(path)
        self.now_playing_label.setText(track_title)

        # Pull precise metadata
        extracted_artist, extracted_album, _, extracted_cover_bytes = read_track_tags(path)
        meta = self.album_display_meta.get(self.active_playing_album_key)
        # Cached for open_showcase_view()/open_lyrics_view() too (see
        # _current_display_cover_bytes) rather than each re-reading the
        # file separately.
        self._current_track_cover_bytes = extracted_cover_bytes

        final_artist = extracted_artist if extracted_artist != "Unknown Artist" else (meta.get("artist", "Unknown Artist") if meta else "Unknown Artist")
        final_album = extracted_album if extracted_album else (meta.get("title", "") if meta else "")

        self.now_playing_artist_label.setText(final_artist)

        # A virtual mix (e.g. Replay Mix) spans tracks from many different
        # real albums, so its own generated tile wouldn't mean anything as
        # "now playing" art - use this specific track's actual embedded
        # cover instead, falling back to the mix's generic tile only if
        # this particular track has no cover of its own. Regular albums
        # just use the already-decoded, cached album pixmap (same image
        # in practice, cheaper than re-decoding it from every track).
        is_virtual_mix = bool(self.active_playing_album_key) and self.active_playing_album_key.startswith("__mix__")
        display_pixmap = None
        if is_virtual_mix and extracted_cover_bytes:
            display_pixmap = self.cover_bytes_to_pixmap(extracted_cover_bytes)
        elif meta and meta.get("pixmap") is not None:
            display_pixmap = meta["pixmap"]

        if display_pixmap is not None:
            self.apply_theme(self.compute_average_color(display_pixmap))
            self.now_playing_art.setPixmap(
                display_pixmap.scaled(60, 60, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )

# LAST.FM INTEGRATION:
        # Reset the repeat lockout state for this play instance, then update Now Playing status
        self.current_scrobbled = False
        self._play_logged_for_current_track = False
        self.current_track_start_ts = int(time.time())
        session_key = self.settings.value("lfm_sk", None)

        # Don't ping Last.fm as "now playing" for a resumed-but-not-yet-
        # actually-playing track - nothing is actually playing yet.
        if autoplay and session_key and final_artist != "Unknown Artist":
            self.send_now_playing_update(final_artist, track_title, final_album)

        self.refresh_views_if_active()
        if resume_position_ms <= 0:
            # Skipped for the resume-load call itself - player.position()
            # doesn't reflect the seek above yet (media hasn't finished
            # loading), so saving here would just overwrite the correct,
            # just-read-from-settings position with a stale 0.
            self._save_resume_state()

    def _save_resume_state(self):
        # Called on every track change, periodically while playing
        # (resume_save_timer), on pause, and on close - so a crash or
        # force-quit doesn't lose more than ~10s of progress, not just a
        # clean exit.
        if self.current_track_index == -1 or not self.active_playing_album_key:
            return
        self.settings.setValue("resume_album_key", self.active_playing_album_key)
        self.settings.setValue("resume_track_row", self.current_track_index)
        self.settings.setValue("resume_position_ms", self.player.position())

    def _resolve_resume_target(self) -> tuple:
        # Pure lookup, no side effects - just figures out what (if
        # anything) should be resumed, so on_scan_complete can pre-set
        # browsing_album_key before the grid is built (for the selection
        # highlight) and actually load it after. Returns
        # (album_key, track_row, position_ms), or (None, -1, 0) if there's
        # nothing valid to resume (first-ever launch, or the saved album
        # isn't in the library anymore).
        album_key = self.settings.value("resume_album_key", None)
        if not album_key or album_key not in self.album_tracks:
            return None, -1, 0

        try:
            row = int(self.settings.value("resume_track_row", -1))
            position_ms = int(self.settings.value("resume_position_ms", 0))
        except (TypeError, ValueError):
            return None, -1, 0

        if not (0 <= row < len(self.album_tracks[album_key])):
            return None, -1, 0

        return album_key, row, max(0, position_ms)

    def play_previous(self):
        if self.current_track_index > 0:
            self.play_track_at(self.current_track_index - 1)

    def play_next(self):
        if self.playback_mode == 3 and len(self.active_playing_tracks) > 1:
            candidates = [i for i in range(len(self.active_playing_tracks)) if i != self.current_track_index]
            self.play_track_at(random.choice(candidates))
            return

        if self.current_track_index + 1 < len(self.active_playing_tracks):
            self.play_track_at(self.current_track_index + 1)
        else:
            if self.playback_mode == 1 and self.active_playing_tracks:
                self.play_track_at(0)
            else:
                self.play_btn.set_playing(False)
                self.is_playing = False
                self._mpris_notify({"PlaybackStatus": "Stopped"})
                if HAS_DBUS_PYTHON:
                    self._mpris_position_timer.stop()


# ------------------------------------------------------------- Last.FM Scrobble Operations --
    def open_lastfm_configuration(self):
        dialog = LastfmLoginDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.status_label.setText("Last.fm session connected and verified successfully!")
            self.status_label.setVisible(True)

    def _get_lastfm_credentials(self):
        """Shared credential/settings check used by both now-playing and scrobble calls."""
        api_key = LASTFM_API_KEY
        secret = LASTFM_API_SECRET
        sk = str(self.settings.value("lfm_sk", "")).strip()
        scrobble_enabled = self.settings.value("lfm_scrobble_enabled", "true") == "true"

        if not api_key or not secret or secret == "PUT_YOUR_LASTFM_API_SECRET_HERE" or not sk or not scrobble_enabled:
            return None
        return api_key, sk, secret

    def send_now_playing_update(self, artist: str, track: str, album: str):
        """Tell Last.fm a track has started playing. Does NOT count as a scrobble."""
        if hasattr(self, 'scrobble_timer') and self.scrobble_timer is not None:
            self.scrobble_timer.stop()

        creds = self._get_lastfm_credentials()
        if creds is None:
            return
        api_key, sk, secret = creds

        np_params = {
            "method": "track.updateNowPlaying",
            "artist": artist,
            "track": track,
            "api_key": api_key,
            "sk": sk
        }
        if album:
            np_params["album"] = album

        np_worker = LastfmWorker("nowplaying", np_params, secret)
        np_worker.task_finished.connect(
            lambda success, msg: self.log_lastfm_response(
                "Last.fm: Now Playing registered!" if success else f"Last.fm NP Error: {msg}"
            )
        )
        self.lfm_async_threads.append(np_worker)
        np_worker.start()

    def submit_scrobble(self, artist: str, track: str, album: str, started_at: int = None):
        """Submit a completed listen to Last.fm as a scrobble. Call this once, after playback finishes."""
        creds = self._get_lastfm_credentials()
        if creds is None:
            return
        api_key, sk, secret = creds

        # Per Last.fm spec, 'timestamp' should be when the track STARTED playing (UTC unix time),
        # not when the scrobble is submitted. Fall back to "now" if a start time wasn't tracked.
        ts = started_at if started_at else int(time.time())

        scrobble_params = {
            "method": "track.scrobble",
            "artist": artist,
            "track": track,
            "timestamp": str(ts),
            "api_key": api_key,
            "sk": sk
        }
        if album:
            scrobble_params["album"] = album

        scrobble_worker = LastfmWorker("scrobble", scrobble_params, secret)
        scrobble_worker.task_finished.connect(
            lambda success, msg: self.log_lastfm_response(
                "Last.fm: Track scrobbled successfully!" if success else f"Last.fm Scrobble Error: {msg}"
            )
        )
        self.lfm_async_threads.append(scrobble_worker)
        scrobble_worker.start()

    def dispatch_scrobble_submission(self):
        # Kept as an empty stub to ensure external hooks don't break
        pass

    def log_lastfm_response(self, msg: str):
        self.status_label.setText(msg)
        self.status_label.setVisible(True)
        # Garbage collect finished worker thread instances to keep memory consumption low
        self.lfm_async_threads = [t for t in self.lfm_async_threads if t.isRunning()]

    # ------------------------------------------------- Local play history --
    # A local, append-only log of tracks that were actually *listened to*
    # (not just skipped past) - the shared foundation Recently Played,
    # Most Played/On Repeat, and recommendations will all read from later,
    # rather than each bolting on a slightly different tracker of its own.
    # Nothing here talks to the network; it's purely local to this machine.
    def _play_history_path(self) -> str:
        # Same app-data directory convention as _library_cache_path, just
        # a different file - one JSON object per line (JSONL) rather than
        # one big JSON blob, since this grows by one small record at a
        # time for as long as the app is used, and appending a line is
        # far cheaper than rewriting a growing file from scratch on every
        # single play.
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not data_dir:
            data_dir = os.path.expanduser(os.path.join("~", ".cache", APP_NAME))
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "play_history.jsonl")

    def _maybe_log_play(self, position_ms: int):
        # Only counts once "meaningfully listened to" - past 30 seconds or
        # half the track, whichever is shorter - so instant-skipping
        # through a track doesn't pollute the data the way logging on
        # track-start would. Gated on is_playing so a resumed-but-paused
        # session (see _resolve_resume_target/play_track_at) doesn't
        # immediately re-log a play just because the saved position
        # already happened to be past the threshold - that position was
        # already earned during a previous real listen, not this one.
        if self._play_logged_for_current_track or not self.is_playing:
            return
        if self.current_track_index == -1 or not self.active_playing_tracks:
            return

        duration_ms = self.player.duration()
        if duration_ms <= 0:
            return  # duration not known yet - try again on the next tick

        threshold_ms = min(30000, duration_ms / 2)
        if position_ms < threshold_ms:
            return

        self._play_logged_for_current_track = True
        path = self.active_playing_tracks[self.current_track_index]
        # active_playing_album_key is whatever shelf/album playback was
        # started from - if that happens to be a smart mix (Replay Mix,
        # Morning Mix, On Repeat, ...), logging it as-is would mean the
        # play never counts toward Jump Back In/Recently Played/Most
        # Played, which all deliberately ignore "__mix__..." keys. Every
        # track a mix contains is still really from a real scanned album
        # though, so resolve that real album via track_to_album_key
        # first - falling back to active_playing_album_key only for a
        # path that genuinely isn't in the scanned library (shouldn't
        # normally happen, but better to log something than nothing).
        real_album_key = self.track_to_album_key.get(path, self.active_playing_album_key)
        self._log_play(path, real_album_key)
        # refresh_home_shelves() otherwise only ever runs on startup, on
        # switching to the Home tab, or on pinning/unpinning an album -
        # nothing previously re-rendered Jump Back In/Made For You/etc.
        # just because a new play got logged, so staying on the Home tab
        # while actually playing something (or playing from Home itself)
        # meant the shelves never visibly updated no matter how much
        # qualifying history piled up underneath. Cheap to call here since
        # this whole method is already gated to run at most once per
        # track (see _play_logged_for_current_track above), not on every
        # position tick.
        self.refresh_home_shelves()

    def _log_play(self, path: str, album_key: Optional[str]):
        record = {"ts": int(time.time()), "path": path, "album_key": album_key}
        try:
            with open(self._play_history_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            # Non-fatal - worst case this one play just doesn't get
            # counted, same tolerance _save_library_cache uses for its
            # own writes.
            pass

    def get_recent_plays(self, limit: Optional[int] = None, since_ts: Optional[int] = None, until_ts: Optional[int] = None) -> list:
        # Returns raw play records, most recent first: [{"ts", "path", "album_key"}, ...].
        # until_ts is an exclusive upper bound - pass both since_ts and
        # until_ts together to select a closed-open window (e.g. one
        # specific calendar month for Month Rewind) rather than just an
        # open-ended "since X" floor.
        # Malformed lines (a truncated write, a corrupt file, etc.) are
        # skipped individually rather than discarding the whole log.
        records = []
        try:
            with open(self._play_history_path(), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    ts = record.get("ts", 0)
                    if since_ts is not None and ts < since_ts:
                        continue
                    if until_ts is not None and ts >= until_ts:
                        continue
                    records.append(record)
        except OSError:
            return []

        records.reverse()  # file is oldest-first (append-only); callers want newest-first
        if limit is not None:
            records = records[:limit]
        return records

    def get_recently_played_albums(self, limit: int = 10) -> list:
        # Distinct album keys, ordered by their most recent play - an
        # album played five times shows up once, at its latest play time,
        # not five separate times.
        seen = set()
        ordered_keys = []
        for record in self.get_recent_plays():
            key = record.get("album_key")
            # Virtual mixes (Replay Mix etc.) aren't real albums someone's
            # been repeatedly listening to, and their membership shifts
            # over time anyway - excluded here so one doesn't recursively
            # show up in its own kind of shelf.
            if not key or key in seen or key.startswith("__mix__"):
                continue
            seen.add(key)
            ordered_keys.append(key)
            if len(ordered_keys) >= limit:
                break
        return ordered_keys

    def get_album_play_counts(self, since_ts: Optional[int] = None) -> dict:
        # album_key -> play count. Pass since_ts for a rolling window (e.g.
        # "last 7 days", for an On Repeat-style shelf) - omit it for
        # all-time Most Played counts.
        counts: dict = {}
        for record in self.get_recent_plays(since_ts=since_ts):
            key = record.get("album_key")
            if not key or key.startswith("__mix__"):
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_track_play_counts(self, since_ts: Optional[int] = None, until_ts: Optional[int] = None) -> dict:
        # path -> play count within [since_ts, until_ts). The shared
        # counting logic behind get_top_tracks and every other
        # track-level smart mix (On Repeat, Forgotten Favorites, Night
        # Owl/Morning Mix, Weekend Mix) - keeping it in one place means
        # they all agree on what counts as "a play" of a given track.
        counts: dict = {}
        for record in self.get_recent_plays(since_ts=since_ts, until_ts=until_ts):
            path = record.get("path")
            if not path or not os.path.exists(path):
                continue
            counts[path] = counts.get(path, 0) + 1
        return counts

    def get_top_tracks(self, since_ts: Optional[int] = None, until_ts: Optional[int] = None, limit: int = 30) -> list:
        # Individual track paths ranked by play count - the track-level
        # counterpart to get_album_play_counts, and what Replay Mix is
        # built from (see refresh_replay_mix). Unlike the album-level
        # shelves, this deliberately doesn't care which album a track
        # came from - two favorite tracks from two different albums both
        # count on equal footing here.
        counts = self.get_track_play_counts(since_ts=since_ts, until_ts=until_ts)
        ranked = sorted(counts.keys(), key=lambda p: counts[p], reverse=True)
        return ranked[:limit]

    def get_recently_added_albums(self, limit: int = 12) -> list:
        # Local stand-in for "new releases" - sorted by folder mtime (see
        # LibraryScanner), most recent first.
        keys = [k for k, meta in self.album_display_meta.items() if meta.get("added_ts")]
        keys.sort(key=lambda k: self.album_display_meta[k].get("added_ts", 0), reverse=True)
        return keys[:limit]

    def get_most_played_albums(self, limit: int = 12, since_ts: Optional[int] = None) -> list:
        # All-time Most Played by default; pass since_ts (e.g. 7 days ago)
        # for an On Repeat-style rolling window instead.
        counts = self.get_album_play_counts(since_ts=since_ts)
        keys = [k for k in counts if k in self.album_display_meta]
        keys.sort(key=lambda k: counts[k], reverse=True)
        return keys[:limit]




# ==================== CHROMECAST METHODS START HERE ====================
    def show_chromecast_menu(self):
        if self.chromecast_device:
            reply = QMessageBox.question(self, "Chromecast",
                                       f"Connected to {self.chromecast_device.name}.\nDisconnect?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.disconnect_chromecast()
            return

        self.status_label.setText("Scanning for Chromecast devices...")
        self.status_label.setVisible(True)

        def discover():
            # NOTE: pychromecast does its own mDNS discovery over the network.
            # This can legitimately take several seconds, and it must NOT touch
            # any Qt widgets directly since this runs on a background thread -
            # Qt widgets are only safe to create/use on the main (GUI) thread.
            # So we just gather the raw device list here and hand it back to
            # the main thread via a signal.
            try:
                devices, browser = pychromecast.get_chromecasts(timeout=10)
                # IMPORTANT: do NOT stop the discovery browser here. Connecting
                # to a cast device (cast.wait()) still needs this same Zeroconf
                # instance to resolve the device's host/port. Stopping it early
                # is what caused the "Zeroconf instance loop must be running"
                # crash when a device was clicked. We keep it alive and only
                # tear it down on disconnect / app close.
                self.chromecast_browser = browser
                self.chromecast_devices_found.emit(devices)
            except Exception as e:
                self.chromecast_scan_failed.emit(str(e))

        threading.Thread(target=discover, daemon=True).start()

    def _handle_chromecast_scan_failure(self, error_message: str):
        self.status_label.setText(f"Chromecast scan error: {error_message}")

    def _show_chromecast_device_menu(self, devices):
        self.status_label.setVisible(False)
        if not devices:
            self.status_label.setText(
                "No Chromecast devices found. Make sure your Nest Mini is on "
                "the same Wi-Fi network as this computer."
            )
            self.status_label.setVisible(True)
            return

        menu = QMenu(self)
        for cast in devices:
            act = QAction(cast.name, self)
            act.triggered.connect(lambda checked=False, c=cast: self.connect_to_chromecast(c))
            menu.addAction(act)

        menu.exec(self.options_btn.mapToGlobal(QPoint(0, self.options_btn.height())))

    def connect_to_chromecast(self, cast):
        self.status_label.setText(f"Connecting to {cast.name}...")
        self.status_label.setVisible(True)

        def connect():
            # cast.wait() blocks doing network I/O to resolve/handshake with
            # the device - this must not run on the GUI thread or the whole
            # window freezes (and, if it also raises, can hard-crash Qt).
            try:
                cast.wait()
                media_controller = MediaController()
                cast.register_handler(media_controller)

                # play_media() only *sends* a load request - it doesn't tell us
                # whether the device actually managed to fetch/play the file.
                # Without this listener, a failure (e.g. the Nest Mini can't
                # reach our HTTP server) happens silently on the device side
                # and we'd never know why nothing played.
                class _StatusListener:
                    def new_media_status(_self, status):
                        self.chromecast_media_status.emit(
                            f"player_state={status.player_state} "
                            f"idle_reason={status.idle_reason} "
                            f"content_id={status.content_id}"
                        )
                media_controller.register_status_listener(_StatusListener())

                self.chromecast_connected.emit((cast, media_controller))
            except Exception as e:
                self.chromecast_connect_failed.emit(str(e))

        threading.Thread(target=connect, daemon=True).start()

    def _handle_chromecast_connected(self, result):
        cast, media_controller = result
        self.chromecast_device = cast
        self.cast_media_controller = media_controller

        self.status_label.setText(f"✅ Connected to {cast.name}")
        print(f"[Chromecast] Successfully connected to: {cast.name}")  # Debug

        # Avoid playing through both the laptop and the cast device at once.
        self.audio_output.setMuted(True)

        # Match the speaker's volume to the current slider position, rather
        # than leaving it at whatever it happened to already be set to.
        try:
            cast.set_volume(self.volume_slider.value() / 100.0)
        except Exception as e:
            print(f"[Chromecast] set_volume() on connect raised: {e}")  # Debug

        # Fresh connection, fresh delay estimate - start re-measuring the
        # lyric sync offset from scratch rather than trusting a number from
        # a previous session/device.
        self.chromecast_lyric_delay_ms = 0
        self._chromecast_delay_timer.start()

        # Try to cast current track if something is playing - start it at
        # the same position the local player is already at, not from 0,
        # otherwise the seekbar shows the middle of the song while the
        # Chromecast restarts it from the beginning.
        if hasattr(self, 'player') and self.player.source().isValid():
            current_position_ms = self.player.position()
            QTimer.singleShot(1200, lambda: self.cast_current_track(current_position_ms))
        else:
            self.status_label.setText(f"✅ Connected to {cast.name} (play a song to cast)")

    def _handle_chromecast_connect_failed(self, error_message: str):
        self.status_label.setText(f"Failed to connect: {error_message}")
        print(f"[Chromecast Error] {error_message}")  # Debug
        self.chromecast_device = None
        self.cast_media_controller = None

    def _handle_chromecast_media_status(self, status_text: str):
        # The listener fires on every status tick (including things like
        # minor buffering updates), which was flooding the console with
        # repeated identical lines. Only print when the status actually
        # changed.
        if status_text == getattr(self, "_last_chromecast_status_text", None):
            return
        self._last_chromecast_status_text = status_text

        print(f"[Chromecast Status] {status_text}")  # Debug - watch for IDLE / error reasons here
        if "idle_reason=ERROR" in status_text:
            self.status_label.setText("Chromecast reported a load/playback error (see console)")
            self.status_label.setVisible(True)

    def disconnect_chromecast(self):
        if self.chromecast_device:
            try:
                self.cast_media_controller.stop()
            except:
                pass
            self.chromecast_device = None
            self.cast_media_controller = None
            self.audio_output.setMuted(False)
            self._last_chromecast_status_text = None
            self._chromecast_delay_timer.stop()
            self.chromecast_lyric_delay_ms = 0
            self.status_label.setText("Disconnected from Chromecast")

        if self.chromecast_browser:
            try:
                pychromecast.discovery.stop_discovery(self.chromecast_browser)
            except Exception:
                pass
            self.chromecast_browser = None

    def start_local_http_server(self):
        if self.local_http_server:
            return
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory="/", **kwargs)
            def end_headers(self):
                # Add CORS + advertise Range support without touching the
                # response line - calling send_response() here AND letting
                # the base do_GET() send its own response line produces a
                # malformed, double HTTP response that some clients
                # (including Chromecast) will fail to parse.
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Accept-Ranges', 'bytes')
                super().end_headers()
            def log_message(self, format, *args):
                pass  # keep the console output clean

            def do_GET(self):
                # http.server.SimpleHTTPRequestHandler ignores the Range
                # header entirely and always serves the whole file from
                # byte 0. That breaks casting mid-track: the Chromecast
                # requests "bytes=START-" to seek to a position, gets the
                # full file instead, and resets the connection because the
                # data it received doesn't match what it asked for. Handle
                # Range ourselves with a real 206 Partial Content response.
                range_header = self.headers.get('Range')
                file_path = self.translate_path(self.path)

                if not range_header or not os.path.isfile(file_path):
                    try:
                        return super().do_GET()
                    except (BrokenPipeError, ConnectionResetError):
                        return  # client (Chromecast) closed the connection early - not an error

                try:
                    file_size = os.path.getsize(file_path)
                    units, _, range_spec = range_header.partition('=')
                    if units.strip() != 'bytes':
                        raise ValueError
                    start_str, _, end_str = range_spec.partition('-')
                    start = int(start_str) if start_str else 0
                    end = int(end_str) if end_str else file_size - 1
                    end = min(end, file_size - 1)
                    if start < 0 or start > end:
                        raise ValueError
                except (ValueError, OSError):
                    self.send_error(416, "Requested Range Not Satisfiable")
                    return

                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', self.guess_type(file_path))
                self.send_header('Content-Length', str(length))
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.end_headers()

                try:
                    with open(file_path, 'rb') as f:
                        f.seek(start)
                        remaining = length
                        chunk_size = 64 * 1024
                        while remaining > 0:
                            chunk = f.read(min(chunk_size, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client (Chromecast) closed the connection early - not an error

        class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # A client (the Chromecast) closing a connection early is
                # normal - e.g. it opens a plain request, then abandons it
                # in favor of a Range request once it knows it wants to seek.
                # Only print a traceback for genuinely unexpected errors.
                exc_value = sys.exc_info()[1]
                if isinstance(exc_value, (BrokenPipeError, ConnectionResetError)):
                    return
                super().handle_error(request, client_address)

        try:
            # ThreadingHTTPServer (not plain HTTPServer) is required here:
            # when we tell the Chromecast to start mid-track (current_time > 0),
            # it issues a Range request to seek into the file. If that request
            # arrives while the original connection is still open, a
            # single-threaded HTTPServer can't answer it, and playback just
            # stalls silently after the first fraction of a second - exactly
            # the "plays briefly then stops" symptom seen when connecting
            # partway through a song.
            self.local_http_server = _QuietThreadingHTTPServer(('0.0.0.0', self.local_server_port), Handler)
            self.local_http_server.daemon_threads = True
            self.local_server_thread = threading.Thread(target=self.local_http_server.serve_forever, daemon=True)
            self.local_server_thread.start()
        except Exception as e:
            self.status_label.setText(f"Could not start local media server: {e}")
            self.status_label.setVisible(True)

    def _get_lan_ip(self) -> str:
        # socket.gethostbyname(socket.gethostname()) is unreliable - on many
        # setups (especially Windows, or machines with a VPN/virtual adapter)
        # it returns 127.0.0.1 or the IP of the wrong network interface,
        # which the Chromecast can never reach. Opening a UDP "connection"
        # (no packets are actually sent) to the cast device's own IP forces
        # the OS to pick the correct outbound-facing local address.
        try:
            target_ip = "8.8.8.8"
            if self.chromecast_device:
                # Attribute name differs across pychromecast versions.
                target_ip = getattr(
                    getattr(self.chromecast_device, "cast_info", None), "host", None
                ) or getattr(self.chromecast_device, "host", None) or "8.8.8.8"
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target_ip, 80))
                return s.getsockname()[0]
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    def cast_current_track(self, start_position_ms: int = 0):
        if not self.chromecast_device or self.current_track_index < 0:
            return
        self.start_local_http_server()

        # New load = fresh buffering on the receiver side, so the old delay
        # estimate may no longer be accurate. Drop it and let
        # _measure_chromecast_delay() re-learn it over the next couple of
        # seconds rather than carrying over a stale/wrong offset.
        self.chromecast_lyric_delay_ms = 0

        path = self.active_playing_tracks[self.current_track_index]
        filename = os.path.basename(path)
        # path already starts with '/' on Linux/macOS, so don't add another
        # one before it - avoids a doubled "//" in the URL.
        encoded = urllib.parse.quote(path.replace(os.sep, '/')).lstrip('/')

        local_ip = self._get_lan_ip()
        url = f"http://{local_ip}:{self.local_server_port}/{encoded}"
        print(f"[Chromecast] Casting URL: {url}")  # Debug - try curl'ing this from another device

        artist, album, _, _ = read_track_tags(path)
        title = clean_track_name(filename)

        try:
            # Newer pychromecast versions (14.x) removed the artist= shortcut
            # kwarg from play_media(); artist/album now go through a
            # metadata dict instead.
            self.cast_media_controller.play_media(
                url,
                content_type="audio/mpeg",
                title=title,
                current_time=max(0, start_position_ms) / 1000.0,
                autoplay=self.is_playing,
                metadata={
                    "metadataType": 3,  # MetadataType.MUSIC_TRACK
                    "title": title,
                    "artist": artist,
                    "albumName": album or "",
                },
            )
            self.status_label.setText(f"🎵 Casting: {title}")
            print(f"[Chromecast] play_media() sent for: {title}")  # Debug
        except Exception as e:
            self.status_label.setText(f"Cast error: {e}")
            print(f"[Chromecast] play_media() raised: {e}")  # Debug







    def _enter_detail_view(self, index: int, back_btn: QPushButton):
        # Only remember the tab if we're actually leaving one - if we're
        # already inside a detail view (e.g. jumping from Showcase
        # straight to Lyrics without going back to a tab first),
        # active_top_level_tab already holds the right answer, and
        # switching Showcase->Lyrics shouldn't overwrite it with
        # "Showcase", since Showcase isn't a tab itself.
        if self.view_stack.currentIndex() in (self.TAB_HOME, self.TAB_LIBRARY, self.TAB_ARTISTS):
            self.active_top_level_tab = self.view_stack.currentIndex()
        back_btn.setText(f"\u2190  {self.TAB_NAMES[self.active_top_level_tab]}")
        self._set_nav_tab_bar_visible(False)
        self.view_stack.setCurrentIndex(index)

    # ------------------------------------------------------------ Showcase view --
    def open_showcase_view(self):
        meta = self.album_display_meta.get(self.active_playing_album_key)
        if meta:
            hi_res_art = self.cover_bytes_to_pixmap(self._current_display_cover_bytes(), size=340, radius=16)
            self.showcase_art.setPixmap(hi_res_art)
            # The real per-track artist (same value shown in the
            # now-playing label and sent to MPRIS), not meta.get("artist") -
            # a virtual mix's "artist" field is actually a descriptive
            # subtitle ("Your top tracks, last 2 weeks"), not a person's
            # name.
            self.showcase_artist_lbl.setText(self.now_playing_artist_label.text() or meta.get("artist", ""))
        self.showcase_title_lbl.setText(self.now_playing_label.text())
        self._enter_detail_view(self.VIEW_SHOWCASE, self.showcase_back_btn)

    def toggle_showcase_view(self):
        if self.view_stack.currentIndex() == self.VIEW_SHOWCASE:
            self.close_current_view()
        else:
            self.open_showcase_view()

    # ------------------------------------------------------------ Lyrics view --
    def parse_lrc_timestamp(self, timestamp_str: str) -> int:
        try:
            parts = timestamp_str.split(":")
            minutes = int(parts[0])
            seconds_parts = parts[1].split(".")
            seconds = int(seconds_parts[0])
            ms = int(seconds_parts[1]) * 10 if len(seconds_parts) > 1 else 0
            if len(seconds_parts) > 1 and len(seconds_parts[1]) == 3:
                ms = int(seconds_parts[1])
            return (minutes * 60 * 1000) + (seconds * 1000) + ms
        except Exception:
            return 0

    def _load_lyrics_for_track(self, track_path: Optional[str]):
        # Parses the .lrc/.txt for whatever's about to play, unconditionally
        # on every track change - not just when the lyrics view happens to
        # be open. That's what lets sync_lyrics_scroll() keep tracking the
        # active line in the background the whole time a track plays, so
        # by the time someone actually opens the lyrics view it already
        # knows exactly where to land instead of starting from scratch.
        self.lyric_timestamps = []
        self._parsed_lyric_lines = []
        self._lyrics_have_real_data = False
        self.last_active_lyric_index = -1
        self.last_scrolled_lyric_index = -1

        if track_path:
            base_path, _ = os.path.splitext(track_path)
            lrc_path = base_path + ".lrc"
            txt_path = base_path + ".txt"
            target_file = lrc_path if os.path.exists(lrc_path) else (txt_path if os.path.exists(txt_path) else None)

            if target_file:
                try:
                    with open(target_file, "r", encoding="utf-8") as f:
                        lines = f.readlines()

                    parsed_lines = []
                    for line in lines:
                        cleaned = line.strip()
                        if cleaned:
                            match = re.match(r"\[(\d+:\d+(?:[\.:]\d+)?)\]", cleaned)
                            if match:
                                ts_ms = self.parse_lrc_timestamp(match.group(1))
                                lyric_text = re.sub(r"\[\d+:\d+(?:[\.:]\d+)?\]", "", cleaned).strip()
                                parsed_lines.append((ts_ms, lyric_text))
                            else:
                                parsed_lines.append((0, cleaned))

                    parsed_lines.sort(key=lambda x: x[0])

                    if parsed_lines:
                        self._parsed_lyric_lines = [(ts, text if text else "...") for ts, text in parsed_lines]
                        self.lyric_timestamps = [ts for ts, _ in self._parsed_lyric_lines]
                        self._lyrics_have_real_data = True
                except Exception:
                    pass

        if not self._lyrics_have_real_data:
            fallback_lines = ["", "", "Lyrics not available", "...", "Enjoy the music!"]
            self._parsed_lyric_lines = [(0, line) for line in fallback_lines]
            self.lyric_timestamps = [0] * len(fallback_lines)

    def _wrap_lyric_line_text(self, text: str, max_width: int) -> str:
        # Fits on one line as-is, or is split onto (at most) two lines at
        # whichever word boundary makes the two lines' rendered widths as
        # close to equal as possible - a "balanced" break instead of the
        # greedy default (cram as much as fits onto line 1, dump the
        # remainder onto line 2), which tends to produce one long line
        # stacked over one short one.
        #
        # The break point is baked in as a literal newline rather than
        # left to lyrics_box's own word-wrap to work out live. That
        # matters because this line's font smoothly grows/shrinks between
        # its dim (16px) and highlighted (19px) states (see
        # _animate_lyric_item) - if the break point were recomputed live
        # against the animating size, how many words fit per line changes
        # every frame, so the break visibly slides mid-animation - text
        # jumping between lines reads as the line "expanding and
        # contracting". A fixed break means only the glyphs' size
        # changes, not which words are on which line.
        #
        # Measured against 19px bold - the biggest a lyric line's font
        # ever gets, once it becomes the highlighted/active line - not
        # the resting 16px size, so a line that fits while dim doesn't
        # overflow the moment it becomes active and grows.
        metrics = QFontMetrics(QFont("SF Pro Text", 19, QFont.Weight.Bold))

        if metrics.horizontalAdvance(text) <= max_width:
            return text  # fits on one line

        words = text.split(" ")

        def width_of(word_slice) -> int:
            return metrics.horizontalAdvance(" ".join(word_slice))

        best_split = None
        best_diff = None
        for i in range(1, len(words)):
            w1 = width_of(words[:i])
            w2 = width_of(words[i:])
            if w1 <= max_width and w2 <= max_width:
                diff = abs(w1 - w2)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_split = i

        if best_split is not None:
            return " ".join(words[:best_split]) + "\n" + " ".join(words[best_split:])

        # Even the most balanced 2-line split can't fit both halves - pack
        # as much as fits onto line 1, then shorten line 2 with a trailing
        # ellipsis until it fits too.
        i = 1
        while i < len(words) and width_of(words[:i + 1]) <= max_width:
            i += 1
        remainder = words[i:]

        while remainder and width_of(remainder) > max_width:
            remainder = remainder[:-1]

        line2 = (" ".join(remainder).rstrip(",.;:- ") + "\u2026") if remainder else "\u2026"
        return " ".join(words[:i]) + "\n" + line2

    def open_lyrics_view(self):
        meta = self.album_display_meta.get(self.active_playing_album_key)
        track_title = self.now_playing_label.text()
        
        if meta:
            hi_res_art = self.cover_bytes_to_pixmap(self._current_display_cover_bytes(), size=340, radius=16)
            self.lyrics_art.setPixmap(hi_res_art)
            # Same fix as Showcase - real per-track artist, not a virtual
            # mix's descriptive subtitle.
            self.lyrics_artist_lbl.setText(self.now_playing_artist_label.text() or meta.get("artist", ""))
        else:
            self.lyrics_artist_lbl.setText("")
            
        self.lyrics_title_lbl.setText(track_title)
        
        for anim in self.lyric_item_anims.values():
            if anim is not None and anim.state() == QVariantAnimation.State.Running:
                anim.stop()

        self.lyrics_box.clear()
        self.lyric_item_anims = {}

        # Switch the page (and button state) over right away so the click
        # feels instant.
        self._enter_detail_view(self.VIEW_LYRICS, self.lyrics_back_btn)
        self.lyrics_btn.setProperty("active", "1")
        self.lyrics_btn.setIcon(self._lyrics_icon_active)
        self.lyrics_btn.style().unpolish(self.lyrics_btn)
        self.lyrics_btn.style().polish(self.lyrics_btn)

        # Everything below - spacer sizing, item population, initial scroll
        # position - depends on lyrics_box's *actual* final viewport size.
        # setCurrentIndex() above gives the page itself real geometry
        # immediately, but that still has to cascade down through the
        # nested layouts (page -> body row -> lyrics_box) before
        # viewport().height() reports the real, final number - which on a
        # fresh/first open can take one more trip through the event loop
        # than we get by just calling straight through synchronously. That
        # gap is exactly what was making the view open a few pixels off and
        # then "fix itself" as soon as anything else (like playback
        # starting) triggered another sync_lyrics_scroll() call later, once
        # the layout had caught up. singleShot(0, ...) runs this after the
        # event loop has processed that pending layout, so it's correct the
        # first time.
        QTimer.singleShot(0, self._populate_lyrics_content)

    def _populate_lyrics_content(self):
        if self.view_stack.currentIndex() != self.VIEW_LYRICS:
            return  # user already navigated away before this fired

        # Resized here (in addition to LyricsListWidget's own resizeEvent)
        # because this is the one moment we've already proven has settled,
        # correct page geometry - see viewport_h below, which relies on
        # the exact same guarantee.
        self.lyrics_box.resize_to_whole_lines()

        # The lyrics themselves were already parsed back when this track
        # started playing (see _load_lyrics_for_track), regardless of
        # whether this view was even open at the time - just build the
        # widgets from that now.
        viewport_h = self.lyrics_box.viewport().height() or 400
        spacer_h = viewport_h // 2
        top_spacer = QListWidgetItem("")
        top_spacer.setFlags(Qt.ItemFlag.NoItemFlags)
        top_spacer.setSizeHint(QSize(10, spacer_h))
        self.lyrics_box.addItem(top_spacer)

        # A little breathing room on each side rather than letting wrapped
        # text run edge-to-edge against the panel.
        lyric_max_width = max(100, self.lyrics_box.viewport().width() - 48)

        for _, text in self._parsed_lyric_lines:
            wrapped_text = self._wrap_lyric_line_text(text, lyric_max_width)
            item = QListWidgetItem(wrapped_text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if self._lyrics_have_real_data:
                item.setFont(QFont("SF Pro Text", 16, QFont.Weight.Medium))
                item.setForeground(QColor(255, 255, 255, 80))
            else:
                item.setFont(QFont("SF Pro Text", 16, QFont.Weight.Bold))
                item.setForeground(QColor(255, 255, 255, 255))
            self.lyrics_box.addItem(item)

        # Matching invisible bottom spacer so the last line can reach center too.
        bottom_spacer = QListWidgetItem("")
        bottom_spacer.setFlags(Qt.ItemFlag.NoItemFlags)
        bottom_spacer.setSizeHint(QSize(10, spacer_h))
        self.lyrics_box.addItem(bottom_spacer)

        # sync_lyrics_scroll() has been tracking last_active_lyric_index
        # in the background this whole time, even while the view was
        # closed - so rather than replaying the highlight/scroll
        # transition from nothing (which is what visibly "catches up"
        # every time you open mid-song), just snap straight to where
        # we already know we are.
        scroll_idx = self.last_active_lyric_index if self.last_active_lyric_index != -1 else 0
        self.last_scrolled_lyric_index = scroll_idx
        if self.last_active_lyric_index != -1:
            self._animate_lyric_item(self.last_active_lyric_index, 19, 19, 255, 255, QFont.Weight.Bold, duration=0)
        self._scroll_lyrics_to_index(scroll_idx, animate=False)

    def _animate_lyric_item(self, index: int, start_size: int, end_size: int,
                             start_alpha: int, end_alpha: int, weight: QFont.Weight, duration: int = 220):
        if not (0 <= index < len(self.lyric_timestamps)):
            return
        item = self.lyrics_box.item(index + 1)  # +1 skips the leading spacer row
        if item is None:
            return

        existing = self.lyric_item_anims.get(index)
        if existing is not None and existing.state() == QVariantAnimation.State.Running:
            existing.stop()

        if duration <= 0:
            # Snap straight to the end state, no fade - used when the
            # lyrics view opens already knowing which line is active (see
            # _populate_lyrics_content), so it doesn't replay a transition
            # that effectively already happened while the view was closed.
            item.setFont(QFont("SF Pro Text", round(end_size), weight))
            item.setForeground(QColor(255, 255, 255, end_alpha))
            self.lyric_item_anims.pop(index, None)
            return

        anim = QVariantAnimation(self)
        anim.setDuration(duration)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _apply(t, item=item, start_size=start_size, end_size=end_size,
                   start_alpha=start_alpha, end_alpha=end_alpha, weight=weight):
            size = start_size + (end_size - start_size) * t
            alpha = int(start_alpha + (end_alpha - start_alpha) * t)
            item.setFont(QFont("SF Pro Text", round(size), weight))
            item.setForeground(QColor(255, 255, 255, alpha))

        anim.valueChanged.connect(_apply)
        self.lyric_item_anims[index] = anim
        anim.start()

    def _measure_chromecast_delay(self):
        # Polled once a second (only while a Chromecast is connected) to
        # keep chromecast_lyric_delay_ms tracking the actual audio delay.
        if not (self.chromecast_device and self.cast_media_controller):
            return

        status = getattr(self.cast_media_controller, "status", None)
        if status is None or status.player_state != "PLAYING":
            # Paused/buffering/idle - either position isn't moving right now
            # or adjusted_current_time can't be trusted, so skip this tick
            # rather than feed in a bad sample.
            return

        try:
            cast_position_ms = status.adjusted_current_time * 1000.0
        except (TypeError, AttributeError):
            return

        local_position_ms = self.player.position()
        if local_position_ms <= 0:
            return

        delay_sample = local_position_ms - cast_position_ms

        # Sanity bounds - throw out samples taken right after a seek/track
        # change before the two positions have settled relative to each
        # other, or anything implausibly large for a home network.
        if not (-500 <= delay_sample <= 8000):
            return

        if self.chromecast_lyric_delay_ms <= 0:
            self.chromecast_lyric_delay_ms = delay_sample
        else:
            # Exponential smoothing so one noisy sample doesn't yank the
            # lyric highlight around; settles to the average delay within
            # a handful of seconds.
            self.chromecast_lyric_delay_ms = (
                0.8 * self.chromecast_lyric_delay_ms + 0.2 * delay_sample
            )

    def _lyric_sync_position(self, local_position_ms: int) -> int:
        # What the lyric view should treat as "now": the local player's
        # position, pulled back by the estimated Chromecast delay so the
        # highlighted line lines up with what's actually audible from the
        # speaker instead of the (earlier) local decode position. No-op
        # when we're not casting or haven't measured a delay yet.
        if (self.chromecast_device and self.cast_media_controller
                and self.chromecast_lyric_delay_ms > 0):
            return max(0, local_position_ms - int(self.chromecast_lyric_delay_ms))
        return local_position_ms

    def sync_lyrics_scroll(self, position: int):
        if not self.lyric_timestamps:
            return

        # The line that's actually being sung right now (or -1 if we're still before the first line).
        highlighted_idx = -1
        for i, ts in enumerate(self.lyric_timestamps):
            if position >= ts:
                highlighted_idx = i
            else:
                break

        index_changed = highlighted_idx != self.last_active_lyric_index

        if self.view_stack.currentIndex() != self.VIEW_LYRICS:
            # Lyrics view isn't on screen right now, so there's nothing to
            # animate or scroll - but keep tracking which line is active
            # regardless, so open_lyrics_view() already knows where to
            # land the moment it's opened instead of having to scroll up
            # from the top to catch up.
            self.last_active_lyric_index = highlighted_idx
            return

        # The line that should be centered on screen. This can move to the upcoming line
        # right away (e.g. line 1 as soon as the view opens), even before it's bold/sung -
        # only the visual highlight waits for the timestamp.
        scroll_idx = highlighted_idx if highlighted_idx != -1 else 0

        if not index_changed and scroll_idx == self.last_scrolled_lyric_index:
            return

        # --- Bold/dim highlight transition ---
        if index_changed:
            previous_idx = self.last_active_lyric_index
            self.last_active_lyric_index = highlighted_idx

            # Smoothly fade/shrink the line that's losing focus, and fade/grow the new one in.
            if previous_idx != -1 and previous_idx != highlighted_idx:
                self._animate_lyric_item(previous_idx, 19, 16, 255, 80, QFont.Weight.Medium)

            if highlighted_idx != -1:
                self._animate_lyric_item(highlighted_idx, 16, 19, 80, 255, QFont.Weight.Bold)

        # --- Centering scroll ---
        if scroll_idx != self.last_scrolled_lyric_index:
            self.last_scrolled_lyric_index = scroll_idx
            self._scroll_lyrics_to_index(scroll_idx, animate=True)

    def _scroll_lyrics_to_index(self, index: int, animate: bool):
        target_item = self.lyrics_box.item(index + 1)  # +1 skips the leading spacer row
        if target_item is None:
            return
        item_visual_rect = self.lyrics_box.visualItemRect(target_item)

        if item_visual_rect.isEmpty():
            return

        scrollbar = self.lyrics_box.verticalScrollBar()

        current_scroll_val = scrollbar.value()
        item_top_in_viewport = item_visual_rect.top()
        item_height = item_visual_rect.height()
        viewport_height = self.lyrics_box.viewport().height()

        target_scroll_val = current_scroll_val + item_top_in_viewport - (viewport_height // 2) + (item_height // 2)
        target_scroll_val = max(int(scrollbar.minimum()), min(int(scrollbar.maximum()), int(target_scroll_val)))

        if self.lyric_scroll_anim and self.lyric_scroll_anim.state() == QPropertyAnimation.State.Running:
            self.lyric_scroll_anim.stop()

        if not animate:
            # Jump straight there - used when the view just opened already
            # knowing the right line, so there's no visible "catch up"
            # scroll to watch.
            scrollbar.setValue(target_scroll_val)
            return

        self.lyric_scroll_anim = QPropertyAnimation(scrollbar, b"value")
        self.lyric_scroll_anim.setDuration(450) 
        self.lyric_scroll_anim.setStartValue(current_scroll_val)
        self.lyric_scroll_anim.setEndValue(target_scroll_val)
        self.lyric_scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic) 
        self.lyric_scroll_anim.start()

    def toggle_lyrics_view(self):
        if self.view_stack.currentIndex() == self.VIEW_LYRICS:
            self.close_current_view()
        else:
            self.open_lyrics_view()

    def close_current_view(self):
        self.view_stack.setCurrentIndex(self.active_top_level_tab)
        self._set_nav_tab_bar_visible(True)
        self._refresh_nav_tab_buttons()
        self.lyrics_btn.setProperty("active", "0")
        self.lyrics_btn.setIcon(self._lyrics_icon_inactive)
        self.lyrics_btn.style().unpolish(self.lyrics_btn)
        self.lyrics_btn.style().polish(self.lyrics_btn)

    def refresh_views_if_active(self):
        if self.view_stack.currentIndex() == self.VIEW_SHOWCASE:
            self.open_showcase_view()
        elif self.view_stack.currentIndex() == self.VIEW_LYRICS:
            self.open_lyrics_view()

# ------------------------------------------------------------ Playback --
    def toggle_play(self):
        if self.player.source().isEmpty():
            if self.song_list_widget.count() > 0:
                self._start_playing_browsed_album()
                self.play_track_at(0)
            return

        if self.is_playing:
            self.player.pause()
            self.play_btn.set_playing(False)
            self._mpris_notify({"PlaybackStatus": "Paused"})
            if HAS_DBUS_PYTHON:
                self._mpris_position_timer.stop()
                self._mpris_push_position()  # freeze the cache at the exact pause point
            if hasattr(self, "scrobble_timer") and self.scrobble_timer is not None:
                self.scrobble_timer.stop()
            self.resume_save_timer.stop()
            self._save_resume_state()  # freeze the resume point too, exactly like MPRIS above
            if self.chromecast_device and self.cast_media_controller:
                try:
                    self.cast_media_controller.pause()
                except Exception as e:
                    print(f"[Chromecast] pause() raised: {e}")  # Debug
        else:
            self.player.play()
            self.play_btn.set_playing(True)
            self._mpris_notify({"PlaybackStatus": "Playing"})
            if HAS_DBUS_PYTHON:
                self._mpris_position_timer.start()
            if hasattr(self, "scrobble_timer") and self.scrobble_timer is not None:
                self.scrobble_timer.start(30000)
            self.resume_save_timer.start()
            if self.chromecast_device and self.cast_media_controller:
                try:
                    self.cast_media_controller.play()
                except Exception as e:
                    print(f"[Chromecast] play() raised: {e}")  # Debug

        self.is_playing = not self.is_playing

    def cycle_playback_mode(self):
        # 0 = off, 1 = repeat playlist, 2 = repeat song, 3 = shuffle
        self.set_playback_mode((self.playback_mode + 1) % 4)

    def set_playback_mode(self, mode: int):
        self.playback_mode = mode
        self.loop_btn.set_mode(self.playback_mode)
        self._mpris_notify({
            "LoopStatus": {0: "None", 1: "Playlist", 2: "Track"}.get(mode, "None"),
            "Shuffle": mode == 3,
        })

    def handle_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._pending_resume_position_ms > 0:
            # The immediate setPosition() call in play_track_at() is only
            # best-effort - some backends ignore a seek issued before the
            # media's actually finished loading. This is the guaranteed
            # path: reapply it now that LoadedMedia confirms it's ready.
            self.player.setPosition(self._pending_resume_position_ms)
            self._pending_resume_position_ms = 0

        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            session_key = self.settings.value("lfm_sk", None)

            
            if session_key and not getattr(self, "current_scrobbled", False):
                path = self.active_playing_tracks[self.current_track_index]
                track_name = os.path.basename(path)
                artist, album, _, _ = read_track_tags(path)

                # Check display fallback meta if file tag lacks album detail
                meta = self.album_display_meta.get(self.active_playing_album_key)
                final_artist = artist if artist != "Unknown Artist" else (meta.get("artist", "Unknown Artist") if meta else "Unknown Artist")
                final_album = album if album else (meta.get("title", "") if meta else "")

                if final_artist != "Unknown Artist":
                    self.current_scrobbled = True
                    track_title = clean_track_name(track_name)
                    self.submit_scrobble(
                        final_artist, track_title, final_album,
                        getattr(self, "current_track_start_ts", None)
                    )

            # Clean out terminated threads safely from memory lists
            self.lfm_async_threads = [t for t in self.lfm_async_threads if t.isRunning()]

            # Handle repeating the same track or advancing forward using QTimer single shots.
            # This yields execution back to the Qt loop to cleanly dismantle the old media stream.
            if self.playback_mode == 2:
                # Track Loop: safely reload and replay this exact item index on next tick
                QTimer.singleShot(0, lambda: self.play_track_at(self.current_track_index))
            else:
                # Safely advance forward without stepping on the media backend thread context
                QTimer.singleShot(0, self.play_next)


    def handle_player_error(self, error, error_string: str):
        if error != QMediaPlayer.Error.NoError:
            self.status_label.setText(f"Playback error: {error_string}")
            self.status_label.setVisible(True)

    # ------------------------------------------------------------ Timeline --
    def update_timeline_position(self, position: int):
        if not self.user_is_dragging_slider:
            self.timeline_slider.setValue(position)
        self.time_elapsed_lbl.setText(format_time(position))
        # Seek bar/time label always reflect true local position; only the
        # lyric view gets the Chromecast delay compensation applied.
        self.sync_lyrics_scroll(self._lyric_sync_position(position))
        self._maybe_log_play(position)

    def update_timeline_duration(self, duration: int):
        self.timeline_slider.setRange(0, duration)
        self.time_total_lbl.setText(format_time(duration))
        # mpris:length is usually unavailable at the moment play_track_at()
        # first sends metadata (duration isn't known until the media
        # finishes loading) - resend it now that it is.
        self._mpris_notify({"Metadata": self.mpris_metadata()})

    def on_timeline_pressed(self):
        self.user_is_dragging_slider = True
        self._set_timeline_labels_adjusting(True)

    def on_timeline_released(self):
        self.user_is_dragging_slider = False
        self._set_timeline_labels_adjusting(False)
        position_ms = self.timeline_slider.value()
        self.player.setPosition(position_ms)  # local playback responds immediately, no debounce needed
        self._mpris_notify_seeked()

        # MediaController.play_media() only loads a track once - it has no
        # idea when you drag the local seek bar, since that's a purely local
        # QMediaPlayer control. We need to explicitly tell the cast device
        # to seek too, or the local UI and the Chromecast just drift apart.
        #
        # Debounced rather than sent immediately: firing one network
        # round-trip + rebuffer request to the Chromecast per seek meant
        # that seeking around quickly (clicking to a few different spots
        # in a row) could queue up requests faster than the cast device
        # could actually process and respond to them, occasionally
        # landing on an already-superseded position instead of catching
        # up to the last one. If another seek comes in before this timer
        # fires, it just gets pushed back - only the final settled
        # position actually gets sent to the cast device.
        if self.chromecast_device and self.cast_media_controller:
            self._pending_cast_seek_ms = position_ms
            self._chromecast_seek_debounce_timer.start(250)

    def _perform_debounced_chromecast_seek(self):
        if not (self.chromecast_device and self.cast_media_controller):
            return
        position_ms = self._pending_cast_seek_ms
        # MediaController.seek() operates within the cast device's existing
        # playback session, and turned out to be unreliable there - most
        # noticeably, seeking close to the end of a track would frequently
        # just get silently ignored (the local seekbar would move, but the
        # actual Chromecast audio wouldn't). cast_current_track() instead
        # does a fresh play_media() reload at the target position - the
        # same proven mechanism already used for switching tracks and for
        # resuming playback position when first connecting to a
        # Chromecast - which doesn't depend on the receiver's in-session
        # seek handling being well-behaved. Clamped a little short of the
        # real duration regardless, so a seek doesn't try to reload
        # starting at (or past) the stream's actual end.
        duration_ms = self.player.duration()
        if duration_ms > 0:
            position_ms = min(position_ms, max(0, duration_ms - 1500))
        self.cast_current_track(start_position_ms=position_ms)

    def on_timeline_moved(self, position: int):
        self.time_elapsed_lbl.setText(format_time(position))
        self.player.setPosition(position)

    def _animate_label_emphasis(self, label: QLabel, state_key: str, adjusting: bool,
                                 rest_alpha: float = 0.6, extra_css: str = ""):
        # Shared by the volume label and the timeline's elapsed/total
        # labels: dims/normal-weight at rest, brightens to full opacity +
        # bold while adjusting=True, eased smoothly either way. state_key
        # just needs to be unique per label so each one tracks its own
        # in-flight alpha/animation independently. Driven by a per-widget
        # stylesheet rather than a QGraphicsOpacityEffect - these labels
        # get setText() called on them constantly while dragging, and that
        # combined with an active opacity-compositing effect was causing
        # the text to flicker out entirely instead of fading.
        alpha_attr = f"_{state_key}_label_alpha"
        anim_attr = f"_{state_key}_label_fade_anim"

        current_alpha = getattr(self, alpha_attr, rest_alpha)
        existing_anim = getattr(self, anim_attr, None)
        if existing_anim is not None and existing_anim.state() == QVariantAnimation.State.Running:
            existing_anim.stop()

        end_alpha = 1.0 if adjusting else rest_alpha
        weight = 700 if adjusting else 600

        anim = QVariantAnimation(self)
        anim.setDuration(180)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(current_alpha)
        anim.setEndValue(end_alpha)

        def _apply(alpha, label=label, weight=weight, alpha_attr=alpha_attr, extra_css=extra_css):
            setattr(self, alpha_attr, alpha)
            # Re-declares the same properties as the label's normal QSS
            # rule in apply_theme()'s stylesheet - a per-widget
            # setStyleSheet() takes precedence over that ancestor rule, so
            # font-size/margin need to be repeated here or they'd reset to
            # Qt defaults.
            label.setStyleSheet(
                f"color: rgba(255,255,255,{alpha:.3f}); font-size: 11px; "
                f"font-weight: {weight}; {extra_css}"
            )

        anim.valueChanged.connect(_apply)
        setattr(self, anim_attr, anim)
        anim.start()

    def _set_volume_label_adjusting(self, adjusting: bool):
        self._animate_label_emphasis(self.volume_label, "volume", adjusting,
                                      rest_alpha=0.7, extra_css="margin-right: 4px;")

    def _set_timeline_labels_adjusting(self, adjusting: bool):
        self._animate_label_emphasis(self.time_elapsed_lbl, "time_elapsed", adjusting, rest_alpha=0.6)
        self._animate_label_emphasis(self.time_total_lbl, "time_total", adjusting, rest_alpha=0.6)

    def change_volume(self, value: int):
        self.audio_output.setVolume(value / 100.0)
        self.volume_label.setText(f"Vol: {value}%")
        self._mpris_notify({"Volume": value / 100.0})
        self.settings.setValue("volume_pct", value)

        # Local volume always gets set above (keeps the slider useful when
        # not casting). If we're also connected to a Chromecast, mirror the
        # same level there - cast volume is a property of the Chromecast
        # device itself (0.0-1.0), not the media controller.
        if self.chromecast_device:
            try:
                self.chromecast_device.set_volume(value / 100.0)
            except Exception as e:
                print(f"[Chromecast] set_volume() raised: {e}")  # Debug

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._update_size_grip_visibility()

    def _update_size_grip_visibility(self):
        # A resize handle doesn't make sense once the window is
        # maximized/full-screen - there's no free edge to drag.
        # changeEvent can fire a WindowStateChange synchronously from
        # inside restoreGeometry() in __init__, before init_ui() has built
        # size_grip yet - harmless to skip that early call, since
        # __init__ explicitly calls this again right after init_ui() runs.
        if not hasattr(self, "size_grip"):
            return
        self.size_grip.setVisible(not (self.isMaximized() or self.isFullScreen()))

    # Frameless windows (Qt.WindowType.FramelessWindowHint) lose the OS's
    # native edge-drag resize entirely - that's normal Qt behavior for a
    # frameless window, not something that broke. This rebuilds it by
    # hand: hovering near any edge/corner shows the matching resize
    # cursor, and dragging from there recomputes the window's geometry
    # directly from the mouse delta - no dependency on the window
    # system's own resize support (startSystemResize()), which isn't
    # reliably available for frameless windows on every Wayland/X11
    # compositor. The event filter is installed on the whole application
    # (not just this window) so edge detection still works even when the
    # cursor is technically over a child widget near the border, not the
    # bare window background.
    _RESIZE_EDGE_MARGIN = 6
    _RESIZE_CURSOR_FOR_EDGES = {
        Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
        Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
    }

    def _resize_edges_at(self, global_pos: QPoint) -> Qt.Edge:
        if self.isMaximized() or self.isFullScreen():
            return Qt.Edge(0)  # nothing to drag when there's no free edge
        local = self.mapFromGlobal(global_pos)
        rect = self.rect()
        if not rect.adjusted(-self._RESIZE_EDGE_MARGIN, -self._RESIZE_EDGE_MARGIN,
                              self._RESIZE_EDGE_MARGIN, self._RESIZE_EDGE_MARGIN).contains(local):
            return Qt.Edge(0)  # nowhere near this window at all
        edges = Qt.Edge(0)
        if local.x() <= self._RESIZE_EDGE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif local.x() >= rect.width() - self._RESIZE_EDGE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if local.y() <= self._RESIZE_EDGE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif local.y() >= rect.height() - self._RESIZE_EDGE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _apply_manual_edge_resize(self, global_pos: QPoint):
        if self._resize_start_geometry is None or self._resize_start_mouse is None:
            return
        delta = global_pos - self._resize_start_mouse
        geo = QRect(self._resize_start_geometry)
        min_w, min_h = self.minimumWidth(), self.minimumHeight()

        if self._resize_active_edges & Qt.Edge.LeftEdge:
            new_left = geo.left() + delta.x()
            max_left = geo.right() - min_w + 1
            geo.setLeft(min(new_left, max_left))
        elif self._resize_active_edges & Qt.Edge.RightEdge:
            geo.setWidth(max(min_w, geo.width() + delta.x()))

        if self._resize_active_edges & Qt.Edge.TopEdge:
            new_top = geo.top() + delta.y()
            max_top = geo.bottom() - min_h + 1
            geo.setTop(min(new_top, max_top))
        elif self._resize_active_edges & Qt.Edge.BottomEdge:
            geo.setHeight(max(min_h, geo.height() + delta.y()))

        self.setGeometry(geo)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove:
            global_pos = event.globalPosition().toPoint()
            if self._resize_active_edges:
                self._apply_manual_edge_resize(global_pos)
                return True
            edges = self._resize_edges_at(global_pos)
            cursor_shape = self._RESIZE_CURSOR_FOR_EDGES.get(edges)
            if cursor_shape != self._resize_cursor_active:
                if self._resize_cursor_active is not None:
                    QApplication.restoreOverrideCursor()
                if cursor_shape is not None:
                    QApplication.setOverrideCursor(QCursor(cursor_shape))
                self._resize_cursor_active = cursor_shape
        elif event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            edges = self._resize_edges_at(event.globalPosition().toPoint())
            if edges:
                self._resize_active_edges = edges
                self._resize_start_mouse = event.globalPosition().toPoint()
                self._resize_start_geometry = self.geometry()
                return True  # consume - don't also let it click through to whatever's underneath

            # Most of this window's transport buttons deliberately use
            # Qt.FocusPolicy.NoFocus (so clicking play/pause etc. doesn't
            # show a focus ring on them) - which also means clicking them
            # would never naturally take focus away from the search box
            # the way clicking a normal focusable widget would. Do it
            # explicitly instead, so e.g. Space still hits the play/pause
            # shortcut instead of getting typed into the search box.
            # Don't consume the event - the click should still go through
            # to whatever's actually under the cursor.
            if self.search_box.hasFocus():
                clicked_widget = obj if isinstance(obj, QWidget) else None
                if clicked_widget is not self.search_box and not (
                    clicked_widget is not None and self.search_box.isAncestorOf(clicked_widget)
                ):
                    self.search_box.clearFocus()
        elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if self._resize_active_edges:
                self._resize_active_edges = Qt.Edge(0)
                self._resize_start_mouse = None
                self._resize_start_geometry = None
                return True
        return super().eventFilter(obj, event)

    def _setup_mpris(self):
        self._mpris_service = None
        self._mpris_bridge = None
        self._mpris_thread = None

        if not HAS_DBUS_PYTHON:
            print("[MPRIS] dbus-python/PyGObject not available - skipping desktop integration. "
                  "Install with your system package manager, e.g. on Arch: "
                  "sudo pacman -S python-dbus python-gobject")
            return

        self._mpris_bridge = _MPRISBridge()
        self._mpris_bridge.nextRequested.connect(self.play_next)
        self._mpris_bridge.previousRequested.connect(self.play_previous)
        self._mpris_bridge.pauseRequested.connect(self._mpris_handle_pause)
        self._mpris_bridge.playRequested.connect(self._mpris_handle_play)
        self._mpris_bridge.playPauseRequested.connect(self.toggle_play)
        self._mpris_bridge.stopRequested.connect(self._mpris_handle_pause)
        self._mpris_bridge.seekRequested.connect(self._mpris_handle_seek)
        self._mpris_bridge.setPositionRequested.connect(self._mpris_handle_set_position)
        self._mpris_bridge.raiseRequested.connect(self._mpris_handle_raise)
        self._mpris_bridge.quitRequested.connect(self.close)
        self._mpris_bridge.setVolumeRequested.connect(self._mpris_handle_set_volume)
        self._mpris_bridge.setLoopStatusRequested.connect(self._mpris_handle_set_loop_status)
        self._mpris_bridge.setShuffleRequested.connect(self._mpris_handle_set_shuffle)
        self.mprisArtReady.connect(self._on_mpris_art_ready)
        self.mprisServiceReady.connect(self._on_mpris_service_ready)

        def _run_glib_loop():
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            try:
                bus = dbus.SessionBus()
                self._mpris_service = _MPRISService(bus, "/org/mpris/MediaPlayer2", self._mpris_bridge)
                print("[MPRIS] Registered as org.mpris.MediaPlayer2.roplayer")
            except Exception as e:
                print(f"[MPRIS] Could not register D-Bus service - skipping desktop integration: {e}")
                self._mpris_service = None
                return
            self.mprisServiceReady.emit()
            GLib.MainLoop().run()

        self._mpris_thread = threading.Thread(target=_run_glib_loop, daemon=True)
        self._mpris_thread.start()

        # The spec deliberately discourages sending Position through
        # PropertiesChanged (it'd fire every second while playing, for
        # something clients are expected to interpolate locally) - but
        # some clients poll Get("Position") directly instead, and without
        # this they'd always get back the stale value from __init__.
        # Keeping the cache fresh (without emitting a change signal for
        # it) covers both kinds of client.
        self._mpris_position_timer = QTimer(self)
        self._mpris_position_timer.setInterval(1000)
        self._mpris_position_timer.timeout.connect(self._mpris_push_position)

    def mpris_metadata(self) -> dict:
        if not (0 <= self.current_track_index < len(self.active_playing_tracks)):
            return {}

        path = self.active_playing_tracks[self.current_track_index]
        meta = self.album_display_meta.get(self.active_playing_album_key, {})
        # The real per-track artist (same value already shown in the
        # now-playing label), not meta.get("artist") - for a real album
        # those are almost always the same, but a virtual mix's "artist"
        # field is actually a descriptive subtitle ("Your top tracks,
        # last 2 weeks"), not a person's name, and that was what was
        # leaking out to MPRIS clients and OS media widgets as if it were
        # the artist.
        artist = self.now_playing_artist_label.text() or meta.get("artist", "Unknown Artist")
        artist_list = [artist] if artist and artist != "Unknown Artist" else []

        metadata = {
            "mpris:trackid": dbus.ObjectPath(f"/org/roplayer/track/{self.current_track_index}"),
            "mpris:length": dbus.Int64(int(self.player.duration()) * 1000),  # ms -> microseconds
            "xesam:title": dbus.String(self.get_track_title(path)),
            "xesam:album": dbus.String(meta.get("title", "")),
            "xesam:artist": dbus.Array([dbus.String(a) for a in artist_list], signature="s"),
            "xesam:url": dbus.String(QUrl.fromLocalFile(path).toString()),
        }

        # Per-track cover art, not meta.get("cover_bytes") directly - a
        # virtual mix has no single cover_bytes of its own (see
        # _current_display_cover_bytes), so this would otherwise just
        # never show any art at all while playing from one.
        art_url = self._mpris_art_url_if_cached(self._current_display_cover_bytes())
        if art_url:
            metadata["mpris:artUrl"] = dbus.String(art_url)

        return dbus.Dictionary(metadata, signature="sv")

    _MPRIS_ART_MAX_DIMENSION = 512

    def _mpris_art_cache_path(self, cover_bytes: bytes) -> str:
        cache_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        if not cache_dir:
            cache_dir = os.path.expanduser(os.path.join("~", ".cache", APP_NAME))
        os.makedirs(cache_dir, exist_ok=True)
        # Named by content hash AND the current max-dimension setting, so
        # if that size ever changes again (it already has once, from
        # native resolution down to this 512px cap), old files written
        # under a previous version of this logic become orphaned instead
        # of silently continuing to be served forever under the same
        # filename.
        digest = hashlib.sha1(cover_bytes).hexdigest()[:16]
        return os.path.join(cache_dir, f"mpris-art-{digest}-{self._MPRIS_ART_MAX_DIMENSION}.png")

    def _mpris_art_url_if_cached(self, cover_bytes: Optional[bytes]) -> Optional[str]:
        # Fast, main-thread-safe path: just checks whether the file already
        # exists - no image decoding here. Encoding a full-resolution PNG
        # is slow enough to visibly freeze playback if done inline on the
        # GUI thread during play_track_at(), which is what native-quality
        # art used to do. If it's not cached yet, kick off the actual
        # writing on a background thread instead and return nothing for
        # now - mpris_notify() will fire again once _on_mpris_art_ready
        # picks up the result, so the art simply appears a moment later
        # rather than blocking the track from starting.
        if not cover_bytes:
            return None
        art_path = self._mpris_art_cache_path(cover_bytes)
        if os.path.exists(art_path):
            return QUrl.fromLocalFile(art_path).toString()
        self._write_mpris_art_async(cover_bytes)
        return None

    def _write_mpris_art_async(self, cover_bytes: bytes):
        def _worker():
            try:
                art_path = self._mpris_art_cache_path(cover_bytes)
                if not os.path.exists(art_path):
                    # QImage, unlike QPixmap, is documented as safe to use
                    # off the GUI thread - no platform-native backing store
                    # to worry about, just pixel data.
                    image = QImage()
                    if not image.loadFromData(cover_bytes):
                        return
                    if (image.width() > self._MPRIS_ART_MAX_DIMENSION
                            or image.height() > self._MPRIS_ART_MAX_DIMENSION):
                        image = image.scaled(
                            self._MPRIS_ART_MAX_DIMENSION, self._MPRIS_ART_MAX_DIMENSION,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    image.save(art_path, "PNG")
                self.mprisArtReady.emit()
            except Exception as e:
                print(f"[MPRIS] Background art write failed: {e}")  # Debug

        threading.Thread(target=_worker, daemon=True).start()

    def _on_mpris_art_ready(self):
        # Previously this only pushed an update if the exact track that
        # triggered the write was still playing, and silently dropped it
        # otherwise - if you skipped tracks faster than the background
        # write finished, that update just vanished and the art never
        # showed up at all. mpris_metadata() always reflects whatever's
        # actually true *right now*, so it's safe to unconditionally
        # refresh here regardless of what originally triggered this: if
        # the currently playing track's own art still isn't ready, this
        # just omits it again and gets caught by that write's own
        # completion instead.
        self._mpris_notify({"Metadata": self.mpris_metadata()})

    def _on_mpris_service_ready(self):
        # The service constructs with a hardcoded Volume=1.0 fallback,
        # since it has no way to know the real starting volume at
        # __init__ time. The slider's own initial value gets set earlier,
        # during init_ui() - before this service even exists yet - so
        # that first (correct) notification fires into a void and is
        # silently dropped. Push the real current volume now that
        # there's actually something listening.
        #
        # busctl confirms our own service reports the correct value
        # immediately - a widget still showing the wrong number after
        # that is a client-side timing quirk, not stale data on our end
        # (the same category of issue Position had). Re-sending a couple
        # more times over the next few seconds covers the case where a
        # widget's own startup/subscription finishes after this first
        # push - cheap and harmless even if only one of them is needed.
        def _push():
            self._mpris_notify({"Volume": self.volume_slider.value() / 100.0})

        _push()
        QTimer.singleShot(1500, _push)
        QTimer.singleShot(4000, _push)

    def _mpris_notify(self, changed_props: dict):
        # The dbus-python service object lives on its own GLib loop in a
        # background thread - GLib.idle_add() is the thread-safe way to
        # schedule work to run on that loop from here (the Qt GUI thread).
        if not HAS_DBUS_PYTHON or self._mpris_service is None:
            return
        GLib.idle_add(self._mpris_service.apply_update, None, changed_props)

    def _mpris_notify_seeked(self):
        if not HAS_DBUS_PYTHON or self._mpris_service is None:
            return
        position_us = int(self.player.position()) * 1000
        GLib.idle_add(self._mpris_service.emit_seeked, position_us)
        GLib.idle_add(self._mpris_service.set_position_cache, position_us)

    def _mpris_push_position(self):
        if not HAS_DBUS_PYTHON or self._mpris_service is None:
            return
        position_us = int(self.player.position()) * 1000
        GLib.idle_add(self._mpris_service.set_position_cache, position_us)

    # --- handlers for incoming MPRIS control requests (arrive via _MPRISBridge, already on the GUI thread) ---
    def _mpris_handle_pause(self):
        if self.is_playing:
            self.toggle_play()

    def _mpris_handle_play(self):
        if not self.is_playing:
            self.toggle_play()

    def _mpris_handle_seek(self, offset_us: int):
        new_pos_ms = self.player.position() + int(offset_us / 1000)
        self.player.setPosition(max(0, new_pos_ms))
        self._mpris_notify_seeked()

    def _mpris_handle_set_position(self, position_us: int):
        self.player.setPosition(max(0, int(position_us / 1000)))
        self._mpris_notify_seeked()

    def _mpris_handle_raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _mpris_handle_set_volume(self, value: float):
        self.volume_slider.setValue(int(max(0.0, min(1.0, value)) * 100))

    def _mpris_handle_set_loop_status(self, value: str):
        # RoPlayer's repeat/shuffle is one combined 4-state cycle rather
        # than independent properties like MPRIS models it - best-effort
        # mapping onto that cycle rather than a perfect one.
        if self.playback_mode == 3:
            return  # currently shuffling - leave that alone
        target_mode = {"None": 0, "Playlist": 1, "Track": 2}.get(value)
        if target_mode is not None:
            self.set_playback_mode(target_mode)

    def _mpris_handle_set_shuffle(self, value: bool):
        if value and self.playback_mode != 3:
            self.set_playback_mode(3)
        elif not value and self.playback_mode == 3:
            self.set_playback_mode(0)

    def closeEvent(self, event):
        self.settings.setValue("window_geometry", self.saveGeometry())
        # Must happen before player.stop() below - stopping resets position
        # back to 0, which would otherwise get saved instead of wherever
        # playback actually was.
        self._save_resume_state()

        self.player.stop()
        self._chromecast_delay_timer.stop()

        if self.chromecast_device:
            try:
                if self.cast_media_controller:
                    self.cast_media_controller.stop()
            except Exception:
                pass
            try:
                # cast_media_controller.stop() only sends a STOP command
                # over the existing connection - it doesn't close it. The
                # Chromecast object keeps its own persistent background
                # thread alive (reading/writing the cast socket, parsing
                # protobuf CastChannel messages) until disconnect() is
                # called on it directly. Without this, that thread is still
                # running native code when the interpreter starts tearing
                # down module state on exit - which is what was segfaulting
                # on quit.
                self.chromecast_device.disconnect(timeout=2, blocking=True)
            except TypeError:
                # Older/newer pychromecast versions may not take these
                # kwargs - fall back to the bare call rather than skip
                # disconnecting entirely.
                try:
                    self.chromecast_device.disconnect()
                except Exception:
                    pass
            except Exception:
                pass

        if self.chromecast_browser:
            try:
                pychromecast.discovery.stop_discovery(self.chromecast_browser)
            except Exception:
                pass

        if self.local_http_server:
            try:
                self.local_http_server.shutdown()
            except Exception:
                pass

        event.accept()


class LibraryScanner(QThread):
    progress = pyqtSignal(int, int) 
    scan_complete = pyqtSignal(dict) 
    scan_failed = pyqtSignal(str)

    def __init__(self, root_folder: str):
        super().__init__()
        self.root_folder = root_folder

    def run(self):
        try:
            folders: dict[str, list[str]] = {}
            folder_mtimes: dict[str, float] = {}
            for root, dirs, files in os.walk(self.root_folder):
                dirs.sort()
                matching = [f for f in sorted(files) if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS]
                if not matching:
                    continue
                folder_name = os.path.basename(root)
                if folder_name == os.path.basename(self.root_folder):
                    folder_name = "Singles"
                for filename in matching:
                    folders.setdefault(folder_name, []).append(os.path.join(root, filename))
                # Multiple distinct paths can share the same leaf folder
                # name (matches how `folders` above is already keyed) -
                # take whichever is most recently touched among them.
                try:
                    root_mtime = os.path.getmtime(root)
                    folder_mtimes[folder_name] = max(folder_mtimes.get(folder_name, 0.0), root_mtime)
                except OSError:
                    folder_mtimes.setdefault(folder_name, 0.0)
            
            albums: dict[str, dict] = {}
            total = len(folders)
            for index, (folder_name, paths) in enumerate(folders.items(), start=1):
                artist, album_title, year, cover_bytes = read_track_tags(paths[0])
                if not album_title:
                    album_title = folder_name

                # One extra tag read per track (title only, no cover art)
                # so real embedded titles (e.g. from Picard) are used
                # instead of always falling back to the filename - paths
                # with no title tag simply aren't added here, and the
                # caller falls back to the filename for those.
                track_titles = {}
                for p in paths:
                    title = read_track_title(p)
                    if title:
                        track_titles[p] = title

                added_ts = folder_mtimes.get(folder_name, 0.0)
                key = f"{artist.lower()}|||{year}|||{album_title.lower()}"
                if key in albums:
                    albums[key]["paths"].extend(paths)
                    albums[key]["track_titles"].update(track_titles)
                    albums[key]["added_ts"] = max(albums[key].get("added_ts", 0.0), added_ts)
                else:
                    albums[key] = {
                        "title": album_title,
                        "artist": artist,
                        "year": year,
                        "paths": paths,
                        "cover_bytes": cover_bytes,
                        "track_titles": track_titles,
                        "added_ts": added_ts,
                    }
                self.progress.emit(index, total)
            self.scan_complete.emit(albums)
        except Exception as exc:
            self.scan_failed.emit(str(exc))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # Run directly from a terminal (python player.py) rather than via an
    # installed launcher, so Qt has no automatic way to know this process
    # corresponds to roplayer.desktop. Several desktop-integration features
    # - including, notably, KDE Plasma's media controller correctly
    # associating our MPRIS player with an actual application - depend on
    # this being set explicitly.
    app.setDesktopFileName("roplayer")
    player = AdaptiveMusicPlayer()
    player.show()
    exit_code = app.exec()
    # By this point closeEvent() has already run and everything that
    # matters (Last.fm state, settings, the Chromecast connection if any)
    # is flushed/closed. os._exit() skips the rest of CPython's own
    # interpreter teardown (module cleanup, GC finalizers, atexit hooks) -
    # which is where native-extension shutdown bugs like the protobuf one
    # above actually crash - rather than continuing on and risking hitting
    # one we haven't worked around.
    os._exit(exit_code)
