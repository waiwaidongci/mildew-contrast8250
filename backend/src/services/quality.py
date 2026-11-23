"""逐行质检：合格记录纳入批次，不合格记录逐条隔离并保留原因。

整批退回会耽误筛查，静默跳过又会让分母失真，因此导入与补交都按行处理：
- 结构层面（表头、列数、行数上限）仍整批拒绝，因为无法可靠地按行解析；
- 单条记录的字段问题（缺列/格式、湿度越界、单号重复）只隔离该行，并保留原始内容与原因。
"""

import csv
import io

from src.domain import analysis
from src.domain.spec import SPEC
from src.services.validation import cell

_FIELDS = SPEC['fields']
_KEYS = [field['key'] for field in _FIELDS]
_HUMIDITY = next(field for field in _FIELDS if field['key'] == 'humidity')

REASON_MISSING = '缺列或字段为空'
REASON_FORMAT = '字段格式不正确'
REASON_HUMIDITY = '湿度越界'
REASON_DUPLICATE = '巡检单号重复'

def _clean(raw):
    return raw.strip() if isinstance(raw, str) else raw


def _check_humidity(value):
    """湿度单独判定：区分格式错误与越界，返回 (值, 原因或 None)。"""
    try:
        parsed = cell(_HUMIDITY, value)
        return parsed, None
    except ValueError:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{_HUMIDITY['label']}格式不正确（{REASON_FORMAT}）"
        return None, (f"{_HUMIDITY['label']}{number:g}% 超出允许范围 "
                      f"{_HUMIDITY['min']:g}–{_HUMIDITY['max']:g}%（{REASON_HUMIDITY}）")


def _row_quality(raw, line_num, seen_ids):
    """检查一行，返回 (是否合格, 规范化行或 None, 原因列表, 原始行, 行号)。"""
    reasons = []
    original = {key: (raw.get(key) if raw.get(key) is not None else '') for key in _KEYS}

    # 列数与表头不一致：DictReader 对多列给出 None 键，对少列给出 None 值。
    if None in raw or any(value is None for value in raw.values()):
        reasons.append('工作表的列数与表头不一致')

    record = {}
    for field in _FIELDS:
        key, label = field['key'], field['label']
        value = _clean(raw.get(key))
        if value is None or value == '':
            if field.get('required', True):
                reasons.append(f'{label}缺失（{REASON_MISSING}）')
            record[key] = None
            continue
        if key == 'humidity':
            parsed, reason = _check_humidity(value)
            if reason:
                reasons.append(reason)
                record[key] = value
            else:
                record[key] = parsed
            continue
        try:
            record[key] = cell(field, value)
        except ValueError:
            reasons.append(f'{label}格式不正确（{REASON_FORMAT}）')
            record[key] = value

    inspection_id = record.get('inspection_id')
    if isinstance(inspection_id, str) and inspection_id:
        folded = inspection_id.casefold()
        if folded in seen_ids:
            reasons.append(f'巡检单号「{inspection_id}」与同批前面的记录重复（{REASON_DUPLICATE}）')
        else:
            seen_ids.add(folded)

    if reasons:
        return False, None, reasons, original, line_num
    return True, record, [], original, line_num


def inspect_csv(text):
    """解析整份 CSV，逐行质检，返回 (纳入行, 隔离行)。

    结构错误（表头不符、无法解析、空文件、超行数）抛出 ValueError 整批拒绝；
    单行业务问题写入 rejected，不影响其他行。
    """
    reader = csv.DictReader(io.StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    required = {field['key'] for field in _FIELDS if field.get('required', True)}
    accepted = []
    rejected = []
    seen_ids = set()
    try:
        headers = reader.fieldnames or []
        if not required <= set(headers) or not set(headers) <= set(_KEYS) or len(headers) != len(set(headers)):
            raise ValueError('CSV 表头与字段说明不符，请下载模板检查列名')
        count = 0
        for raw in reader:
            count += 1
            if count > 5000:
                raise ValueError('一个数据批次最多导入 5000 行')
            ok, record, reasons, original, line_num = _row_quality(raw, reader.line_num, seen_ids)
            if ok:
                accepted.append(record)
            else:
                rejected.append({'line_no': line_num, 'raw': original, 'reasons': reasons})
    except csv.Error:
        raise ValueError(f'CSV 第 {reader.line_num} 行附近格式不正确') from None
    if not accepted and not rejected:
        raise ValueError('CSV 至少需要一行有效数据')
    # 与计算口径保持一致的兜底校验（正常情况下不会触发）。
    if accepted:
        analysis.validate_rows(accepted)
    return accepted, rejected


def inspect_correction(text, accepted_ids, open_quarantine_keys):
    """质检补交修正版。

    accepted_ids：当前批次已纳入单号（小写折叠）集合；
    open_quarantine_keys：仍处于隔离状态的单号集合。
    只补入原先隔离的单号：已纳入的拒绝、不在隔离清单的拒绝、同次重复只计一次。
    """
    reader = csv.DictReader(io.StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    required = {field['key'] for field in _FIELDS if field.get('required', True)}
    accepted = []
    rejected = []
    file_seen = set()  # 只追踪本次补交文件内部的单号重复
    try:
        headers = reader.fieldnames or []
        if not required <= set(headers) or not set(headers) <= set(_KEYS) or len(headers) != len(set(headers)):
            raise ValueError('CSV 表头与字段说明不符，请下载模板检查列名')
        count = 0
        for raw in reader:
            count += 1
            if count > 5000:
                raise ValueError('一次补交最多包含 5000 行')
            original = {key: (raw.get(key) if raw.get(key) is not None else '') for key in _KEYS}
            ok, record, reasons, _original, line_num = _row_quality(raw, reader.line_num, file_seen)
            inspection_id = record.get('inspection_id') if record else None
            folded = inspection_id.casefold() if isinstance(inspection_id, str) and inspection_id else None
            # 已纳入分母的单号优先给出明确原因；重复执行补交也落到这里，保证幂等文案。
            if folded is not None and folded in accepted_ids:
                reasons = [reason for reason in reasons if REASON_DUPLICATE not in reason]
                reasons.append(f'巡检单号「{inspection_id}」已纳入本批次，补交不重复计数')
                ok = False
            if not ok:
                rejected.append({'line_no': line_num, 'raw': original, 'reasons': reasons})
                continue
            if folded not in open_quarantine_keys:
                rejected.append({'line_no': line_num, 'raw': original,
                                 'reasons': [f'巡检单号「{inspection_id}」不在原隔离清单中，'
                                             '补交只接受原先隔离的单号']})
                continue
            # 通过全部检查后才占用单号，同一修正版内再次出现的同单号会被识别为重复。
            file_seen.add(folded)
            accepted.append(record)
    except csv.Error:
        raise ValueError(f'CSV 第 {reader.line_num} 行附近格式不正确') from None
    if not accepted and not rejected:
        raise ValueError('补交 CSV 至少需要一行数据')
    if accepted:
        analysis.validate_rows(accepted)
    return accepted, rejected
