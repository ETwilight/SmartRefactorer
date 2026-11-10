"""One entry point for isolated method plugins and capacity-aware environment providers."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from runtime.layout import RESULTS_ROOT, RunLayout
from .registry import METHODS, ENVIRONMENTS, resolve
from .scheduler import Scheduler, write

RESEARCH = Path(__file__).resolve().parents[3]


def interpreter(method):
    if method != 'smartrefactorer':
        raise ValueError('This entry point runs SmartRefactorer only')
    # Run with the explicitly selected dependency environment, not another
    # algorithm's directory. Worker preflight checks its required packages.
    path = Path(sys.executable).resolve()
    if not path.is_file():
        raise ValueError('Method interpreter is missing: ' + str(path))
    return path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='action', required=True)
    for action in ('run', 'batch'):
        command = sub.add_parser(action)
        if action == 'run':
            command.add_argument('--method', choices=sorted(METHODS), required=True)
        else:
            command.add_argument(
                '--methods', nargs='+', choices=sorted(METHODS), default=list(METHODS)
            )
        command.add_argument('--environment', choices=sorted(ENVIRONMENTS), default='techmc')
        command.add_argument(
            '--goal',
            default='Complete tasks from the public task catalogue using the supplied budget and observed evidence.',
        )
        command.add_argument('--max-rounds', type=int, default=50)
        command.add_argument('--token-budget', type=int, default=50_000_000)
        command.add_argument(
            '--round-token-budget',
            type=int,
            help='Explicit trial-only per-round input+output cap; default unlimited',
        )
        command.add_argument('--output-root', type=Path, default=RESULTS_ROOT)
        command.add_argument('--ports', type=int, nargs='+')
        command.add_argument('--heap-mb', type=int, default=2048)
        command.add_argument('--actor')
        command.add_argument('--allow-execution-agent', action='store_true')
        command.add_argument('--world-type', choices=['void', 'natural'], default='void')
        command.add_argument('--seed', type=int, default=0)
        command.add_argument('--sleep-every', type=int, default=10)
        command.add_argument('--max-actor-calls', type=int, default=100)
        command.add_argument('--max-sleep-calls', type=int, default=100)
        command.add_argument('--max-task-attempts', type=int)
        command.add_argument(
            '--task-sequence',
            choices=['curriculum', 'interleaved', 'family_return'],
            help='TaskStream ordering; default curriculum',
        )
        command.add_argument(
            '--task-sequence-file',
            type=Path,
            help='JSON array of task IDs; repeats revisit that task in its retained world',
        )
        command.add_argument('--max-parallel', type=int, default=4)
        command.add_argument('--poll-seconds', type=float, default=5)
        command.add_argument('--library-dir', type=Path)
        command.add_argument(
            '--resume-from',
            type=Path,
            help='Continue a stopped SmartRefactorer run with its saved world, method state and original limits',
        )
        command.add_argument(
            '--resume-recovery-receipt',
            type=Path,
            help='Environment-verified receipt for recovery of one exact saved continuation',
        )
        command.add_argument('--bge-model-dir', type=Path)
        command.add_argument('--bge-revision')
        command.add_argument('--bge-device', choices=['cpu', 'cuda:0'], default='cuda:0')
        command.add_argument(
            '--bge-precision', choices=['float32', 'float16'], default='float32'
        )
        command.add_argument(
            '--check',
            action='store_true',
            help='Validate configuration and dependencies only; do not allocate or call models',
        )
    status = sub.add_parser('status')
    status.add_argument('batch_dir', type=Path)
    return result


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.action == 'status':
        print((args.batch_dir / 'queue.json').read_text(encoding='utf-8'))
        return 0
    if (
        args.max_rounds < 1
        or args.token_budget < 16
        or (args.round_token_budget is not None and args.round_token_budget < 16)
    ):
        argument_parser.error(
            'Round count and budgets must be positive; null means no round token cap'
        )
    if args.max_parallel < 1 or args.poll_seconds <= 0:
        argument_parser.error('Invalid queue settings')
    methods = [args.method] if args.action == 'run' else args.methods
    if len(set(methods)) != len(methods):
        argument_parser.error('Each method may appear only once per batch')
    resume_launch = None
    if args.resume_recovery_receipt and not args.resume_from:
        argument_parser.error('--resume-recovery-receipt requires --resume-from')
    if args.resume_from:
        if args.action != 'run' or methods != ['smartrefactorer']:
            argument_parser.error('--resume-from supports one SmartRefactorer run only')
        try:
            source = args.resume_from.resolve()
            resume_launch = json.loads((source / 'launch.json').read_text(encoding='utf-8'))
            source_report = json.loads((source / 'report.json').read_text(encoding='utf-8'))
            if resume_launch.get('method') != methods[0] or source_report.get(
                'workflow'
            ) not in ('smartrefactorer', 'smartrefactorer_compare'):
                raise ValueError('The source does not belong to the selected method')
            from runtime.continuation.safety import clean_discovery_only_failure

            if source_report.get('status') not in (
                'operator_stop',
                'model_context_blocked',
                'source_revision_changed',
                'runtime_revision_changed',
            ) and not clean_discovery_only_failure(source, source_report):
                raise ValueError(
                    'Explicit continuation requires a clean stopped SmartRefactorer source'
                )
            from .resume_accounting import validate_resume_accounting

            validate_resume_accounting(source, source_report)
            args.token_budget = source_report['limits']['total_tokens']
            args.max_rounds = source_report['limits']['max_rounds']
            args.round_token_budget = source_report['limits']['round_tokens']
            args.goal = source_report['goal']
            args.environment = resume_launch['environment']
            args.world_type = resume_launch['world_type']
            args.heap_mb = resume_launch['heap_mb']
            source_port = resume_launch['port']
            if args.ports is not None and args.ports != [source_port]:
                raise ValueError('Continuation must retain the original server port')
            args.ports = [source_port]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            argument_parser.error('Cannot continue the source: ' + str(exc))
    task_ids = None
    if args.task_sequence_file:
        try:
            task_ids = json.loads(args.task_sequence_file.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            argument_parser.error('Cannot read task sequence: ' + str(exc))
        if (
            not isinstance(task_ids, list)
            or not task_ids
            or any(not isinstance(x, str) or not x for x in task_ids)
        ):
            argument_parser.error('Task sequence must be a nonempty JSON array of task IDs')
    batch = args.output_root / 'batches' / str(uuid.uuid4())
    jobs = []
    for method in methods:
        identifier = str(uuid.uuid4())
        layout = RunLayout(args.output_root / 'runs' / identifier).prepare()
        options = {
            k: str(v.resolve()) if isinstance(v, Path) else v
            for k, v in {
                'library_dir': args.library_dir,
                'bge_model_dir': args.bge_model_dir,
                'bge_revision': args.bge_revision,
                'bge_device': args.bge_device,
                'bge_precision': args.bge_precision,
                'seed': args.seed,
                'sleep_every': args.sleep_every,
                'max_actor_calls': args.max_actor_calls,
                'max_sleep_calls': args.max_sleep_calls,
                'max_task_attempts': args.max_task_attempts,
            }.items()
            if v is not None
        }
        if resume_launch is not None:
            options = dict(resume_launch.get('options', {}))
            # Startup recovery receipts authenticate one exact parent checkpoint;
            # they cannot be inherited by a later, successfully resumed segment.
            for key in list(options):
                if key.endswith(('_resume_from', '_resume_recovery_receipt')):
                    options.pop(key)
            options[method + '_resume_from'] = str(source)
            if args.resume_recovery_receipt:
                options[method + '_resume_recovery_receipt'] = str(
                    args.resume_recovery_receipt.resolve()
                )
        config = {
            'method': method,
            'environment': args.environment,
            'goal': args.goal,
            'run_dir': str(layout.root.resolve()),
            'max_rounds': args.max_rounds,
            'token_budget': args.token_budget,
            'round_token_budget': args.round_token_budget,
            'heap_mb': args.heap_mb,
            'options': options,
        }
        if resume_launch is not None:
            config['port'] = source_port
        options['task_sequence'] = options.get(
            'task_sequence', args.task_sequence or 'curriculum'
        )
        if task_ids is not None:
            options['task_ids'] = list(task_ids)
        config['task_worlds'] = True
        config['environment_options'] = {}
        if args.actor:
            config['environment_options']['actor'] = args.actor
        if args.allow_execution_agent:
            config['environment_options']['allow_execution_agent'] = True
        config['world_type'] = args.world_type
        config['controller_mb'] = 4096
        config_path = layout.root / 'launch.json'
        write(config_path, config)
        # Dependency checks use each method's own interpreter and consume zero model tokens.
        check = subprocess.run(
            [
                str(interpreter(method)),
                '-X',
                'utf8',
                '-m',
                'adapters.methods.smartrefactorer.worker',
                '--config',
                str(config_path.resolve()),
                '--check',
            ],
            cwd=RESEARCH,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        if check.returncode:
            write(layout.root / 'preflight-error.json', {'error': check.stderr[-6000:]})
            raise RuntimeError(method + ' preflight failed: ' + check.stderr[-2000:])
        jobs.append(
            {
                'id': identifier,
                'method': method,
                'status': 'queued',
                'python': str(interpreter(method)),
                'run_dir': str(layout.root.resolve()),
                'configuration': config,
                'queued_ns': time.time_ns(),
            }
        )
    write(
        batch / 'queue.json',
        {'jobs': jobs, 'status': 'validated_only' if args.check else 'queued'},
    )
    print(
        json.dumps(
            {
                'batch_dir': str(batch),
                'methods': methods,
                'status': 'validated_only' if args.check else 'queued',
            }
        ),
        flush=True,
    )
    if args.check:
        return 0
    provider = resolve(ENVIRONMENTS, args.environment)
    scheduler = Scheduler(
        batch, provider, jobs, max_parallel=args.max_parallel, poll_seconds=args.poll_seconds
    )
    results = scheduler.run(provider.slots(args.ports))
    return int(any(j['status'] == 'failed' for j in results))


if __name__ == '__main__':
    from runtime.quiet_processes import enable_background_children

    enable_background_children()
    raise SystemExit(main())
