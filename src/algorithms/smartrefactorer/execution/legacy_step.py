"""Historical step-profile compatibility, not the language for new skills.

Every skill defines step(state, feedback, args). It returns a JSON transition:
{state, request}, {state, call, args}, or {state, done:true, result}.
All side effects belong to yielded public requests. No external world is replayed.
The AST allowlist is the execution language, not a general Python sandbox.
"""

import ast
import copy
import json
import operator

BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
BUILTINS = {
    'len': len,
    'str': str,
    'int': int,
    'float': float,
    'bool': bool,
    'min': min,
    'max': max,
    'sum': sum,
    'sorted': sorted,
    'list': list,
    'dict': dict,
    'abs': abs,
    'round': round,
}
ALLOWED = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.If,
    ast.Expr,
    ast.Pass,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Dict,
    ast.List,
    ast.Tuple,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.keyword,
    ast.Attribute,
    *BIN,
    *CMP,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.And,
    ast.Or,
)


def parse(source):
    try:
        tree = ast.parse(source)
    except RecursionError as exc:
        raise ValueError('Source AST nesting exceeds the language limit') from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError('A component must define exactly one step function')
    fn = tree.body[0]
    if (
        fn.name != 'step'
        or [a.arg for a in fn.args.args] != ['state', 'feedback', 'args']
        or fn.decorator_list
        or fn.args.defaults
        or fn.args.kwonlyargs
        or fn.args.vararg
        or fn.args.kwarg
        or fn.args.posonlyargs
    ):
        raise ValueError('Required explicit signature: step(state, feedback, args)')
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED):
            raise ValueError('Unsupported language construct: ' + type(node).__name__)
        if isinstance(node, ast.FunctionDef) and node is not fn:
            raise ValueError('Nested functions are unavailable')
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
            raise ValueError('Dictionary unpacking is unavailable')
        if isinstance(node, ast.Assign) and any(
            not isinstance(t, (ast.Name, ast.Subscript)) for t in node.targets
        ):
            raise ValueError('Assignment requires a name or indexed target')
        if isinstance(node, ast.Name) and node.id.startswith('_'):
            raise ValueError('Private names are unavailable')
        if isinstance(node, ast.Attribute) and node.attr != 'get':
            raise ValueError('Only dictionary.get is supported')
        if isinstance(node, ast.Call):
            if any(k.arg is None for k in node.keywords):
                raise ValueError('Dynamic keyword expansion is unavailable')
            if isinstance(node.func, ast.Name) and node.func.id not in BUILTINS:
                raise ValueError('Unknown pure function')
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                raise ValueError('Dynamic calls are unavailable')
    return fn


def step(source, state, feedback, args):
    fn = parse(source)
    scope = copy.deepcopy({'state': state, 'feedback': feedback, 'args': args})
    fuel = [10000]

    def expr(n):
        fuel[0] -= 1
        if fuel[0] < 0:
            raise ValueError('Pure step expression limit exceeded')
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return scope[n.id]
        if isinstance(n, ast.Dict):
            return {expr(k): expr(v) for k, v in zip(n.keys, n.values)}
        if isinstance(n, (ast.List, ast.Tuple)):
            return [expr(v) for v in n.elts]
        if isinstance(n, ast.Subscript):
            return expr(n.value)[expr(n.slice)]
        if isinstance(n, ast.Slice):
            return slice(*(expr(v) if v else None for v in (n.lower, n.upper, n.step)))
        if isinstance(n, ast.BinOp):
            a, b = expr(n.left), expr(n.right)
            if isinstance(n.op, ast.Mult):
                sequence, count = (a, b) if isinstance(a, (str, list)) else (b, a)
                if isinstance(sequence, (str, list)) and (
                    not isinstance(count, int) or max(0, count) * len(sequence) > 100000
                ):
                    raise ValueError('Step value too large')
            if (
                isinstance(n.op, ast.Add)
                and isinstance(a, (str, list))
                and isinstance(b, type(a))
                and len(a) + len(b) > 1000000
            ):
                raise ValueError('Sequence construction exceeds step allocation limit')
            return BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp):
            return {ast.Not: operator.not_, ast.USub: operator.neg, ast.UAdd: operator.pos}[
                type(n.op)
            ](expr(n.operand))
        if isinstance(n, ast.IfExp):
            return expr(n.body if expr(n.test) else n.orelse)
        if isinstance(n, ast.BoolOp):
            value = expr(n.values[0])
            for v in n.values[1:]:
                if (
                    isinstance(n.op, ast.And)
                    and not value
                    or isinstance(n.op, ast.Or)
                    and value
                ):
                    return value
                value = expr(v)
            return value
        if isinstance(n, ast.Compare):
            a = expr(n.left)
            for op, bnode in zip(n.ops, n.comparators):
                b = expr(bnode)
                if not CMP[type(op)](a, b):
                    return False
                a = b
            return True
        if isinstance(n, ast.Call):
            values = [expr(v) for v in n.args]
            kwargs = {v.arg: expr(v.value) for v in n.keywords}
            if isinstance(n.func, ast.Name):
                return BUILTINS[n.func.id](*values, **kwargs)
            target = expr(n.func.value)
            if not isinstance(target, dict):
                raise ValueError('get receiver must be a dictionary')
            return target.get(*values, **kwargs)
        raise ValueError('Unsupported expression')

    def statements(nodes):
        for n in nodes:
            if isinstance(n, ast.Return):
                return True, expr(n.value)
            if isinstance(n, ast.Assign):
                value = expr(n.value)
                for target in n.targets:
                    if isinstance(target, ast.Name):
                        scope[target.id] = copy.deepcopy(value)
                    elif isinstance(target, ast.Subscript):
                        expr(target.value)[expr(target.slice)] = copy.deepcopy(value)
                    else:
                        raise ValueError('Unsupported assignment target')
            elif isinstance(n, ast.If):
                found, value = statements(n.body if expr(n.test) else n.orelse)
                if found:
                    return found, value
            elif isinstance(n, ast.Expr):
                expr(n.value)
            elif not isinstance(n, ast.Pass):
                raise ValueError('Unsupported statement')
        return False, None

    _, result = statements(fn.body)
    return validate_transition(result)


