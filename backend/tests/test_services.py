"""导入、补交、运行快照与报告发布的服务级测试。"""
import csv
import io

from src.config.database import Session
from src.constants.enums import Quarantine, Status
from src.domain.analysis import analyze
from src.models.jobs import Run
from src.models.source import Dataset, QuarantineRecord
from src.services import execution
from src.services.datasets import (apply_correction, build_snapshot,
                                   import_batch)
from src.utils.exports import write_report
from tests.conftest import csv_text, good_row


def _mixed_batch():
    return csv_text([
        good_row('PQ-01', humidity=60, mildew='false'),
        good_row('PQ-02', humidity=120, mildew='true'),   # 湿度越界
        good_row('PQ-03', humidity='', mildew='false'),   # 缺列
        good_row('PQ-01', humidity=70, mildew='true'),    # 重复单号
        good_row('PQ-04', humidity=72, mildew='true'),
        good_row('PQ-05', humidity=55, mildew='false'),
    ])


_PARAMS = {'humidity_threshold': 65, 'smoothing': 0.5,
           'min_positive_count': 1, 'min_total_count': 1, 'min_growth': 1.01}


def test_import_segregates_and_keeps_denominator():
    with Session.begin() as session:
        dataset, summary, created = import_batch(session, '测试批次', 'raw.csv',
                                                 _mixed_batch(), 'analyst')
        assert created is True
        # 6 个物理行：纳入 3、待隔离 2（PQ-02/PQ-03）、拦截 1（重复的 PQ-01）。
        assert summary['total_count'] == 6
        assert summary['accepted_count'] == 3
        assert summary['open_count'] == 2
        assert summary['blocked_count'] == 1
        assert [row['inspection_id'] for row in dataset.rows] == ['PQ-01', 'PQ-04', 'PQ-05']
    with Session() as session:
        quarantined = session.query(QuarantineRecord).order_by(QuarantineRecord.id).all()
        statuses = {item.inspection_id: item.status for item in quarantined}
        assert statuses['PQ-02'] == Quarantine.OPEN
        assert statuses['PQ-03'] == Quarantine.OPEN
        # 重复单号已纳入分母，只登记拦截、不占待隔离名额。
        dup = next(item for item in quarantined if item.inspection_id == 'PQ-01')
        assert dup.status == Quarantine.REJECTED
        reasons = {item.inspection_id: '；'.join(item.reasons) for item in quarantined}
        assert '湿度越界' in reasons['PQ-02']
        assert '缺列或字段为空' in reasons['PQ-03']
        assert '巡检单号重复' in reasons['PQ-01']


def test_reimport_same_file_is_idempotent():
    with Session.begin() as session:
        import_batch(session, '测试批次', 'raw.csv', _mixed_batch(), 'analyst')
    with Session.begin() as session:
        dataset, summary, created = import_batch(session, '测试批次', 'raw.csv',
                                                 _mixed_batch(), 'analyst')
        assert created is False
        assert summary['total_count'] == 6
    with Session() as session:
        assert session.query(Dataset).count() == 1
        # 同物理行重复导入只保留一条隔离记录（按行号幂等）。
        assert session.query(QuarantineRecord).count() == 3


def test_correction_resolves_only_quarantined_ids():
    with Session.begin() as session:
        dataset, _, _ = import_batch(session, '测试批次', 'raw.csv', _mixed_batch(), 'analyst')
        dataset_id = dataset.id
        before_revision = dataset.revision
    correction = csv_text([
        good_row('PQ-02', humidity=61, mildew='true'),
        good_row('PQ-03', humidity=58, mildew='false'),
        good_row('PQ-01', humidity=59, mildew='false'),   # 已纳入，拒绝
        good_row('PQ-99', humidity=59, mildew='false'),   # 不在清单，拒绝
    ])
    with Session.begin() as session:
        dataset = session.get(Dataset, dataset_id)
        summary = apply_correction(session, dataset, correction, 'analyst')
        assert summary['this_accepted'] == 2
        assert dataset.revision == before_revision + 1
        assert dataset.row_count == 5
        # 原始 6 行口径不变：纳入 5、待隔离 0；拦截 3 = 原始重复 PQ-01 + 补交被拒 2 行。
        assert summary['total_count'] == 6
        assert summary['open_count'] == 0
        assert summary['blocked_count'] == 3
        assert {'PQ-02', 'PQ-03'} <= {row['inspection_id'] for row in dataset.rows}
    with Session() as session:
        resolved = session.query(QuarantineRecord).filter(
            QuarantineRecord.status == Quarantine.RESOLVED).count()
        assert resolved == 2
        refused = session.query(QuarantineRecord).filter(
            QuarantineRecord.source == 'correction',
            QuarantineRecord.status == Quarantine.REJECTED).all()
        messages = {item.inspection_id: '；'.join(item.reasons) for item in refused}
        assert '已纳入本批次，补交不重复计数' in messages['PQ-01']
        assert '不在原隔离清单中' in messages['PQ-99']


