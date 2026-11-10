"""上下文的内容寻址存储与重复内容编码。

完整 JSON 以规范字节计算哈希；展示视图把重复的大对象换成精确引用，
read 根据引用和 JSON Pointer 取回对应内容。
"""

import copy
import hashlib
import json
from pathlib import Path
import re
import uuid
from collections import Counter


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')


class ContextStore:
    def __init__(self, folder, min_bytes=512):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.min_bytes = min_bytes
        self.last_stats = {}

    def put(self, value):
        data = canonical(value)
        identifier = hashlib.sha256(data).hexdigest()
        target = self.folder / (identifier + '.json')
        if not target.exists():
            temporary = self.folder / (identifier + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
        return identifier

    def read(self, identifier, path=''):
        if not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise ValueError('Invalid context reference')
        target = self.folder / (identifier + '.json')
        if not target.is_file():
            raise ValueError('Unknown context reference')
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != identifier:
            raise ValueError('Context reference integrity check failed')
        value = json.loads(data)
        if path:
            if not path.startswith('/'):
                raise ValueError('Use a JSON Pointer beginning with /')
            for key in path[1:].split('/'):
                # JSON Pointer 先解码 ~1，再解码 ~0；顺序决定字面 ~1 如何还原。
                key = key.replace('~1', '/').replace('~0', '~')
                if isinstance(value, list):
                    if not re.fullmatch(r'0|[1-9][0-9]*', key):
                        raise ValueError('Invalid JSON Pointer array index')
                    value = value[int(key)]
                else:
                    value = value[key]
        return value

    def reference(self, value):
        identifier = self.put(value)
        return {
            'id': identifier,
            'read': {'decision': 'read_context', 'id': identifier, 'path': ''},
        }

    def view(self, value):
        """生成重复对象的精确引用视图，并保存原始上下文。

        用户数据包含保留标记时使用原结构；已召回反馈作为字面内容保留。
        """
        original = self.put(value)
        collision = False

        def normalize(node, literal=False):
            nonlocal collision
            if isinstance(node, dict):
                collision = collision or bool(
                    set(node) & {'$context_ref', '$json_text', '$source_text'}
                )
                recalled = (
                    (node.get('request') or {}).get('decision') == 'read_context'
                    if isinstance(node.get('request'), dict)
                    else False
                )
                return {
                    k: normalize(v, literal or (recalled and k == 'feedback'))
                    for k, v in node.items()
                }
            if isinstance(node, list):
                return [normalize(v, literal) for v in node]
            if (
                not literal
                and isinstance(node, str)
                and len(node) >= self.min_bytes
                and node.lstrip().startswith(('{', '['))
            ):
                try:
                    parsed = json.loads(node)
                except (ValueError, RecursionError):
                    return node
                return {'$json_text': normalize(parsed), '$source_text': self.put(node)}
            return node

        normalized = normalize(value)
        # 原始数据出现编码标记时保留原结构，标记空间与用户内容分别解释。
        if collision:
            size = len(canonical(value))
            self.last_stats = {
                'original_context_ref': original,
                'original_bytes': size,
                'presented_bytes': size,
                'inline_objects': 0,
                'encoded': False,
                'reason': 'reserved marker appears in source data',
            }
            return copy.deepcopy(value)
        counts = Counter()

        def identity(node):
            if not isinstance(node, (dict, list, str)):
                return None
            data = canonical(node)
            return hashlib.sha256(data).hexdigest() if len(data) >= self.min_bytes else None

        def count(node):
            identifier = identity(node)
            if identifier:
                counts[identifier] += 1
            if isinstance(node, dict):
                for item in node.values():
                    count(item)
            elif isinstance(node, list):
                for item in node:
                    count(item)

        count(normalized)
        objects = {}

        def encode(node, skip=None):
            identifier = identity(node)
            if identifier and identifier != skip and counts[identifier] > 1:
                if identifier not in objects:
                    objects[identifier] = encode(node, skip=identifier)
                    # Exact object recall does not depend on the inline pool.
                    assert self.put(node) == identifier
                return {'$context_ref': identifier}
            if isinstance(node, dict):
                return {k: encode(v) for k, v in node.items()}
            if isinstance(node, list):
                return [encode(v) for v in node]
            return node

        payload = encode(normalized)
        result = {
            'context_encoding': 'exact_refs_v1',
            'payload': payload,
            'context_objects': objects,
            'original_context_ref': original,
        }
        before = len(canonical(value))
        after = len(canonical(result))
        used = after < before
        self.last_stats = {
            'original_context_ref': original,
            'original_bytes': before,
            'presented_bytes': after if used else before,
            'inline_objects': len(objects) if used else 0,
            'encoded': used,
        }
        return result if used else copy.deepcopy(value)
