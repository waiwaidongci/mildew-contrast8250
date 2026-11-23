import csv
import hashlib
import io
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.config.database import Session
from src.constants.enums import Quarantine
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.source import Dataset, QuarantineRecord
from src.services.quality import inspect_correction, inspect_csv
from src.utils.time import now


def checksum(rows):
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def build_snapshot(dataset, quarantine=None, db=None):
    """排队时固化的批次快照：纳入行、质检口径与隔离明细随运行不可变。"""
    accepted = dataset.accepted_count or dataset.row_count
    if quarantine is None:
        # 延迟导入，避免与模型模块产生循环依赖。
        from src.models.source import QuarantineRecord
        if db is None:
            # 独立会话兜底；服务内调用优先传入当前会话，避免读到未提交数据。
            with Session() as own:
                quarantine = own.scalars(select(QuarantineRecord)
                                         .where(QuarantineRecord.dataset_id == dataset.id)
                                         .order_by(QuarantineRecord.line_no, QuarantineRecord.id)).all()
        else:
            quarantine = db.scalars(select(QuarantineRecord)
                                    .where(QuarantineRecord.dataset_id == dataset.id)
                                    .order_by(QuarantineRecord.line_no, QuarantineRecord.id)).all()
    quarantine_view = [{'line_no': item.line_no,
                        'inspection_id': item.inspection_id or '（无单号）',
                        'reasons': list(item.reasons),
                        'status': item.status,
                        'source': item.source} for item in quarantine]
    return {
        'rows': [dict(row) for row in dataset.rows],
        'total_count': dataset.total_count or dataset.row_count,
        'accepted_count': accepted,
        'rejected_count': dataset.rejected_count or 0,
        'blocked_count': dataset.blocked_count or 0,
        'revision': dataset.revision or 1,
        'quarantine': quarantine_view,
    }


def _quarantine_key(raw_row, line_no):
    """隔离记录的稳定键：有单号用单号（小写折叠），否则用行号占位。"""
    value = raw_row.get('inspection_id')
    if isinstance(value, str) and value.strip():
        return value.strip().casefold()
    return f'__line_{line_no}'


def _counts(db, dataset_id):
    """返回 (待补交隔离数, 拦截数, 已补入数)，用于口径对账。"""
    statuses = db.scalars(select(QuarantineRecord.status).where(
        QuarantineRecord.dataset_id == dataset_id)).all()
    return (sum(1 for value in statuses if value == Quarantine.OPEN),
            sum(1 for value in statuses if value == Quarantine.REJECTED),
            sum(1 for value in statuses if value == Quarantine.RESOLVED))


def _refresh_totals(db, dataset):
    """对账口径：原始总行数 = 纳入数 + 待隔离数 + 拦截数；支持度分母只用纳入数。

    total_count 持久化保存首次导入收到的物理行数，补交被拒行只登记不增加总行数，
    因此这里只回算纳入/隔离/拦截三个分项。
    """
    open_count, blocked_count, _ = _counts(db, dataset.id)
    dataset.accepted_count = len(dataset.rows)
    dataset.blocked_count = blocked_count
    dataset.rejected_count = open_count + blocked_count


def _upsert_quarantine(db, dataset_id, item, source, status, actor):
    """按 (批次, 来源, 单号键, CSV 行号) 更新或新建隔离记录。

    同一物理行重复导入时幂等更新；同一单号出现在不同物理行时各自保留一条，
    使总行数与实际收到的行数对得上。
    """
    key = _quarantine_key(item['raw'], item['line_no'])
    record = db.scalar(select(QuarantineRecord).where(
        QuarantineRecord.dataset_id == dataset_id,
        QuarantineRecord.source == source,
        QuarantineRecord.inspection_id_key == key,
        QuarantineRecord.line_no == item['line_no']).with_for_update())
    if record is None:
        record = QuarantineRecord(dataset_id=dataset_id, inspection_id_key=key, source=source)
        db.add(record)
    record.inspection_id = item['raw'].get('inspection_id') or None
    record.line_no = item['line_no']
    record.raw_row = item['raw']
    record.reasons = item['reasons']
    record.status = status
    record.author = actor
    record.resolved_at = now() if status == Quarantine.RESOLVED else None
    return record


