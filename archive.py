"""Historical recording extraction for supported camera archives."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener
from uuid import uuid4
from xml.etree import ElementTree

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_CREDENTIAL_PATTERN = re.compile(r"(?P<scheme>\b(?:rtsp|https?)://)[^@\s/]+@", re.IGNORECASE)


class ArchiveError(RuntimeError):
    """Raised when an archive clip cannot be located or extracted."""


class RecordingNotReadyError(ArchiveError):
    """Raised when the requested recording is not indexed yet."""


@dataclass(frozen=True, slots=True)
class HikvisionArchiveSource:
    """Connection information resolved from an existing HA camera entry."""

    host: str
    username: str
    password: str
    track_id: int = 101
    http_port: int = 80
    rtsp_port: int = 554


@dataclass(frozen=True, slots=True)
class RecordingSegment:
    """One recording segment returned by ISAPI."""

    start_ts: float
    end_ts: float
    playback_uri: str


async def async_resolve_hikvision_source(
    hass: HomeAssistant,
    camera: dict[str, Any],
) -> HikvisionArchiveSource:
    """Reuse credentials from the camera entity's HA config entry in memory."""

    from homeassistant.helpers import entity_registry as er

    entity_id = str(camera["entity_id"])
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.config_entry_id is None:
        raise ArchiveError(f"Camera entity {entity_id} has no config entry")
    config_entry = hass.config_entries.async_get_entry(registry_entry.config_entry_id)
    if config_entry is None:
        raise ArchiveError(f"Camera entity {entity_id} config entry is unavailable")

    values = {**config_entry.data, **config_entry.options}
    username = str(values.get("username") or "")
    password = str(values.get("password") or "")
    if not username or not password:
        raise ArchiveError(f"Camera entity {entity_id} does not provide reusable credentials")

    configured_host = str(camera.get("archive_host") or "").strip()
    configured_url = str(values.get("stream_source") or values.get("still_image_url") or "").strip()
    parsed = urlsplit(configured_url)
    host = configured_host or parsed.hostname or ""
    if not host:
        raise ArchiveError(f"Camera entity {entity_id} does not provide an archive host")

    track_id = int(camera.get("track_id") or _track_id_from_path(parsed.path) or 101)
    return HikvisionArchiveSource(
        host=host,
        username=username,
        password=password,
        track_id=track_id,
        http_port=int(camera.get("http_port", 80)),
        rtsp_port=int(camera.get("rtsp_port", 554)),
    )


def search_recordings(
    source: HikvisionArchiveSource,
    start_ts: float,
    end_ts: float,
    *,
    timeout: float = 15.0,
) -> list[RecordingSegment]:
    """Query Hikvision ISAPI for device archive segments overlapping a UTC interval."""

    if end_ts <= start_ts:
        raise ArchiveError("Archive clip end must be after start")
    base_url = f"http://{source.host}:{source.http_port}"
    password_manager = HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, base_url, source.username, source.password)
    opener = build_opener(HTTPDigestAuthHandler(password_manager))
    search_id = f"{{{uuid4()}}}"
    payload = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<CMSearchDescription>"
        f"<searchID>{search_id}</searchID>"
        f"<trackList><trackID>{source.track_id}</trackID></trackList>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{_isapi_time(start_ts)}</startTime>"
        f"<endTime>{_isapi_time(end_ts)}</endTime>"
        "</timeSpan></timeSpanList>"
        "<maxResults>40</maxResults>"
        "<searchResultPostion>0</searchResultPostion>"
        "<metadataList>"
        "<metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>"
        "</metadataList>"
        "</CMSearchDescription>"
    ).encode()
    request = Request(
        f"{base_url}/ISAPI/ContentMgmt/search?timeType=STD",
        data=payload,
        headers={"Content-Type": "application/xml; charset=UTF-8"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as error:
        raise ArchiveError(f"ISAPI search failed with HTTP {error.code}") from error
    except (TimeoutError, URLError) as error:
        raise ArchiveError(f"ISAPI search failed: {error.reason if isinstance(error, URLError) else error}") from error

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ArchiveError("ISAPI search returned invalid XML") from error
    if _local_name(root.tag) == "ResponseStatus":
        status = _first_text(root, "subStatusCode") or _first_text(root, "statusString") or "unknown"
        raise ArchiveError(f"ISAPI search rejected the request: {status}")
    if (_first_text(root, "responseStatusStrg") or "").upper() not in {"", "OK"}:
        raise ArchiveError(f"ISAPI search failed: {_first_text(root, 'responseStatusStrg')}")

    segments: list[RecordingSegment] = []
    for item in _elements(root, "searchMatchItem"):
        start = _first_text(item, "startTime")
        end = _first_text(item, "endTime")
        playback_uri = _first_text(item, "playbackURI")
        if not start or not end or not playback_uri:
            continue
        segments.append(
            RecordingSegment(
                start_ts=_parse_isapi_time(start),
                end_ts=_parse_isapi_time(end),
                playback_uri=playback_uri,
            )
        )
    if not any(segment.start_ts < end_ts and segment.end_ts > start_ts for segment in segments):
        raise RecordingNotReadyError("The requested recording is not indexed yet")
    return segments


async def async_extract_hikvision_clip(
    source: HikvisionArchiveSource,
    start_ts: float,
    end_ts: float,
    output_path: Path,
) -> int:
    """Copy an exact historical interval from Hikvision RTSP into an MP4."""

    duration = max(end_ts - start_ts, 1.0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    playback_uri = _playback_uri(source, start_ts, end_ts)
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        playback_uri,
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        str(partial_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=duration + 30.0)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.communicate()
        partial_path.unlink(missing_ok=True)
        raise ArchiveError("Historical RTSP extraction timed out") from error
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        partial_path.unlink(missing_ok=True)
        raise
    if process.returncode != 0:
        partial_path.unlink(missing_ok=True)
        detail = redact_credentials(stderr.decode(errors="replace")).strip()
        raise ArchiveError(f"ffmpeg failed ({process.returncode}): {detail[-500:]}")
    if not partial_path.is_file() or partial_path.stat().st_size == 0:
        partial_path.unlink(missing_ok=True)
        raise ArchiveError("ffmpeg did not create a historical clip")
    partial_path.replace(output_path)
    size = output_path.stat().st_size
    _LOGGER.debug("Extracted %s-byte historical clip to %s", size, output_path)
    return size


def redact_credentials(value: str) -> str:
    """Remove URI user-info before an error reaches logs or storage."""

    return _CREDENTIAL_PATTERN.sub(r"\g<scheme>***@", value)


def _playback_uri(source: HikvisionArchiveSource, start_ts: float, end_ts: float) -> str:
    username = quote(source.username, safe="")
    password = quote(source.password, safe="")
    start = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = datetime.fromtimestamp(end_ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        f"rtsp://{username}:{password}@{source.host}:{source.rtsp_port}"
        f"/Streaming/tracks/{source.track_id}/?starttime={start}&endtime={end}"
    )


def _track_id_from_path(path: str) -> int | None:
    match = re.search(r"/(?:tracks|channels)/(\d+)", path, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _isapi_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_isapi_time(value: str) -> float:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).timestamp()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _elements(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if _local_name(element.tag) == name]


def _first_text(root: ElementTree.Element, name: str) -> str | None:
    for element in root.iter():
        if _local_name(element.tag) == name:
            return element.text
    return None