def validate_transition(result):
    """Check the same wire contract for execution and proposed exact fixtures."""
    if (
        not isinstance(result, dict)
        or 'state' not in result
        or sum(k in result for k in ('request', 'call', 'done')) != 1
    ):
        raise ValueError('Step must return one explicit transition and state')
    if 'done' in result and result['done'] is not True:
        raise ValueError('done must be true')
    if 'request' in result:
        request = result['request']
        if (
            not isinstance(request, dict)
            or not isinstance(request.get('op'), str)
            or not isinstance(request.get('params'), dict)
        ):
            raise ValueError('Request transition requires an operation and parameter object')
        if 'protocol_version' in request and request['protocol_version'] != '1.0':
            raise ValueError('Unsupported request protocol')
        if 'request_id' in request:
            import re

            if not isinstance(request['request_id'], str) or not re.fullmatch(
                r'[A-Za-z0-9_.:-]{1,128}', request['request_id']
            ):
                raise ValueError('Invalid explicit request identity')
    if 'call' in result and not isinstance(result['call'], str):
        raise ValueError('Helper target must be a component ID')
    encoded = json.dumps(result, allow_nan=False)
    if len(encoded) > 1000000:
        raise ValueError('Step result too large')
    if json.loads(encoded) != result:
        raise ValueError('Step transitions require JSON values and string object keys')
    return result


def references(source):
    fn = parse(source)
    calls = set()
    dynamic = False
    nodes = list(ast.walk(fn))

    def key(n):
        if isinstance(n, ast.Name):
            return ('name', n.id)
        if isinstance(n, ast.Subscript):
            return ('subscript', key(n.value), ast.dump(n.slice, include_attributes=False))
        return ('expression', ast.dump(n, include_attributes=False))

    pending = [n.value for n in nodes if isinstance(n, ast.Return) and n.value is not None]
    seen = set()
    assignments = [n for n in nodes if isinstance(n, ast.Assign)]

    def record(value):
        nonlocal dynamic
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            calls.add(value.value)
        else:
            dynamic = True

    while pending:
        n = pending.pop()
        identity = key(n)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(n, ast.IfExp):
            pending.extend([n.body, n.orelse])
        elif isinstance(n, ast.Dict):
            values = []
            for k, v in zip(n.keys, n.values):
                if not isinstance(k, ast.Constant):
                    dynamic = True
                elif k.value == 'call':
                    values.append(v)
            if values:
                record(values[-1])
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'dict':
            values = [k.value for k in n.keywords if k.arg == 'call']
            if values:
                record(values[-1])
            elif len(n.args) == 1:
                pending.append(n.args[0])
            elif n.args:
                dynamic = True
        elif isinstance(n, (ast.List, ast.Tuple)):
            values = []
            for pair in n.elts:
                if not isinstance(pair, (ast.List, ast.Tuple)) or len(pair.elts) != 2:
                    dynamic = True
                    continue
                k, v = pair.elts
                if not isinstance(k, ast.Constant):
                    dynamic = True
                elif k.value == 'call':
                    values.append(v)
            if values:
                record(values[-1])
        elif isinstance(n, ast.Call):
            dynamic = True
        if isinstance(n, (ast.Name, ast.Subscript)):
            resolved = False
            for assignment in assignments:
                for target in assignment.targets:
                    if key(target) == identity:
                        pending.append(assignment.value)
                        resolved = True
                    if (
                        isinstance(target, ast.Subscript)
                        and key(target.value) == identity
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == 'call'
                    ):
                        record(assignment.value)
                        resolved = True
            if not resolved:
                dynamic = True
    return {'calls': sorted(calls), 'unresolved_dynamic_calls': dynamic}