def save(db, name, filename, rows, actor):
    signature = checksum(rows)
    lookup = select(Dataset).where(Dataset.name == name, Dataset.checksum == signature)
    existing = db.scalar(lookup)
    if existing:
        return existing, False
    dataset = Dataset(name=name, filename=filename, checksum=signature, rows=rows,
                      row_count=len(rows), total_count=len(rows), accepted_count=len(rows),
                      rejected_count=0, revision=1, author=actor)
    try:
        with db.begin_nested():
            db.add(dataset)
            db.flush()
    except IntegrityError:
        existing = db.scalar(lookup.with_for_update())
        if existing:
            return existing, False
        raise
    return dataset, True


def import_batch(db, name, filename, text, actor):
    """逐行质检导入：合格行建批次，不合格行逐条隔离。返回 (dataset, summary, created)。"""
    accepted, rejected = inspect_csv(text)
    signature = checksum(accepted)
    lookup = select(Dataset).where(Dataset.name == name, Dataset.checksum == signature)
    existing = db.scalar(lookup)
    if existing and not rejected:
        return existing, _summary(existing, 0, 0, 0, []), False

    dataset = existing or Dataset(name=name, filename=filename, checksum=signature,
                                  rows=accepted, row_count=len(accepted),
                                  total_count=len(accepted) + len(rejected), author=actor)
    if existing:
        dataset.filename = filename
    else:
        db.add(dataset)
        db.flush()

    accepted_keys = {row['inspection_id'].casefold() for row in dataset.rows}
    stored = []
    for item in rejected:
        key = _quarantine_key(item['raw'], item['line_no'])
        if key in accepted_keys:
            # 单号已在纳入分母中（如重复单号）：登记为拦截，不能再占用待隔离名额。
            stored.append(_upsert_quarantine(db, dataset.id, item, 'import',
                                             Quarantine.REJECTED, actor))
        else:
            stored.append(_upsert_quarantine(db, dataset.id, item, 'import',
                                             Quarantine.OPEN, actor))
    _refresh_totals(db, dataset)
    dataset.updated_at = now()
    db.flush()
    this_accepted = 0 if existing else len(accepted)
    return dataset, _summary(dataset, len(stored), 0, this_accepted, stored), not existing


def apply_correction(db, dataset, text, actor):
    """补交修正版：只补入原先隔离的单号，已纳入与重复补交不重复计数。"""
    open_rows = db.scalars(select(QuarantineRecord).where(
        QuarantineRecord.dataset_id == dataset.id,
        QuarantineRecord.status == Quarantine.OPEN).with_for_update()).all()
    open_keys = {item.inspection_id_key for item in open_rows}
    accepted_ids = {row['inspection_id'].casefold() for row in dataset.rows}
    accepted, rejected = inspect_correction(text, accepted_ids, open_keys)

    resolved = []
    if accepted:
        by_key = {row['inspection_id'].casefold(): row for row in accepted}
        for item in open_rows:
            if item.inspection_id_key in by_key:
                item.status = Quarantine.RESOLVED
                item.resolved_at = now()
                resolved.append(item)
        dataset.rows = [*dataset.rows, *accepted]
        dataset.row_count = len(dataset.rows)
        dataset.revision += 1
        dataset.checksum = checksum(dataset.rows)
        dataset.updated_at = now()

    # 补交仍被拒的逐条登记；同单号重复补交更新原记录而非新增，分母不失真。
    extra = [_upsert_quarantine(db, dataset.id, item, 'correction',
                                Quarantine.REJECTED, actor) for item in rejected]
    _refresh_totals(db, dataset)
    db.flush()
    return _summary(dataset, len(extra), len(resolved), len(accepted), resolved + extra)


def _summary(dataset, rejected, resolved, accepted, records):
    return {
        'total_count': dataset.total_count,
        'accepted_count': dataset.accepted_count,
        'rejected_count': dataset.rejected_count,
        'blocked_count': dataset.blocked_count,
        'open_count': dataset.rejected_count - dataset.blocked_count,
        'revision': dataset.revision,
        'this_accepted': accepted,
        'this_resolved': resolved,
        'this_rejected': rejected,
        'records': [{'line_no': item.line_no, 'inspection_id': item.inspection_id,
                     'reasons': item.reasons, 'status': item.status} for item in records],
    }


def template():
    fields = [field['key'] for field in SPEC['fields']]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    # The complete representative dataset also works as a reproducible import fixture.
    writer.writerows(demo_rows())
    return '\ufeff' + buffer.getvalue()
