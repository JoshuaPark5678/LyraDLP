#!/usr/bin/env python3
"""LYRA - a pretty CLI for downloading YouTube / YouTube Music audio as Opus with album art and lyrics."""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

console = Console()


def check_ytdlp_update() -> None:
    """Check PyPI for a newer yt-dlp version and print a warning if outdated."""
    import json
    import urllib.request
    import yt_dlp

    installed = yt_dlp.version.__version__
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/yt-dlp/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]
    except Exception:
        return

    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    if parse(latest) > parse(installed):
        python = sys.executable
        console.print(
            f"[yellow]⚠ yt-dlp {installed} is outdated (latest: {latest})[/]\n"
            f"  [grey70]Run:[/] {python} -m pip install --upgrade yt-dlp\n"
        )


BANNER = r"""
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
"""

TAGLINE = "melodic opus ripper"


def print_banner() -> None:
    gradient = ["#c77dff", "#b15aff", "#9d4edd", "#7b2cbf", "#5a189a"]
    lines = BANNER.strip("\n").splitlines()
    text = Text()
    for i, line in enumerate(lines):
        color = gradient[i % len(gradient)]
        text.append(line + "\n", style=f"bold {color}")
    console.print(text)
    console.print(Text(TAGLINE.center(46), style="italic grey70"))
    console.print()


def show_track_info(info: dict) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("[bold magenta]Title[/]", info.get("title", "?"))
    table.add_row("[bold magenta]Artist[/]", info.get("artist") or info.get("uploader", "?"))
    duration = info.get("duration")
    if duration:
        mins, secs = divmod(int(duration), 60)
        table.add_row("[bold magenta]Duration[/]", f"{mins}:{secs:02d}")

    title = "[bold]Track[/]"
    index = info.get("playlist_index")
    count = info.get("playlist_count") or info.get("n_entries")
    if index and count:
        title = f"[bold]Track {index}/{count}[/]"

    console.print(Panel(table, title=title, border_style="magenta", expand=False))


def make_progress_hook(progress: Progress, state: dict):
    state.setdefault("tracks", {})

    def record_track(d: dict, info: dict) -> None:
        num = info.get("track_number") or info.get("playlist_index")
        total = (
            info.get("playlist_count")
            or info.get("n_entries")
            or info.get("total_tracks")
        )
        try:
            num_int = int(num) if num is not None else None
        except (TypeError, ValueError):
            num_int = None
        try:
            total_int = int(total) if total is not None else None
        except (TypeError, ValueError):
            total_int = None
        artist, track = guess_artist_and_title(info)
        meta = {
            "artist": artist,
            "track": track,
            "duration": info.get("duration"),
        }
        if num_int is not None:
            meta["num"] = num_int
            meta["total"] = total_int
        filename = d.get("filename")
        keys = set()
        if filename:
            keys.add(Path(filename).stem)
        video_id = info.get("id")
        if video_id:
            keys.add(f"id:{video_id}")
        for key in keys:
            state["tracks"][key] = meta

    def hook(d: dict) -> None:
        info = d.get("info_dict") or {}
        status = d.get("status")
        video_id = info.get("id")

        if status == "downloading":
            record_track(d, info)
            if video_id != state["current_id"]:
                if state["task_id"] is not None:
                    progress.remove_task(state["task_id"])
                show_track_info(info)
                state["current_id"] = video_id
                state["task_id"] = progress.add_task(
                    f"[cyan]Downloading[/] {info.get('title', '')[:40]}", total=None
                )

            task_id = state["task_id"]
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress.update(task_id, total=total, completed=d.get("downloaded_bytes", 0))
        elif status == "finished":
            record_track(d, info)
            if state["task_id"] is not None:
                progress.update(state["task_id"], description="[yellow]Converting to Opus...[/]")

    return hook


def build_ydl_opts(
    output_dir: Path,
    quality: str,
    progress: Progress,
    state: dict,
    cookies_from_browser: str | None = None,
) -> dict:
    outtmpl = str(output_dir / "%(artist,uploader)s - %(title)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": False,
        "writethumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "download_archive": str(output_dir / ".lyra-archive.txt"),
        "progress_hooks": [make_progress_hook(progress, state)],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": quality,
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    return opts


PLAYLIST_TITLE_PREFIXES = ("Album - ", "Single - ", "EP - ", "Playlist - ")


def clean_playlist_title(title: str) -> str:
    """Strip YouTube Music's auto-generated 'Album - '/'Single - '/etc. prefix
    so the folder is named after just the album/playlist itself."""
    for prefix in PLAYLIST_TITLE_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix):]
    return title


