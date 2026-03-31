from __future__ import annotations

import gzip
import html
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
from urllib.parse import quote, unquote, urljoin

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
_DATE_PAGE_ENTRY_RE = re.compile(
    r'<LI><A HREF="(?P<href>[^"]+)">(?P<subject>.*?)</A><A NAME="[^"]*">&nbsp;</A>\s*<I>(?P<author>.*?)</I>',
    re.IGNORECASE | re.DOTALL,
)
_MESSAGE_PAGE_SUBJECT_RE = re.compile(
    r"<H1>\s*(?P<subject>.*?)\s*</H1>",
    re.IGNORECASE | re.DOTALL,
)
_MESSAGE_PAGE_HEADER_RE = re.compile(
    r"<B>(?P<name>.*?)</B>\s*<A\s+HREF=\"mailto:[^\"]*\"[^>]*>(?P<addr>.*?)</A><BR>\s*<I>(?P<date>[^<]+)</I>",
    re.IGNORECASE | re.DOTALL,
)
_MESSAGE_PAGE_ID_RE = re.compile(
    r'In-Reply-To=(?P<msgid>[^"&]+)',
    re.IGNORECASE,
)
_OZLABS_TZ_OFFSETS = {
    "AEDT": "+1100",
    "AEST": "+1000",
    "UTC": "+0000",
    "GMT": "+0000",
}


@dataclass
class _SeriesAccumulator:
    root_message_id: str
    version: int
    total: int = 0
    root_subject: str | None = None
    title: str | None = None
    latest_date: datetime | None = None
    submitter_name: str | None = None
    submitter_addr: str | None = None
    cover_message_id: str | None = None
    touched_in_window: bool = False
    patches_by_index: dict[int, PatchMail] = field(default_factory=dict)


@dataclass(frozen=True)
class _DateIndexEntry:
    archive_month: str
    message_url: str
    subject: str


@dataclass(frozen=True)
class _CandidateMessage:
    archive_month: str
    message_url: str
    message_id: str
    subject: str
    date: datetime


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


def normalize_message_id(value: str | None) -> str | None:
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
    message_id = normalize_message_id(msg.get("Message-ID"))
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
    in_reply_to = normalize_message_id(msg.get("In-Reply-To"))
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


def _fetch_url_text(url: str) -> str:
    return _fetch_url_bytes(url).decode("utf-8", errors="replace")


def _clean_html_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value).strip())


def _deobfuscate_addr(value: str) -> str:
    return value.replace(" at ", "@").replace(" dot ", ".").strip()


def _parse_ozlabs_page_date(value: str) -> datetime:
    cleaned = _clean_html_text(value)
    match = re.match(r"^(?P<prefix>.+?) (?P<tz>[A-Z]{2,5}|[+-]\d{4}) (?P<year>\d{4})$", cleaned)
    if match:
        prefix = match.group("prefix")
        tz_name = match.group("tz")
        tz_value = _OZLABS_TZ_OFFSETS.get(tz_name, tz_name)
        parsed = datetime.strptime(
            f"{prefix} {tz_value} {match.group('year')}",
            "%a %b %d %H:%M:%S %z %Y",
        )
        return parsed.astimezone(UTC)

    parsed = parsedate_to_datetime(cleaned)
    if parsed is None:
        raise ValueError(f"unable to parse archive date {value!r}")
    if parsed.tzinfo is None:
        raise ValueError(f"archive date missing timezone {value!r}")
    return parsed.astimezone(UTC)


def _parse_date_index_entries(
    html_text: str,
    *,
    archive_month: str,
    date_page_url: str,
) -> list[_DateIndexEntry]:
    entries: list[_DateIndexEntry] = []
    for match in _DATE_PAGE_ENTRY_RE.finditer(html_text):
        subject = _clean_html_text(match.group("subject"))
        href = html.unescape(match.group("href")).strip()
        if not subject or not href.endswith(".html"):
            continue
        entries.append(
            _DateIndexEntry(
                archive_month=archive_month,
                message_url=urljoin(date_page_url, href),
                subject=subject,
            )
        )
    return entries


def fetch_month_date_index_entries(
    archive_root: str,
    archive_month: str,
) -> list[_DateIndexEntry]:
    date_page_url = urljoin(archive_root, f"{archive_month}/date.html")
    html_text = _fetch_url_text(date_page_url)
    entries = _parse_date_index_entries(
        html_text,
        archive_month=archive_month,
        date_page_url=date_page_url,
    )
    LOG.info("loaded date index month=%s entries=%d", archive_month, len(entries))
    return entries


