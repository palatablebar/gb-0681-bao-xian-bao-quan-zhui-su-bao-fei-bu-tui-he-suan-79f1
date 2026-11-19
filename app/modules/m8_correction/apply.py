"""隔离补正：由原始字节 + 补正台账确定性派生后继批次输入。

设计原则（见 docs/CORRECTION-RULING.md）：
- 不触碰父批次任何数据，只读取其原始字节与隔离清单；
- 每条隔离记录必须在台账中有且仅有一次处置（CORRECTED / REJECTED）；
- 派生过程纯函数化，同输入同输出，支撑幂等重跑。
"""
import csv
import io
import json
import hashlib
from datetime import datetime, timezone

from app.errors import DomainError
from app.config import REQUIRED

DISPOSITIONS = ('CORRECTED', 'REJECTED')


def _group_key(item):
    """冲突按同键整组处置；其余隔离按来源行处置。"""
    if item.get('code') == 'CONFLICT':
        return ('CONFLICT', item.get('id', ''))
    return ('LINE', item['line'])


def _parse_target(raw_target):
    """前端提交的行号是字符串，统一归一为内部键类型。"""
    kind, value = raw_target[0], raw_target[1]
    if kind == 'LINE':
        try:
            return ('LINE', int(value))
        except (TypeError, ValueError):
            raise DomainError('LEDGER', '行目标必须是数字: %r' % (value,))
    if kind == 'CONFLICT':
        return ('CONFLICT', str(value))
    raise DomainError('LEDGER', '未知目标类型: %s' % kind)


def quarantine_targets(quarantine):
    """父批次隔离记录的处置目标集合（冲突整组算一个目标）。"""
    targets = {}
    for item in quarantine:
        key = _group_key(item)
        targets.setdefault(key, []).append(item)
    return targets


def canonical_ledger(ledger):
    """台账规范化序列化：用于幂等键与重复提交判定。"""
    normalized = [
        {
            'target': sorted(str(k) for k in entry['target']),
            'disposition': entry['disposition'],
            'after': entry.get('after') or {},
            'basis': str(entry.get('basis', '')).strip(),
        }
        for entry in ledger
    ]
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def validate_ledger(quarantine, ledger):
    """校验台账：目标全覆盖且不重复、依据齐全、补正取值结构合法。"""
    if not isinstance(ledger, list) or not ledger:
        raise DomainError('LEDGER', '补正台账为空或格式无效')
    targets = quarantine_targets(quarantine)
    seen = set()
    normalized = []
    for entry in ledger:
        if not isinstance(entry, dict):
            raise DomainError('LEDGER', '补正条目必须是对象')
        disposition = entry.get('disposition')
        if disposition not in DISPOSITIONS:
            raise DomainError('LEDGER', '处置方式必须为 CORRECTED 或 REJECTED')
        raw_target = entry.get('target')
        if not isinstance(raw_target, list) or not raw_target:
            raise DomainError('LEDGER', '补正目标缺失')
        try:
            target = _parse_target(raw_target)
        except DomainError:
            raise
        except (TypeError, IndexError):
            raise DomainError('LEDGER', '补正目标格式无效')
        if target not in targets:
            raise DomainError('LEDGER', '补正目标不属于当前隔离记录: %s' % (raw_target,))
        if target in seen:
            raise DomainError('LEDGER', '同一隔离记录不得重复处置: %s' % (raw_target,))
        seen.add(target)
        basis = str(entry.get('basis', '')).strip()
        if not basis:
            raise DomainError('LEDGER', '补正必须填写依据（补件/授权来源）')
        after = entry.get('after') or {}
        if not isinstance(after, dict):
            raise DomainError('LEDGER', '补正后取值必须是对象')
        if disposition == 'CORRECTED' and not after:
            raise DomainError('LEDGER', 'CORRECTED 必须提供补正后取值')
        normalized.append({
            'target': list(target), 'disposition': disposition,
            'after': after, 'basis': basis,
        })
    missing = [list(k) for k in targets.keys() if k not in seen]
    if missing:
        raise DomainError('LEDGER', '存在未处置的隔离记录: %s' % missing)
    return normalized