def resolve_target_dir(url: str) -> Path | None:
    """Return a subfolder path if url points to a playlist/album (so its tracks
    get organized together), or None if it's a single track."""
    import yt_dlp
    from yt_dlp.utils import sanitize_filename

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True}) as probe:
            info = probe.extract_info(url, download=False)
    except Exception:  # noqa: BLE001
        return None

    if not info or not info.get("entries"):
        return None

    title = info.get("title")
    if not title:
        return None

    return Path(sanitize_filename(clean_playlist_title(title), restricted=False))


def probe_image_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    width, height = result.stdout.strip().split("x")
    return int(width), int(height)


def crop_to_square(image_path: Path) -> Path:
    """Center-crop image_path to a square JPEG. Returns the new file's path,
    or image_path unchanged if it's already square."""
    width, height = probe_image_size(image_path)
    if width == height:
        return image_path

    square_path = image_path.with_name(image_path.stem + ".cover.jpg")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(image_path),
            "-vf", "crop=min(iw\\,ih):min(iw\\,ih)",
            str(square_path),
        ],
        check=True,
    )
    return square_path


MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def embed_cover_art(opus_path: Path, image_path: Path) -> None:
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus

    size, _ = probe_image_size(image_path)
    picture = Picture()
    picture.type = 3
    picture.desc = "Cover"
    picture.mime = MIME_TYPES.get(image_path.suffix.lower(), "image/jpeg")
    picture.width = size
    picture.height = size
    picture.depth = 24
    picture.data = image_path.read_bytes()

    audio = OggOpus(str(opus_path))
    audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def embed_track_numbers(target_dir: Path, track_map: dict) -> None:
    """Write TRACKNUMBER / TRACKTOTAL vorbis comments into each .opus file.

    track_map keys are either the file stem (e.g. 'Artist - Title') captured
    from yt-dlp's progress hook filename, or 'id:<video_id>' as a fallback.
    Values are dicts with 'num'/'total' (plus artist/track/duration for lyrics).
    """
    if not track_map:
        return
    from mutagen.oggopus import OggOpus

    for opus_path in target_dir.glob("*.opus"):
        meta = track_map.get(opus_path.stem)
        if meta is None:
            continue
        num = meta.get("num")
        if num is None:
            continue
        try:
            audio = OggOpus(str(opus_path))
            audio["tracknumber"] = str(num)
            total = meta.get("total")
            if total is not None:
                audio["tracktotal"] = str(total)
                audio["totaltracks"] = str(total)
            audio.save()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]⚠ Couldn't embed track number for {opus_path.name}:[/] {exc}")


def embed_square_album_art(target_dir: Path) -> None:
    """Find each track's downloaded thumbnail, crop it to a square, embed it,
    then remove the raw thumbnail file."""
    for opus_path in target_dir.glob("*.opus"):
        thumbnail_path = next(
            (p for ext in (".jpg", ".jpeg", ".png", ".webp") if (p := opus_path.with_suffix(ext)).exists()),
            None,
        )
        if thumbnail_path is None:
            continue
        try:
            square_path = crop_to_square(thumbnail_path)
            embed_cover_art(opus_path, square_path)
            if square_path != thumbnail_path:
                square_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]⚠ Couldn't embed album art for {opus_path.name}:[/] {exc}")
        finally:
            thumbnail_path.unlink(missing_ok=True)


def cleanup_orphan_thumbnails(output_dir: Path) -> None:
    """Remove leftover cover-art images (e.g. a playlist/album's own thumbnail)
    that don't belong to any downloaded track."""
    audio_stems = {p.stem for p in output_dir.glob("*.opus")}
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
        for img in output_dir.glob(ext):
            if img.stem not in audio_stems:
                img.unlink(missing_ok=True)


LRCLIB_API = "https://lrclib.net/api"
LRCLIB_HEADERS = {"User-Agent": "LYRA/1.0"}

