from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = ROOT / ".github" / "iptv_sources.txt"
DEFAULT_BLOCKED_URLS = ROOT / ".github" / "iptv_blocked_urls.txt"
DEFAULT_OUTPUT = ROOT / "IPTV.m3u"
DEFAULT_YW_OUTPUT = ROOT / "ywIPTV.m3u"
DEFAULT_REVIEW_OUTPUT = ROOT / "host-review.m3u"


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


def load_blocked_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


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


def filter_blocked_urls(entries: list[Entry], blocked_urls: set[str]) -> tuple[list[Entry], int]:
    kept = [entry for entry in entries if entry.url not in blocked_urls]
    return kept, len(entries) - len(kept)


def cctv_channel_number(entry: Entry) -> int | None:
    metadata, title = entry.extinf.split(",", 1)
    match = re.search(
        r"\bCCTV[\s-]*0*(\d{1,2})(?!\d)(?!\s*\+)",
        f"{metadata} {title}".upper().replace("－", "-").replace("—", "-"),
    )
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= 15 else None


def build_yw_entries(
    valid: list[Entry],
    candidates: list[Entry],
    previous: list[Entry],
    blocked_urls: set[str],
) -> tuple[list[Entry], dict[str, str], list[str]]:
    valid_yw = filter_cctv_and_satellite(valid)
    entries = [normalize_yw_group(entry) for entry in valid_yw]
    present = {cctv_channel_number(entry) for entry in valid_yw}
    annotations: dict[str, str] = {}
    missing: list[str] = []

    for number in range(1, 16):
        if number in present:
            continue
        fallback = next(
            (
                entry
                for entry in previous
                if cctv_channel_number(entry) == number
                and entry.url not in blocked_urls
                and is_cctv_or_satellite(entry)
            ),
            None,
        )
        if fallback is None:
            fallback = next(
                (
                    entry
                    for entry in candidates
                    if cctv_channel_number(entry) == number
                    and is_cctv_or_satellite(entry)
                ),
                None,
            )
        if fallback is None:
            missing.append(f"CCTV-{number}")
            continue
        normalized = normalize_yw_group(fallback)
        entries.append(normalized)
        annotations[normalized.url] = "unverified fallback; no current candidate passed validation"

    return entries, annotations, missing


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    query = urlencode(
        [(key, "[REDACTED]") for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    )
    return parsed._replace(query=query or "[REDACTED]", fragment="").geturl()


def render_host_review(entries: list[tuple[int, str, Entry]], blocked_urls: set[str]) -> str:
    lines = ["#EXTM3U", "# Review only: not a subscription playlist."]
    for source_number, source, entry in entries:
        status = "temporarily blocked exact URL" if entry.url in blocked_urls else "not blocked"
        lines.extend(
            (
                f"# Review-Source: {source_number} {redact_url(source)}",
                f"# Review-Status: {status}",
                entry.extinf,
                redact_url(entry.url),
            )
        )
    return "\n".join(lines) + "\n"


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


def normalize_yw_group(entry: Entry) -> Entry:
    metadata, title = entry.extinf.split(",", 1)
    group = "央视" if re.search(r"\bCCTV[\s-]?\d", f"{metadata} {title}", re.IGNORECASE) else "卫视"
    group_attr = re.compile(r'group-title\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
    if group_attr.search(metadata):
        metadata = group_attr.sub(f'group-title="{group}"', metadata, count=1)
    else:
        metadata = f'{metadata} group-title="{group}"'
    return Entry(f"{metadata},{title}", entry.url)


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
        "-f",
        "framecrc",
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
    time_base = None
    decoded_ticks = 0
    try:
        # framecrc includes the final frame's duration, unlike FFmpeg 6.1's
        # null-muxer progress timestamp, which stops at its starting DTS.
        for line in completed.stdout.splitlines():
            if line.startswith("#tb 0:"):
                time_base = Fraction(line.split(":", 1)[1].strip())
            elif line.strip() and not line.startswith("#"):
                fields = next(csv.reader([line]))
                if len(fields) < 6 or int(fields[0]) != 0:
                    return False, "invalid decode progress"
                duration, size = int(fields[3]), int(fields[4])
                if duration <= 0 or size <= 0:
                    return False, "invalid decode progress"
                decoded_ticks += duration
    except (ValueError, ZeroDivisionError, csv.Error):
        return False, "invalid decode progress"
    if time_base is None or time_base <= 0 or decoded_ticks * time_base < 3:
        return False, "insufficient decoded media"
    return True, "ok"


def quick_probe(url: str, timeout: int) -> tuple[bool, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "iptv-sync/1.0",
            "Range": "bytes=0-4095",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status not in (200, 206):
                return False, f"probe HTTP {status}"
            if response.headers.get_content_type() == "text/html":
                return False, "probe html response"
            payload = response.read(4096)
    except HTTPError as exc:
        return False, f"probe HTTP {exc.code}"
    except (TimeoutError, socket.timeout):
        return False, "probe timeout"
    except URLError:
        return False, "probe network error"
    except ValueError:
        return False, "probe invalid URL"
    except OSError:
        return False, "probe network error"
    if not payload:
        return False, "probe empty response"
    sample = payload.lstrip().lower()
    if sample.startswith((b"<!doctype html", b"<html", b"<head")):
        return False, "probe html response"
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


def quick_probe_entries(
    entries: list[Entry],
    timeout: int,
    workers: int,
    order: dict[Entry, int] | None = None,
) -> tuple[list[Entry], list[dict[str, str]]]:
    order = order or {entry: index for index, entry in enumerate(entries)}
    candidates: list[Entry] = []
    failed: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(quick_probe, entry.url, timeout): entry for entry in entries}
        for future in concurrent.futures.as_completed(futures):
            entry = futures[future]
            try:
                ok, reason = future.result()
            except Exception:
                raise SyncError("quick probe did not complete") from None
            if ok:
                candidates.append(entry)
            else:
                failed.append(
                    {
                        "entry": str(order[entry] + 1),
                        "reason": reason,
                    }
                )
    candidates.sort(key=order.__getitem__)
    failed.sort(key=lambda item: int(item["entry"]))
    return candidates, failed


def check_entries(
    entries: list[Entry],
    timeout: int,
    workers: int,
    retries: int,
    order: dict[Entry, int] | None = None,
) -> tuple[list[Entry], list[dict[str, str]]]:
    valid: list[Entry] = []
    failed: list[dict[str, str]] = []
    order = order or {entry: index for index, entry in enumerate(entries)}
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


QUALITY_SUFFIX = re.compile(r"\s*\((?:\d{3,4}p|\d{3,4}i|SD|HD)\)\s*$", re.IGNORECASE)


def display_entry(entry: Entry) -> tuple[str, str | None]:
    metadata, title = entry.extinf.split(",", 1)
    display_title = QUALITY_SUFFIX.sub("", title).rstrip()
    return f"{metadata},{display_title}", (title if display_title != title else None)


def render(entries: list[Entry]) -> str:
    return render_with_annotations(entries, {})


def render_with_annotations(entries: list[Entry], annotations: dict[str, str]) -> str:
    lines = ["#EXTM3U", f"# Channel-Count: {len(entries)}"]
    for entry in entries:
        extinf, original_title = display_entry(entry)
        if original_title is not None:
            lines.append(f"# Original-Name: {original_title}")
        if entry.url in annotations:
            lines.append(f"# Stream-Status: {annotations[entry.url]}")
        lines.extend((extinf, entry.url))
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
    blocked_urls = load_blocked_urls(getattr(args, "blocked_urls", DEFAULT_BLOCKED_URLS))
    review_hosts = {
        parsed.hostname.lower()
        for url in blocked_urls
        if (parsed := urlparse(url)).hostname
    }
    source_stats = []
    all_entries: list[Entry] = []
    review_entries: list[tuple[int, str, Entry]] = []
    blocked_entries = 0
    input_count = 0
    for source_number, source in enumerate(sources, 1):
        try:
            entries = parse_m3u(fetch(source, args.download_timeout))
        except Exception:
            raise SyncError(f"source {len(source_stats) + 1} download or format validation failed") from None
        source_stats.append({"host": urlparse(source).hostname or "unknown", "entries": len(entries)})
        input_count += len(entries)
        for entry in entries:
            host = (urlparse(entry.url).hostname or "").lower()
            if host in review_hosts:
                review_entries.append((source_number, source, entry))
            eligible, blocked = filter_blocked_urls([entry], blocked_urls)
            blocked_entries += blocked
            all_entries.extend(eligible)

    unique_entries = deduplicate(all_entries)
    entry_order = {entry: index for index, entry in enumerate(unique_entries)}
    probe_candidates, probe_failed = quick_probe_entries(
        unique_entries, getattr(args, "probe_timeout", 4), args.workers, entry_order
    )
    valid, decode_failed = check_entries(
        probe_candidates, args.timeout, args.workers, args.retries, entry_order
    )
    failed = sorted(probe_failed + decode_failed, key=lambda item: int(item["entry"]))
    old_count = existing_count(args.output)
    yw_output = getattr(args, "yw_output", args.output.with_name("ywIPTV.m3u"))
    previous_yw = parse_m3u(yw_output.read_text(encoding="utf-8-sig")) if yw_output.exists() else []
    yw_entries, yw_annotations, missing_cctv = build_yw_entries(
        valid, unique_entries, previous_yw, blocked_urls
    )
    review_output = getattr(args, "review_output", args.output.with_name("host-review.m3u"))
    write_if_changed(review_output, render_host_review(review_entries, blocked_urls))
    report = {
        "sources": source_stats,
        "input_entries": input_count,
        "eligible_entries": len(all_entries),
        "deduplicated_entries": len(unique_entries),
        "valid_entries": len(valid),
        "quick_probe_candidates": len(probe_candidates),
        "quick_probe_failed_entries": len(probe_failed),
        "full_decode_entries": len(probe_candidates),
        "yw_entries": len(yw_entries),
        "failed_entries": len(failed),
        "duplicate_entries": len(all_entries) - len(unique_entries),
        "temporarily_blocked_entries": blocked_entries,
        "host_review_entries": len(review_entries),
        "yw_unverified_fallback_entries": len(yw_annotations),
        "missing_cctv_channels": missing_cctv,
        "failures": failed,
        "previous_entries": old_count,
        "updated": False,
        "dry_run": args.dry_run,
    }
    try:
        ensure_safe_to_publish(len(unique_entries), len(valid), old_count)
    except SyncError as exc:
        report["playlist_blocked"] = str(exc)
    if not yw_entries:
        report["blocked"] = "no CCTV or satellite channels available for ywIPTV"
        return report
    if missing_cctv:
        report["blocked"] = f"required CCTV channels unavailable: {', '.join(missing_cctv)}"
        return report
    if args.dry_run:
        report["would_update"] = (
            "playlist_blocked" not in report
            and (
                not args.output.exists()
                or args.output.read_text(encoding="utf-8-sig") != render(valid)
            )
        )
        report["yw_would_update"] = (
            not yw_output.exists()
            or yw_output.read_text(encoding="utf-8-sig") != render_with_annotations(yw_entries, yw_annotations)
        )
    else:
        if "playlist_blocked" not in report:
            report["updated"] = write_if_changed(args.output, render(valid))
        else:
            report["updated"] = False
        report["yw_updated"] = write_if_changed(
            yw_output, render_with_annotations(yw_entries, yw_annotations)
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch, validate, and publish an IPTV M3U playlist.")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--blocked-urls", type=Path, default=DEFAULT_BLOCKED_URLS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--yw-output", type=Path, default=DEFAULT_YW_OUTPUT)
    parser.add_argument("--review-output", type=Path, default=DEFAULT_REVIEW_OUTPUT)
    parser.add_argument("--download-timeout", type=int, default=20)
    parser.add_argument("--probe-timeout", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Validate all streams without writing the playlist.")
    args = parser.parse_args()
    if min(args.download_timeout, args.probe_timeout, args.timeout, args.workers) < 1 or args.retries < 0:
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
