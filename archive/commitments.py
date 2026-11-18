import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from archive.schema import decode_natural, encode_natural
from source.frame import GrayFrame


def _encode(fields: tuple) -> bytes:
    return json.dumps(fields, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def payload_identity(payload: bytes) -> str:
    return hashlib.sha256(b'frame-payload-v1\n' + payload).hexdigest()


def slot_commitment_digest(
    session: str, capture_id: int, slot: int, width: int, height: int,
    origin_camera: str, device: str, position: int, payload_digest: str,
) -> str:
    return hashlib.sha256(_encode((
        'slot-commitment-v1', session, capture_id, encode_natural(slot),
        encode_natural(width), encode_natural(height), origin_camera, device,
        encode_natural(position), payload_digest,
    ))).hexdigest()


@dataclass(frozen=True, slots=True)
class SlotCommitment:
    slot: int
    width: int
    height: int
    origin_camera: str
    device: str
    position: int
    payload_digest: str
    commitment_digest: str

    def fulfilled_by(self, frame: GrayFrame) -> bool:
        origin = frame.origin
        return (
            frame.width == self.width
            and frame.height == self.height
            and origin.camera == self.origin_camera
            and origin.device == self.device
            and origin.position == self.position
            and payload_identity(frame.payload) == self.payload_digest
        )


def commit_frame(session: str, capture_id: int, frame: GrayFrame) -> SlotCommitment:
    payload_digest = payload_identity(frame.payload)
    digest = slot_commitment_digest(
        session, capture_id, frame.slot, frame.width, frame.height,
        frame.origin.camera, frame.origin.device, frame.origin.position, payload_digest,
    )
    return SlotCommitment(
        frame.slot, frame.width, frame.height, frame.origin.camera,
        frame.origin.device, frame.origin.position, payload_digest, digest,
    )


def window_commitments_digest(commitments: Iterable[SlotCommitment]) -> str:
    digest = hashlib.sha256(b'window-commitments-v1\n')
    for commitment in sorted(commitments, key=lambda commitment: commitment.slot):
        digest.update(commitment.commitment_digest.encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def insert_slot_commitments(
    connection: sqlite3.Connection, capture_id: int, commitments: Iterable[SlotCommitment]
) -> None:
    rows = (
        (
            capture_id, encode_natural(commitment.slot), encode_natural(commitment.width),
            encode_natural(commitment.height), commitment.origin_camera, commitment.device,
            encode_natural(commitment.position), commitment.payload_digest,
            commitment.commitment_digest,
        )
        for commitment in commitments
    )
    connection.executemany(
        '''INSERT INTO slot_commitments (
               capture_id, slot, width, height, origin_camera, device, position,
               payload_digest, commitment_digest
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        rows,
    )


def load_slot_commitments(
    connection: sqlite3.Connection, session: str, capture_id: int
) -> tuple[SlotCommitment, ...]:
    rows = connection.execute(
        '''SELECT slot, width, height, origin_camera, device, position,
                  payload_digest, commitment_digest
           FROM slot_commitments WHERE capture_id = ?''',
        (capture_id,),
    ).fetchall()
    commitments = []
    for row in rows:
        slot = decode_natural(row['slot'])
        width = decode_natural(row['width'])
        height = decode_natural(row['height'])
        position = decode_natural(row['position'])
        origin_camera = row['origin_camera']
        device = row['device']
        payload_digest = row['payload_digest']
        stored = row['commitment_digest']
        expected = slot_commitment_digest(
            session, capture_id, slot, width, height,
            origin_camera, device, position, payload_digest,
        )
        if not isinstance(stored, str) or stored != expected:
            raise ValueError(f'slot commitment for slot {slot} fails its sealed digest')
        commitments.append(SlotCommitment(
            slot, width, height, origin_camera, device, position, payload_digest, stored
        ))
    slots = [commitment.slot for commitment in commitments]
    if len(set(slots)) != len(slots):
        raise ValueError('duplicate slot commitments for one window')
    return tuple(sorted(commitments, key=lambda commitment: commitment.slot))