# YouTube decorations like '(Official Music Video)' or '[Lyrics HD]' that
# would poison lyrics lookups.
OFFICIAL_TAG_JUNK = re.compile(
    r"[\(\[\{][^\)\]\}\n]*"
    r"(official|video|audio|lyric|remaster|hd|hq|mv)"
    r"[^\)\]\}\n]*[\)\]\}]",
    re.IGNORECASE,
)


def normalize_text(text: str | None) -> str:
    """Lowercase and drop everything but letters/digits, so fuzzy comparisons
    ignore punctuation, spaces, and case."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def guess_artist_and_title(info: dict) -> tuple[str | None, str | None]:
    """Best-effort (artist, title) for lyrics lookups. Real metadata is used
    when yt-dlp resolved it (e.g. YouTube Music); plain YouTube uploads usually
    only have titles like 'Artist - Song (Official Video)' to work from."""
    title = (info.get("track") or info.get("title") or "").strip()
    if not title:
        return None, None
    title = OFFICIAL_TAG_JUNK.sub("", title).strip(" -")

    artist = info.get("artist") or info.get("creator")
    if not artist:
        # YouTube 'topic' channels upload under names like 'Artist - Topic'
        artist = re.sub(r"\s*-\s*Topic$", "", info.get("uploader") or "") or None

    if " - " in title and not info.get("track"):
        prefix, rest = title.split(" - ", 1)
        if not artist or normalize_text(prefix) != normalize_text(artist):
            artist, title = prefix, rest

    return artist or None, title or None


def lrclib_get(path: str, params: dict) -> dict | list | None:
    """GET one JSON resource from LRCLIB. Returns None for an empty body;
    HTTP errors (like a 404 on /api/get) raise and are handled by the caller.
    Transient upstream hiccups (429 / 5xx) get one retry after a short wait."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    query = urllib.parse.urlencode(params)
    url = f"{LRCLIB_API}{path}?{query}"
    for attempt in range(MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers=LRCLIB_HEADERS)
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read())
            return body or None
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def fetch_lyrics(artist: str | None, track: str | None, duration: float | None) -> dict | None:
    """Fetch lyrics for a track from LRCLIB. /api/get fuzzily matches on
    artist/track/duration server-side; if it has no exact-enough hit, widen to
    /api/search and score the candidates locally. Returns the result dict
    (plainLyrics / syncedLyrics keys) or None when nothing matches."""
    if not track:
        return None

    try:
        query = {"track_name": track}
        if artist:
            query["artist_name"] = artist
        if duration:
            query["duration"] = str(int(duration))
        exact = lrclib_get("/get", query)
    except Exception:  # noqa: BLE001
        exact = None
    if exact:
        return exact

    try:
        results = lrclib_get("/search", {"q": f"{artist or ''} {track}".strip()})
    except Exception:  # noqa: BLE001
        return None
    return pick_best_lyrics(results or [], artist, track, duration)


def pick_best_lyrics(
    results: list[dict],
    artist: str | None,
    track: str | None,
    duration: float | None,
) -> dict | None:
    """Score /api/search candidates: a track-title match beats an artist match,
    which beats duration closeness (within a few seconds counts as near)."""

    def score(result: dict) -> float:
        value = 0.0
        if result.get("trackName") and normalize_text(result["trackName"]) == normalize_text(track):
            value += 2
        if result.get("artistName") and normalize_text(result["artistName"]) == normalize_text(artist):
            value += 1
        result_duration = result.get("duration")
        if duration and result_duration:
            value += max(0.0, 1 - abs(float(result_duration) - float(duration)) / 10)
        return value

    return max(results, key=score, default=None)


def write_lrc_sidecar(
    sidecar_path: Path,
    artist: str | None,
    title: str | None,
    album: str | None,
    synced: str,
) -> None:
    """Write an .lrc file next to the audio. Players like rmpc match these
    files by the [ti:]/[ar:] header tags (NOT by filename), so including them
    is what makes the sidecar findable."""
    header = "".join(
        f"[{tag}:{value}]\n"
        for tag, value in (("ar", artist), ("ti", title), ("al", album))
        if value
    )
    sidecar_path.write_text(header + synced, encoding="utf-8")


