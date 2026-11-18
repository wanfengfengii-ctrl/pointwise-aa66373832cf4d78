from dataclasses import dataclass
from enum import Enum

from archive.commitments import (
    SlotCommitment, load_slot_commitments, window_commitments_digest,
)
from archive.evidence import frame_evidence_digest
from archive.frame_store import load_frame_at_slot
from archive.readback import (
    ArchiveVerificationError, ArchivedWindow, WindowReader, WindowSummary,
)
from archive.session_catalog import list_session_capture_ids
from inspection.grading import WindowGrade
from inspection.luminance import LuminanceTotals
from review.verify import verify_window
from source.frame import GrayFrame
from source.packet import SourcePacket


class SlotStatus(str, Enum):
    ORIGINAL = 'original'
    SUBSTITUTED = 'substituted'
    UNVERIFIED = 'unverified'


@dataclass(frozen=True, slots=True)
class RecoveredSlot:
    slot: int
    status: SlotStatus
    frame: GrayFrame | None
    donor: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    session: str
    capture_id: int
    packet_id: str
    slots: tuple[RecoveredSlot, ...]
    totals: LuminanceTotals | None
    grade: WindowGrade | None


def _commitments_mark(reader: WindowReader, capture_id: int) -> str | None:
    row = reader.connection.execute(
        'SELECT commitments_digest FROM windows WHERE capture_id = ?', (capture_id,)
    ).fetchone()
    return None if row is None else row[0]


def _verified_commitments(
    reader: WindowReader, session: str, capture_id: int, mark: str
) -> tuple[SlotCommitment, ...]:
    commitments = load_slot_commitments(reader.connection, session, capture_id)
    if not commitments:
        raise ValueError('expected slot commitments are missing')
    if not isinstance(mark, str) or window_commitments_digest(commitments) != mark:
        raise ValueError('slot commitments disagree with the fixed commitments digest')
    return commitments


def _original_frame(
    reader: WindowReader, capture_id: int, commitment: SlotCommitment
) -> GrayFrame | None:
    try:
        frame = load_frame_at_slot(reader.connection, capture_id, commitment.slot)
    except (ValueError, TypeError):
        return None
    if frame is None or not commitment.fulfilled_by(frame):
        return None
    return frame


def _candidate_originals(
    reader: WindowReader, session: str, candidate_id: int
) -> tuple[tuple[int, GrayFrame], ...]:
    try:
        summary = reader.read_summary(session, candidate_id)
    except ArchiveVerificationError:
        return ()
    if summary is None:
        return ()
    mark = _commitments_mark(reader, candidate_id)
    if mark is None:
        try:
            window = reader.read_window(session, candidate_id)
        except ArchiveVerificationError:
            return ()
        if window is None:
            return ()
        return tuple((frame.slot, frame) for frame in window.frames)
    try:
        commitments = _verified_commitments(reader, session, candidate_id, mark)
    except (ValueError, TypeError):
        return ()
    originals = []
    for commitment in commitments:
        frame = _original_frame(reader, candidate_id, commitment)
        if frame is not None:
            originals.append((commitment.slot, frame))
    return tuple(originals)


def _find_donor(
    reader: WindowReader, session: str, target_id: int, commitment: SlotCommitment
) -> tuple[GrayFrame, int, int] | None:
    for candidate_id in list_session_capture_ids(reader.connection, session):
        if candidate_id == target_id:
            continue
        for donor_slot, frame in _candidate_originals(reader, session, candidate_id):
            if commitment.fulfilled_by(frame):
                delivered = GrayFrame(
                    commitment.slot, frame.width, frame.height, frame.payload, frame.origin
                )
                return delivered, candidate_id, donor_slot
    return None


def _legacy_report(reader: WindowReader, summary: WindowSummary) -> RecoveryReport:
    session, capture_id = summary.session, summary.capture_id
    window = reader.read_window(session, capture_id)
    if window is None:
        raise ArchiveVerificationError(f'catalogued window {session!r}/{capture_id} is missing')
    verified = verify_window(window)
    slots = tuple(
        RecoveredSlot(frame.slot, SlotStatus.ORIGINAL, frame, None) for frame in window.frames
    )
    return RecoveryReport(
        session, capture_id, summary.header.packet_id, slots, verified.totals, verified.grade
    )


def recover_window(reader: WindowReader, session: str, capture_id: int) -> RecoveryReport | None:
    summary = reader.read_summary(session, capture_id)
    if summary is None:
        return None
    mark = _commitments_mark(reader, capture_id)
    if mark is None:
        return _legacy_report(reader, summary)
    try:
        commitments = _verified_commitments(reader, session, capture_id, mark)
    except (ValueError, TypeError) as exc:
        raise ArchiveVerificationError(f'window {session!r}/{capture_id}: {exc}') from exc
    recovered = []
    for commitment in commitments:
        frame = _original_frame(reader, capture_id, commitment)
        if frame is not None:
            recovered.append(RecoveredSlot(commitment.slot, SlotStatus.ORIGINAL, frame, None))
            continue
        donor = _find_donor(reader, session, capture_id, commitment)
        if donor is None:
            recovered.append(RecoveredSlot(commitment.slot, SlotStatus.UNVERIFIED, None, None))
        else:
            frame, candidate_id, donor_slot = donor
            recovered.append(RecoveredSlot(
                commitment.slot, SlotStatus.SUBSTITUTED, frame, (candidate_id, donor_slot)
            ))
    slots = tuple(recovered)
    packet_id = summary.header.packet_id
    if any(slot.status is SlotStatus.UNVERIFIED for slot in slots):
        return RecoveryReport(session, capture_id, packet_id, slots, None, None)
    frames = tuple(slot.frame for slot in slots)
    try:
        if frame_evidence_digest(frames) != summary.frames_digest:
            raise ValueError('recovered frames disagree with the fixed frame evidence digest')
        packet = SourcePacket(summary.header, frames)
        verified = verify_window(ArchivedWindow(summary, packet))
    except (ValueError, TypeError) as exc:
        raise ArchiveVerificationError(f'window {session!r}/{capture_id}: {exc}') from exc
    return RecoveryReport(session, capture_id, packet_id, slots, verified.totals, verified.grade)
