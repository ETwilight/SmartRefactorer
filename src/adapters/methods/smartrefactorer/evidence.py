"""Store original evidence and build compact request and development-history indexes."""

import copy
import json
from pathlib import Path
from runtime.context.store import ContextStore
from runtime.context.store import canonical
from algorithms.smartrefactorer.library import atomic


class EvidenceCatalog:
    """Make recorded feedback and development results readable by content ID.

    contexts is a ContextStore holding complete JSON records. index maps content IDs
    to request metadata and is saved in evidence-index.json. controller is attached
    when the adapter binds; development_history then lists the operator outputs and
    accepted plans stored in that controller's history.

    presentation() saves its original value and replaces large parts of the model
    view with references. A later context read follows an ID and JSON Pointer back
    to the saved value.

    Example::

        evidence = EvidenceCatalog(directory, recorder)
        identifier = evidence.remember_evidence(request, feedback)
        original = evidence.contexts.read(identifier, '/feedback')

    original is the feedback object supplied to remember_evidence().
    """

    def __init__(self, directory, recorder):
        """Open the evidence store and load its saved request index."""

        self.directory = Path(directory)
        self.recorder = recorder
        self.controller = None
        self.contexts = ContextStore(self.directory / 'evidence')
        index = self.directory / 'evidence-index.json'
        self.index = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {}
        self.development_history = {}

    def save_index(self):
        """Write the request metadata index to evidence-index.json."""
        atomic(self.directory / 'evidence-index.json', self.index)

    def presentation(self, value, inline_bytes=2048, depth=3):
        """Save a JSON value and return a view with references for its large subtrees."""
        identifier = self.contexts.put(value)

        def present(node, path, depth):
            """Inline a small subtree, descend into a small dictionary, or return its
            reference.
            """

            if len(canonical(node)) <= inline_bytes:
                return copy.deepcopy(node)
            if isinstance(node, dict) and len(node) <= 32 and depth:
                return {
                    key: present(
                        item, path + '/' + key.replace('~', '~0').replace('/', '~1'), depth - 1
                    )
                    for key, item in node.items()
                }
            reference = {'id': identifier, 'path': path}
            # index: dict replacing an oversized node with its content ID, pointer, and size
            # metadata.
            index = {
                'presentation': 'evidence_index',
                'exact_value': reference,
                'json_type': (
                    'object'
                    if isinstance(node, dict)
                    else 'array' if isinstance(node, list) else 'string'
                ),
                'bytes': len(canonical(node)),
            }
            if isinstance(node, dict):
                index['field_count'] = len(node)
                if len(node) <= 32:
                    index['keys'] = list(node)
                else:
                    index['keys_reference'] = {'id': self.contexts.put(list(node)), 'path': ''}
            elif isinstance(node, (list, str)):
                index['length'] = len(node)
            return index

        return present(value, '', depth)

    def remember_evidence(self, request, feedback):
        """Store a request/feedback pair and add its metadata to the index."""
        identifier = self.contexts.put(
            {'request': copy.deepcopy(request), 'feedback': copy.deepcopy(feedback)}
        )
        params = request.get('params', {})
        action = params.get('action') or {}
        self.index[identifier] = {
            'id': identifier,
            'op': request.get('op'),
            'method': action.get('method'),
            'request_id': request.get('request_id'),
            'status': feedback.get('status'),
            'document_id': (
                params.get('id') if request.get('op') == 'environment.document' else None
            ),
            'document_sha256': (
                params.get('sha256') if request.get('op') == 'environment.document' else None
            ),
            'schema_name': params.get('name') if request.get('op') == 'schemas.get' else None,
        }
        return identifier

    def sync_evidence(self):
        """Index recorded environment calls and select recent development records by role."""
        for call in self.recorder.env_calls:
            if call.get('stage') in ('discovery', 'workflow', 'knowledge') and isinstance(
                call.get('feedback'), dict
            ):
                self.remember_evidence(call['request'], call['feedback'])
        atomic(self.directory / 'evidence-index.json', self.index)
        records = []
        if self.controller is not None:
            for ordinal, record in enumerate(self.controller.s.get('history', [])):
                if not (record.get('role') or record.get('kind') == 'plan.accepted'):
                    continue
                identifier = self.contexts.put({'development_record': record})
                records.append(
                    {
                        'id': identifier,
                        'ordinal': ordinal,
                        'role': record.get('role'),
                        'kind': record.get('kind', 'raw_operator_output'),
                        'content_path': (
                            '/development_record/plan'
                            if record.get('kind') == 'plan.accepted'
                            else '/development_record/output'
                        ),
                        'validity': (
                            'host_validated_plan'
                            if record.get('kind') == 'plan.accepted'
                            else 'raw_output_not_automatically_valid'
                        ),
                    }
                )
        # selected: set[int] of positions in records, rather than content IDs.
        selected = set()
        for role, limit in (
            ('Plan', 3),
            ('Propose', 2),
            ('Reflect', 1),
            ('Analyze', 2),
            ('CodeGen', 2),
            ('Rewrite', 2),
            ('Patch', 2),
        ):
            matches = [i for i, e in enumerate(records) if e.get('role') == role]
            selected.update(matches[-limit:])
            if role == 'Plan' and matches:
                selected.add(matches[0])
        accepted = [i for i, e in enumerate(records) if e.get('kind') == 'plan.accepted']
        if accepted:
            selected.add(accepted[-1])
        self.development_history = {
            'catalog_ref': self.contexts.put(records),
            'total_records': len(records),
            'selected_records': [records[i] for i in sorted(selected)],
            'record_output_path': '/development_record/output',
            'guide': 'Read catalog_ref with read_context for the full history index; each record can be recalled exactly. Raw model output does not imply validity.',
        }