def sidecar_has_metadata(sidecar: Path) -> bool:
    content = sidecar.read_text(encoding="utf-8", errors="ignore")
    return content.lstrip().startswith(("[ti:", "[ar:"))


def synthesize_synced_lyrics(text: str, duration: float | None) -> str:
    """Distribute plain-text lyrics evenly across the song's duration and
    return them as LRC content. The timing is invented (visual filler for
    players like rmpc, which cannot display untimed lyrics at all), so prefer
    real synced lyrics whenever they exist."""
    lines = [line.rstrip() for line in (text or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""

    step = max(1.0, ((duration or len(lines) * 4.0) - 1.0) / len(lines))
    out = []
    t = 0.5
    for line in lines:
        mins, whole_secs = divmod(int(t), 60)
        frac = int(round((t % 60) * 100)) % 100
        out.append(f"[{mins:02d}:{whole_secs % 60:02d}.{frac:02d}]{line}")
        t += step
    return "\n".join(out) + "\n"


def embed_lyrics(target_dir: Path, track_map: dict) -> None:
    """Fetch lyrics from LRCLIB for each downloaded track and embed them as
    vorbis comments: LYRICS (human-readable text) and SYNCEDLYRICS (LRC
    timestamps). LYRICS gets the LRC content as a fallback when no plain text
    version exists, so every player shows something readable. When synced lyrics
    exist, a same-named .lrc sidecar (with [ti:]/[ar:] metadata headers, as
    players like rmpc match by those) is written next to the audio.

    track_map is the same dict embed_track_numbers reads; matching metadata is
    captured by record_track during the download hook.
    """
    if not track_map:
        return

    for opus_path in target_dir.glob("*.opus"):
        meta = track_map.get(opus_path.stem)
        if not meta or not meta.get("track"):
            continue

        lyrics = fetch_lyrics(meta.get("artist"), meta["track"], meta.get("duration"))
        text = (lyrics or {}).get("plainLyrics")
        synced = (lyrics or {}).get("syncedLyrics")
        if not text and not synced:
            console.print(f"[grey70]No lyrics found for {opus_path.stem}[/]")
            continue

        try:
            from mutagen.oggopus import OggOpus

            audio = OggOpus(str(opus_path))
            audio["LYRICS"] = text or synced
            if synced:
                audio["SYNCEDLYRICS"] = synced
            audio.save()
            lrc_content = synced or synthesize_synced_lyrics(
                text, meta.get("duration")
            )
            if lrc_content:
                # Headers come from the file's own tags, since that's what the
                # player sees and compares the .lrc against.
                write_lrc_sidecar(
                    opus_path.with_suffix(".lrc"),
                    (audio.get("artist") or [None])[0],
                    (audio.get("title") or [None])[0],
                    (audio.get("album") or [None])[0],
                    lrc_content,
                )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]⚠ Couldn't embed lyrics for {opus_path.name}:[/] {exc}")


# Every audio format -L will walk. Mutagen reads tags for all of these the
# same way (mutagen.File(..., easy=True)), but each family stores lyrics
# differently, hence STORES_LYRICS_EMBEDDED below.
LYRIC_GLOBS = ("*.opus", "*.ogg", "*.mp3", "*.m4a", "*.flac", "*.wav", "*.wma")

# Formats whose tags can hold lyrics directly (.lrc sidecar is still written
# alongside them). Formats not in this set get sidecar-only treatment, so
# lyrics turn up in the player without LYRA writing tags it doesn't know how
# to write.
STORES_LYRICS_EMBEDDED = {".opus", ".ogg", ".mp3", ".m4a", ".flac"}


def first_tag(audio, key: str) -> str | None:
    values = audio.get(key)
    return values[0] if values else None


def read_tag_info(audio) -> tuple[str | None, str | None, str | None, float | None]:
    artist = first_tag(audio, "artist")
    title = first_tag(audio, "title")
    album = first_tag(audio, "album")
    duration = audio.info.length if audio.info else None
    return artist, title, album, duration


def read_embedded_lyrics(audio_path: Path, suffix: str) -> tuple[str | None, str | None]:
    """Back out (plain, synced) from whatever lyrics the format natively
    stores. Opens the format's own tag class, since the easy-format view
    mutagen gives us for tag reading can't see lyric frames."""
    try:
        if suffix in (".opus", ".ogg", ".flac"):
            from mutagen.flac import FLAC
            from mutagen.oggopus import OggOpus

            audio = OggOpus(str(audio_path)) if suffix != ".flac" else FLAC(str(audio_path))
            return first_tag(audio, "LYRICS"), first_tag(audio, "SYNCEDLYRICS")
        if suffix == ".mp3":
            from mutagen.id3 import ID3

            frames = ID3(str(audio_path)).getall("USLT")
            return (frames[0].text if frames else None), None
        if suffix == ".m4a":
            from mutagen.mp4 import MP4

            return first_tag(MP4(str(audio_path)), "\xa9lyr"), None
    except Exception:  # noqa: BLE001
        pass
    return None, None


def embed_lyric_tags(
    audio_path: Path,
    text: str,
    synced: str | None,
) -> None:
    """Write lyrics into the file's own tags, dispatching by format family.
    Synced lyrics are embedded only where a genre supports it; mp3/m4a get the
    plain text (their synced counterparts, SYLT/CHAP, are rarely supported by
    players, the .lrc sidecar covers timed display)."""
    suffix = audio_path.suffix.lower()

    if suffix == ".mp3":
        from mutagen.id3 import ID3, USLT

        audio = ID3(str(audio_path))
        # Rating-tolerant replace: drop all USLT frames so a stale lyric
        # doesn't survive next to the fresh one.
        audio.delall("USLT")
        audio.add(USLT(encoding=3, lang="eng", desc="", text=text))
        audio.save()
        return

    if suffix == ".m4a":
        from mutagen.mp4 import MP4

        audio = MP4(str(audio_path))
        audio["\xa9lyr"] = [text]
        audio.save()
        return

    # Vorbis comment family: LYRICS + SYNCEDLYRICS
    from mutagen.oggopus import OggOpus

    if suffix == ".flac":
        from mutagen.flac import FLAC

        audio = FLAC(str(audio_path))
        audio["LYRICS"] = text or synced
        if synced:
            audio["SYNCEDLYRICS"] = synced
        audio.save()
        return

    audio = OggOpus(str(audio_path))
    audio["LYRICS"] = text or synced
    if synced:
        audio["SYNCEDLYRICS"] = synced
    audio.save()


def refresh_lyrics(output_dir: Path) -> None:
    """Backfill lyrics for audio files already in the library under output_dir,
    whatever the format (opus, mp3, m4a, ... — including files not downloaded
    by LYRA originally).

    Files with embedded synced lyrics just get their .lrc sidecar written.
    Header-less sidecars from older LYRA versions get rewritten with metadata
    headers (that's all rmpc matches on). Files with no lyrics tags at all get
    looked up on LRCLIB, embedded where the format supports it (tagged sidecar
    only otherwise), and sidecar'd. Files that already have a findable sidecar
    are skipped, so repeated runs stay cheap and gentle on the API.
    """
    paths = [p for ext in LYRIC_GLOBS for p in output_dir.rglob(ext)]
    for audio_path in sorted(paths):
        suffix = audio_path.suffix.lower()
        sidecar = audio_path.with_suffix(".lrc")
        if sidecar.exists() and sidecar_has_metadata(sidecar):
            continue

        try:
            import mutagen

            audio = mutagen.File(str(audio_path), easy=True)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]⚠ Couldn't read {audio_path.name}:[/] {exc}")
            continue
        if audio is None:
            continue

        artist, title, album, duration = read_tag_info(audio)

        if sidecar.exists():
            content = sidecar.read_text(encoding="utf-8", errors="ignore")
            write_lrc_sidecar(sidecar, artist, title, album, content)
            console.print(f"[grey70]Rewrote {sidecar.name} with metadata headers[/]")
            continue

        embedded_plain, embedded_synced = read_embedded_lyrics(audio_path, suffix)
        synced = embedded_synced
        if synced:
            write_lrc_sidecar(sidecar, artist, title, album, synced)
            console.print(f"[grey70]Lyrics for {audio_path.stem} (sidecar from embedded tags)[/]")
            continue

        # Real synced lyrics from LRCLIB always beat embedded plain text —
        # files obtained elsewhere often carry an untimed USLT/©lyr copy, and
        # inventing timestamps for that (especially with instrumental intros)
        # desyncs from the actual audio.
        fetched = fetch_lyrics(artist, title, duration)
        text = (fetched or {}).get("plainLyrics")
        fetched_synced = (fetched or {}).get("syncedLyrics")
        if fetched_synced or text:
            try:
                if suffix in STORES_LYRICS_EMBEDDED:
                    embed_lyric_tags(audio_path, text or fetched_synced, fetched_synced)
                    console.print(f"[green]✓ Lyrics for {audio_path.name}[/]")
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]⚠ Couldn't embed lyrics for {audio_path.name}:[/] {exc}")
            lrc_content = fetched_synced or synthesize_synced_lyrics(text, duration)
            if lrc_content:
                write_lrc_sidecar(sidecar, artist, title, album, lrc_content)
            time.sleep(1)
            continue

        if embedded_plain:
            content = synthesize_synced_lyrics(embedded_plain, duration)
            if content:
                write_lrc_sidecar(sidecar, artist, title, album, content)
                console.print(f"[grey70]Approx-timed lyrics for {audio_path.stem} (evenly spaced, no match on LRCLIB)[/]")
                continue

        console.print(f"[grey70]No lyrics found for {audio_path.stem}[/]")
        continue


