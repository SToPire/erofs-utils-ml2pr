from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message


@dataclass(frozen=True)
class SubjectInfo:
    full_subject: str
    title: str
    version: int
    index: int
    total: int
    is_cover: bool
    is_rfc: bool


@dataclass(frozen=True)
class ArchiveMessage:
    archive_month: str
    subject: str
    message_id: str
    date: datetime
    from_name: str
    from_addr: str
    in_reply_to: str | None
    references: tuple[str, ...]
    body: str
    raw_message: Message
    subject_info: SubjectInfo | None = None


@dataclass(frozen=True)
class PatchMail:
    message: ArchiveMessage
    title: str
    index: int
    total: int


@dataclass
class PatchSeries:
    key: str
    title: str
    root_message_id: str
    version: int
    total: int
    latest_date: datetime
    submitter_name: str
    submitter_addr: str
    cover_message_id: str | None = None
    patches: list[PatchMail] = field(default_factory=list)

    def is_complete(self) -> bool:
        if not self.patches or self.total <= 0:
            return False
        indexes = [patch.index for patch in self.patches]
        return indexes == list(range(1, self.total + 1))
