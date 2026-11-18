import argparse
import json
import tempfile
import unittest
from os import PathLike
from pathlib import Path

from archive.profile_store import ProfileStore
from archive.readback import ArchiveVerificationError
from archive.schema import open_archive_connection
from archive.window_store import WindowStore
from camera.profiles import ProfileRegistry
from camera.sessions import SessionRegistry
from review.handover import HandoverService
from source.replay import PacketCapture, ReplaySource

ROOT = Path(__file__).resolve().parent


class Station:
    def __init__(self, database: str | PathLike[str]) -> None:
        self.connection = open_archive_connection(database)
        self.profiles = ProfileRegistry(ProfileStore(self.connection))
        self.sessions = SessionRegistry(self.connection, self.profiles)
        self.windows = WindowStore(self.connection)
        self.capture = PacketCapture(self.sessions, self.windows)
        self.review = HandoverService(self.windows)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> 'Station':
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _verify() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def _scenario(
    case_id: str, inputs: dict, expected: object, actual: object,
    downstream_expected: object, downstream_actual: object, code_paths: list[str], oracle: str,
) -> None:
    if actual != expected or downstream_actual != downstream_expected:
        raise AssertionError(f'{case_id}: {(actual, downstream_actual)!r} disagrees with the sample')
    record = {
        'case_id': case_id, 'input': inputs, 'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': code_paths, 'oracle': oracle,
    }
    print('POINTWISE_SCENARIO ' + json.dumps(record, ensure_ascii=False, sort_keys=True))


def _demo() -> None:
    with tempfile.TemporaryDirectory(prefix='strip-station-') as directory:
        database = Path(directory) / 'handover.sqlite3'
        received = []
        with Station(database) as station:
            profile = station.profiles.register_profile('C1', 'R1', 1, 1, 20)
            station.sessions.open_session('A', profile)
            with ReplaySource((ROOT / 'samples' / 'day-shift.jsonl').open(encoding='utf-8')) as source:
                while (packet := source.next_packet()) is not None:
                    received.append((packet, station.capture.capture_packet('A', packet)))
            station.profiles.register_profile('C1', 'R2', 1, 1, 5)

        with Station(database) as station:
            (light_packet, light), (dark_packet, dark) = received
            fixed = station.windows.read_window('A', light.capture_id)
            dark_fixed = station.windows.read_window('A', dark.capture_id)
            if fixed is None or dark_fixed is None:
                raise RuntimeError('published sample window missing after reopening')
            entries = station.review.handover('A', dark_only=True)
            light_totals = [light.totals.gray_sum, light.totals.pixel_count, light.grade.value]
            light_frames = [[f.slot, list(f.payload), f.origin.position] for f in fixed.frames]
            _scenario(
                'exact_luminance_case',
                {'threshold': fixed.summary.profile.threshold,
                 'frames': [list(f.payload) for f in light_packet.frames]},
                [60, 3, 'pass'], light_totals,
                [[4, [10], 101], [5, [20], 108], [6, [30], 121]], light_frames,
                ['inspection/admission.py', 'inspection/luminance.py', 'archive/readback.py'],
                '10 + 20 + 30 = 60, with three pixels and 60 equal to 20 × 3',
            )
            report = [{
                'capture_id': entry.capture_id, 'packet_id': entry.packet_id,
                'threshold': entry.profile.threshold, 'grade': entry.grade.value,
                'totals': [entry.totals.gray_sum, entry.totals.pixel_count],
                'origins': [[slot, origin.camera, origin.device, origin.position]
                            for slot, origin in entry.origins],
            } for entry in entries]
            _scenario(
                'reopen_case',
                {'session': 'A', 'dark_only': True,
                 'candidate_frames': [[list(f.payload) for f in p.frames] for p, _ in received]},
                {'totals': [20, 2], 'payloads': [[0], [20]], 'grade': 'dark'},
                {'totals': [dark_fixed.summary.totals.gray_sum, dark_fixed.summary.totals.pixel_count],
                 'payloads': [list(f.payload) for f in dark_fixed.frames],
                 'grade': dark_fixed.summary.grade.value},
                [{'capture_id': 2, 'packet_id': 'dark-B', 'threshold': 20, 'grade': 'dark',
                  'totals': [20, 2], 'origins': [[9, 'C1', 'head-left', 201],
                                                 [10, 'C1', 'head-left', 202]]}],
                report,
                ['archive/readback.py', 'review/selection.py', 'review/verify.py', 'review/handover.py'],
                '0 and 20 are both valid pixels, so their mean is 10 under the fixed threshold 20',
            )
            if fixed.packet != light_packet or dark_fixed.packet != dark_packet:
                raise AssertionError('reopened packets disagree with camera input')