# Splits joined-up artist names ('A, B', 'A feat. B', 'A; B') into separate
# tag values.
MULTI_VALUE_SPLIT = re.compile(r"\s*(?:,\s*|;\s+|\s(?:feat|ft|featuring)\.?\s+)", re.IGNORECASE)


def split_tag_values(value: str) -> list[str]:
    parts = [part.strip() for part in MULTI_VALUE_SPLIT.split(value)]
    return [part for part in parts if part]


def split_multi_artist_tags(target_dir: Path) -> None:
    """Rewrite ARTIST/ALBUMARTIST values that contain several artists
    ('Porter Robinson, TEED') as one tag value per artist. Without this,
    players show 'Porter Robinson, TEED' as a whole separate artist.

    Known tradeoff: real artists whose names contain commas
    ('Earth, Wind & Fire' style) would get split too; LYRA assumes those
    don't appear in your library more often than collabs do.
    """
    from mutagen.oggopus import OggOpus

    for opus_path in target_dir.glob("*.opus"):
        try:
            audio = OggOpus(str(opus_path))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]⚠ Couldn't read {opus_path.name}:[/] {exc}")
            continue

        changed = False
        for tag in ("artist", "albumartist"):
            values = audio.get(tag)
            if not values:
                continue
            split = [part for value in values for part in split_tag_values(value)]
            if split != values:
                audio[tag] = split
                changed = True
        if changed:
            try:
                audio.save()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]⚠ Couldn't save {opus_path.name}:[/] {exc}")


