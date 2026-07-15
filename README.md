# RoPlayer

A desktop music player for Linux, built with PyQt6. Point it at a folder of local audio files and it organizes everything into a browsable library — albums, artists, and a YouTube Music-style home screen — with cover art, lyrics, Last.fm scrobbling, Chromecast support, and native Linux desktop integration.

## Features

- **Local library scanning** — recursively scans a music folder, grouping tracks into albums by folder and tag metadata. Supports `.mp3`, `.flac`, `.m4a`, `.mp4`, `.ogg`, `.oga`, `.wav`, `.wma`, and `.aac`.
- **Cover art & tags** — pulls embedded artwork and metadata (artist, album, year, title) straight from your files via Mutagen.
- **Home, Library, and Artist views** — browsable shelves and artist detail pages with photos and public listener/scrobble stats.
- **Last.fm scrobbling** — logs in via your browser, tracks now-playing status, and scrobbles as you listen.
- **Lyrics view** — dedicated panel for following along while a track plays.
- **Chromecast support** — cast playback to any Chromecast device on your network.
- **MPRIS2 integration** — media keys, system tray widgets, and lock-screen "now playing" info on Linux desktops (KDE Plasma, GNOME, etc.) via D-Bus.
- **Playback modes** — shuffle and repeat (track/playlist), with playback position remembered between sessions.
- **Search** — quick search across your library.

## Requirements

- Python 3
- PyQt6 (including `QtMultimedia`)
- [Mutagen](https://mutagen.readthedocs.io/) for tag/cover-art reading
- [pychromecast](https://github.com/home-assistant-libs/pychromecast) for Chromecast support
- `dbus-python` and `PyGObject` (optional — enables MPRIS2 desktop integration on Linux; the app runs fine without them, just without media-key/tray support)

## Installation

### Arch Linux

A `PKGBUILD` is included:

```bash
makepkg -si
```

### Manual

```bash
git clone https://github.com/rohanisawesome/RoPlayer.git
cd RoPlayer
pip install PyQt6 mutagen pychromecast
python player.py
```

## Usage

Launch RoPlayer and point it at your music folder to build your library. From there:

- Browse by **Home**, **Library**, or **Artist**
- Use the search bar to jump straight to a track, album, or artist
- Enable Last.fm scrobbling from the settings dialog to start tracking plays
- Right-click a Chromecast-capable device to cast playback

## Configuration & data

- Settings are stored via `QSettings` under the `RohanApps/AdaptiveMusicPlayer` namespace.
- Cached artist images, bios, and stats live in `~/.cache/AdaptiveMusicPlayer`.
- Last.fm session keys are stored locally per-user and are never included in this repository.

## License

MIT — see [LICENSE](LICENSE).
