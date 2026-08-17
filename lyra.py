#!/usr/bin/env python3
"""LYRA - a pretty CLI for downloading YouTube / YouTube Music audio as Opus with album art."""

from __future__ import annotations

import argparse
import base64
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

TAGLINE = "yt-dlp powered opus ripper"


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
    def hook(d: dict) -> None:
        info = d.get("info_dict") or {}
        status = d.get("status")
        video_id = info.get("id")

        if status == "downloading":
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
        elif status == "finished" and state["task_id"] is not None:
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
            state = {"current_id": None, "task_id": None}
            try:
                with progress:
                    ydl_opts = build_ydl_opts(target_dir, quality, progress, state, cookies_from_browser)
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                embed_square_album_art(target_dir)
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

    if not args.urls:
        failures = interactive_mode(args.output_dir, args.quality, args.cookies_from_browser)
        return 1 if failures else 0

    print_banner()
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