MAX_ATTEMPTS = 3


def download(
    urls: list[str],
    output_dir: Path,
    quality: str,
    cookies_from_browser: str | None = None,
) -> int:
    import yt_dlp

    failures = 0

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    for url in urls:
        subfolder = resolve_target_dir(url)
        target_dir = output_dir / subfolder if subfolder else output_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            state = {"current_id": None, "task_id": None, "tracks": {}}
            try:
                with progress:
                    ydl_opts = build_ydl_opts(target_dir, quality, progress, state, cookies_from_browser)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                embed_track_numbers(target_dir, state["tracks"])
                embed_square_album_art(target_dir)
                embed_lyrics(target_dir, state["tracks"])
                split_multi_artist_tags(target_dir)
                cleanup_orphan_thumbnails(target_dir)
                console.print(f"[bold green]✓[/] Saved to [underline]{target_dir}[/]\n")
                break
            except Exception as exc:  # noqa: BLE001
                if attempt < MAX_ATTEMPTS:
                    console.print(
                        f"[yellow]⚠ Attempt {attempt}/{MAX_ATTEMPTS} failed, retrying...[/] [grey70]{exc}[/]"
                    )
                    time.sleep(2 * attempt)
                else:
                    failures += 1
                    console.print(f"[bold red]✗ Failed:[/] {url}\n  [red]{exc}[/]\n")

    return failures


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lyra",
        description="Download YouTube / YouTube Music audio as Opus with embedded album art.",
    )
    parser.add_argument(
        "urls", nargs="*", default=[],
        help="One or more YouTube / YouTube Music URLs. Omit to launch interactive mode.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=Path.home() / "Music" / "Lyra",
        help="Directory to save downloaded tracks (default: ~/Music/Lyra)",
    )
    parser.add_argument(
        "-q", "--quality",
        default="0",
        help="Opus quality passed to ffmpeg (0 = best VBR, default: 0)",
    )
    parser.add_argument(
        "-c", "--cookies-from-browser",
        default=None,
        metavar="BROWSER",
        help="Read cookies from BROWSER (chrome, firefox, edge, brave, opera, safari, vivaldi, whale) "
             "to authenticate as your logged-in session. Helps with 403s on some tracks.",
    )
    parser.add_argument(
        "-L", "--refresh-lyrics",
        action="store_true",
        help="Skip downloading; fetch lyrics and .lrc sidecars for audio files "
             "(.opus, .mp3, .m4a, .flac, ...) already under --output-dir, "
             "including files not downloaded by LYRA",
    )
    return parser.parse_args(argv)