def test_repeated_correction_does_not_double_count():
    with Session.begin() as session:
        dataset, _, _ = import_batch(session, '测试批次', 'raw.csv', _mixed_batch(), 'analyst')
        dataset_id = dataset.id
    correction = csv_text([good_row('PQ-02', humidity=61, mildew='true')])
    with Session.begin() as session:
        dataset = session.get(Dataset, dataset_id)
        first = apply_correction(session, dataset, correction, 'analyst')
        assert first['this_accepted'] == 1
    with Session.begin() as session:
        dataset = session.get(Dataset, dataset_id)
        second = apply_correction(session, dataset, correction, 'analyst')
        assert second['this_accepted'] == 0
        assert dataset.row_count == 4          # 只增加过一次
        assert dataset.accepted_count == 4
    with Session() as session:
        # 同一单号不会产生多条 correction 隔离记录。
        count = session.query(QuarantineRecord).filter(
            QuarantineRecord.source == 'correction',
            QuarantineRecord.inspection_id == 'PQ-02').count()
        assert count == 1


def test_queued_run_snapshot_is_frozen_across_correction():
    with Session.begin() as session:
        dataset, _, _ = import_batch(session, '测试批次', 'raw.csv', _mixed_batch(), 'analyst')
        run = Run(name='筛查', dataset_id=dataset.id, params=_PARAMS,
                  snapshot=build_snapshot(dataset, db=session), author='analyst')
        session.add(run)
        session.flush()
        run_id, bound_dataset_id = run.id, run.dataset_id
    # 排队之后再补交修正版。
    correction = csv_text([good_row('PQ-02', humidity=61, mildew='true'),
                           good_row('PQ-03', humidity=58, mildew='false')])
    with Session.begin() as session:
        apply_correction(session, session.get(Dataset, bound_dataset_id), correction, 'analyst')
    # 领取到的仍是排队时的 3 条纳入行。
    lease = execution.claim()
    assert lease is not None
    job_id, attempt, rows, params, quality, dataset_id, quarantine = lease
    assert job_id == run_id
    assert attempt == 1
    assert len(rows) == 3
    assert quality['accepted_count'] == 3 and quality['total_count'] == 6
    assert len(quarantine) == 3
    # 执行完成，结果与证据按旧快照发布。
    execution.execute(lease)
    with Session() as session:
        finished = session.get(Run, run_id)
        assert finished.status == Status.COMPLETE
        assert finished.result['quality']['accepted_count'] == 3
        assert finished.export_file
    # 失败重试沿用同一 Run 与同一批次快照。
    with Session.begin() as session:
        again = session.get(Run, run_id)
        again.status = Status.QUEUED
        again.lease_until = None
    retry = execution.claim()
    assert retry[0] == run_id and retry[1] == 2 and len(retry[2]) == 3


def test_report_contains_totals_and_each_quarantine_reason():
    with Session.begin() as session:
        dataset, _, _ = import_batch(session, '测试批次', 'raw.csv', _mixed_batch(), 'analyst')
        snapshot = build_snapshot(dataset, db=session)
    quality = {key: value for key, value in snapshot.items()
               if key not in ('rows', 'quarantine')}
    result = analyze(snapshot['rows'], _PARAMS, quality)
    assert result['quality']['total_count'] == 6
    assert result['quality']['accepted_count'] == 3
    assert result['quality']['open_count'] == 2
    assert result['quality']['blocked_count'] == 1
    name = write_report(99, 1, result, quarantine=snapshot['quarantine'])
    from src.config.settings import ARTIFACTS
    content = (ARTIFACTS / name).read_text(encoding='utf-8-sig')
    assert '总行数' in content and '纳入数' in content
    assert '湿度越界' in content and '缺列或字段为空' in content and '巡检单号重复' in content
    reader = list(csv.reader(io.StringIO(content)))
    assert any(row and row[0] == '隔离记录明细（逐条原因，不计入支持度分母）' for row in reader)
