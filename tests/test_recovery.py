import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from archive.readback import ArchiveVerificationError
from inspection.grading import WindowGrade
from inspection.luminance import LuminanceTotals
from samples.recovery_fixture import (
    RecoveryMaterialsTests, capture_sample, damage_original, load_legacy_archive,
)
from source.frame import GrayFrame
from source.origin import FrameOrigin
from station import Station
from support import StationCase, packet

# RecoveryMaterialsTests is imported so the sample-fixture material checks run as
# part of the joint verification discovered by `station.py verify`.

TARGET_FRAME_4 = GrayFrame(4, 2, 1, bytes([10, 30]), FrameOrigin('C1', 'head-left', 401))
TARGET_FRAME_5 = GrayFrame(5, 2, 1, bytes([0, 20]), FrameOrigin('C1', 'head-left', 402))


class RecoveryTests(StationCase):
    def capture_sample_windows(self):
        ids = capture_sample(self.station)
        self.assertEqual(
            ids, {'target': 1, 'equal-sum': 2, 'neighbor-damage': 3, 'later-copy': 4}
        )
        return ids

    def test_substitution_recovers_damaged_slot(self):
        ids = self.capture_sample_windows()
        damage_original(self.station.connection, ids['target'], 4, bytes([11, 29]))
        damage_original(self.station.connection, ids['neighbor-damage'], 33, bytes([90, 0]))
        self.station.close()
        with self.open_station() as reopened:
            for label in ('target', 'neighbor-damage'):
                with self.subTest(strict_read=label), self.assertRaises(ArchiveVerificationError):
                    reopened.windows.read_window('A', ids[label])
            with self.assertRaises(ArchiveVerificationError):
                reopened.review.handover('A', True)
            self.assertIsNone(reopened.review.recover_window('A', 99))
            self.assertIsNone(reopened.review.recover_window('unknown', ids['target']))

            report = reopened.review.recover_window('A', ids['target'])
            self.assertEqual(
                (report.session, report.capture_id, report.packet_id), ('A', 1, 'target')
            )
            self.assertEqual([slot.slot for slot in report.slots], [4, 5])
            slot4, slot5 = report.slots
            self.assertEqual(slot4.status.value, 'substituted')
            self.assertEqual(slot4.donor, (3, 32))
            self.assertEqual(slot4.frame, TARGET_FRAME_4)
            self.assertEqual(slot5.status.value, 'original')
            self.assertIsNone(slot5.donor)
            self.assertEqual(slot5.frame, TARGET_FRAME_5)
            self.assertEqual(report.totals, LuminanceTotals(60, 4))
            self.assertIs(report.grade, WindowGrade.DARK)

            # window 2 slot 20 reverses the bytes and slot 21 moves the position, so
            # neither qualifies; eligible donors are ordered by window, then slot.
            damage_original(reopened.connection, 3, 32, bytes([11, 29]))
            self.assertEqual(reopened.review.recover_window('A', 1).slots[0].donor, (3, 34))
            damage_original(reopened.connection, 3, 34, bytes([11, 29]))
            self.assertEqual(reopened.review.recover_window('A', 1).slots[0].donor, (4, 2))
            damage_original(reopened.connection, 4, 2, bytes([11, 29]))
            slot4 = reopened.review.recover_window('A', 1).slots[0]
            self.assertEqual(slot4.status.value, 'unverified')
            self.assertIsNone(slot4.frame)
            self.assertIsNone(slot4.donor)

    def test_unverified_slot_withholds_totals(self):
        ids = self.capture_sample_windows()
        damage_original(self.station.connection, ids['target'], 4, bytes([11, 29]))
        damage_original(self.station.connection, ids['target'], 5, b'')
        self.station.close()
        with self.open_station() as reopened:
            report = reopened.review.recover_window('A', ids['target'])
            slot4, slot5 = report.slots
            self.assertEqual(slot4.status.value, 'substituted')
            self.assertEqual(slot4.donor, (3, 32))
            self.assertEqual(slot4.frame, TARGET_FRAME_4)
            self.assertEqual(slot5.status.value, 'unverified')
            self.assertIsNone(slot5.frame)
            self.assertIsNone(slot5.donor)
            self.assertIsNone(report.totals)
            self.assertIsNone(report.grade)

            # earlier reports never serve as donors: once every sealed donor is
            # damaged, the slot becomes unverified as well
            for capture_id, slot in ((3, 32), (3, 34), (4, 2)):
                damage_original(reopened.connection, capture_id, slot, bytes([11, 29]))
            report = reopened.review.recover_window('A', ids['target'])
            self.assertEqual(
                [slot.status.value for slot in report.slots], ['unverified', 'unverified']
            )
            self.assertEqual([slot.frame for slot in report.slots], [None, None])
            self.assertIsNone(report.totals)
            self.assertIsNone(report.grade)

    def open_legacy_station(self):
        directory = tempfile.TemporaryDirectory(prefix='strip-legacy-test-')
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / 'legacy.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            load_legacy_archive(connection)
        station = Station(database)
        self.addCleanup(station.close)
        return station

    def test_legacy_window_recovery_and_retry(self):
        with self.open_legacy_station() as station:
            report = station.review.recover_window('A', 1)
            self.assertEqual(report.packet_id, 'old-A')
            self.assertEqual(
                [slot.status.value for slot in report.slots], ['original', 'original']
            )
            self.assertEqual(
                [slot.frame for slot in report.slots], [TARGET_FRAME_4, TARGET_FRAME_5]
            )
            self.assertEqual([slot.donor for slot in report.slots], [None, None])
            self.assertEqual(report.totals, LuminanceTotals(60, 4))
            self.assertIs(report.grade, WindowGrade.DARK)

            legacy_packet = packet(
                'old-A', [bytes([10, 30]), bytes([0, 20])],
                first=4, trigger=5, width=2, positions=[401, 402],
            )
            receipt = station.capture.capture_packet('A', legacy_packet)
            self.assertEqual(receipt.capture_id, 1)
            self.assertEqual(receipt.totals, LuminanceTotals(60, 4))
            self.assertIs(receipt.grade, WindowGrade.DARK)
            # reopening and identical resubmission must not fabricate commitments
            self.assertIsNone(station.connection.execute(
                'SELECT commitments_digest FROM windows WHERE capture_id = 1'
            ).fetchone()[0])
            self.assertEqual(station.connection.execute(
                'SELECT count(*) FROM slot_commitments'
            ).fetchone()[0], 0)

            damage_original(station.connection, 1, 4, bytes([11, 29]))
            with self.assertRaises(ArchiveVerificationError):
                station.review.recover_window('A', 1)

    def test_legacy_candidate_requires_whole_window_verification(self):
        with self.open_legacy_station() as station:
            target = packet(
                'target', [bytes([10, 30]), bytes([0, 20])],
                first=4, trigger=5, width=2, positions=[401, 402],
            )
            receipt = station.capture.capture_packet('A', target)
            self.assertEqual(receipt.capture_id, 2)
            damage_original(station.connection, 2, 4, bytes([11, 29]))
            report = station.review.recover_window('A', 2)
            slot4 = report.slots[0]
            self.assertEqual(slot4.status.value, 'substituted')
            self.assertEqual(slot4.donor, (1, 4))
            self.assertEqual(slot4.frame, TARGET_FRAME_4)
            self.assertEqual(report.totals, LuminanceTotals(60, 4))
            self.assertIs(report.grade, WindowGrade.DARK)

            # a legacy donor qualifies only while its whole-window evidence verifies
            damage_original(station.connection, 1, 5, bytes([1, 2]))
            report = station.review.recover_window('A', 2)
            slot4 = report.slots[0]
            self.assertEqual(slot4.status.value, 'unverified')
            self.assertIsNone(slot4.frame)
            self.assertIsNone(report.totals)
            self.assertIsNone(report.grade)

    def test_new_window_fixed_evidence_damage_rejected(self):
        ids = self.capture_sample_windows()
        self.station.connection.execute(
            'UPDATE windows SET grade = ? WHERE capture_id = ?', ('pass', ids['target'])
        )
        self.station.connection.execute(
            "UPDATE slot_commitments SET payload_digest = '00' WHERE capture_id = ? AND slot = '14'",
            (ids['equal-sum'],),
        )
        self.station.connection.execute(
            'DELETE FROM slot_commitments WHERE capture_id = ?', (ids['neighbor-damage'],)
        )
        self.station.connection.execute(
            'UPDATE windows SET commitments_digest = NULL WHERE capture_id = ?',
            (ids['later-copy'],),
        )
        self.station.close()
        with self.open_station() as reopened:
            for label in ('target', 'equal-sum', 'neighbor-damage', 'later-copy'):
                with self.subTest(label=label), self.assertRaises(ArchiveVerificationError):
                    reopened.review.recover_window('A', ids[label])
            # the original frames are intact, so strict reads keep their behavior
            for label in ('equal-sum', 'neighbor-damage'):
                with self.subTest(strict_read=label):
                    self.assertIsNotNone(reopened.windows.read_window('A', ids[label]))

    def test_commitment_write_abort_rolls_back(self):
        self.bind(width=2)
        kept_packet = packet('kept', [b'\x0a\x1e'], width=2, positions=[401])
        kept = self.station.capture.capture_packet('A', kept_packet)
        reader_station = self.open_station()
        observations = []

        def observe(capture_id, staged_commitments):
            writer = self.station.connection
            staged_header = writer.execute(
                'SELECT packet_id FROM windows WHERE capture_id = ?', (capture_id,)
            ).fetchone()
            staged_frames = writer.execute(
                'SELECT count(*) FROM frames WHERE capture_id = ?', (capture_id,)
            ).fetchone()[0]
            with reader_station.windows.read_snapshot() as reader:
                observations.append((
                    capture_id, staged_commitments, staged_frames, staged_header[0],
                    reader.read_window('A', capture_id),
                    tuple(s.capture_id for s in reader.list_summaries('A')),
                    reader.read_window('A', kept.capture_id).packet,
                ))

        writer = self.station.connection
        writer.create_function('observe_inflight', 2, observe)
        writer.execute('''CREATE TEMP TRIGGER abort_second_commitment
                          BEFORE INSERT ON slot_commitments WHEN NEW.slot = '5'
                          BEGIN
                              SELECT observe_inflight(NEW.capture_id, (
                                  SELECT count(*) FROM slot_commitments
                                  WHERE capture_id = NEW.capture_id
                              ));
                              SELECT RAISE(ABORT, 'commitment write interrupted');
                          END''')
        second = packet(
            'second', [b'\x0a\x1e', b'\x00\x14'], first=4, trigger=5, width=2,
            positions=[401, 402],
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'commitment write interrupted'):
            self.station.capture.capture_packet('A', second)
        self.assertEqual(len(observations), 1)
        failed_id, commitment_count, frame_count, packet_id, visible, visible_ids, old_packet = (
            observations[0]
        )
        self.assertEqual((commitment_count, frame_count, packet_id), (1, 2, 'second'))
        self.assertIsNone(visible)
        self.assertEqual(visible_ids, (kept.capture_id,))
        self.assertEqual(old_packet, kept_packet)
        reader_station.close()
        self.station.close()

        with self.open_station() as reopened:
            self.assertIsNone(reopened.windows.read_window('A', failed_id))
            self.assertEqual(reopened.connection.execute(
                'SELECT count(*) FROM slot_commitments'
            ).fetchone()[0], 1)
            self.assertEqual(reopened.connection.execute(
                'SELECT count(*) FROM frames'
            ).fetchone()[0], 1)
            self.assertEqual(reopened.windows.read_window('A', kept.capture_id).packet, kept_packet)
            retried = reopened.capture.capture_packet('A', second)
            self.assertEqual(
                (retried.totals.gray_sum, retried.totals.pixel_count, retried.grade.value),
                (60, 4, 'dark'),
            )
            self.assertEqual(reopened.windows.read_window('A', retried.capture_id).packet, second)
            report = reopened.review.recover_window('A', retried.capture_id)
            self.assertEqual(
                [slot.status.value for slot in report.slots], ['original', 'original']
            )
            self.assertEqual([slot.frame for slot in report.slots], list(second.frames))
            self.assertEqual(report.totals, LuminanceTotals(60, 4))
            self.assertIs(report.grade, WindowGrade.DARK)
            self.assertEqual(reopened.capture.capture_packet('A', second), retried)
            self.assertEqual(reopened.connection.execute(
                'SELECT count(*) FROM slot_commitments'
            ).fetchone()[0], 3)