def _demo_recovery() -> None:
    from samples.recovery_fixture import capture_sample, damage_original

    def slot_record(slot):
        frame = None
        if slot.frame is not None:
            frame = [list(slot.frame.payload), slot.frame.origin.camera,
                     slot.frame.origin.device, slot.frame.origin.position]
        donor = None if slot.donor is None else list(slot.donor)
        return [slot.slot, slot.status.value, frame, donor]

    with tempfile.TemporaryDirectory(prefix='strip-recovery-') as directory:
        database = Path(directory) / 'recovery.sqlite3'
        with Station(database) as station:
            ids = capture_sample(station)
            damage_original(station.connection, ids['target'], 4, bytes([11, 29]))
            damage_original(station.connection, ids['neighbor-damage'], 33, bytes([90, 0]))

        with Station(database) as station:
            refused = []
            for label in ('target', 'neighbor-damage'):
                try:
                    station.windows.read_window('A', ids[label])
                except ArchiveVerificationError:
                    refused.append(label)
            if refused != ['target', 'neighbor-damage']:
                raise AssertionError('strict reads must refuse the damaged windows')
            report = station.review.recover_window('A', ids['target'])
            _scenario(
                'recovery_substitution_case',
                {'session': 'A', 'capture_id': ids['target'],
                 'damaged_slots': [[ids['target'], 4], [ids['neighbor-damage'], 33]],
                 'strict_read_refused': refused},
                [[4, 'substituted', [[10, 30], 'C1', 'head-left', 401], [3, 32]],
                 [5, 'original', [[0, 20], 'C1', 'head-left', 402], None]],
                [slot_record(slot) for slot in report.slots],
                [60, 4, 'dark'],
                [report.totals.gray_sum, report.totals.pixel_count, report.grade.value],
                ['archive/commitments.py', 'review/recovery.py'],
                'slot 4 is proven by the sealed original of window 3 slot 32; window 2 '
                'slot 20 reverses the bytes and slot 21 moves the position, so the '
                'smallest eligible donor is (3, 32)',
            )
            damage_original(station.connection, ids['target'], 5, b'')
            report = station.review.recover_window('A', ids['target'])
            totals = None
            if report.totals is not None:
                totals = [report.totals.gray_sum, report.totals.pixel_count, report.grade.value]
            _scenario(
                'recovery_unverified_case',
                {'session': 'A', 'capture_id': ids['target'],
                 'damaged_slots': [[ids['target'], 4], [ids['target'], 5],
                                   [ids['neighbor-damage'], 33]]},
                [[4, 'substituted', [[10, 30], 'C1', 'head-left', 401], [3, 32]],
                 [5, 'unverified', None, None]],
                [slot_record(slot) for slot in report.slots],
                None, totals,
                ['archive/commitments.py', 'review/recovery.py'],
                'with the slot 5 bytes destroyed and no sealed donor for them, the slot '
                'stays unverified and the window totals and grade are withheld',
            )
        print('交接说明：新窗封存时原帧与逐槽承诺在同一事务提交；承诺固定班次、窗口、绝对槽、'
              '出处、宽高与原字节身份，原帧字节局部损坏后承诺仍可核验，重开与重投不补造旧窗承诺。')
        print('交接说明：复核领取时目标槽优先交付可核验的封存原件；原件损坏时，候选槽是同班次'
              '另一窗口中封存归属与实际原件均核验通过的帧，交付帧改用目标绝对槽号并兑现目标承诺'
              '的出处、形状与字节，合格替代按窗口号、候选槽取最小。')
        print('交接说明：恢复只读，恢复报告不充当候选；严格读取与交班不借用替代结果，'
              '任一槽未证时不交付整窗计量与分级。')


def main() -> int:
    parser = argparse.ArgumentParser(description='Offline strip inspection handover')
    parser.add_argument('action', choices=('verify', 'demo'))
    args = parser.parse_args()
    if args.action == 'verify':
        return _verify()
    _demo()
    _demo_recovery()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
