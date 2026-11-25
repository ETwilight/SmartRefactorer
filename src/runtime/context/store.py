"""Store JSON values by content hash and build views with repeated values factored out."""

import copy
import hashlib
import json
from pathlib import Path
import re
import uuid
from collections import Counter


def canonical(value):
    """Encode a JSON value as sorted, compact UTF-8 bytes for hashing."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')


class ContextStore:
    """Keep complete JSON values on disk and present repeated subtrees as references.

    folder is a Path containing one JSON file per SHA-256 content ID. min_bytes
    sets the minimum encoded size for factoring out a value. last_stats records
    the original and presented byte counts from the most recent view(). read()
    retrieves a complete stored value or a subtree selected by JSON Pointer.

    Example:
        store = ContextStore(folder)
        identifier = store.put(feedback)
        result = store.read(identifier, '/result')

    result is the original feedback's result field, loaded from its stored JSON.
    """

    def __init__(self, folder, min_bytes=512):
        """Create the storage directory and initialize presentation statistics."""

        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.min_bytes = min_bytes
        self.last_stats = {}

    def put(self, value):
        """Write a JSON value under its content hash and return that hash."""
        # data: bytes; the hash identifies these exact canonical JSON bytes.

        data = canonical(value)
        identifier = hashlib.sha256(data).hexdigest()
        target = self.folder / (identifier + '.json')
        if not target.exists():
            temporary = self.folder / (identifier + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
        return identifier

    def read(self, identifier, path=''):
        """Load a stored value, verify its hash, and follow an optional JSON Pointer."""

        if not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise ValueError('Invalid context reference')
        target = self.folder / (identifier + '.json')
        if not target.is_file():
            raise ValueError('Unknown context reference')
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != identifier:
            raise ValueError('Context reference integrity check failed')
        # value: a JSON object, array, or scalar decoded from the stored bytes.
        value = json.loads(data)
        if path:
            if not path.startswith('/'):
                raise ValueError('Use a JSON Pointer beginning with /')
            for key in path[1:].split('/'):
                # Decode ~1 before ~0 so a literal ~1 encoded as ~01 remains ~1.
                key = key.replace('~1', '/').replace('~0', '~')
                if isinstance(value, list):
                    if not re.fullmatch(r'0|[1-9][0-9]*', key):
                        raise ValueError('Invalid JSON Pointer array index')
                    # value becomes the selected array element, which may have a different JSON
                    # type.
                    value = value[int(key)]
                else:
                    # value becomes the selected object field, which may have a different JSON
                    # type.
                    value = value[key]
        return value

    def reference(self, value):
        """Store a value and return its ID with a complete read_context request."""

        identifier = self.put(value)
        return {
            'id': identifier,
            'read': {'decision': 'read_context', 'id': identifier, 'path': ''},
        }

    def view(self, value):
        """Build a smaller reference view when factoring repeated values saves bytes.

        Store the original value first. Normalize JSON embedded in strings, except
        within recalled feedback. Source data containing reserved markers keeps its
        original structure. last_stats describes the view actually returned.
        """
        original = self.put(value)
        collision = False

        def normalize(node, literal=False):
            """Expose embedded JSON while retaining its source text for exact reads."""

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
                    # node is str here; parsed is the JSON container encoded by that string.
                    parsed = json.loads(node)
                except (ValueError, RecursionError):
                    return node
                return {'$json_text': normalize(parsed), '$source_text': self.put(node)}
            return node

        normalized = normalize(value)
        # Reserved markers in source data require the original, unencoded view.
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
            """Return a content ID for a sufficiently large container or string."""

            if not isinstance(node, (dict, list, str)):
                return None
            data = canonical(node)
            return hashlib.sha256(data).hexdigest() if len(data) >= self.min_bytes else None

        def count(node):
            """Count occurrences of each large value throughout the normalized tree."""

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
        # objects: dict[str, object], mapping content IDs to their encoded definitions.
        objects = {}

        def encode(node, skip=None):
            """Replace repeated values with references and collect their inline definitions."""

            identifier = identity(node)
            if identifier and identifier != skip and counts[identifier] > 1:
                if identifier not in objects:
                    objects[identifier] = encode(node, skip=identifier)
                    # Persist each referenced value so read_context can retrieve it by ID.
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