def _logical_rows(raw_text):
    """用 csv 解析取回逻辑记录（含引号内换行），保留物理行号。"""
    reader = csv.reader(io.StringIO(raw_text, newline=''))
    header = [s.strip().lstrip('\ufeff') for s in next(reader)]
    rows = []
    for row in reader:
        rows.append((reader.line_num, row))
    return header, rows


def build_corrected_content(source_bytes, encoding, quarantine, ledger):
    """返回 (corrected_bytes, corrections)。

    WIDTH 整行替换；MISSING 等字段级补正按列改值；CONFLICT 整组替换为授权行；
    REJECTED 的物理行从后继输入中剔除（仍在台账与导出中留痕）。
    """
    try:
        raw_text = source_bytes.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        raw_text = source_bytes.decode('utf-8-sig')
    header, logical = _logical_rows(raw_text)
    targets = quarantine_targets(quarantine)
    by_target = {_parse_target(e['target']): e for e in ledger}

    drop_lines = set()          # REJECTED / 被整组替换掉的物理行
    line_replacements = {}      # 物理行 -> 新行（WIDTH 整行 / 字段改值）
    insert_after = {}           # 物理行 -> 待插入的新行列表（CONFLICT 授权行）
    corrections = []
    now = datetime.now(timezone.utc).isoformat()

    for key, members in targets.items():
        entry = by_target[key]
        member_lines = [m['line'] for m in members]
        record = {
            'target': list(key), 'lines': member_lines,
            'codes': sorted({m.get('code', '') for m in members}),
            'ids': sorted({m.get('id', '') for m in members if m.get('id')}),
            'before': [m.get('raw', {}) for m in members],
            'disposition': entry['disposition'],
            'after': entry['after'], 'basis': entry['basis'],
            'created_at': now,
        }
        if entry['disposition'] == 'REJECTED':
            record['outcome'] = '维持隔离'
            drop_lines.update(member_lines)
            corrections.append(record)
            continue

        after = entry['after']
        if key[0] == 'CONFLICT':
            # 整组撤下，替换为一条授权记录
            drop_lines.update(member_lines)
            missing_cols = [col for col in header if col not in after]
            if missing_cols:
                raise DomainError('LEDGER', '冲突授权记录缺列: %s' % missing_cols)
            new_row = [str(after[col]) for col in header]
            anchor = max(member_lines)
            insert_after.setdefault(anchor, []).append(new_row)
            record['outcome'] = '冲突整组替换为授权记录'
        elif members[0].get('code') == 'WIDTH':
            missing_cols = [col for col in header if col not in after]
            if missing_cols:
                raise DomainError('LEDGER', 'WIDTH 补正必须提供全部 %d 列，缺: %s'
                                  % (len(header), missing_cols))
            new_row = [str(after[col]) for col in header]
            line_replacements[member_lines[0]] = new_row
            record['outcome'] = '短行整行补正'
        else:
            original = next((row for line, row in logical if line == member_lines[0]), None)
            if original is None:
                raise DomainError('LEDGER', '隔离行 %s 在原始输入中不存在' % member_lines[0])
            new_row = list(original)
            for col, value in after.items():
                if col not in header:
                    raise DomainError('LEDGER', '补正列不存在: %s' % col)
                new_row[header.index(col)] = str(value)
            line_replacements[member_lines[0]] = new_row
            record['outcome'] = '字段补正后重入'
        corrections.append(record)

    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(header)
    for line, row in logical:
        if line in drop_lines:
            if line in insert_after:
                for new_row in insert_after.pop(line):
                    writer.writerow(new_row)
            continue
        writer.writerow(line_replacements.get(line, row))
        if line in insert_after:
            for new_row in insert_after.pop(line):
                writer.writerow(new_row)
    if insert_after:  # 理论不可达：锚点行必然存在
        raise DomainError('LEDGER', '补正插入锚点丢失')
    corrected = ('\ufeff' + out.getvalue()).encode('utf-8')
    # 再校验派生输入自身表头合规，避免补正引入坏数据
    check_text = corrected.decode('utf-8-sig')
    check_header, check_rows = _logical_rows(check_text)
    if check_header != header or not set(REQUIRED) <= set(check_header):
        raise DomainError('LEDGER', '补正后输入表头不合规')
    return corrected, corrections


def ledger_fingerprint(ledger):
    return hashlib.sha256(canonical_ledger(ledger).encode('utf-8')).hexdigest()
