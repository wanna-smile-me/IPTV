from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = ROOT / ".github" / "iptv_sources.txt"
DEFAULT_OUTPUT = ROOT / "IPTV.m3u"
DEFAULT_YW_OUTPUT = ROOT / "ywIPTV.m3u"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    extinf: str
    url: str


def load_sources(path: Path) -> list[str]:
    sources = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not sources:
        raise SyncError(f"no sources found in {path}")
    if any(not url.startswith(("http://", "https://")) for url in sources):
        raise SyncError("source list contains a non-HTTP URL")
    return sources


def fetch(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": "iptv-sync/1.0"})
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise SyncError(f"HTTP {response.status}")
        return response.read().decode("utf-8-sig")


def parse_m3u(text: str) -> list[Entry]:
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise SyncError("playlist is missing #EXTM3U")
    entries: list[Entry] = []
    current: str | None = None
    for line in lines[1:]:
        if line.startswith("#EXTINF:"):
            if current is not None or "," not in line:
                raise SyncError("playlist contains an incomplete #EXTINF")
            current = line
            continue
        if line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTGRP:", "#EXT-X-")):
            raise SyncError("playlist uses unsupported playback directives")
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            if current is None:
                raise SyncError("playlist contains a URL without #EXTINF")
            parsed = urlparse(line)
            if not parsed.hostname or parsed.username or parsed.password:
                raise SyncError("playlist contains an invalid or credential-bearing URL")
            entries.append(Entry(current, line))
            current = None
        else:
            raise SyncError("playlist contains an unsupported URL or unexpected content")
    if current is not None:
        raise SyncError("playlist ends with an #EXTINF without a URL")
    if not entries:
        raise SyncError("playlist contains no playable entries")
    return entries


def deduplicate(entries: list[Entry]) -> list[Entry]:
    seen: set[tuple[str, str]] = set()
    result: list[Entry] = []
    for entry in entries:
        key = (entry.extinf, entry.url)
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def is_radio(entry: Entry) -> bool:
    return bool(re.search(r"广播|廣播|电台|電台|\bradio\b|\bFM\b|\bAM\b", entry.extinf, re.IGNORECASE))


def is_cctv_or_satellite(entry: Entry) -> bool:
    metadata = entry.extinf.split(",", 1)[0]
    title = entry.extinf.split(",", 1)[-1]
    normalized = f"{metadata} {title}".upper().replace("－", "-").replace("—", "-")
    excluded = re.search(
        r"CGTN|外语|外語|英语|英語|俄语|俄語|法语|法語|西语|西語|阿语|阿語|"
        r"德语|德語|日语|日語|韩语|韓語|葡语|葡語|ENGLISH|RUSSIAN|FRENCH|"
        r"ARABIC|GERMAN|JAPANESE|KOREAN|游戏|遊戲|足球|熊猫直播|熊貓直播|PANDA\s*LIVE",
        normalized,
    )
    if excluded:
        return False
    cctv = re.search(r"\bCCTV[\s-]?\d", normalized) or re.search(r"\b(?:CGTN|CHC)\b", normalized)
    satellite = "卫视" in normalized or "衛視" in normalized
    groups = re.search(r'group-title\s*=\s*["\']([^"\']+)["\']', metadata, re.IGNORECASE)
    group = groups.group(1) if groups else ""
    return bool(cctv or satellite or group in {"央视频道", "卫视频道", "央視頻道", "衛視頻道"})


def filter_cctv_and_satellite(entries: list[Entry]) -> list[Entry]:
    return [entry for entry in entries if is_cctv_or_satellite(entry)]


def failure_reason(stderr: str) -> str:
    text = stderr.lower()
    for tokens, reason in [
        (("401", "403", "unauthorized", "forbidden"), "access denied"),
        (("404", "not found"), "not found"),
        (("timed out", "timeout"), "timeout"),
        (("resolve", "connection", "network is unreachable", "no route"), "network error"),
        (("matches no streams", "does not contain any stream"), "required media missing"),
        (("invalid data", "error decoding", "corrupt", "decoder"), "decode error"),
    ]:
        if any(token in text for token in tokens):
            return reason
    return "ffmpeg failed"


def run_ffmpeg(url: str, timeout: int, ffmpeg: str = "ffmpeg", radio: bool = False) -> tuple[bool, str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-nostats",
        "-xerror",
        "-abort_on",
        "empty_output",
        "-protocol_whitelist",
        "http,https,tcp,tls,crypto",
        "-rw_timeout",
        str(timeout * 1_000_000),
        "-threads",
        "1",
        "-i",
        url,
        "-t",
        "3",
        "-map",
        "0:a:0" if radio else "0:v:0",
        "-vn" if radio else "-an",
        "-threads",
        "1",
        "-progress",
        "pipe:1",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError:
        raise SyncError("could not start ffmpeg") from None
    if completed.returncode != 0:
        return False, failure_reason(completed.stderr or "")
    progress = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    try:
        duration = int(progress.get("out_time_us", "0"))
        frames = int(progress.get("frame", "0"))
    except ValueError:
        return False, "invalid decode progress"
    if progress.get("progress") != "end" or duration < 3_000_000 or (not radio and frames <= 0):
        return False, "insufficient decoded media"
    return True, "ok"


def check_entry(entry: Entry, timeout: int, retries: int) -> tuple[bool, str]:
    last_reason = "not checked"
    for attempt in range(retries + 1):
        valid, reason = run_ffmpeg(entry.url, timeout, radio=is_radio(entry))
        if valid:
            return True, "ok"
        last_reason = reason
        if attempt < retries:
            time.sleep(0.2)
    return False, last_reason


def check_entries(entries: list[Entry], timeout: int, workers: int, retries: int) -> tuple[list[Entry], list[dict[str, str]]]:
    valid: list[Entry] = []
    failed: list[dict[str, str]] = []
    order = {entry: index for index, entry in enumerate(entries)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_entry, entry, timeout, retries): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                ok, reason = future.result()
            except Exception:
                raise SyncError("validation did not complete") from None
            if ok:
                valid.append(entry)
            else:
                failed.append(
                    {
                        "entry": str(order[entry] + 1),
                        "reason": reason,
                    }
                )
    valid.sort(key=order.__getitem__)
    failed.sort(key=lambda item: int(item["entry"]))
    return valid, failed


def render(entries: list[Entry]) -> str:
    lines = ["#EXTM3U", f"# Channel-Count: {len(entries)}"]
    for entry in entries:
        lines.extend((entry.extinf, entry.url))
    return "\n".join(lines) + "\n"


def existing_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(parse_m3u(path.read_text(encoding="utf-8-sig")))


def ensure_safe_to_publish(input_count: int, valid_count: int, old_count: int) -> None:
    if valid_count == 0:
        raise SyncError("no stream passed ffmpeg validation")
    minimum = max(1, math.ceil(old_count / 2)) if old_count else max(1, math.ceil(input_count * 0.2))
    if valid_count < minimum:
        baseline = f"previous={old_count}" if old_count else f"initial-input={input_count}"
        raise SyncError(f"too few valid streams: {valid_count} < {minimum} ({baseline})")


def write_if_changed(path: Path, content: str) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == content:
        return False
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def run(args: argparse.Namespace) -> dict:
    if shutil.which("ffmpeg") is None:
        raise SyncError("ffmpeg is not installed")
    sources = load_sources(args.sources)
    source_stats = []
    all_entries: list[Entry] = []
    for source in sources:
        try:
            entries = parse_m3u(fetch(source, args.download_timeout))
        except Exception:
            raise SyncError(f"source {len(source_stats) + 1} download or format validation failed") from None
        source_stats.append({"host": urlparse(source).hostname or "unknown", "entries": len(entries)})
        all_entries.extend(entries)

    unique_entries = deduplicate(all_entries)
    valid, failed = check_entries(unique_entries, args.timeout, args.workers, args.retries)
    old_count = existing_count(args.output)
    yw_output = getattr(args, "yw_output", args.output.with_name("ywIPTV.m3u"))
    yw_entries = filter_cctv_and_satellite(valid)
    report = {
        "sources": source_stats,
        "input_entries": len(all_entries),
        "deduplicated_entries": len(unique_entries),
        "valid_entries": len(valid),
        "yw_entries": len(yw_entries),
        "failed_entries": len(failed),
        "duplicate_entries": len(all_entries) - len(unique_entries),
        "failures": failed,
        "previous_entries": old_count,
        "updated": False,
        "dry_run": args.dry_run,
    }
    try:
        ensure_safe_to_publish(len(unique_entries), len(valid), old_count)
    except SyncError as exc:
        report["blocked"] = str(exc)
        return report
    if not yw_entries:
        report["blocked"] = "no CCTV or satellite channels passed validation"
        return report
    if args.dry_run:
        report["would_update"] = (
            not args.output.exists() or args.output.read_text(encoding="utf-8-sig") != render(valid)
        )
        report["yw_would_update"] = (
            not yw_output.exists() or yw_output.read_text(encoding="utf-8-sig") != render(yw_entries)
        )
    else:
        report["updated"] = write_if_changed(args.output, render(valid))
        report["yw_updated"] = write_if_changed(yw_output, render(yw_entries))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch, validate, and publish an IPTV M3U playlist.")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--yw-output", type=Path, default=DEFAULT_YW_OUTPUT)
    parser.add_argument("--download-timeout", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Validate all streams without writing the playlist.")
    args = parser.parse_args()
    if min(args.download_timeout, args.timeout, args.workers) < 1 or args.retries < 0:
        parser.error("timeouts/workers must be positive and retries must be nonnegative")
    try:
        report = run(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if "blocked" in report else 0
    except (SyncError, OSError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, SyncError) else "local file or encoding error"
        print(json.dumps({"updated": False, "blocked": reason}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