def _parse_candidate_message_page(
    html_text: str,
    *,
    archive_month: str,
    message_url: str,
) -> _CandidateMessage:
    subject_match = _MESSAGE_PAGE_SUBJECT_RE.search(html_text)
    header_match = _MESSAGE_PAGE_HEADER_RE.search(html_text)
    message_id_match = _MESSAGE_PAGE_ID_RE.search(html_text)
    if subject_match is None or header_match is None or message_id_match is None:
        raise ValueError(f"unable to parse message page {message_url}")

    subject = _clean_html_text(subject_match.group("subject"))
    message_id = normalize_message_id(unquote(message_id_match.group("msgid")))
    if message_id is None:
        raise ValueError(f"missing message id in page {message_url}")

    date_value = _parse_ozlabs_page_date(header_match.group("date"))

    return _CandidateMessage(
        archive_month=archive_month,
        message_url=message_url,
        message_id=message_id,
        subject=subject,
        date=date_value,
    )


def fetch_candidate_message(
    archive_month: str,
    message_url: str,
) -> _CandidateMessage:
    html_text = _fetch_url_text(message_url)
    return _parse_candidate_message_page(
        html_text,
        archive_month=archive_month,
        message_url=message_url,
    )


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
        LOG.info("loaded mbox source=%s messages=%d", archive_month, len(messages))
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


def _subject_for_pr_title(message: ArchiveMessage) -> str:
    if message.subject_info is not None:
        return message.subject_info.title
    return message.subject.strip()


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
        if group.root_subject is None:
            root_message = messages_by_id.get(root_message_id)
            if root_message is not None:
                group.root_subject = _subject_for_pr_title(root_message)

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

        title = group.root_subject or group.title or group.patches_by_index[1].title

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


def _series_contains_message(series: PatchSeries, message_id: str) -> bool:
    if series.cover_message_id == message_id:
        return True
    return any(patch.message.message_id == message_id for patch in series.patches)


def _resolve_series_from_candidate_message(
    candidate: _CandidateMessage,
    *,
    raw_message_root: str,
    now: datetime,
) -> PatchSeries:
    raw_messages = fetch_lore_thread_messages(raw_message_root, candidate.message_id)
    raw_series = _build_series(
        raw_messages,
        now=now,
        lookback_hours=24 * 365 * 20,
        require_recent=False,
    )

    for series in raw_series:
        if _series_contains_message(series, candidate.message_id):
            return series

    raise LookupError(
        "unable to reconstruct matching raw series for "
        f"{candidate.message_id}"
    )


def discover_recent_series(
    archive_root: str,
    *,
    raw_message_root: str,
    lookback_hours: int,
    now: datetime | None = None,
) -> list[PatchSeries]:
    if now is None:
        now = datetime.now(tz=UTC)

    cutoff = now - timedelta(hours=lookback_hours)
    months = candidate_archive_months(now, lookback_hours)
    series_by_key: dict[tuple[str, int], PatchSeries] = {}

    for month in months:
        entries = fetch_month_date_index_entries(archive_root, month)
        for entry in reversed(entries):
            info = parse_patch_subject(entry.subject)
            if info is None or info.is_rfc:
                continue
            if not _is_tracked_patch_title(info.title):
                continue

            try:
                candidate = fetch_candidate_message(month, entry.message_url)
            except Exception as exc:
                LOG.warning(
                    "message page parse failed month=%s url=%s error=%s",
                    month,
                    entry.message_url,
                    exc,
                )
                continue

            if candidate.date > now:
                continue
            if candidate.date < cutoff:
                break

            try:
                series = _resolve_series_from_candidate_message(
                    candidate,
                    raw_message_root=raw_message_root,
                    now=now,
                )
            except Exception as exc:
                LOG.warning(
                    "raw thread reconstruction failed msgid=%s url=%s error=%s",
                    candidate.message_id,
                    candidate.message_url,
                    exc,
                )
                continue

            key = (series.root_message_id, series.version)
            existing = series_by_key.get(key)
            if existing is None or series.latest_date > existing.latest_date:
                series_by_key[key] = series

    return sorted(series_by_key.values(), key=lambda item: item.latest_date, reverse=True)


def resolve_series_by_root_message(
    raw_message_root: str,
    root_message_id: str,
    *,
    version: int | None = None,
    now: datetime | None = None,
) -> PatchSeries:
    normalized_root = normalize_message_id(root_message_id)
    if normalized_root is None:
        raise ValueError(f"invalid root message id {root_message_id!r}")

    if now is None:
        now = datetime.now(tz=UTC)

    raw_messages = fetch_lore_thread_messages(raw_message_root, normalized_root)
    raw_series = _build_series(
        raw_messages,
        now=now,
        lookback_hours=24 * 365 * 20,
        require_recent=False,
    )

    matches = [series for series in raw_series if series.root_message_id == normalized_root]
    if version is not None:
        matches = [series for series in matches if series.version == version]

    if not matches:
        version_text = f" version={version}" if version is not None else ""
        raise LookupError(
            "unable to reconstruct raw series for "
            f"{normalized_root}{version_text}"
        )

    return sorted(matches, key=lambda item: item.version, reverse=True)[0]


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
