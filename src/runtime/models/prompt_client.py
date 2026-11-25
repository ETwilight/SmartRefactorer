"""Let structured model turns read skill definitions, traces, and environment knowledge."""

import copy
import json
import uuid
from jsonschema import Draft202012Validator
from runtime.contracts import obj


class PromptClient:
    """Add evidence-reading turns to a method adapter's structured model client.

    Subclasses supply ask(), the environment revision check, knowledge method
    discovery and request construction, a knowledge_handler, and a recorder.
    read_library() and read_trace() serve method evidence; an optional
    read_context() enables content-ID and JSON Pointer reads. knowledge_calls
    retains copies of knowledge-query call records for this client.

    ask_with_knowledge() owns a local tool history, immutable-read cache, and
    per-address feedback signatures. Successful immutable reads can reuse an
    earlier history entry. Dynamic queries execute again. After two consecutive
    results add no information, the next turn permits only the typed response.

    Example, on an adapter implementing these hooks:
        result = client.ask_with_knowledge(instructions, data, schema, scope='plan')

    The model may request evidence before responding. result is the response
    validated against schema; control then returns to the calling algorithm.
    """

    knowledge_guide = 'Read the environment discovery for public knowledge interfaces.\n'
    environment_guide = 'Environment-owned guidance and observed feedback define task rules.\n'

    def __init__(self):
        """Initialize the recorded knowledge-query call list."""

        self.knowledge_calls = []

    def query_knowledge(self, method, params, request_id=None):
        """Check the source revision, execute a public knowledge request, and retain its call
        record.
        """

        self.check_environment_revision()
        if method not in self.knowledge_methods():
            raise ValueError('Unknown public knowledge method: ' + method)
        request = self.knowledge_request(method, params, request_id or str(uuid.uuid4()))
        feedback, call = self.recorder.observe_env(
            request, self.knowledge_handler, stage='knowledge'
        )
        self.knowledge_calls.append(copy.deepcopy(call))
        return copy.deepcopy(feedback)

    def ask_with_knowledge(self, instructions, data, schema, scope, max_queries=4, max_reads=4):
        """Serve evidence turns until the model returns a schema-valid response.

        Count queries and uncached reads against their separate allowances. Store
        duplicate results as references to earlier history entries. Return the
        validated respond.response value, or raise if the turn allowance ends.
        """
        history = []
        queries = 0
        reads = 0
        # read_cache: dict[tuple, int], mapping immutable read addresses to history positions.
        read_cache = {}
        # seen_feedback: dict[tuple, int], mapping (address, feedback signature) to its first
        # history position.
        seen_feedback = {}
        # last_feedback: dict[tuple, str], retaining the latest feedback signature at each
        # address.
        last_feedback = {}
        no_progress = 0
        duplicate_history = []
        choices = [
            obj({'decision': {'enum': ['respond']}, 'response': schema}),
            obj(
                {
                    'decision': {'enum': ['read_skills']},
                    'version': {'type': 'integer', 'minimum': 0},
                    'names': {'type': 'array', 'items': {'type': 'string'}},
                }
            ),
            obj(
                {
                    'decision': {'enum': ['read_trace']},
                    'round': {'type': 'integer', 'minimum': 1},
                }
            ),
        ]
        if hasattr(self, 'read_context'):
            choices.append(
                obj(
                    {
                        'decision': {'enum': ['read_context']},
                        'id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                        'path': {'type': 'string'},
                    }
                )
            )
        methods = sorted(self.knowledge_methods())
        if methods:
            choices.append(
                obj(
                    {
                        'decision': {'enum': ['knowledge_query']},
                        'method': {'enum': methods},
                        'params_json': {'type': 'string'},
                        'request_id': {'type': ['string', 'null']},
                    }
                )
            )
        turns = obj({'turn': {'anyOf': choices}})
        guide = (
            'Return a native turn object. respond.response is the final typed result. '
            'knowledge_query uses method, params_json and request_id (null creates a fresh ID). '
            'read_skills reads exact definitions by version and names ([] means all). '
            'read_trace reads an observed round. Reads never execute skills. '
            'already_returned feedback points to an earlier tool_history index whose exact feedback remains available. '
            'Do not repeat a read or query for information already supplied; request new information or respond. '
            'Respect queries_remaining and reads_remaining, then respond.\n'
        )
        if hasattr(self, 'read_context'):
            guide += (
                'exact_refs_v1 payloads replace duplicates with $context_ref; definitions are in context_objects. '
                '$json_text is parsed JSON message content; $source_text recalls its exact original string. '
                'read_context takes id and path (empty for full, otherwise JSON Pointer). '
                'original_context_ref recalls the complete original request data. '
                'null remaining limits mean no per-call read/query cap; the run budget still applies.\n'
            )
        limit = (
            None if max_queries is None or max_reads is None else max_queries + max_reads + 1
        )
        iteration = 0
        while limit is None or iteration < limit:
            iteration += 1
            response_only = no_progress >= 2
            if response_only:
                event = getattr(getattr(self, 'recorder', None), 'event', None)
                if event is not None:
                    event(
                        'method.evidence_loop_fallback',
                        reason='consecutive_no_new_information',
                        scope=scope,
                        history_indices=list(duplicate_history),
                        duplicate_decisions=[
                            history[index]['request']['decision'] for index in duplicate_history
                        ],
                        response_only=True,
                    )
            completion_guide = (
                (
                    'Two consecutive evidence results added no new information. '
                    'Use the exact evidence already supplied and return the original typed result now. '
                    'This turn only permits respond; control then returns to the native caller.\n'
                )
                if response_only
                else ''
            )
            turn_schema = obj({'turn': choices[0]}) if response_only else turns
            answer = self.ask(
                instructions
                + self.knowledge_guide
                + self.environment_guide
                + guide
                + completion_guide,
                {
                    **data,
                    'tool_history': history,
                    'queries_remaining': None if max_queries is None else max_queries - queries,
                    'reads_remaining': None if max_reads is None else max_reads - reads,
                    **({'completion_requirement': completion_guide} if response_only else {}),
                },
                turn_schema,
                scope,
            )['turn']
            if answer['decision'] == 'respond':
                Draft202012Validator(schema).validate(answer['response'])
                return answer['response']
            if response_only:
                raise ValueError('Response-only evidence completion did not return respond')
            cache_key = None
            cached = False
            no_information = False
            # Use the turn itself as the initial evidence address, including parsing errors.
            # A parsed query or read replaces this key with its specific address.
            progress_key = (
                'turn',
                json.dumps(
                    {k: v for k, v in answer.items() if k != 'request_id'},
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                ),
            )
            try:
                if answer['decision'] == 'knowledge_query':
                    # params: the JSON value decoded from params_json, usually a query parameter
                    # dictionary.
                    params = json.loads(answer['params_json'])
                    progress_key = (
                        'query',
                        answer['method'],
                        json.dumps(
                            params, sort_keys=True, separators=(',', ':'), allow_nan=False
                        ),
                    )
                    if max_queries is not None and queries >= max_queries:
                        raise ValueError('Knowledge query allowance exhausted')
                    queries += 1
                    # Cache a document read when its parameters pin a content hash.
                    # Status and search queries execute on every request.
                    if (
                        answer['method'] == 'environment.document'
                        and isinstance(params, dict)
                        and params.get('sha256')
                    ):
                        cache_key = (
                            'document',
                            json.dumps(
                                params, sort_keys=True, separators=(',', ':'), allow_nan=False
                            ),
                        )
                    if cache_key in read_cache:
                        cached = True
                        no_information = True
                        # source: int, the history position supplying the cached feedback.
                        source = read_cache[cache_key]
                    else:
                        feedback = self.query_knowledge(
                            answer['method'], params, answer['request_id']
                        )
                else:
                    if answer['decision'] == 'read_context':
                        cache_key = ('context', answer['id'], answer['path'])
                    elif answer['decision'] == 'read_skills':
                        cache_key = (
                            'skills',
                            answer['version'],
                            tuple(sorted(set(answer['names']))),
                        )
                    else:
                        cache_key = ('trace', answer['round'])
                    progress_key = cache_key
                    if cache_key in read_cache:
                        cached = True
                        no_information = True
                        # source: int, the history position supplying the cached feedback.
                        source = read_cache[cache_key]
                    else:
                        if max_reads is not None and reads >= max_reads:
                            raise ValueError('Evidence read allowance exhausted')
                        reads += 1
                        if answer['decision'] == 'read_context':
                            result = self.read_context(answer['id'], answer['path'])
                        else:
                            result = (
                                self.read_library(answer['version'], answer['names'])
                                if answer['decision'] == 'read_skills'
                                else self.read_trace(answer['round'])
                            )
                        feedback = {'status': 'completed', 'result': result}
            except Exception as exc:
                # Propagate source revision changes to the runner.
                if type(exc).__name__.endswith('RevisionChanged'):
                    raise
                feedback = {
                    'status': 'error',
                    'error': {'type': type(exc).__name__, 'message': str(exc)},
                }
            if not cached:
                # The outer request ID varies between otherwise identical feedback records.
                # All result, status, and error fields remain part of the signature.
                signature = json.dumps(
                    {k: v for k, v in feedback.items() if k != 'request_id'},
                    sort_keys=True,
                    separators=(',', ':'),
                    allow_nan=False,
                )
                # Compare each address with its last result to detect consecutive duplicates.
                # A new address counts as progress even when another address returned the same
                # value.
                # A dynamic result changing from A to B and back to A also counts as progress.
                if progress_key is not None:
                    no_information = last_feedback.get(progress_key) == signature
                    last_feedback[progress_key] = signature
                evidence_key = (progress_key, signature)
                if evidence_key in seen_feedback:
                    source = seen_feedback[evidence_key]
                    cached = True
                else:
                    seen_feedback[evidence_key] = len(history)
                if cache_key is not None and feedback.get('status') == 'completed':
                    read_cache[cache_key] = source if cached else len(history)
            if cached:
                feedback = {
                    'status': 'already_returned',
                    'source_tool_history_index': source,
                    'path': '/feedback',
                    'instruction': 'Exact feedback is still at the referenced tool_history index. Request new information or respond.',
                }
            if no_information:
                no_progress += 1
                duplicate_history.append(len(history))
            else:
                no_progress = 0
                duplicate_history = []
            history.append({'request': answer, 'feedback': feedback})
        raise ValueError('Typed model response missing')
