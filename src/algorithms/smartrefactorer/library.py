"""技能库的版本与发布事务。

commit 将父版本、候选、方案和检查证据绑定为事务，依次写入 intent、版本、
manifest 与 receipt。reconcile 根据已落盘的父子版本补齐中断的提交记录。
"""

import copy
import hashlib
import json
import os
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()
    ).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.new')
    # The C encoder produces the same complete JSON values in one pass.
    # Pretty iterencode expands large region receipts and writes millions of
    # tiny chunks on every checkpoint; formatting is not execution evidence.
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')
    with temporary.open('wb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    deadline = time.monotonic() + 5
    while True:
        try:
            # 替换目标文件是落盘的提交点；前面的写入和 fsync 已完成。
            os.replace(temporary, path)
            break
        except PermissionError as error:
            # Windows readers/antivirus may briefly deny deletion sharing.
            # Retry only committing the already-fsynced bytes, never any skill
            # or environment operation. Permanent errors retain both versions.
            if (
                os.name != 'nt'
                or getattr(error, 'winerror', None) not in (5, 32, 33)
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)


class Library:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'manifest.json'
        self.manifest = (
            json.loads(self.path.read_text(encoding='utf-8'))
            if self.path.exists()
            else {'version': 0, 'components': {}, 'commits': []}
        )
        self.reconcile()

    def reconcile(self):
        intent = self.folder / 'intent.json'
        if not intent.exists():
            return
        value = json.loads(intent.read_text(encoding='utf-8'))
        if digest(self.manifest) == value['candidate_hash']:
            atomic(self.folder / 'receipts' / (value['id'] + '.json'), value)
        elif digest(self.manifest) != value['parent_hash']:
            raise ValueError('Unresolved library transaction; retain intent and artifacts')

    def commit(self, parent, components, evidence, proposal, *, validated_candidate=None):
        """将当前候选的检查证据和父版本写入一次发布事务。

        事务键由父版本、组件、证据和方案共同计算，重复提交返回同一个事务。
        """
        if not evidence or any(e.get('status') != 'PASS' for e in evidence):
            raise ValueError('Publication requires all required checks to pass')
        validated = components if validated_candidate is None else validated_candidate
        validated_hash = digest(validated)
        if any(e.get('candidate_hash') != validated_hash for e in evidence):
            raise ValueError(
                'Publication evidence belongs to a different or unspecified candidate'
            )
        for name, component in components.items():
            if component != self.manifest['components'].get(
                name
            ) and component != validated.get(name):
                raise ValueError('Published component differs from the validated candidate')
        # 事务键让同一组发布输入得到相同提交身份。
        transaction = digest([parent, components, evidence, proposal])
        if transaction in self.manifest['commits']:
            return transaction
        if parent != digest(self.manifest):
            raise ValueError('Stale library parent')
        candidate = {
            'version': self.manifest['version'] + 1,
            'components': copy.deepcopy(components),
            'commits': self.manifest['commits'] + [transaction],
        }
        for name, component in components.items():
            if component['id'] != name or component['hash'] != digest(component['source']):
                raise ValueError('Component identity/hash mismatch')
        intent = {
            'id': transaction,
            'parent_hash': parent,
            'candidate_hash': digest(candidate),
            'validated_candidate_hash': validated_hash,
            'evidence': evidence,
            'proposal': proposal,
        }
        # 先记录提交意图，再写版本和 manifest；reconcile 通过这些落盘状态续接收据。
        atomic(self.folder / 'intent.json', intent)
        atomic(self.folder / 'versions' / (digest(candidate) + '.json'), candidate)
        atomic(self.path, candidate)
        self.manifest = candidate
        atomic(self.folder / 'receipts' / (transaction + '.json'), intent)
        return transaction
