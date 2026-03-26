from __future__ import annotations

import gzip
import logging
import mailbox
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote, urljoin

from .models import ArchiveMessage, PatchMail, PatchSeries, SubjectInfo

LOG = logging.getLogger(__name__)

USER_AGENT = "erofs-cibot/0.1"
TRACKED_TITLE_PREFIX = "erofs-utils:"
_MESSAGE_ID_RE = re.compile(r"<[^>]+>")
_PATCH_SUBJECT_RE = re.compile(
    r"^\s*\[(?P<tags>[^\]]*\bPATCH\b[^\]]*)\]\s*(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"(?:^|\s)v(?P<version>\d+)(?:\s|$)", re.IGNORECASE)
_INDEX_RE = re.compile(r"(?P<index>\d+)\s*/\s*(?P<total>\d+)")
_RFC_RE = re.compile(r"\bRFC\b", re.IGNORECASE)


@dataclass
class _SeriesAccumulator:
    root_message_id: str
    version: int
    total: int = 0
    title: str | None = None
    latest_date: datetime | None = None
    submitter_name: str | None = None
    submitter_addr: str | None = None
    cover_message_id: str | None = None
    touched_in_window: bool = False
    patches_by_index: dict[int, PatchMail] = field(default_factory=dict)


def candidate_archive_months(now: datetime, lookback_hours: int) -> list[str]:
    """Return archive month labels that may contain messages in the window."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    months: list[str] = []
    cursor = datetime(now.year, now.month, 1, tzinfo=UTC)
    earliest = now.timestamp() - lookback_hours * 3600

    while True:
        months.append(cursor.strftime("%Y-%B"))
        if cursor.timestamp() <= earliest:
            break
        if cursor.month == 1:
            cursor = datetime(cursor.year - 1, 12, 1, tzinfo=UTC)
        else:
            cursor = datetime(cursor.year, cursor.month - 1, 1, tzinfo=UTC)

    return months


def parse_patch_subject(subject: str) -> SubjectInfo | None:
    match = _PATCH_SUBJECT_RE.match(subject)
    if not match:
        return None

    tags = match.group("tags")
    title = match.group("title").strip()
    if not title:
        return None

    version_match = _VERSION_RE.search(tags)
    version = int(version_match.group("version")) if version_match else 1

    index_match = _INDEX_RE.search(tags)
    if index_match:
        index = int(index_match.group("index"))
        total = int(index_match.group("total"))
    else:
        index = 1
        total = 1

    return SubjectInfo(
        full_subject=subject,
        title=title,
        version=version,
        index=index,
        total=total,
        is_cover=index == 0 and total > 0,
        is_rfc=bool(_RFC_RE.search(tags)),
    )


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _is_tracked_patch_title(title: str) -> bool:
    return title.startswith(TRACKED_TITLE_PREFIX)


def _normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _MESSAGE_ID_RE.search(value)
    if match:
        return match.group(0)
    cleaned = value.strip()
    if not cleaned:
        return None
    return f"<{cleaned.strip('<>')}>"


def _extract_message_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(_MESSAGE_ID_RE.findall(value))


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        text = part.get_payload()
        return text if isinstance(text, str) else ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message) -> str:
    if not msg.is_multipart():
        return _decode_part(msg).strip()

    parts: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        text = _decode_part(part).strip()
        if text:
            parts.append(text)

    return "\n\n".join(parts)


def _parse_archive_message(msg: Message, archive_month: str) -> ArchiveMessage | None:
    message_id = _normalize_message_id(msg.get("Message-ID"))
    if not message_id:
        return None

    date_value = parsedate_to_datetime(msg.get("Date"))
    if date_value is None:
        return None
    if date_value.tzinfo is None:
        date_value = date_value.replace(tzinfo=UTC)
    date_value = date_value.astimezone(UTC)

    subject = _decode_header_value(msg.get("Subject"))
    from_name, from_addr = parseaddr(_decode_header_value(msg.get("From")))
    in_reply_to = _normalize_message_id(msg.get("In-Reply-To"))
    references = _extract_message_ids(msg.get("References"))
    subject_info = parse_patch_subject(subject)

    return ArchiveMessage(
        archive_month=archive_month,
        subject=subject,
        message_id=message_id,
        date=date_value,
        from_name=from_name,
        from_addr=from_addr,
        in_reply_to=in_reply_to,
        references=references,
        body=_extract_body(msg),
        raw_message=msg,
        subject_info=subject_info,
    )


def _fetch_url_bytes(url: str) -> bytes:
    import requests

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:  # pragma: no cover
            last_error = exc
            LOG.warning(
                "fetch failed attempt=%d url=%s error=%s",
                attempt,
                url,
                exc,
            )
    assert last_error is not None
    raise last_error


def fetch_month_messages(archive_root: str, archive_month: str) -> list[ArchiveMessage]:
    month_url = urljoin(archive_root, f"{archive_month}.txt.gz")
    compressed = _fetch_url_bytes(month_url)
    raw_mbox = gzip.decompress(compressed)

    return _parse_mbox_bytes(raw_mbox, archive_month)


def _parse_mbox_bytes(raw_mbox: bytes, archive_month: str) -> list[ArchiveMessage]:
    with NamedTemporaryFile(prefix="erofs-cibot-", suffix=".mbox", delete=False) as tmp:
        tmp.write(raw_mbox)
        temp_path = Path(tmp.name)

    try:
        mbox = mailbox.mbox(temp_path, create=False)
        messages: list[ArchiveMessage] = []
        for key in mbox.iterkeys():
            parsed = _parse_archive_message(mbox.get_message(key), archive_month)
            if parsed is not None:
                messages.append(parsed)
        LOG.info("loaded archive month=%s messages=%d", archive_month, len(messages))
        return messages
    finally:
        temp_path.unlink(missing_ok=True)


def fetch_lore_thread_messages(
    raw_message_root: str,
    message_id: str,
) -> list[ArchiveMessage]:
    encoded_message_id = quote(message_id.strip("<>"), safe="")
    base = raw_message_root.rstrip("/") + "/"
    thread_url = urljoin(base, f"{encoded_message_id}/t.mbox.gz")
    compressed = _fetch_url_bytes(thread_url)
    raw_mbox = gzip.decompress(compressed)

    return _parse_mbox_bytes(raw_mbox, f"lore:{encoded_message_id}")


def _resolve_thread_root(
    message: ArchiveMessage,
    messages_by_id: dict[str, ArchiveMessage],
) -> str:
    root_id = message.message_id
    parent_id = message.in_reply_to or (message.references[-1] if message.references else None)
    if parent_id:
        root_id = parent_id

    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = messages_by_id.get(parent_id)
        if parent is None:
            break
        root_id = parent.message_id
        parent_id = parent.in_reply_to or (parent.references[-1] if parent.references else None)

    return root_id


def _build_series(
    messages: list[ArchiveMessage],
    *,
    now: datetime,
    lookback_hours: int,
    require_recent: bool,
) -> list[PatchSeries]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    cutoff = now - timedelta(hours=lookback_hours)
    messages_by_id = {message.message_id: message for message in messages}
    groups: dict[tuple[str, int], _SeriesAccumulator] = {}

    for message in sorted(messages, key=lambda item: item.date):
        info = message.subject_info
        if info is None or info.is_rfc:
            continue
        if not _is_tracked_patch_title(info.title):
            continue

        root_message_id = _resolve_thread_root(message, messages_by_id)
        key = (root_message_id, info.version)
        group = groups.setdefault(
            key,
            _SeriesAccumulator(
                root_message_id=root_message_id,
                version=info.version,
            ),
        )

        if cutoff <= message.date <= now:
            group.touched_in_window = True

        if group.latest_date is None or message.date > group.latest_date:
            group.latest_date = message.date
        if group.submitter_name is None:
            group.submitter_name = message.from_name
            group.submitter_addr = message.from_addr

        group.total = max(group.total, info.total)

        if info.is_cover:
            group.cover_message_id = message.message_id
            group.title = info.title
            continue

        patch = PatchMail(
            message=message,
            title=info.title,
            index=info.index,
            total=info.total,
        )
        existing = group.patches_by_index.get(info.index)
        if existing is None or patch.message.date > existing.message.date:
            group.patches_by_index[info.index] = patch

        if group.title is None and info.index == 1:
            group.title = info.title

    series_list: list[PatchSeries] = []
    for (root_message_id, version), group in groups.items():
        if require_recent and not group.touched_in_window:
            continue
        if not group.patches_by_index:
            continue

        total = group.total or max(group.patches_by_index)
        patch_indexes = sorted(group.patches_by_index)
        if patch_indexes != list(range(1, total + 1)):
            LOG.info(
                "skipping incomplete series root=%s version=%d indexes=%s total=%d",
                root_message_id,
                version,
                patch_indexes,
                total,
            )
            continue

        title = group.title or group.patches_by_index[1].title

        series = PatchSeries(
            key=root_message_id,
            title=title,
            root_message_id=root_message_id,
            version=version,
            total=total,
            latest_date=group.latest_date or now,
            submitter_name=group.submitter_name or "",
            submitter_addr=group.submitter_addr or "",
            cover_message_id=group.cover_message_id,
            patches=[group.patches_by_index[index] for index in range(1, total + 1)],
        )
        if series.is_complete():
            series_list.append(series)

    return sorted(series_list, key=lambda item: item.latest_date, reverse=True)


def build_recent_series(
    messages: list[ArchiveMessage],
    *,
    now: datetime,
    lookback_hours: int,
) -> list[PatchSeries]:
    return _build_series(
        messages,
        now=now,
        lookback_hours=lookback_hours,
        require_recent=True,
    )


def resolve_series_from_raw_thread(
    discovered_series: PatchSeries,
    *,
    raw_message_root: str,
    now: datetime | None = None,
) -> PatchSeries:
    if now is None:
        now = datetime.now(tz=UTC)

    raw_messages = fetch_lore_thread_messages(
        raw_message_root,
        discovered_series.root_message_id,
    )
    raw_series = _build_series(
        raw_messages,
        now=now,
        lookback_hours=24 * 365 * 20,
        require_recent=False,
    )

    for series in raw_series:
        if (
            series.root_message_id == discovered_series.root_message_id
            and series.version == discovered_series.version
        ):
            return series

    raise LookupError(
        "unable to reconstruct matching raw series for "
        f"{discovered_series.root_message_id} v{discovered_series.version}"
    )


def discover_recent_series(
    archive_root: str,
    *,
    lookback_hours: int,
    now: datetime | None = None,
) -> list[PatchSeries]:
    if now is None:
        now = datetime.now(tz=UTC)
    months = candidate_archive_months(now, lookback_hours)

    messages: list[ArchiveMessage] = []
    for month in months:
        messages.extend(fetch_month_messages(archive_root, month))

    return build_recent_series(messages, now=now, lookback_hours=lookback_hours)


def write_series_mailbox(series: PatchSeries, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    mbox = mailbox.mbox(path, create=True)
    try:
        for patch in series.patches:
            mbox.add(patch.message.raw_message)
        mbox.flush()
    finally:
        mbox.close()
