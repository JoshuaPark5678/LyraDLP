# LYRA

A CLI for ripping YouTube / YouTube Music links to Opus audio.

```
 888
 888
 888
 888          888  888 888d888  8888b.
 888          888  888 888P"       "88b
 888          888  888 888     .d888888
 888     d88  Y88b 888 888     888  888
 88888888888   "Y88888 888     "Y888888
                   888
              Y8b d88P
               "Y88P"
```

## Features

- Downloads to Opus (best VBR quality by default) via `yt-dlp` + `ffmpeg`
- Embeds album art, automatically center-cropped to a square if the source thumbnail isn't
- Auto-detects playlists/albums and organizes them into their own subfolder
- Interactive mode: launch with no arguments and it drops into a `lyra>` prompt that waits for links
- Retries failed downloads automatically (YouTube's bot-detection can be flaky)
- Optional `--cookies-from-browser` for tracks that need an authenticated session

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) (`ffmpeg` and `ffprobe` on your `PATH`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

Interactive mode (recommended) — launch with no arguments and paste links as you go:

```bash
./lyra
```

Or pass links directly:

```bash
./lyra "https://music.youtube.com/watch?v=..." [more URLs...]
```

Playlist/album links are detected automatically and downloaded into their own subfolder.

### Options

```
usage: lyra [-h] [-o OUTPUT_DIR] [-q QUALITY] [-c BROWSER] [urls ...]

positional arguments:
  urls                  One or more YouTube / YouTube Music URLs. Omit to
                        launch interactive mode.

options:
  -h, --help            show this help message and exit
  -o, --output-dir OUTPUT_DIR
                        Directory to save downloaded tracks (default: ~/Music/Lyra)
  -q, --quality QUALITY
                        Opus quality passed to ffmpeg (0 = best VBR, default: 0)
  -c, --cookies-from-browser BROWSER
                        Read cookies from BROWSER (chrome, firefox, edge, brave,
                        opera, safari, vivaldi, whale) to authenticate as your
                        logged-in session. Helps with 403s on some tracks.
```

### Interactive commands

```
<url> [url2 ...]   queue and download one or more links (playlists auto-detected)
output <dir>       change the output directory
quality <0-10>     change opus VBR quality (0 = best)
cookies <browser>  authenticate using BROWSER's cookies (or 'none' to clear)
settings           show current settings
help               show this message
quit / exit        leave LYRA
```
