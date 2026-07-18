# RoPlayer

A desktop music player for Linux, built with PyQt6. Point it at a folder of local audio files and it organizes everything into a browsable library — albums, artists, and a YouTube Music-style home screen — with cover art, lyrics, Last.fm scrobbling, Chromecast support, and native Linux desktop integration.

## Features

### Home

Auto-generated, YouTube Music-style shelves built entirely from your own local listening history — nothing pulled from an online account:

- **Replay Mix** — your top tracks over the last 2 weeks
- **On Repeat** — what you're playing right now
- **Forgotten Favorites** — old favorites you've drifted away from
- **Night Owl** / **Morning Mix** / **Weekend Mix** — mixes built around when you actually listen
- **Album Rewind** — whole albums you used to spin and have since stopped
- **Month Rewind** — your top tracks from a specific past month

### Library

Recursively scans a folder of local audio files — `.mp3`, `.flac`, `.m4a`, `.mp4`, `.ogg`, `.oga`, `.wav`, `.wma`, `.aac` — grouping tracks into albums by folder and tag metadata, with cover art and metadata pulled straight from your files via Mutagen. Pin favorite albums to keep them anchored at the front of the grid regardless of sort order, and use the search bar (3+ characters) for instant filtering across your whole collection.

### Artists View

Dedicated pages per artist, pulling public bio text, listener counts, and play counts straight from Last.fm's read-only artist API (no login needed for this part), plus artist photos, top tracks — cross-referenced against your own library so anything you actually own is playable straight from the list — and top albums ranked by Last.fm listener count.

### Playlists

Build your own playlists from any tracks in your library: add and remove tracks, reorder, rename, delete, and set a custom cover image per playlist, independent of whatever art the tracks themselves carry.

### Lyrics

A dedicated full-screen panel that follows along as a track plays. RoPlayer reads lyrics from a `.lrc` or `.txt` file sitting next to the audio file itself (same filename, different extension) — a `.lrc` with timestamps gets line-by-line synced highlighting, while a plain `.txt` just displays as static lyrics. Lyrics aren't fetched from the internet automatically, so you'll need to supply your own files for a track to show anything beyond "Lyrics not available."

### Playback

- **Playback modes** — shuffle and repeat (off / repeat playlist / repeat one track), with playback position remembered between sessions
- **Queue** — a dedicated up-next view separate from whatever playlist or album you're currently browsing
- **Showcase view** — hit `Tab` for a distraction-free, full-screen now-playing view
- **Dynamic theming** — the app's accent color shifts to match whatever's playing, pulled straight from that track's own cover art, with a smooth crossfade between themes
- **Keyboard shortcuts** — `Space` to play/pause, `Tab` to toggle Showcase view

### Integration

- **Last.fm scrobbling** — logs in via your browser, tracks now-playing status, and scrobbles as you listen
- **Chromecast support** — cast playback to any Chromecast device on your network
- **MPRIS2 integration** — media keys, system tray widgets, and lock-screen "now playing" info on Linux desktops (KDE Plasma, GNOME, etc.) via D-Bus

## Requirements

- Python 3
- PyQt6 (including `QtMultimedia`)
- [Mutagen](https://mutagen.readthedocs.io/) for tag/cover-art reading
- [pychromecast](https://github.com/home-assistant-libs/pychromecast) for Chromecast support
- `dbus-python` and `PyGObject` (optional — enables MPRIS2 desktop integration on Linux; the app runs fine without them, just without media-key/tray support)

## Installation

### Arch Linux

**Via the AUR (recommended)** — installs like a normal package and shows up in future `yay -Syu` / `paru -Syu` runs:

```bash
yay -S roplayer
```

**From source** — useful if you want to build an unreleased change before it's tagged:

```bash
git clone https://github.com/rohanisawesome/RoPlayer.git
cd RoPlayer
makepkg -si
roplayer
```

### Manual (any distro)

```bash
git clone https://github.com/rohanisawesome/RoPlayer.git
cd RoPlayer
pip install PyQt6 mutagen pychromecast
python player.py
```

Optional, for MPRIS2 desktop integration (media keys, tray "now playing"):

```bash
pip install dbus-python PyGObject
```

`dbus-python` and `PyGObject` need system D-Bus/GLib headers to build. If the
`pip install` above fails, install them from your distro's repos instead
(e.g. `sudo pacman -S python-dbus python-gobject` on Arch, or
`sudo apt install python3-dbus python3-gi` on Debian/Ubuntu) — RoPlayer runs
fine without them either way, just without those extras.

#### No sound after launching?

`pip install PyQt6` installs the Qt bindings, but Qt's multimedia playback
still needs a system-level FFmpeg (or GStreamer) backend to actually decode
audio — pip can't install that part for you. If the window opens but nothing
plays:

- **Arch**: `sudo pacman -S qt6-multimedia qt6-multimedia-ffmpeg`
- **Debian/Ubuntu**: `sudo apt install ffmpeg`
- **Fedora**: `sudo dnf install qt6-qtmultimedia ffmpeg`

This is a known Linux-specific PyQt6 packaging quirk, not something specific
to RoPlayer — either Arch install path above avoids it entirely, since the
backend comes in automatically as a dependency either way.

## Usage

Launch RoPlayer and point it at your music folder to build your library. From there:

- Browse by **Home**, **Library**, or **Artist**
- Use the search bar to jump straight to a track, album, or artist
- Enable Last.fm scrobbling from the settings dialog to start tracking plays
- Right-click a Chromecast-capable device to cast playback

## Configuration & data

- Settings are stored via `QSettings` under the `RoPlayer/RoPlayer` namespace (`~/.config/RoPlayer/`).
- Cached artist images, bios, and stats live in `~/.cache/RoPlayer` (or `~/.local/share/RoPlayer` depending on your desktop's XDG config).
- Last.fm session keys are stored locally per-user and are never included in this repository.

## License

MIT — see [LICENSE](LICENSE).