def print_settings(output_dir: Path, quality: str, cookies_from_browser: str | None) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("[grey70]Output dir[/]", str(output_dir))
    table.add_row("[grey70]Quality[/]", f"opus VBR {quality}")
    table.add_row("[grey70]Cookies from[/]", cookies_from_browser or "none")
    console.print(Panel(table, title="[bold]Settings[/]", border_style="grey50", expand=False))


HELP_TEXT = """\
[bold]Commands[/]
  [cyan]<url> [url2 ...][/]   queue and download one or more links (playlists auto-detected)
  [cyan]output <dir>[/]       change the output directory
  [cyan]quality <0-10>[/]     change opus VBR quality (0 = best)
  [cyan]cookies <browser>[/]  authenticate using BROWSER's cookies (or 'none' to clear)
  [cyan]settings[/]           show current settings
  [cyan]help[/]               show this message
  [cyan]quit[/] / [cyan]exit[/]         leave LYRA
"""


def interactive_mode(output_dir: Path, quality: str, cookies_from_browser: str | None) -> int:
    print_banner()
    check_ytdlp_update()
    print_settings(output_dir, quality, cookies_from_browser)
    console.print("[grey70]Paste a URL and hit enter. Type 'help' for commands.[/]\n")

    failures = 0
    while True:
        try:
            line = Prompt.ask("[bold magenta]lyra[/][grey70]>[/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[grey70]Bye![/]")
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            console.print("[grey70]Bye![/]")
            break
        elif cmd == "help":
            console.print(Panel(HELP_TEXT, border_style="grey50", expand=False))
        elif cmd == "settings":
            print_settings(output_dir, quality, cookies_from_browser)
        elif cmd == "output" and len(parts) > 1:
            output_dir = Path(" ".join(parts[1:])).expanduser()
            console.print(f"[green]Output directory set to[/] {output_dir}")
        elif cmd == "quality" and len(parts) > 1:
            quality = parts[1]
            console.print(f"[green]Quality set to[/] opus VBR {quality}")
        elif cmd == "cookies" and len(parts) > 1:
            cookies_from_browser = None if parts[1].lower() == "none" else parts[1].lower()
            console.print(f"[green]Cookies from:[/] {cookies_from_browser or 'none'}")
        else:
            urls = parts
            console.print()
            failures += download(urls, output_dir, quality, cookies_from_browser)

    return failures


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.refresh_lyrics:
        print_banner()
        refresh_lyrics(args.output_dir)
        return 0

    if not args.urls:
        failures = interactive_mode(args.output_dir, args.quality, args.cookies_from_browser)
        return 1 if failures else 0

    print_banner()
    check_ytdlp_update()
    console.print(f"[grey70]Output directory:[/] {args.output_dir}")
    console.print(f"[grey70]Tracks queued:[/] {len(args.urls)}\n")

    failures = download(args.urls, args.output_dir, args.quality, args.cookies_from_browser)

    console.print()
    if failures:
        console.print(Panel(f"[bold red]{failures} download(s) failed[/]", border_style="red"))
        return 1
    console.print(Panel("[bold green]All done![/]", border_style="green"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
