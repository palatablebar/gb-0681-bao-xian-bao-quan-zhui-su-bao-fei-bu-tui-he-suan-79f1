"""补正编排：父批次 + 台账 -> 后继批次（完整重跑 M1-M7）。

后继批次不修改父批次；其身份由父批次、台账指纹与规则版本决定，
同一份补正重复提交得到同一 successor_id（幂等）。
"""
import base64
import hashlib
import json
import time

from app.config import RULE_VERSION
from app.errors import DomainError
from app.workflow import run
from app.modules.m8_correction.apply import (
    build_corrected_content,
    ledger_fingerprint,
    validate_ledger,
)


def successor_identity(parent_id, fingerprint):
    raw = parent_id.encode() + fingerprint.encode() + RULE_VERSION.encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def correction_balance(parent_state, corrections):
    """修订平衡：父隔离目标数 = CORRECTED + REJECTED（冲突按组计）。"""
    from app.modules.m8_correction.apply import quarantine_targets
    targets = quarantine_targets(parent_state['quarantine'])
    corrected = sum(1 for c in corrections if c['disposition'] == 'CORRECTED')
    rejected = sum(1 for c in corrections if c['disposition'] == 'REJECTED')
    return {
        'parent_isolated_targets': len(targets),
        'parent_isolated_rows': len(parent_state['quarantine']),
        'corrected': corrected,
        'rejected': rejected,
        'balanced': len(targets) == corrected + rejected,
    }


def build_successor(parent_state, ledger, operator='reviewer'):
    """纯计算：返回后继批次 state（不落库）。"""
    normalized = validate_ledger(parent_state.get('quarantine', []), ledger)
    source_b64 = parent_state.get('source_b64')
    if not source_b64:
        raise DomainError('LEDGER', '父批次缺少原始来源，无法补正')
    source_bytes = base64.b64decode(source_b64)
    encoding = parent_state.get('encoding', 'utf-8-sig')
    corrected_bytes, corrections = build_corrected_content(
        source_bytes, encoding, parent_state['quarantine'], normalized)

    params = dict(parent_state.get('manifest', {}).get('parameters', {}))
    start = time.monotonic()
    successor = run(corrected_bytes, params)
    elapsed = round((time.monotonic() - start) * 1000)

    fingerprint = ledger_fingerprint(normalized)
    parent_id = parent_state['run_id']
    successor_id = successor_identity(parent_id, fingerprint)
    # 重算身份以补正派生口径为准，覆盖 workflow 按派生字节算出的 run_id
    successor['run_id'] = successor_id
    successor['parent_run_id'] = parent_id
    successor['lifecycle'] = 'active'
    successor['lineage_id'] = parent_state.get('lineage_id', parent_id)
    successor['correction_no'] = int(parent_state.get('correction_no', 0)) + 1
    successor['corrections'] = corrections
    successor['correction_ledger_fp'] = fingerprint
    successor['correction_balance'] = correction_balance(parent_state, corrections)
    successor['operator'] = operator
    successor.setdefault('manifest', {})['derived_from'] = {
        'parent_run_id': parent_id,
        'source_sha256': parent_state.get('manifest', {}).get('sha256'),
        'ledger_fingerprint': fingerprint,
        'correction_elapsed_ms': elapsed,
    }
    # 后继输入同样留底，保证链上任一批次都能独立复算
    successor['source_b64'] = base64.b64encode(corrected_bytes).decode('ascii')
    if not successor['correction_balance']['balanced']:
        raise DomainError('LEDGER', '修订平衡校验失败')
    # run_id 与 manifest 在 run() 之后才定稿，归档校验和须按最终态重算
    from app.modules.m7_archive.export_out import checksum
    from app.modules.m7_archive import service as archive_service
    successor = archive_service.execute(successor)
    successor['archive']['sha256'] = checksum(successor)
    return successor
