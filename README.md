# RoPlayer
A desktop music player for Linux, built with PyQt6. Point it at a folder of local audio files and it organizes everything into a browsable library — albums, artists, playlists, and a YouTube Music-style home screen — with cover art, synced lyrics, Last.fm scrobbling, Chromecast support, and native Linux desktop integration.

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/8b637bec-373f-4238-937a-86badab73e96" />

## Features

### Library & playback
- **Local library scanning** — recursively scans a music folder, grouping tracks into albums by folder and tag metadata. Supports `.mp3`, `.flac`, `.m4a`, `.mp4`, `.ogg`, `.oga`, `.wav`, `.wma`, and `.aac`.
- **Cover art & tags** — pulls embedded artwork and metadata (artist, album, year, title) straight from your files via Mutagen.
- **Home, Library, and Artist views** — browsable shelves and artist detail pages with photos and public listener/scrobble stats.
- **Adaptive theming** — the UI's accent color smoothly crossfades to match the average color of whatever's currently playing, so the whole app subtly shifts with your cover art.
- **Playback modes** — shuffle and repeat (track/playlist), with playback position remembered between sessions.
- **Search** — quick search across your library.

 <img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/eb1949ff-dd9c-4ab7-b767-be5fef5060f1" />

### Playlists & queue
- **Playlists** — create, rename, delete, and reorganize playlists; add or remove tracks from anywhere in the app via right-click; set a custom cover image per playlist, or let RoPlayer generate one from the tracks inside it.
- **Play queue** — a session queue separate from your playlists. "Play Next" or "Add to Queue" from any track's context menu, then drag-and-drop to reorder the queue itself.
- **Pinned albums** — pin favorite albums so they always surface at the top of your library.

### Home screen & smart mixes
- **Jump Back In** — surfaces whatever you've been listening to heavily lately, including your own generated mixes.
- **Made For You** — a set of algorithmically generated mixes built from your own listening history, each with its own generated cover art:
  - **Replay Mix** and **On Repeat** — built from your overall and current heavy-rotation listening.
  - **Forgotten Favorites** — tracks you used to play a lot but haven't touched recently.
  - **Album Rewind** and **Month Rewind** — revisits albums and stretches of your history you've drifted away from.
  - **Night Owl** and **Morning Mix** / **Weekend Mix** — time-of-day and day-of-week aware mixes.
- **Recently Played, Recently Added, Most Played, Favorites** — additional home shelves rounding out your library at a glance.
- **Artist Top Songs** — each artist page ranks their most-played tracks in your library, with a one-click toggle to play them as a mix.
- **Fan-favorite badges** — artist and album cards can show a "fan favorite" flame badge sourced from Last.fm's public listening data.

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/430e3fd8-61bf-41d1-87f9-7265f7003a48" />


### Lyrics
- **Lyrics view** — dedicated panel for following along while a track plays.
- **Synced lyrics** — automatically picks up a matching `.lrc` (or timestamped `.txt`) file alongside a track and highlights lines in time with playback; falls back to plain static lyrics when no timed file is found.

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/7f63a7a7-c490-4609-9d02-c23cacf2fcb7" />


### Scrobbling & casting
- **Last.fm scrobbling** — logs in via your browser, tracks now-playing status, and scrobbles as you listen.
- **Chromecast support** — cast playback to any Chromecast device on your network.

### Desktop integration
- **MPRIS2 integration** — media keys, system tray widgets, and lock-screen "now playing" info on Linux desktops (KDE Plasma, GNOME, etc.) via D-Bus.

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
- Browse **Home** for your generated mixes and shelves, or jump into **Library**, **Artist**, **Playlists**, or the **Queue**
- Use the search bar to jump straight to a track, album, or artist
- Right-click any track, album, or playlist to **Play Next**, **Add to Queue**, or add it to a playlist (including creating a new one on the spot)
- Pin an album from its context menu to keep it at the top of your library
- Drop a `.lrc` file next to a track (same filename, `.lrc` extension) to get synced, line-by-line lyrics
- Enable Last.fm scrobbling from the settings dialog to start tracking plays
- Right-click a Chromecast-capable device to cast playback

## Configuration & data
- Settings are stored via `QSettings` under the `RohanApps/AdaptiveMusicPlayer` namespace.
- Cached artist images, bios, and stats live in `~/.cache/AdaptiveMusicPlayer`.
- Playlists, pinned albums, and queue state are stored locally alongside your other settings.
- Last.fm session keys are stored locally per-user and are never included in this repository.

## License
MIT — see [LICENSE](LICENSE).
