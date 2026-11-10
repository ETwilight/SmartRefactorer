"""Shared model calls, operator continuations and protocol-output validation.

This adapter owns provider/format errors and exact tool-result delivery. It does
not choose algorithm phases or reinterpret native feedback as task success.
"""

import copy
import json
import uuid
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from runtime.models.transport import structured_call
from runtime.models.transport import ModelOutputError
from runtime.context.delivery import GUIDE as DELIVERY_GUIDE
from runtime.context.delivery import history_views
from runtime.context.delivery import recover_rejected_delivery
from runtime.contracts import obj, validate
from audit.control import RunStopped
from algorithms.smartrefactorer.library import digest
from algorithms.smartrefactorer.execution import python_backend

CODE_OUTPUT_TOKENS = 32768
RECORD_FORMATS = FormatChecker()


@RECORD_FORMATS.checks('python-source', raises=(SyntaxError, ValueError, TypeError))
def valid_python_source(source):
    compile(source, '<operator-check>', 'exec')
    return True


def json_value(text):

    def invalid(value):
        raise ValueError('Non-finite JSON number: ' + value)

    return json.loads(text, parse_constant=invalid)


class OperatorClient:
    """One metered transport for coordinator and serial development operators."""

    def __init__(self, recorder, environment, evidence, save):
        self.recorder = recorder
        self.environment = environment
        self.evidence = evidence
        self.persist = save
        self.controller = None
        self.requests = []

    def guard(self):
        if self.recorder.model_guard:
            self.recorder.model_guard()

    def sync_evidence(self):
        self.evidence.sync_evidence()

    def coordinate(self, instructions, data, schema):
        # Public observations and complete program returns can contain entire
        # regions. Index unrequested evidence; retain explicit reads in full.
        key = digest(
            {
                'instructions': instructions,
                'schema': schema,
                'binding': data.get('task_binding'),
                'development': data.get('development'),
            }
        )
        session = self.controller.s.get('coordinator_session')
        if not session or session.get('key') != key:
            session = {'key': key, 'history': []}
            self.controller.s['coordinator_session'] = session
        indexed = self.evidence.presentation(data)
        wire_schema = copy.deepcopy(schema)
        wire_schema['properties']['decision']['enum'].extend(
            ['read_context', 'read_context_batch']
        )
        guide = (
            '\nLarge unrequested values are evidence_index availability records, not their contents. '
            'Use decision=read_context and request_json={"id":ID,"path":JSON_POINTER} '
            'to read the exact value or a subtree; array indices and listed keys can extend the pointer. '
            'Use decision=read_context_batch with request_json={"items":[{"id":ID,"path":POINTER},...]} '
            'for several reads. Empty path reads the complete object. Every model-accepted result remains '
            'fully supplied in coordinator_tool_history across continuation; indexes do not prove unseen facts. '
            'These reads access stored evidence and do not dispatch environment operations.'
        )
        guide += DELIVERY_GUIDE
        recover_rejected_delivery(
            session,
            getattr(self.recorder, 'models', []),
            'smartrefactorer_coordinate',
            self.evidence,
            field='coordinator_tool_history',
        )
        while True:
            self.persist()
            try:
                decision, metadata = structured_call(
                    instructions + guide,
                    {
                        **indexed,
                        'coordinator_tool_history': history_views(session, self.evidence),
                    },
                    wire_schema,
                    'smartrefactorer_coordinate',
                    max_output_tokens=8000,
                )
            except RunStopped as error:
                if error.reason != 'model_context_blocked' or not recover_rejected_delivery(
                    session,
                    getattr(self.recorder, 'models', []),
                    'smartrefactorer_coordinate',
                    self.evidence,
                    field='coordinator_tool_history',
                ):
                    raise
                self.recorder.event(
                    'model.context_delivery_recovered', scope='smartrefactorer_coordinate'
                )
                continue
            session['accepted_history_length'] = len(session['history'])
            self.requests.append(metadata)
            if decision['decision'] not in ('read_context', 'read_context_batch'):
                return decision, metadata
            try:
                parameters = json_value(decision['request_json'])
                items = (
                    parameters['items']
                    if decision['decision'] == 'read_context_batch'
                    else [parameters]
                )
                if not isinstance(items, list) or not items:
                    raise ValueError('Evidence read requires a nonempty list of references')
                results = []
                for reference in items:
                    try:
                        if (
                            not isinstance(reference, dict)
                            or not isinstance(reference.get('id'), str)
                            or not isinstance(reference.get('path', ''), str)
                        ):
                            raise ValueError(
                                'Each evidence reference requires a string id and JSON Pointer path'
                            )
                        result = self.evidence.contexts.read(
                            reference['id'], reference.get('path', '')
                        )
                        results.append(
                            {
                                'id': reference['id'],
                                'path': reference.get('path', ''),
                                'status': 'completed',
                                'result': result,
                            }
                        )
                    except (ValueError, KeyError, IndexError, TypeError) as error:
                        results.append(
                            {
                                'reference': copy.deepcopy(reference),
                                'status': 'error',
                                'error': {'type': type(error).__name__, 'message': str(error)},
                            }
                        )
                feedback = {'status': 'completed', 'results': results}
            except (ValueError, KeyError, IndexError, TypeError) as error:
                feedback = {
                    'status': 'error',
                    'error': {'type': type(error).__name__, 'message': str(error)},
                }
            session['history'].append(
                {'request': copy.deepcopy(decision), 'feedback': feedback}
            )
            self.persist()

    def ask(self, role, instructions, data):
        """Deliver all valid reads in full, preserve the operator cursor, and meter every actual model turn."""
        self.persist()
        self.sync_evidence()
        methods = sorted(self.environment.knowledge_methods()) if self.environment else []
        final = obj({'decision': {'enum': ['respond']}, 'record_json': {'type': 'string'}})
        if role in ('CodeGen', 'Rewrite', 'Patch'):
            component_output = obj(
                {
                    'decision': {'enum': ['write_component']},
                    'target': {'type': 'string'},
                    'expected_hash': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                    'source': {'type': 'string'},
                    'interface_json': {'type': 'string'},
                    'contract_json': {'type': 'string'},
                }
            )
            blocked_output = obj(
                {
                    'decision': {'enum': ['component_blocked']},
                    'target': {'type': 'string'},
                    'expected_hash': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                    'conflict': {'type': 'string'},
                    'evidence_refs_json': {'type': 'string'},
                }
            )
            final = {'anyOf': [component_output, blocked_output]}
        choices = [
            final,
            obj(
                {
                    'decision': {'enum': ['read_source']},
                    'component_ids': {'type': 'array', 'items': {'type': 'string'}},
                }
            ),
        ]
        choices.append(
            obj(
                {
                    'decision': {'enum': ['read_context']},
                    'id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                    'path': {'type': 'string'},
                }
            )
        )
        choices.append(
            obj(
                {
                    'decision': {'enum': ['read_context_batch']},
                    'items': {
                        'type': 'array',
                        'minItems': 1,
                        'maxItems': 32,
                        'items': obj(
                            {
                                'id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                                'path': {'type': 'string'},
                            }
                        ),
                    },
                }
            )
        )
        if methods:
            choices.append(
                obj(
                    {
                        'decision': {'enum': ['knowledge_query']},
                        'method': {'enum': methods},
                        'params_json': {'type': 'string'},
                    }
                )
            )
        if self.environment:
            choices.append(
                obj(
                    {
                        'decision': {'enum': ['environment_query']},
                        'request_json': {'type': 'string'},
                    }
                )
            )
        key = digest(
            {
                'role': role,
                'instructions': instructions,
                'data': {k: v for k, v in data.items() if k != 'public'},
                'binding': self.controller.s.get('binding'),
                'phase': self.controller.s.get('phase'),
                'edit_cursor': self.controller.s.get('edit_cursor'),
                'repair_cursor': self.controller.s.get('repair_cursor'),
                'candidate': self.controller.s.get('candidate'),
                'parent': self.controller.s.get('parent'),
            }
        )
        session = self.controller.s.get('operator_session')
        if session is not None:
            valid = (
                isinstance(session, dict)
                and isinstance(session.get('key'), str)
                and isinstance(session.get('role'), str)
                and isinstance(session.get('history'), list)
                and all(
                    (
                        isinstance(e, dict) and 'request' in e and ('feedback' in e)
                        for e in session['history']
                    )
                )
                and isinstance(session.get('cache'), dict)
                and all(
                    (
                        isinstance(k, str)
                        and type(i) is int
                        and (0 <= i < len(session['history']))
                        for k, i in session['cache'].items()
                    )
                )
                and (type(session.get('invalid_outputs')) is int)
                and (session['invalid_outputs'] >= 0)
                and isinstance(session.get('retry_instruction'), str)
                and ('result' not in session or isinstance(session['result'], dict))
            )
            if not valid:
                raise RunStopped(
                    'operator_stop',
                    'Invalid saved operator continuation; retain checkpoint instead of silently restarting paid reasoning',
                )
        if not session or session.get('key') != key:
            migration = self.controller.s.get('operator_format_migration') or {}
            unchanged = (
                session
                and migration.get('session_key') == session.get('key')
                and (migration.get('role') == role)
                and (migration.get('phase') == self.controller.s['phase'])
                and (migration.get('edit_cursor') == self.controller.s.get('edit_cursor'))
                and (migration.get('repair_cursor') == self.controller.s.get('repair_cursor'))
                and (migration.get('candidate_hash') == digest(self.controller.s['candidate']))
                and (migration.get('binding') == self.controller.s.get('binding'))
            )
            if unchanged:
                session['instruction_migration'] = {
                    'previous_key': session['key'],
                    'execution_profile': python_backend.PROFILE,
                }
                session['key'] = key
                self.controller.s.pop('operator_format_migration', None)
            else:
                session = {
                    'key': key,
                    'role': role,
                    'history': [],
                    'cache': {},
                    'invalid_outputs': 0,
                    'retry_instruction': '',
                }
                self.controller.s['operator_session'] = session
        history = session['history']
        cache = session['cache']

        def cached_entry(index):
            seen = set()
            while True:
                if type(index) is not int or not 0 <= index < len(history) or index in seen:
                    raise RunStopped(
                        'operator_stop',
                        'Saved tool cache has a dangling or cyclic reference; no model request was dispatched',
                    )
                seen.add(index)
                entry = history[index]
                feedback = entry['feedback']
                if not isinstance(feedback, dict):
                    raise RunStopped(
                        'operator_stop', 'Saved tool cache has no complete feedback object'
                    )
                if feedback.get('status') != 'already_returned':
                    return entry
                index = feedback.get('source_tool_history_index')

        def tool_key(output):
            normalized = copy.deepcopy(output)
            for field in ('params_json', 'request_json'):
                if field in normalized:
                    try:
                        normalized[field] = json_value(normalized[field])
                    except (ValueError, TypeError):
                        pass
            return digest(normalized)

        for index in set(cache.values()):
            cached_entry(index)
            cache.setdefault(tool_key(history[index]['request']), index)
        recover_rejected_delivery(
            session,
            getattr(self.recorder, 'models', []),
            'smartrefactorer_' + role.lower(),
            self.evidence,
        )
        if session['invalid_outputs']:
            corrected = set(session.get('corrected_provider_state_calls', []))
            calls = []
            for prior in reversed(
                [
                    c
                    for c in getattr(self.recorder, 'models', [])
                    if c.get('scope') == 'smartrefactorer_' + role.lower()
                ]
            ):
                response = prior.get('response') or {}
                if not isinstance(response, dict) or response.get('status') not in (
                    'incomplete',
                    'failed',
                    'cancelled',
                    'queued',
                    'in_progress',
                ):
                    break
                if prior['call_id'] not in corrected:
                    calls.append(prior['call_id'])
            if calls:
                session['invalid_outputs'] = max(0, session['invalid_outputs'] - len(calls))
                if session['invalid_outputs'] == 0:
                    session['retry_instruction'] = ''
                session['corrected_provider_state_calls'] = sorted(corrected | set(calls))
                self.recorder.event(
                    'smartrefactorer.operator.retry_classification_corrected',
                    role=role,
                    call_ids=calls,
                )

        def append_history(entry):
            history.append(entry)
            self.persist()

        def delivered(value):
            session['result'] = copy.deepcopy(value)
            self.persist()
            return value

        def record_contract(value):
            specification = data.get('operator_record_schema')
            if specification:
                try:
                    Draft202012Validator(specification, format_checker=RECORD_FORMATS).validate(
                        value
                    )
                except ValidationError as error:
                    path = '/' + '/'.join(map(str, error.absolute_path))
                    cause = ': ' + str(error.cause) if error.cause else ''
                    raise ValueError(
                        'Operator record contract at ' + path + ': ' + error.message + cause
                    ) from error

        self.guard()
        if 'result' in session:
            try:
                record_contract(session['result'])
            except ValueError as error:
                invalid_record = session.pop('result')
                append_history(
                    {
                        'request': {'decision': 'record_contract_migration'},
                        'feedback': {
                            'status': 'error',
                            'record': invalid_record,
                            'error': {
                                'type': 'InvalidOperatorRecordContract',
                                'message': str(error),
                            },
                        },
                    }
                )
            else:
                return copy.deepcopy(session['result'])

        def component_binding(record):
            edit = data.get('edit') or {}
            component = data.get('component') or {}
            target = edit.get('target') or component.get('id')
            expected = edit.get('expected_hash') if edit else component.get('hash')
            if target and (
                record.get('target') != target or record.get('expected_hash') != expected
            ):
                raise ValueError(
                    'Component delivery does not match assigned target/source version'
                )

        guide = 'Return decision=respond with record_json containing the operator record, or use knowledge_query with an exact public method and params_json, or read_source with component_ids. Interpret published JSON schemas literally: a result schema {} permits any JSON value. Retain opaque results and inspect separately documented observations when effects need verification; do not demand an invented narrower result schema as a prerequisite to passing opaque data through. Operation completion never establishes the task goal. Use environment_query(request_json) for exact provider-declared read-only public observations needed during development; mutations and lifecycle changes are rejected before dispatch. Inspect actual result schemas through observations instead of waiting for a complete task implementation to observe the workspace. Knowledge queries are read-only and do not execute candidate skills. Read missing public documents or exact schemas rather than inventing contracts. Use read_context(id,path) to recall exact durable evidence from evidence_catalog; path is a JSON Pointer, empty for the complete object. Each evidence object contains request and feedback. Cite stable evidence IDs plus paths in operator records. tool_history and public.history positions are local to their invocation and must not be cited across roles. development_history contains exact prior algorithm records, including the original Plan. Read those records when revising requirements; do not substitute a requirement to recover the plan for the actual public task. Catalog entries prove availability, not their unseen contents. Read the exact evidence needed; do not invent a transcript or a schema from an index. Every model-accepted tool result for this operator remains fully supplied across later turns and resume. Provider-rejected delivery is represented explicitly as a delivery error with exact original references. It is not evicted by age or history length. Shared transport encoding may intern duplicates but includes their full values in the same prompt. Each valid read returns its complete requested result, including on an immutable cache hit. When several external evidence references are needed, use read_context_batch(items=[{id,path},...]) to retrieve them together. The runner enforces the cumulative model budget.'
        if role in ('CodeGen', 'Rewrite', 'Patch'):
            guide += ' Use write_component for actual code: source must be a string; interface_json/contract_json encode objects. If implementation is genuinely blocked, use component_blocked with the exact target/hash, conflict and evidence_refs_json list. Never deliver null source or pass a diagnostic as a replacement component. Keep the component small; supplied host protocol machinery is not a generated skill.'
        if data.get('operator_record_schema'):
            guide += ' The decoded record_json must satisfy operator_record_schema. Check sources are ordinary Python; narrative test plans are not executable checks.'
        guide += (
            ' Large unrequested input subtrees use evidence_index availability records. '
            'Their exact_value supplies an id and JSON Pointer path for read_context or read_context_batch. '
            'Extend that path to read listed fields or array indices. An index is not the unseen value. '
            'Model-accepted tool results in tool_history remain fully supplied. A provider-rejected delivery uses an explicit error view while its original feedback is retained.'
        )
        guide += DELIVERY_GUIDE
        presented_data = self.evidence.presentation(data)
        # Output protocol is mandatory input, not optional task evidence.
        if 'operator_record_schema' in data:
            presented_data['operator_record_schema'] = copy.deepcopy(
                data['operator_record_schema']
            )
        invalid_outputs = session['invalid_outputs']
        retry_instruction = session['retry_instruction']
        while True:
            session.update(invalid_outputs=invalid_outputs, retry_instruction=retry_instruction)
            self.persist()
            schema = obj({'turn': {'anyOf': choices}})
            while True:
                try:
                    output, meta = structured_call(
                        'SmartRefactorer '
                        + role
                        + '. '
                        + instructions
                        + '\n'
                        + guide
                        + retry_instruction,
                        {
                            **presented_data,
                            'development_history': self.evidence.presentation(
                                self.evidence.development_history
                            ),
                            'evidence_catalog': self.evidence.presentation(
                                [self.evidence.index[k] for k in sorted(self.evidence.index)]
                            ),
                            'tool_history': history_views(session, self.evidence),
                        },
                        schema,
                        'smartrefactorer_' + role.lower(),
                        max_output_tokens=(
                            CODE_OUTPUT_TOKENS
                            if role in ('CodeGen', 'Rewrite', 'Patch')
                            else 16000
                        ),
                    )
                    session['accepted_history_length'] = len(history)
                    break
                except RunStopped as error:
                    if (
                        error.reason != 'model_context_blocked'
                        or not recover_rejected_delivery(
                            session,
                            getattr(self.recorder, 'models', []),
                            'smartrefactorer_' + role.lower(),
                            self.evidence,
                        )
                    ):
                        raise
                    self.recorder.event(
                        'model.context_delivery_recovered',
                        scope='smartrefactorer_' + role.lower(),
                    )
                    self.persist()
                    continue
                except ModelOutputError as exc:
                    session['accepted_history_length'] = len(history)
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    self.recorder.event(
                        'smartrefactorer.operator.output_retry',
                        role=role,
                        response_id=getattr(exc, 'response_id', None),
                        attempt=invalid_outputs,
                        error={'type': type(exc).__name__, 'message': str(exc)},
                        environment_dispatched=False,
                    )
                    if invalid_outputs >= 3:
                        raise
                    retry_instruction = '\nThe previous response violated the required output format. Return ONLY one JSON object matching the supplied schema, with no preface, Markdown, or trailing prose. Repeat this operator response; no environment request from that malformed reply was executed.'
                    session['retry_instruction'] = retry_instruction
                    self.persist()
            self.requests.append(meta)
            output = output['turn']
            if output['decision'] == 'component_blocked':
                try:
                    refs = json_value(output['evidence_refs_json'])
                    if not isinstance(refs, list) or not output['conflict'].strip():
                        raise ValueError(
                            'Blocked delivery requires a nonempty reason and evidence reference list'
                        )
                    component_binding(output)
                    self.recorder.event(
                        'smartrefactorer.operator.evidence',
                        role=role,
                        history=history,
                        response_id=meta.get('response_id'),
                    )
                    return {
                        'target': output['target'],
                        'expected_hash': output['expected_hash'],
                        'conflict': output['conflict'],
                        'evidence_refs': refs,
                        'delivery_status': 'blocked',
                    }
                except (ValueError, TypeError) as exc:
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    self.recorder.event(
                        'smartrefactorer.operator.invalid_output',
                        role=role,
                        response_id=meta.get('response_id'),
                        error={'type': 'InvalidBlockedDelivery', 'message': str(exc)},
                    )
                    if invalid_outputs >= 3:
                        raise ModelOutputError(
                            'Invalid blocked delivery: ' + str(exc), meta.get('response_id')
                        ) from exc
                    append_history(
                        {
                            'request': {
                                'decision': 'component_blocked',
                                'response_id': meta.get('response_id'),
                            },
                            'feedback': {
                                'status': 'error',
                                'error': {
                                    'type': 'InvalidBlockedDelivery',
                                    'message': str(exc),
                                },
                            },
                        }
                    )
                    continue
            if output['decision'] == 'write_component':
                try:
                    interface = json_value(output['interface_json'])
                    contract = json_value(output['contract_json'])
                    if not isinstance(interface, dict) or not isinstance(contract, dict):
                        raise ValueError(
                            'Component interface and contract must be JSON objects'
                        )
                    if not isinstance(output['source'], str) or not output['source'].strip():
                        raise ValueError('Component source must contain actual text')
                    component_binding(output)
                    self.recorder.event(
                        'smartrefactorer.operator.evidence',
                        role=role,
                        history=history,
                        response_id=meta.get('response_id'),
                    )
                    return delivered(
                        {
                            'target': output['target'],
                            'expected_hash': output['expected_hash'],
                            'source': output['source'],
                            'interface': interface,
                            'contract': contract,
                        }
                    )
                except (ValueError, TypeError) as exc:
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    self.recorder.event(
                        'smartrefactorer.operator.invalid_output',
                        role=role,
                        response_id=meta.get('response_id'),
                        error={'type': 'InvalidComponentMetadata', 'message': str(exc)},
                    )
                    if invalid_outputs >= 3:
                        raise ModelOutputError(
                            'Invalid component metadata: ' + str(exc), meta.get('response_id')
                        ) from exc
                    append_history(
                        {
                            'request': {
                                'decision': 'write_component',
                                'response_id': meta.get('response_id'),
                            },
                            'feedback': {
                                'status': 'error',
                                'error': {
                                    'type': 'InvalidComponentMetadata',
                                    'message': str(exc),
                                },
                            },
                        }
                    )
                    continue
            if output['decision'] == 'respond':
                try:
                    value = json_value(output['record_json'])
                    if not isinstance(value, dict):
                        raise ValueError('Operator record must be an object')
                    record_contract(value)
                    if role in ('CodeGen', 'Rewrite', 'Patch'):
                        component_binding(value)
                        if value.get('conflict'):
                            value = {**value, 'delivery_status': 'blocked'}
                        elif (
                            not {'target', 'expected_hash', 'source', 'interface', 'contract'}
                            <= set(value)
                            or not isinstance(value['target'], str)
                            or (not value['target'])
                            or (not isinstance(value['source'], str))
                            or (not value['source'].strip())
                            or (not isinstance(value['interface'], dict))
                            or (not isinstance(value['contract'], dict))
                        ):
                            raise ValueError(
                                'A component delivery requires actual source text and object interface/contract; diagnostics must use component_blocked'
                            )
                    self.recorder.event(
                        'smartrefactorer.operator.evidence',
                        role=role,
                        history=history,
                        response_id=meta.get('response_id'),
                    )
                    return delivered(value)
                except (ValueError, TypeError) as exc:
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    self.recorder.event(
                        'smartrefactorer.operator.invalid_output',
                        role=role,
                        response_id=meta.get('response_id'),
                        error={'type': 'InvalidOperatorRecord', 'message': str(exc)},
                    )
                    if invalid_outputs >= 3:
                        raise ModelOutputError(
                            'Invalid operator record: ' + str(exc), meta.get('response_id')
                        ) from exc
                    append_history(
                        {
                            'request': {
                                'decision': 'respond',
                                'response_id': meta.get('response_id'),
                            },
                            'feedback': {
                                'status': 'error',
                                'error': {'type': 'InvalidOperatorRecord', 'message': str(exc)},
                            },
                        }
                    )
                    continue
            key = tool_key(output)
            identifier = None
            cache_source = None
            if key in cache:
                original = cached_entry(cache[key])
                feedback = copy.deepcopy(original['feedback'])
                identifier = original.get('evidence_id')
                cache_source = cache[key]
            elif output['decision'] == 'read_context_batch':
                items = []
                for ref in output['items']:
                    try:
                        item = {
                            **ref,
                            'status': 'completed',
                            'result': self.evidence.contexts.read(ref['id'], ref['path']),
                        }
                    except (ValueError, KeyError, IndexError) as exc:
                        item = {
                            **ref,
                            'status': 'error',
                            'error': {'type': type(exc).__name__, 'message': str(exc)},
                        }
                    except OSError as exc:
                        raise RunStopped(
                            'operator_stop',
                            'Exact context batch could not be read; preserve operator state before another model call',
                        ) from exc
                    items.append(item)
                feedback = {'status': 'completed', 'items': items}
                if all((item['status'] == 'completed' for item in items)):
                    cache[key] = len(history)
            elif output['decision'] == 'read_context':
                try:
                    feedback = {
                        'status': 'completed',
                        'result': self.evidence.contexts.read(output['id'], output['path']),
                    }
                except (ValueError, KeyError, IndexError) as exc:
                    feedback = {
                        'status': 'error',
                        'error': {'type': type(exc).__name__, 'message': str(exc)},
                    }
                except OSError as exc:
                    raise RunStopped(
                        'operator_stop',
                        'Exact context could not be read; preserve operator state before another model call',
                    ) from exc
                if feedback['status'] == 'completed':
                    cache[key] = len(history)
            elif output['decision'] == 'read_source':
                feedback = {
                    'status': 'completed',
                    'result': {
                        n: self.controller.s['candidate'][n]
                        for n in output['component_ids']
                        if n in self.controller.s['candidate']
                    },
                    'missing': [
                        n
                        for n in output['component_ids']
                        if n not in self.controller.s['candidate']
                    ],
                }
                cache[key] = len(history)
            else:
                try:
                    if output['decision'] == 'environment_query':
                        request = json_value(output['request_json'])
                        if not isinstance(request, dict):
                            raise ValueError('Public request must be an object')
                        request.setdefault('protocol_version', '1.0')
                        request.setdefault('request_id', str(uuid.uuid4()))
                    else:
                        params = json_value(output['params_json'])
                        if not isinstance(params, dict):
                            raise ValueError('Public query params must be an object')
                        request = self.environment.knowledge_request(
                            output['method'], params, str(uuid.uuid4())
                        )
                except ValueError as exc:
                    self.recorder.event(
                        'smartrefactorer.operator.invalid_output',
                        role=role,
                        response_id=meta.get('response_id'),
                        error={'type': 'InvalidQueryJSON', 'message': str(exc)},
                    )
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    if invalid_outputs >= 3:
                        raise ModelOutputError(
                            'Repeated invalid development query JSON', meta.get('response_id')
                        ) from exc
                    append_history(
                        {
                            'request': output,
                            'feedback': {
                                'status': 'error',
                                'error': {'type': 'InvalidQueryJSON', 'message': str(exc)},
                            },
                        }
                    )
                    continue
                if not self.environment.readonly(request):
                    invalid_outputs += 1
                    session['invalid_outputs'] = invalid_outputs
                    self.persist()
                    if invalid_outputs >= 3:
                        raise ModelOutputError(
                            'Repeated development requests outside read-only capability',
                            meta.get('response_id'),
                        )
                    append_history(
                        {
                            'request': output,
                            'feedback': {
                                'status': 'error',
                                'execution': {'state': 'not_started'},
                                'error': {
                                    'type': 'ReadOnlyDevelopmentQueryRequired',
                                    'message': 'Development tools permit only provider-declared read-only requests',
                                },
                            },
                        }
                    )
                    continue

                def invoke(req):
                    result = self.environment.handle(req)
                    return (
                        self.environment.raw_feedback(req['request_id'], request=req) or result
                        if hasattr(self.environment, 'raw_feedback')
                        else result
                    )

                feedback, call = self.recorder.observe_env(request, invoke, stage='knowledge')
                identifier = self.evidence.remember_evidence(request, feedback)
                self.evidence.save_index()
                if (
                    feedback.get('status') == 'completed'
                    and request.get('op') == 'environment.document'
                    and request.get('params', {}).get('sha256')
                ):
                    cache[key] = len(history)
            entry = {'request': copy.deepcopy(output), 'feedback': copy.deepcopy(feedback)}
            if (
                output['decision'] in ('knowledge_query', 'environment_query')
                and identifier is not None
            ):
                entry['evidence_id'] = identifier
            elif output['decision'] == 'read_context':
                entry['evidence_id'] = output['id']
            if cache_source is not None:
                entry['cache_source_tool_history_index'] = cache_source
            append_history(entry)

    def output_blocked(self, exc):
        """Pause malformed operator output without sending a fake code failure to Reflect."""
        self.controller.s['output_failure'] = {
            'type': type(exc).__name__,
            'message': str(exc),
            'response_id': getattr(exc, 'response_id', None),
        }
        self.recorder.event(
            'smartrefactorer.operator.invalid_output', **self.controller.s['output_failure']
        )
        self.persist()
        raise RunStopped(
            'operator_stop',
            'Repeated malformed model output; preserve the exact operator phase/proposal/cursor for diagnosis instead of Reflect/replanning',
        ) from exc
