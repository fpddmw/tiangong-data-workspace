#!/usr/bin/env python3
"""Bounded role runtime adapters on the original SQLite request/run records.

No daemon, queue, credential provisioning, model fallback or main-chat messaging.
Frozen review and explicitly configured native worker turns are supported.
Existing qualified production hosts retain their original Goal/stop contracts.
"""
import argparse
import contextlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import workspace_files as entry
from codex_host import HostError, safe_diagnostic
from harness_hosts import ADAPTERS, ADAPTER_HOSTS, HOSTS, NativeStartRejected, confirmed_not_started, supports
from production_review import read_original, scientific_path

# Runtime assembly uses shared primitives; host implementations are loaded by the registry.
from agent_base import (
    CapabilityUnavailable, RuntimeError, StopRequested,
    PARENT_IDENTITY_ENV, CODEX_USAGE_FIELDS, CLAUDE_USAGE_FIELDS,
    child_environment, configure_workspace_sandbox, digest, identity,
    native_cli_environment, native_duration, native_model_name,
    native_start_boundary, now, observed_usage, production_skill,
    program_original_path, read_receipt, same_actor, scope_workspace_writes,
)

ROLES = ('global_main', 'task_lead', 'executor', 'scientific_reviewer', 'maintainer')
CONFIG_SCHEMA = 'data-workspace.agent-runtime-config.v1'
OBSERVATION_SCHEMA = 'data-workspace.agent-runtime-observation.v1'


def load_config(root, path, role):
    """Read one exact configuration; configured models are never advertised live."""
    path = entry.safe_path(root, path)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('schema') != CONFIG_SCHEMA or not isinstance(value.get('version'), str):
        raise RuntimeError('Expected a versioned agent runtime configuration.')
    if type(value.get('programmatic', False)) is not bool:
        raise RuntimeError('Runtime programmatic selection must be a boolean.')
    roles, hosts, providers = (value.get(key) for key in ('roles', 'hosts', 'providers'))
    if not all(isinstance(container, dict) for container in (roles, hosts, providers)):
        raise RuntimeError('Runtime roles, hosts and providers must be configuration objects.')
    if role not in ROLES:
        raise RuntimeError('Unknown organizational role.')
    if role not in roles:
        raise CapabilityUnavailable('The selected organizational role has no runtime profile: ' + role)
    row = roles[role]
    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ('host', 'provider', 'model')):
        raise RuntimeError('The selected role needs explicit host, provider and model identifiers.')
    if row['host'] not in hosts or row['provider'] not in providers:
        raise RuntimeError('Selected role references an unknown host or provider.')
    profile = row.get('profile', 'read-only-evidence')
    if profile not in ('native-workspace', 'read-only-evidence'):
        raise CapabilityUnavailable('Unsupported role tool profile.')
    if role == 'scientific_reviewer' and profile == 'native-workspace':
        raise CapabilityUnavailable('Independent reviewers use read-only-evidence.')
    selected = {**row, 'profile': profile}
    host, provider = hosts[selected['host']], providers[selected['provider']]
    if not isinstance(host, dict) or not isinstance(provider, dict):
        raise RuntimeError('Host and provider configurations must be objects.')
    if host.get('adapter') not in ADAPTER_HOSTS:
        raise CapabilityUnavailable('Configured host has no implemented adapter.')
    binary = host.get('binary')
    if not isinstance(binary, str) or not binary or '\x00' in binary:
        raise RuntimeError('Host binary must be an explicit executable path or name; shell commands are not accepted.')
    selected.update(role=role, host_id=selected['host'], host=host, provider_id=selected['provider'],
                    provider=provider, config_version=value['version'], programmatic=value.get('programmatic', False),
                    config_ref={'path': str(path), 'sha256': digest(raw)})
    if selected.get('profile') == 'native-workspace':
        policy = selected.get('native_policy')
        if not isinstance(policy, dict):
            raise RuntimeError('Native worker needs an explicit host tool policy; no automatic escalation.')
    adapter_type(host['adapter']).configure(root, selected)
    return selected


def capability(config):
    adapter = adapter_type(config['host']['adapter'])
    return {'host': config['host_id'], 'adapter': config['host']['adapter'],
            'configured_model': config['model'], 'configured_provider': config['provider_id'],
            'enabled': config['host'].get('enabled') is True,
            'executable_found': shutil.which(config['host']['binary']) is not None,
            'model_availability': 'not_probed', 'provider_access': 'not_probed',
            'profile': config.get('profile', 'read-only-evidence'),
            'tool_boundary': adapter.tool_boundary,
            'capabilities': sorted(HOSTS[ADAPTER_HOSTS[config['host']['adapter']]].capabilities),
            'operations': {'start': 'bounded_native_turn', 'resume': 'exact_native_identity_after_terminal_receipt',
                           'readresult': adapter.read_operation, 'stop': adapter.stop_operation},
            'qualification': 'source_and_offline_fixtures_only; no_live_host_qualification'}




def adapter_type(name):
    module, implementation = ADAPTERS[name].implementation
    owner = sys.modules[__name__] if module == 'agent_runtime' else importlib.import_module(module)
    return getattr(owner, implementation)


def adapter_for(root, config):
    if config['host'].get('enabled') is not True:
        raise CapabilityUnavailable('Configured host is disabled; no native invocation admitted.')
    if not shutil.which(config['host']['binary']):
        raise CapabilityUnavailable('Configured host executable is unavailable in this environment.')
    return adapter_type(config['host']['adapter'])(root, config)


class Runtime:
    """The dispatcher stores intent first, then native observations in same core."""
    def __init__(self, root, *, core=None, adapter_factory=adapter_for):
        self.root = Path(root).resolve(strict=True)
        if core is None:
            import harness_tasks as core
        self.core, self.adapter_factory = core, adapter_factory
        self.actor = core.verified_actor(self.root)
        # In-process native handles only. SQLite remains canonical; this cache
        # creates no service and is never used to recover missing ownership.
        self.live_adapters = {}
        self.live_lock = threading.RLock()

    def _write(self, home, run_id, value):
        try:
            launch=self._original_launch(self.core.read_run(self.root,run_id))
            home=launch.get('evidence_home') or home
        except self.core.WorkConflict as exc:
            if exc.code!='not_found':raise
        home=Path(home)/'runs'/run_id
        path = scientific_path(self.root, str(home), must_exist=False)
        path.mkdir(parents=True, exist_ok=True)
        # Observations are original evidence, not a parallel run ledger. The
        # canonical current run, identity and outcome remain in SQLite.
        output = path / ('runtime-' + digest(run_id.encode())[:24] + '-' + uuid.uuid4().hex + '.json')
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        with output.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return {'path': str(output), 'sha256': digest(raw)}

    def _admit(self, task_id, request_id, plan_revision):
        return self.core.admission(self.root, task_id, self.actor, request_id=request_id,
                                   expected_plan_revision=plan_revision)

    def _launch_ref(self, home, run_id, request_id, session_id, native_cwd=None, *,
                    native_parent_run_id=None, continuation_ref=None, goal_budget=None):
        """Retain the native directory and any exact requested session."""
        directory = scientific_path(self.root, str(home), must_exist=False)
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / 'runs' / run_id / 'launch.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        spec = {'schema': 'data-workspace.agent-runtime-launch.v1', 'run_id': run_id,
                'request_id': request_id,'work_home':str(directory), 'identity_status': 'requested_only_not_native_observed'}
        if session_id:
            spec['requested_session_id'] = session_id
            spec['native_parent_run_id'] = native_parent_run_id
        if native_cwd:
            spec['native_cwd'] = native_cwd
        if continuation_ref:
            spec['continuation_ref'] = self.core.evidence_ref(self.root, continuation_ref)
        if goal_budget:
            spec['goal_budget']=goal_budget
        raw = (json.dumps(spec, sort_keys=True) + '\n').encode()
        try:
            with output.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if output.read_bytes() != raw:
                raise RuntimeError('Original launch specification differs; reconcile the same run.')
        return {'path': str(output), 'sha256': digest(raw)}

    def _original_launch(self, run):
        from harness_package import launch_spec
        return launch_spec(self.root, run)

    def run_config(self, run, role, config_path=None):
        from harness_hosts import installed_program
        installed = (installed_program(self.root) or {}).get('path') if not config_path else None
        selected = config_path or installed or run.get('config_ref', {}).get('path')
        if not selected:
            raise CapabilityUnavailable('Select an installed host configuration to observe this native Session; its saved evidence remains readable.')
        config = load_config(self.root, selected, role)
        if not config_path and ADAPTER_HOSTS[config['host']['adapter']] != run['target_host'] and run.get('config_ref'):
            config = load_config(self.root, run['config_ref']['path'], role)
        if ADAPTER_HOSTS[config['host']['adapter']] != run['target_host']:
            raise CapabilityUnavailable('Select the installed host that owns this native Session.')
        return config


    def confirmed_start_rejection(self, run, receipt=None, *, allow_stopped=False):
        if (run['outcome'] != 'failed' or (run['stop_requested'] and not allow_stopped)
                or not run.get('latest_receipt')):
            return False
        launch = self._original_launch(run)
        if (not run['native_identity'] and run['operation'] == 'resume' and launch.get('requested_session_id')
                and 'native_parent_run_id' not in launch):
            return False  # Historical retry lacks a prior-run binding. Preserve the original session.
        receipt = receipt if receipt is not None else read_receipt(self.root, run['latest_receipt'])
        return confirmed_not_started(receipt, host=run['target_host'], run_id=run['id'],
                                     request_id=run['request_id'], config_sha256=run['config_ref']['sha256'],
                                     native_identity=run['native_identity'])

    def _recovery_config(self, run, config):
        launch = self._original_launch(run)
        directory = Path(launch['evidence_home']) / 'runs' / run['id'] if launch.get('evidence_home') else None
        return {**config, 'native_cwd': str(self.root), 'task_run_id': run['id'], 'task_request_id': run['request_id'],
                **({'_native_run_directory': str(directory)} if directory else {})}

    def execute(self, *, config_path, role, task_id, request_id, run_id, plan_revision=None,
                input_path, input_sha256, evidence_home, operation='start', previous_run_id=None,
                program_context=None, queued_handoff=None):
        from execution_lease import ExecutionLease, ExecutionBusy, observe_run
        from safe_diagnostics import DIRECTORY
        request = self.core.read_request(self.root, request_id)
        previous = self.core.read_run(self.root, previous_run_id) if previous_run_id else None
        native = previous.get('native_identity') if previous else None
        explicit_stop_recovery = bool(previous and
            ((program_context or {}).get('technical_recovery') or {}).get('resume_stopped_run') == previous['id'])
        if previous and not native and self.confirmed_start_rejection(previous,allow_stopped=explicit_stop_recovery):
            launch=self._original_launch(previous)
            if parent_id:=launch.get('native_parent_run_id'):
                parent=self.core.read_run(self.root,parent_id)
                native=parent.get('native_identity')
                if (parent['request_id']!=previous['request_id'] or not native
                        or native['id']!=launch.get('requested_session_id')):
                    raise ExecutionBusy('Original rejected startup lost its exact parent Session binding')
        # A fenced queued resume must lock its original Session without replacing
        # the old transport metadata before the exact handoff claim verifies it.
        with ExecutionLease(self.root, request, evidence_home, run_id, native,
                            defer_metadata=bool(queued_handoff)) as lease:
            if previous and supports(previous['target_host'], 'tracked_processes'):
                observed = observe_run(self.root, previous)
                if observed['state'] != 'exited':
                    raise ExecutionBusy('Original execution is ' + observed['state'] + ': ' + observed.get('reason', '') + '; read original run ' + previous['id'])
            token = DIRECTORY.set(str(lease.path.parent / 'diagnostics'))
            try:
                return self._execute(config_path=config_path, role=role, task_id=task_id,
                    request_id=request_id, run_id=run_id, plan_revision=plan_revision,
                    input_path=input_path, input_sha256=input_sha256, evidence_home=evidence_home,
                    operation=operation, previous_run_id=previous_run_id, program_context=program_context,
                    execution_lease=lease,queued_handoff=queued_handoff)
            finally:
                DIRECTORY.reset(token)

    def _execute(self, *, config_path, role, task_id, request_id, run_id, plan_revision,
                 input_path, input_sha256, evidence_home, operation, previous_run_id,
                 program_context, execution_lease, queued_handoff=None):
        config = load_config(self.root, config_path, role)
        config['_execution_lease'] = execution_lease
        config['diagnostic_directory'] = str(execution_lease.path.parent / 'diagnostics')
        config['task_request_id']=request_id
        config['task_run_id']=run_id
        config['technical_continue'] = bool((program_context or {}).get('technical_recovery'))
        if config['programmatic'] != (program_context is not None):
            raise RuntimeError('Programmatic configuration must enter through the original work-dispatch loop.')
        if config['host'].get('enabled') is not True:
            raise CapabilityUnavailable('Configured host is disabled; no run intent or native invocation admitted.')
        if self.adapter_factory is adapter_for and not shutil.which(config['host']['binary']):
            raise CapabilityUnavailable('Configured executable unavailable; no run intent or native invocation admitted.')
        if (settings := config['host'].get('settings_file')) and not Path(settings).is_file():
            raise CapabilityUnavailable('The selected existing Claude CLI settings file is unavailable.')
        path, contents = read_original(self.root, input_path, input_sha256)
        request = self.core.read_request(self.root, request_id)
        if request.get('task_id') != task_id:
            raise RuntimeError('Request belongs to a different original task.')
        original_input = (((program_context or {}).get('technical_recovery') or {}).get('ref')
                          if operation == 'resume' else None) or request.get('input_ref', {})
        if (entry.safe_path(self.root, original_input.get('path', ''), must_exist=False) != path
            or original_input.get('sha256') != input_sha256):
            raise RuntimeError('Dispatch must consume the explicitly selected original instruction bytes.')
        from harness_package import input_status
        package_status = input_status(self.root, request)
        package_inputs = package_status['available']
        expected_role = 'executor' if role == 'scientific_reviewer' else role
        if request.get('recipient_role') != expected_role:
            raise RuntimeError('Configured role differs from the original request responsibility.')
        if request.get('independent') and role != 'scientific_reviewer':
            raise RuntimeError('Independent review must use the independent scientific_reviewer profile, not an ordinary executor profile.')
        previous, native_parent_run_id = None, None
        if operation == 'resume':
            if not previous_run_id:
                raise RuntimeError('Resume requires the exact previous run ID.')
            old = self.core.read_run(self.root, previous_run_id)
            old_request = self.core.read_request(self.root, old['request_id'])
            explicit_unknown = (old.get('outcome') == 'unknown' and supports(old['target_host'], 'interrupted_resume')
                                and bool((program_context or {}).get('technical_recovery')))
            if old_request.get('task_id') != task_id or old.get('outcome') not in ('succeeded', 'failed', 'stopped') and not explicit_unknown:
                raise RuntimeError('Previous run is not a terminal same-task run; unknown/running work cannot be resumed as a replacement.')
            previous = old.get('native_identity')
            if explicit_unknown:
                with contextlib.closing(self.core._connect(self.root, True)) as c:
                    origin = self.core.session_origin(c, self.core._row(c, 'work_runs', old['id']))
                    previous = json.loads(origin['native_identity_json'])
                    native_parent_run_id = origin['id']
            if old['request_id'] != request_id:
                from harness_package import admit_author_run
                with contextlib.closing(self.core._connect(self.root, True)) as c:
                    admit_author_run(c, self.core._row(c, 'work_requests', request_id), 'resume', old['id'])
            if ADAPTER_HOSTS[config['host']['adapter']] != old['target_host']:
                raise RuntimeError('The selected host cannot resume a Session owned by another host.')
            recorded = read_receipt(self.root, old['latest_receipt']) if old.get('latest_receipt') else {}
            explicit_stop_recovery = ((program_context or {}).get('technical_recovery') or {}).get('resume_stopped_run') == old['id']
            if previous:
                native_parent_run_id = native_parent_run_id or old['id']
                config['previous_thread_only'] = self.confirmed_start_rejection(old, recorded,allow_stopped=explicit_stop_recovery)
            else:
                if not self.confirmed_start_rejection(old, recorded,allow_stopped=explicit_stop_recovery):
                    raise RuntimeError('Previous run lacks an observed native identity.')
                launch = self._original_launch(old)
                if native_parent_run_id := launch.get('native_parent_run_id'):
                    bound = self.core.read_run(self.root, native_parent_run_id)
                    previous = bound.get('native_identity')
                    if (not previous or previous['id'] != launch['requested_session_id']
                            or bound['request_id'] != old['request_id']
                            or bound['outcome'] not in ('succeeded', 'failed', 'stopped')):
                        raise RuntimeError('Recover the exact native session retained by the original launch.')
                    config['previous_thread_only'] = self.confirmed_start_rejection(bound)
            prior = recorded.get('result') or {}
            meter = prior.get('metering') or {}
            config['prior_thread_usage'] = meter.get('thread_total')
            if (config['host']['adapter'] == 'dsh-headless' and config['technical_continue']
                    and old['request_id'] == request_id and previous == old.get('native_identity')):
                from dsh_auto_resume import recent_timeout_deadline
                config['recent_timeout_until_ms'] = recent_timeout_deadline(old, prior, config['provider']['dsh_provider'])
        elif operation != 'start':
            raise RuntimeError('Unsupported dispatch operation.')
        if goal_budget := ((program_context or {}).get('technical_recovery') or {}).get('goal_budget'):
            if (config['host']['adapter']!='dsh-headless' or role!='executor' or operation!='resume'
                    or not previous or goal_budget.get('original_run_id')!=previous_run_id
                    or goal_budget.get('session_id')!=previous['id']):
                raise RuntimeError('The original authorized budget belongs to another author or Session.')
            config['goal_budget']=goal_budget
        if config['programmatic'] and not request['independent']:
            from production_review import program_home
            config['program_work_home'] = str(program_home(request, root=self.root) / 'work')
        config = self._execution_scope(config, request, contents, evidence_home)

        self._admit(task_id, request_id, plan_revision)
        if operation=='start':
            import harness_scope
            with contextlib.closing(self.core._connect(self.root,True)) as c:
                c.execute('BEGIN')
                dispatch_check,check_ref=harness_scope.checked_report(c,self.root,'request',request_id,'dispatch')
            if not dispatch_check['ready']:
                raise CapabilityUnavailable('New dispatch pending: '+check_ref['path']+'; draft and original reports remain readable.')
        from production_review import independent_task_input
        review = independent_task_input(self.root, request, role, self.core, input_ref=original_input)
        if review and previous and any(same_actor(previous, author) for author in review['authors']):
            raise RuntimeError('Independent reviewer cannot resume an actual author context.')
        config['native_cwd'] = str(self.root)
        guides = {'global_main': 'global-main', 'task_lead': 'task-lead',
                  'executor': 'data', 'scientific_reviewer': 'reviewer', 'maintainer': 'maintainer'}
        from harness_snapshot import current_plan_ref
        plan_binding = self.core.read_task(self.root, task_id)['task']['plan_ref']
        current_plan = current_plan_ref(self.root, plan_binding)
        originals = [('工作区入口', self.root / 'AGENTS.md'),
                     ('本职指南', self.root / 'roles' / (guides[role] + '.md')),
                     ('原请求文件', Path(request['input_ref']['path']))]
        if current_plan:
            originals.append(('当前接续页', Path(current_plan['path'])))
        if operation=='start' and request_id in dispatch_check.get('effective_delegations',{}):
            originals.append(('有效委派及其父承诺、范围和子终点', Path(check_ref['path'])))
        try:
            frozen_plan = self.core.evidence_ref(self.root, plan_binding)
        except (OSError, ValueError):
            frozen_plan = None
        if frozen_plan and frozen_plan != current_plan:
            originals.append(('冻结承诺', Path(frozen_plan['path'])))
        if review and review.get('frozen_submission_ref'):
            originals.append(('本轮冻结候选和原审阅问题', Path(review['frozen_submission_ref']['path'])))
        for key, label in (('parent_brief_ref', '作者原委派'),
                           ('prior_receipt_ref', '上一运行回执（事实记录）')):
            if ref := (program_context or {}).get(key):
                originals.append((label, self.root / ref['path']))
        replacement=(program_context or {}).get('writer_replacement')
        if replacement:
            originals.append(('前任 Session 未收到指令的原回执',Path(replacement['receipt']['path'])))
        recovery = (program_context or {}).get('technical_recovery') or {}
        continuation_ref = recovery.get('ref')
        automatic = recovery.get('automatic') is True
        if continuation_ref:
            originals.append(('本次故障接续说明' if automatic else '本次接续说明（负责人原文）',
                              self.root / continuation_ref['path']))
        duties={'global_main':'持续组织原父目标的完整投入、余项和跨包依赖。',
                'task_lead':'用原生子智能体协助明确制作计划，把制作包交给 worker，持续组织整包交付、成果采用及获准的平台提交。',
                'executor':('在原Session'+('和原生Goal' if supports(ADAPTER_HOSTS[config['host']['adapter']], 'native_goal') else '')+
                            '中自主完成本包取证、制作、自查、独审返修及交接。'),
                'scientific_reviewer':'独立只读核准确冻结候选与用途，将有据科学结论、条件及可操作意见直接交作者。',
                'maintainer':'沿原计划和维护board亲自修共享能力，验证原入口并交回实际边界。'}
        text = '本次职责：' + duties[role] + '\n工作目录：' + str(self.root) + '\n原请求：' + request_id + '\n直接完整读取原件：\n'
        text += '\n'.join(label + '：' + str(item) for label, item in originals)
        text += '\n合同、全范围与交接入口：' + str(self.root/'workflows/business-flow/README.md')
        text += '\n卡片是预览，科学判断、采用与授权前读取完整原结论和条件；阶段报告随时可交回，继续可执行余项。'
        if automatic:
            text += '\n' + contents.strip()
        if review and (facts := review.get('facts_ref')):
            text += ('\n审阅材料索引 '+facts['path']+'\n先用索引定位本次冻结稿、确切版本、交换和来源，'
                     '以及发起独审时已有的完整读回对照。意见绑定本次冻结稿；科学适用性直接核原稿和来源。')
        if review and (focus := review.get('focus')):
            prior = review['prior_review']
            text += ('\n聚焦复审核对已返回的原意见、每项修订及受影响用途。打开完整意见 '+prior['opinion_ref']['path']+
                     '\n旧冻结包 '+prior['submission_ref']['path']+'\n差异索引 '+focus.get('index_ref',focus['delta_ref'])['path']+
                     '\n完整字段和交换差异 '+focus['delta_ref']['path']+
                     '\n通过新旧包的准确指针读取完整原件，复核关联消费者、分母和受影响的科学条件。'
                     '可用 local_check 一次执行离线批算，在临时目录输出核算，再交完整意见给原作者。')
        if config.get('program_work_home'):
            text += '\n本包工件位置：' + config['program_work_home']
        payload = {'id': run_id, 'request_id': request_id,
                   'config_ref': config['config_ref'], 'input_refs': [{'path': str(path), 'sha256': input_sha256}],
                   'target_host': ADAPTER_HOSTS[config['host']['adapter']], 'operation': operation}
        if plan_revision is not None:
            payload['plan_revision'] = plan_revision
        raw_config = Path(config['config_ref']['path']).read_bytes()
        if digest(raw_config) != config['config_ref']['sha256']:
            raise RuntimeError('Selected configuration changed during launch preparation; retry before dispatch.')
        from production_review import _save_fixed
        payload['input_refs'].append(_save_fixed(self.root, Path(evidence_home) / 'runs' / run_id /
            ('runtime-config-' + digest(raw_config) + '.json'), raw_config))
        if previous_run_id:
            payload['previous_run_id'] = previous_run_id
        if replacement:
            payload['writer_replacement']=replacement
            payload['input_refs'].append(self.core.evidence_ref(self.root,replacement['receipt']))
        if automatic:
            payload['dsh_auto_continue_from'] = previous_run_id
            config['automatic_recovery_probe'] = program_context['technical_recovery'].get('probe') is True
        if program_context:
            if recovery := program_context.get('technical_recovery'):
                payload['input_refs'].append(self.core.evidence_ref(self.root, recovery['ref']))
                payload['input_refs'].extend(self.core._refs(self.root, recovery['evidence_refs'],
                                                            'Original continuation evidence', False))
                if recovery.get('ordinary_continuation'):
                    payload['ordinary_continuation']=recovery['ordinary_continuation']
        payload['input_refs'].extend(package_inputs)
        if review and review.get('frozen_submission_ref'):
            payload['input_refs'].append(review['frozen_submission_ref'])
        requested_session_id = None
        if config['host']['adapter'] == 'claude-code-print':
            requested_session_id = previous['id'] if previous else str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.root) + '\n' + request_id + '\n' + run_id))
        if previous and not requested_session_id:
            requested_session_id = previous['id']
        if requested_session_id or config.get('native_cwd'):
            payload['input_refs'].append(self._launch_ref(evidence_home, run_id, request_id, requested_session_id,
                                                          config.get('native_cwd'),
                                                          native_parent_run_id=native_parent_run_id,
                                                          continuation_ref=continuation_ref,
                                                          goal_budget=config.get('goal_budget')))
        # This action rejects an existing/uncertain intent before adapter creation.
        recovery_watch = None
        if payload['target_host'] == 'deepseek_harness':
            from dsh_capacity import recovery_managed
            dsh_provider = config['provider'].get('dsh_provider')
            if recovery_managed(self.root, dsh_provider):
                recovery_watch = {'provider':dsh_provider, 'manager':execution_lease.value['manager']}
                payload['dsh_recovery_watch'] = recovery_watch
        if queued_handoff:
            # An explicitly fenced, never-started transport retains this exact
            # native intent. This does not create/retry a scientific request.
            from dsh_capacity_handoff import claim
            original=self.core.read_run(self.root,run_id)
            if (operation!=original['operation'] or operation not in ('start','resume') or original['target_host']!='deepseek_harness'
                    or original['request_id']!=request_id or original['config_ref']!=config['config_ref']
                    or original['input_refs']!=payload['input_refs']):
                raise RuntimeError('Queued handoff must retain the original operation, runtime and complete immutable inputs')
            claim(self.root,original,execution_lease,queued_handoff)
            intent={**original,'may_launch':True}
        else:intent = self.core.mutate(self.root, 'run-intent', self.actor, payload)
        if intent.get('may_launch') is not True:
            raise RuntimeError('Original run intent already exists; read/reconcile the same run, never dispatch again.')
        execution_lease.intent()
        if recovery_watch:
            from dsh_recovery_host import wake, reattach
            if queued_handoff:
                reattach(self.root, self.actor, run_id, recovery_watch)
            wake(self.root)
        from dsh_capacity import Capacity, DSH, DEVIN, POOL_NAMES
        host = intent['target_host']
        if host in POOL_NAMES:
            capacity_key = '_dsh_capacity' if host == DSH else '_devin_capacity'
            config[capacity_key] = Capacity(self.root, execution_lease,
                'review' if role == 'scientific_reviewer' else 'author', self.actor, handoff=queued_handoff,
                provider=config['provider'].get('dsh_provider'), host=host)
        dependency_issues = {}
        def remember_input_issues(issues):
            dependency_issues.update((self.core._json(issue), issue) for issue in issues)
        def current_input_issues():
            return self.core.check_inputs(self.root, intent['input_refs'])
        remember_input_issues(current_input_issues())

        @contextlib.contextmanager
        def launch_boundary():
            with self.core.dispatch_admission(self.root, task_id, self.actor,
                                               request_id=request_id, run_id=run_id,
                                               expected_plan_revision=plan_revision) as admitted:
                remember_input_issues((admitted or {}).get('input_issues', []))
                if operation=='start':
                    # Recheck the same short boundary after acquiring the ledger
                    # transaction; no native scientific checks are executed here.
                    with contextlib.closing(self.core._connect(self.root,True)) as checked_connection:
                        current=harness_scope.build_check(checked_connection,self.root,'request',request_id,'dispatch')
                    if not current['ready'] or current['input_fingerprint']!=dispatch_check['input_fingerprint']:
                        raise CapabilityUnavailable('Original dispatch basis changed; reread work-check for '+request_id)
                yield admitted

        def run_guard():
            if self.core.read_run(self.root, run_id).get('stop_requested'):
                raise StopRequested('Original run stop was requested; no new native turn admitted.', run_id)
            if automatic and self.core.read_run(self.root, previous_run_id).get('stop_requested'):
                self.core.mutate(self.root,'run-stop',self.actor,{'id':run_id,'summary':'Original attempt stop applies to its automatic continuation.'})
                raise StopRequested('Original attempt stop still applies to its automatic continuation.', run_id)
        adapter = None
        review_service = None
        started_at, start = now(), time.monotonic()
        try:
            if host == DEVIN:
                execution_lease.stack.enter_context(config['_devin_capacity'].admit(run_guard))
            adapter = self.adapter_factory(self.root, config)
            if supports(ADAPTER_HOSTS[config['host']['adapter']], 'author_review') and role == 'executor':
                from scientific_review.author_tools import ReviewService
                review_service = ReviewService(self, request, config, run_id, evidence_home, run_guard)
                adapter.review_service = review_service
            with self.live_lock:
                self.live_adapters[run_id] = adapter
            def observe(native, native_status, observation):
                execution_lease.bind(native)
                if review and any(same_actor(native, author) for author in review['authors']):
                    raise RuntimeError('Independent reviewer native identity equals the actual author.')
                value = {'schema': OBSERVATION_SCHEMA, 'host': native['host'], 'operation': operation,
                         'run_id': run_id, 'request_id': request_id,
                         'source': ADAPTERS[config['host']['adapter']].source,
                         'native_identity': native, 'native_status': native_status,
                         'config_sha256': config['config_ref']['sha256'], 'config_version': config['config_version'],
                         'runtime_role': config['role'],
                         'requested_model': config['model'], 'requested_provider': config['provider_id'],
                         'observed_at': now(), 'observation': observation}
                receipt = self._write(evidence_home, run_id, value)
                self.core.mutate(self.root, 'run-bind', self.actor, {'id': run_id, 'native_identity': native, 'receipt': receipt})
            result = adapter.execute(text, observe, run_guard, previous=previous,
                                     requested_session_id=requested_session_id,
                                     boundary=launch_boundary)
            result['metering'] = {'usage': None, 'cost_usd': None, **(result.get('metering') or {}),
                                  'started_at': started_at, 'finished_at': now(),
                                  'elapsed_seconds': round(time.monotonic() - start, 6)}
            # Native terminal status remains a fact even when the delivered
            # body is incomplete or the input changed during execution.
            if review_service:
                result.update(review_service.finish())
            try:
                read_original(self.root, input_path, input_sha256)
                remember_input_issues(current_input_issues())
                unavailable_inputs = [*package_status['unavailable'], *dependency_issues.values()]
                for material in package_inputs:
                    try:
                        self.core.evidence_ref(self.root, material)
                    except (ValueError, OSError) as exc:
                        unavailable_inputs.append({'ref': material, 'reason': safe_diagnostic(exc)})
                if unavailable_inputs:
                    result['input_issues'] = unavailable_inputs
                if review:
                    independent_task_input(self.root, request, role, self.core)
                    if not config['programmatic'] and result.get('native_status') == 'completed':
                        result['review_text'] = result.get('result')
            except (ValueError, OSError) as exc:
                result.setdefault('input_issues', []).append({'reason': safe_diagnostic(exc)})
            recorded = self._record_result(run_id, config, result, evidence_home)
            return recorded
        except Exception as exc:
            rejected = isinstance(exc, NativeStartRejected)
            outcome = 'failed' if rejected else 'unknown'
            receipt = self._write(evidence_home, run_id, {'schema': OBSERVATION_SCHEMA,
                'run_id': run_id, 'request_id': request_id,
                'host': ADAPTER_HOSTS[config['host']['adapter']] if rejected else config['host']['adapter'],
                'operation': operation, 'source': 'adapter_exception',
                'config_sha256': config['config_ref']['sha256'], 'observed_at': now(),
                'runtime_role': config['role'], 'requested_model': config['model'], 'requested_provider': config['provider_id'],
                'error_type': type(exc).__name__, 'detail': safe_diagnostic(exc), 'outcome': outcome,
                **({'failure_origin':{'surface':'workspace_ledger','code':exc.sqlite_errorname}}
                   if self.core.write_contention(exc) else {}),
                **({'control_stop_run_id': exc.run_id} if isinstance(exc, StopRequested) else {}),
                **({'observation': exc.observation} if hasattr(exc, 'observation') else {}),
                **({'native_status': 'not_started'} if rejected else {}),
                **({'native_identity': exc.ready_thread} if rejected and exc.ready_thread else {}),
                'metering': {'started_at': started_at, 'finished_at': now(),
                             'elapsed_seconds': round(time.monotonic() - start, 6), 'usage': None, 'cost_usd': None}})
            current = self.core.read_run(self.root, run_id)
            if current.get('outcome') not in ('succeeded', 'failed', 'stopped'):
                self.core.mutate(self.root, 'run-outcome', self.actor, {'id': run_id, 'outcome': outcome,
                    'receipt': receipt,
                    'summary': ('The original invocation did not start.'
                                if rejected else 'Reconcile original native result/registration; no duplicate start.')})
            if rejected:
                raise CapabilityUnavailable(safe_diagnostic(exc)) from exc
            raise
        finally:
            if review_service:
                review_service.close()
            if adapter:
                with self.live_lock:
                    self.live_adapters.pop(run_id, None)
                adapter.close()
            # Only a normally closed transport/confirmed pre-start rejection
            # can settle this manager's local execution. Hard crashes leave intent.
            current = self.core.read_run(self.root, run_id)
            if current['outcome'] in ('succeeded', 'failed', 'stopped'):
                execution_lease.settled()
            elif adapter and getattr(adapter, 'process', None) is not None:
                process = adapter.process
                if process.poll() is not None:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        execution_lease.settled()
                    except PermissionError:
                        pass

    def _execution_scope(self, config, request, contents, evidence_home):
        """Apply original package authorization before asking the selected host for tools."""
        try:
            document = json.loads(contents)
        except ValueError:
            document = {}
        required = document.get('required_capabilities', []) if isinstance(document, dict) else []
        adapter = adapter_type(config['host']['adapter'])
        supported = {'text', *adapter.tool_capabilities(config)}
        if not isinstance(required, list) or any(not isinstance(v, str) for v in required):
            raise CapabilityUnavailable('required_capabilities must list actual required host surfaces')
        missing = set(required) - supported
        if missing:
            raise CapabilityUnavailable('Selected host does not supply: ' + ', '.join(sorted(missing)) + '; keep original task and choose its qualified existing host.')
        if config.get('profile') != 'native-workspace':
            return config
        if adapter.requires_program_work_root and not config.get('program_work_home'):
            raise CapabilityUnavailable('The selected workspace profile requires an exact original package work directory.')
        original = entry.safe_path(self.root, Path(request['input_ref']['path']).parent, must_exist=False)
        receipt = entry.safe_path(self.root, evidence_home, must_exist=False)
        work = entry.safe_path(self.root, config['program_work_home'], must_exist=False) if config.get('program_work_home') else None
        return adapter.scope_writes(self.root, config, original=original, work=work, receipt=receipt)


    def _record_result(self, run_id, config, result, home, *, existing_return=None, observation_only=False):
        from safe_diagnostics import safe_value, retain
        for field in ('native_error', 'native_diagnostic', 'native_session_observation'):
            if field in result:
                result[field] = safe_value(result[field])
        observed_error = (result.get('native_session_observation') or {}).get('native_error')
        if observed_error or result.get('native_diagnostic'):
            result['safe_diagnostic'] = retain(observed_error or result['native_diagnostic'],
                directory=Path(home) / 'runs' / run_id / 'diagnostics', source='native_result',
                code=result.get('native_diagnostic'))
        status = result.get('native_status')
        run = self.core.read_run(self.root, run_id)
        from delivery_evidence import ingest_run
        result['delivery_evidence_observation']=ingest_run(self.root,run,self.actor)
        request = self.core.read_request(self.root, run['request_id'])
        outcome = {'completed': 'succeeded', 'failed': 'failed', 'interrupted': 'stopped',
                   'inProgress': 'running', 'in_progress': 'running', 'running': 'running'}.get(status, 'unknown')
        native = result.get('native_identity')
        metadata = {'runtime_role': config['role'], 'requested_model': config['model'],
                    'requested_provider': config['provider_id'], 'config_version': config['config_version']}
        if config['config_ref'] != run['config_ref']:
            original = read_receipt(self.root, run['latest_receipt']) if run.get('latest_receipt') else {}
            metadata = {key: original.get(key) for key in metadata}
        receipt = self._write(home, run_id, {'schema': OBSERVATION_SCHEMA,
            'run_id': run_id, 'request_id': run['request_id'],
            'host': native['host'], 'native_identity': native, 'native_status': status,
            'source': HOSTS[native['host']].source,
            'config_sha256': run['config_ref']['sha256'], 'observed_at': now(),
            'observer_config_ref': config['config_ref'], **metadata,
            'stop_confirmed': result.get('stop_confirmed') is True, 'result': result,
            'authority': 'native_turn_evidence_only; scientific_adoption_and_professional_receipt_not_inferred'})
        payload={'id':run_id,'outcome':outcome,'receipt':receipt,
            'summary':'Original native turn '+str(status)+'; result saved, task return/adoption remains explicit.'}
        if observation_only:
            # The original executor may have published its terminal or advanced
            # the live turn after this read began. Decide inside the same ledger
            # transaction as publication; a stale poll never rewrites that fact.
            retained=None
            with self.core.transaction(self.root) as c:
                current=self.core._run_view(c,self.core._row(c,'work_runs',run_id))
                latest=current.get('native_identity') or {}
                advanced=(latest and native and same_actor(latest,native)
                    and latest.get('turn_id')!=native.get('turn_id'))
                if current['outcome'] in ('succeeded','failed','stopped') or advanced:retained=current
                else:self.core.apply(c,self.root,'run-outcome',self.actor,payload)
            if retained:
                if retained['outcome'] in ('succeeded','failed','stopped'):
                    return {**self._retained_result(retained,result['delivery_evidence_observation']),
                        'observer_source':'concurrent_original_terminal','stale_observation_receipt':receipt}
                return {'run_id':run_id,'outcome':retained['outcome'],'receipt':retained.get('latest_receipt'),
                    'native_identity':retained.get('native_identity'),'result':result,
                    'source':'concurrent_original_turn_advanced','stale_observation_receipt':receipt}
        else:self.core.mutate(self.root,'run-outcome',self.actor,payload)
        delivery = (self._registered_delivery(existing_return, result) if existing_return
                    else self._return(run_id, receipt, result))
        observations = self._retain_tool_observations(run_id, receipt, result)
        effects = self._handoff_effects(run_id,result)
        return {'run_id': run_id, 'outcome': outcome, 'receipt': receipt, 'native_identity': result.get('native_identity'),
                'management_return': delivery, 'maintenance_observations': observations,
                **({'delivery_effects':effects} if effects else {}),
                **({'transport_diagnostic': result['transport_diagnostic']} if result.get('transport_diagnostic') else {}),
                **({'metering': result['metering']} if result.get('metering') else {}),
                **({'local_readiness': result['local_readiness']} if 'local_readiness' in result else {}),
                'scientific_adoption': 'not_inferred'}

    def _retain_tool_observations(self, run_id, receipt, result):
        if not result.get('tool_failures') and result.get('native_status') != 'failed':
            return {'status': 'none_observed'}
        from task_ledger import maintenance as harness_maintenance
        try:
            paths = harness_maintenance.retain_runtime_observations(self.root, self.actor, run_id, receipt)
            return {'status': 'retained' if paths else 'already_recorded', 'count': len(paths)}
        except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
            return {'status': 'needs_attention', 'error_type': type(exc).__name__,
                    'recovery': 'Read the same original native run again to retry observation triage. The native receipt is retained.'}

    def _registered_delivery(self, return_id, result=None):
        returned = self.core.read_return(self.root, return_id)
        return {'status': 'registered', 'return_id': returned['id'],
                'result_ref': returned['result_ref'], 'identity_assurance': returned['identity_assurance']}

    def _handoff_effects(self,run_id,result):
        from delivery_evidence import read_handoff_effects
        run=self.core.read_run(self.root,run_id)
        request=self.core.read_request(self.root,run['request_id'])
        return read_handoff_effects(self.root,request,run=run,result=result)


    def _return(self, run_id, receipt, result):
        """Index a retained native report. Never interpret prose or start an agent."""
        raw = result.get('result')
        if result.get('native_status') != 'completed' or not isinstance(raw, str):
            return {'status': 'no_final', 'receipt': receipt}
        from production_review import _save_fixed
        source = _save_fixed(self.root, Path(receipt['path']).parent /
                             ('report-' + digest(raw.encode()) + '.md'), raw.encode())
        try:
            run = self.core.read_run(self.root, run_id)
            request = self.core.read_request(self.root, run['request_id'])
            # These references came from explicit author tool calls, never text extraction.
            from delivery_evidence import return_artifacts
            with contextlib.closing(self.core._connect(self.root,True)) as connection:
                artifacts = return_artifacts(connection,self.root,request,result)
            returned = self.core.mutate(self.root, 'return', self.actor, {
                'id': 'runtime-return:' + run_id, 'request_id': run['request_id'],
                'run_id': run_id, 'native_receipt': receipt, 'result_ref': source,
                'summary': raw or 'Native final is empty; inspect the retained original turn.',
                'artifact_refs': artifacts, 'coverage': [], 'remaining_scope': []})
            return self._registered_delivery(returned['id'])
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, entry.ProductionError) as exc:
            return {'status': 'index_pending', 'result_ref': source, 'receipt': receipt,
                    'diagnostic': safe_diagnostic(exc)}


    def _retained_result(self,run,archive):
        run_id=run['id'];receipt=run['latest_receipt']
        # Reading a saved fact has no runtime or organizational prerequisites.
        # Keep its original envelope even when a current parser cannot interpret it.
        value = read_receipt(self.root, receipt)
        result = value.get('result') if isinstance(value, dict) else None
        rejected=confirmed_not_started(value,host=run['target_host'],run_id=run_id,request_id=run['request_id'],
            config_sha256=run['config_ref']['sha256'],native_identity=run.get('native_identity'))
        diagnostic = None
        if run['target_host'] == 'deepseek_harness':
            from dsh_headless import retained_transport_diagnostic
            diagnostic = retained_transport_diagnostic(result, run.get('native_identity'))
        delivery=(self._registered_delivery(run['return_ids'][-1]) if run['return_ids'] else
                  self._return(run_id,receipt,result) if isinstance(result,dict) else None)
        effects=self._handoff_effects(run_id,result)
        return {'run_id': run_id, 'outcome': run['outcome'], 'receipt': receipt,
                **({'execution_state':'not_started','target_session':(value.get('observation') or {}).get('target_session'),
                    'stop_requested':run.get('stop_requested',False),'replay_allowed':False,
                    'recovery':'Keep this original Session. Resolve the reported startup condition before explicit normal continuation; reading this receipt never sends a prompt.'} if rejected else {}),
                'delivery_evidence_observation':archive,
                'result': result, 'original_receipt': value, 'native_identity': run.get('native_identity'),
                'observation': value.get('observation') if isinstance(value, dict) else None,
                'source': 'retained_original_receipt',
                **({'transport_diagnostic': diagnostic} if diagnostic else {}),
                **({'delivery_effects':effects} if effects else {}),
                'maintenance_observations': self._retain_tool_observations(run_id, receipt, result) if isinstance(result, dict) else None,
                'management_return':delivery}

    def _reconcile_pre_prompt_start(self,run):
        """Append a verified original pre-prompt refusal; never probe by loading."""
        from devin_acp import pre_prompt_rejection
        from execution_lease import observe_run,FileLock
        try:
            launch=self._original_launch(run)
            target=launch.get('requested_session_id');parent_id=launch.get('native_parent_run_id')
            if not target or not parent_id or not run.get('latest_receipt'):return run
            parent=self.core.read_run(self.root,parent_id)
            native=parent.get('native_identity') or {}
            if (parent['request_id']!=run['request_id'] or parent['target_host']!=run['target_host']
                    or native.get('id')!=target):return run
            # Serialize with this original request chain. A new observation of
            # an uncertain or still-owned manager cannot certify nonexecution.
            from harness_package import author_lineage
            request=self.core.read_request(self.root,run['request_id'])
            with contextlib.closing(self.core._connect(self.root,True)) as c:
                chain=author_lineage(c,request)[-1]
            with FileLock(self.root,['request',chain]):
                current=self.core.read_run(self.root,run['id'])
                if current['outcome']!='unknown' or current['native_identity']:return current
                execution=observe_run(self.root,current)
                if execution['state']!='exited':return current
                old=read_receipt(self.root,current['latest_receipt'])
                if (old.get('source')!='adapter_exception' or old.get('run_id')!=current['id']
                        or old.get('request_id')!=current['request_id'] or old.get('operation')!=current['operation']
                        or old.get('config_sha256')!=current['config_ref']['sha256']):return current
                observation={'native_submission':'not_started','target_session':target,'prompt_submitted':False,
                             'prior_unknown_receipt':current['latest_receipt'],'execution_observation':execution}
                if current['target_host']=='deepseek_harness':
                    if (old.get('host')!='dsh-headless' or not execution.get('cancelled_before_spawn_event')):
                        return current
                    observation.update(category='original_dispatch_guard_rejected',
                                       verification_source='original_admission_cancelled_before_spawn')
                    detail='The original manager cancelled its admitted ticket before spawning the native Session.'
                else:
                    if old.get('error_type')!='ACPError':return current
                    path=scientific_path(self.root,str(Path(launch['evidence_home'])/'runs'/current['id']/'acp.jsonl'))
                    raw=path.read_bytes();rows=[json.loads(line) for line in raw.splitlines() if line.strip()]
                    fact=pre_prompt_rejection(rows,session_id=target)
                    if (not fact or fact['method']!='session/load'
                            or fact['error']!=(old.get('observation') or {}).get('error')):return current
                    observation.update(category='acp_request_rejected_before_prompt', acp_request=fact,
                        verification_source='original_acp_transcript',transcript_ref={'path':str(path),'sha256':digest(raw)})
                    detail='Original session/load returned an exact error response before any session/prompt.'
                receipt=self._write(launch['evidence_home'],current['id'],{
                    'schema':OBSERVATION_SCHEMA,'source':'adapter_exception','host':current['target_host'],
                    'run_id':current['id'],'request_id':current['request_id'],'operation':current['operation'],
                    'config_sha256':current['config_ref']['sha256'],'observed_at':now(),
                    'error_type':'NativeStartRejected','native_status':'not_started','outcome':'failed',
                    'detail':detail,'observation':observation})
                self.core.mutate(self.root,'run-outcome',self.actor,{'id':current['id'],'outcome':'failed',
                    'receipt':receipt,'summary':detail})
                return self.core.read_run(self.root,current['id'])
        except (OSError,ValueError,KeyError):
            return run  # Missing/conflicting evidence remains unknown, no replay.

    def _reconcile_devin_setup_exit(self, run):
        """Read a closed original transport, whose transcript logs intent before sending."""
        from devin_acp import completed_setup_without_prompt
        from execution_lease import FileLock, boot_identity, observe_run
        from harness_package import author_lineage
        try:
            request = self.core.read_request(self.root,run['request_id'])
            with contextlib.closing(self.core._connect(self.root,True)) as c:
                chain = author_lineage(c,request)[-1]
            with FileLock(self.root,['request',chain]):
                current = self.core.read_run(self.root,run['id'])
                native = current.get('native_identity')
                if current['outcome'] not in ('running','unknown') or not native or native.get('turn_id'):
                    return current
                execution = observe_run(self.root,current)
                if execution['state']!='exited':
                    return current
                launch = self._original_launch(current)
                home = Path(launch['evidence_home'])/'runs'/current['id']
                lease = json.loads(scientific_path(self.root,home/'execution.json').read_bytes())
                boot = boot_identity()
                if (not boot or (lease.get('manager') or {}).get('boot')!=boot
                        or (home/'acp-result.json').exists() or any((home/'delivery').glob('operation-*'))):
                    return current
                path = scientific_path(self.root,home/'acp.jsonl')
                raw = path.read_bytes()
                if not raw.endswith(b'\n'):
                    return current
                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                proof = completed_setup_without_prompt(rows,native['id'])
                if not proof:
                    return current
                self.core.recovery_admission(self.root,current['id'],self.actor)
                detail = 'The original Devin transport exited after ACP setup without submitting a prompt.'
                receipt = self._write(launch['evidence_home'],current['id'],{
                    'schema':OBSERVATION_SCHEMA,'source':'adapter_exception','host':current['target_host'],
                    'run_id':current['id'],'request_id':current['request_id'],'operation':current['operation'],
                    'config_sha256':current['config_ref']['sha256'],'observed_at':now(),'native_identity':native,
                    'error_type':'NativeStartRejected','native_status':'not_started','outcome':'failed','detail':detail,
                    'observation':{**proof,'native_submission':'not_started','phase':'before_prompt',
                        'category':'acp_transport_exited_before_prompt','execution_observation':execution,
                        'transcript_ref':{'path':str(path),'sha256':digest(raw)}}})
                self.core.mutate(self.root,'run-outcome',self.actor,{'id':current['id'],'outcome':'failed',
                    'receipt':receipt,'summary':detail})
                return self.core.read_run(self.root,current['id'])
        except (OSError,ValueError,KeyError,TypeError):
            return run

    def readresult(self, *, run_id, config_path, role, evidence_home):
        run = self.core.read_run(self.root, run_id)
        from delivery_evidence import ingest_run
        archive=ingest_run(self.root,run,self.actor)
        if run['target_host'] in ('devin_cli','deepseek_harness') and run['outcome']=='unknown' and not run['native_identity']:
            run=self._reconcile_pre_prompt_start(run)
        if run['target_host']=='devin_cli' and run['outcome'] in ('running','unknown') and run['native_identity']:
            run = self._reconcile_devin_setup_exit(run)
        receipt = run.get('latest_receipt')
        if receipt and run['outcome'] in ('succeeded', 'failed', 'stopped'):
            return self._retained_result(run,archive)
        self.core.recovery_admission(self.root, run_id, self.actor)
        from dsh_capacity import waiting_run
        waiting = waiting_run(self.root, run)
        if waiting:
            return {'run_id': run_id, 'outcome': run['outcome'], 'execution_state': 'queued',
                    'receipt': receipt, 'native_identity': run.get('native_identity'),
                    'stop_requested': run.get('stop_requested', False), 'replay_allowed': False,
                    'source': 'original_capacity_queue', 'capacity': waiting,
                    'reason': 'The original request is queued; admission continues automatically when capacity is available.'}
        native = run.get('native_identity')
        if not native:
            if run['target_host'] == 'deepseek_harness' and run['outcome'] == 'intent':
                from dsh_capacity_handoff import abandoned_transport
                abandoned = abandoned_transport(self.root, run)
                if abandoned:
                    import shlex
                    return {'run_id': run_id, 'outcome': run['outcome'], 'execution_state': 'abandoned_before_start',
                            'receipt': receipt, 'native_identity': None, 'replay_allowed': False,
                            'source': 'original_capacity_and_execution_evidence',
                            'sequence': abandoned['sequence'], 'requested_session_id': abandoned['native_session'],
                            'owner_continue_command': shlex.join(['python3', 'bin/harness.py', 'work-dispatch',
                                '--request', run['request_id'], '--run-id', run_id, '--execute', '--continue']),
                            'reason': 'The original queued intent was never admitted and its transport exited. The responsible actor can explicitly continue this same intent.'}
            from execution_lease import observe_run
            return {'run_id': run_id, 'outcome': run['outcome'], 'execution_state': 'launch_unresolved',
                    'receipt': receipt, 'native_identity': None, 'replay_allowed': False,
                    'source': 'original_launch_observation', 'execution_observation': observe_run(self.root, run),
                    'reason': 'No observed native identity or live queue ticket; inspect original launch evidence before retrying.'}
        config = self.run_config(run, role, config_path)
        recovery = self._recovery_config(run, config)
        adapter = self.adapter_factory(self.root, recovery)
        try:
            self.core.recovery_admission(self.root, run_id, self.actor)
            result = adapter.readresult(native, boundary=lambda: self.core.recovery_boundary(self.root, run_id, self.actor, native))
            if result.get('native_status') == 'unknown':
                # An inconclusive poll is not a transport failure. The original
                # executor still owns its outcome and may be writing a result
                # concurrently; do not replace its receipt or running state.
                current = self.core.read_run(self.root, run_id)
                from execution_lease import observe_run
                execution = observe_run(self.root, current)
                if execution['state'] == 'exited' and current['outcome'] not in ('succeeded', 'failed', 'stopped'):
                    result['execution_observation'] = execution
                    result.setdefault('transport_status', 'interrupted; native turn unresolved')
                    return self._record_result(run_id, recovery, result, evidence_home, observation_only=True)
                return {'run_id': run_id, 'outcome': current['outcome'],
                        'receipt': current.get('latest_receipt'), 'result': result,
                        'native_identity': current.get('native_identity'),
                        'source': 'inconclusive_native_observation', 'execution_observation': execution}
            return self._record_result(run_id, recovery, result, evidence_home, observation_only=True)
        except self.core.WorkConflict as exc:
            if exc.code!='recovery_identity_changed':raise
            current=self.core.read_run(self.root,run_id)
            latest=current.get('native_identity')
            if not latest or not same_actor(latest,native):raise
            # A live Session may publish its turn between our snapshot and the
            # short read boundary. Return that actual fact without probing the
            # stale identity or converting a normal observation into failure.
            if current['outcome'] in ('succeeded','failed','stopped'):
                return self._retained_result(current,archive)
            return {'run_id':run_id,'outcome':current['outcome'],'receipt':current.get('latest_receipt'),
                'native_identity':latest,'source':'concurrent_original_turn_advanced',
                'replay_allowed':False,'reason':'Original native turn advanced before this read was admitted; read the current original receipt.'}
        finally:
            adapter.close()

    def stop(self, *, run_id, config_path, role, evidence_home, reason=None):
        self.core.recovery_admission(self.root, run_id, self.actor)
        run = self.core.read_run(self.root, run_id)
        if run.get('outcome') in ('succeeded', 'failed', 'stopped'):
            if run['outcome'] == 'failed' and run['target_host'] == 'deepseek_harness':
                from dsh_auto_resume import pending_view
                if pending_view(run):
                    self.core.mutate(self.root, 'run-stop', self.actor, {'id':run_id, 'summary':reason or
                        'Original dispatcher stopped the pending DSH recovery.'})
                    from dsh_recovery_host import wake
                    wake(self.root)
                    return {'run_id':run_id, 'outcome':'failed', 'stop_requested':True,
                            'stop_confirmed':False, 'native_terminal':True}
            return {'run_id': run_id, 'outcome': run['outcome'], 'stop_requested': run['stop_requested'],
                    'stop_confirmed': run['outcome'] == 'stopped', 'native_terminal': True,
                    'reason': 'Original native turn has a retained terminal receipt.'}
        self.core.mutate(self.root, 'run-stop', self.actor, {'id': run_id, 'summary': reason or
            'Original dispatcher requested native stop; actual confirmation follows separately.'})
        pending = {'run_id': run_id, 'stop_requested': True, 'stop_confirmed': False}
        if not run.get('native_identity'):
            return {**pending, 'reason': 'Original native identity unavailable; dispatch closed, actual execution remains unknown.'}
        try:
            config = self.run_config(run, role, config_path)
            if run.get('latest_receipt'):
                observed = read_receipt(self.root, run['latest_receipt'])
                if observed.get('runtime_role') and observed['runtime_role'] != role:
                    raise RuntimeError('Original runtime role differs; recover the selected original role.')
        except (ValueError, OSError, entry.ProductionError) as exc:
            return {**pending, 'reason': safe_diagnostic(exc)}
        adapter, created = None, False
        try:
            with self.live_lock:
                adapter = self.live_adapters.get(run_id)
                created = adapter is None
                adapter = adapter or self.adapter_factory(self.root, self._recovery_config(run, config))
                # Keep the actual original host alive until its stop observation
                # has finished; execute's finally uses this same handle lock.
                self.core.recovery_admission(self.root, run_id, self.actor)
                result = adapter.stop(run['native_identity'], boundary=lambda: self.core.recovery_boundary(self.root, run_id, self.actor, run['native_identity']))
            try:
                recorded = self._record_result(run_id, config, result, evidence_home)
            except self.core.WorkConflict as exc:
                if exc.code != 'terminal_run':
                    raise
                terminal = self.core.read_run(self.root, run_id)
                return {**pending, 'outcome': terminal['outcome'], 'native_terminal': True,
                        'stop_confirmed': terminal['outcome'] == 'stopped', 'receipt': terminal['latest_receipt']}
            return {**recorded, 'stop_requested': True, 'stop_confirmed': result.get('stop_confirmed') is True}
        except (CapabilityUnavailable, HostError, OSError) as exc:
            return {**pending, 'reason': safe_diagnostic(exc)}
        finally:
            if created and adapter:
                adapter.close()


def original_arguments(root, args):
    """Derive operational metadata from the selected original request/run.

    Explicit legacy arguments remain supported and are verified by Runtime.
    A repeated default start has the same intent id and cannot launch twice.
    """
    import harness_tasks as tasks
    if args.action == 'config':
        if not args.config or not args.org_role:
            raise RuntimeError('Configuration inspection requires --config and --org-role')
        return args
    run = None
    if args.action in ('readresult', 'stop'):
        if not args.run_id:
            raise RuntimeError('Recover the exact original --run-id')
        run = tasks.read_run(root, args.run_id)
        request = tasks.read_request(root, run['request_id'])
    else:
        if not args.request_id:
            raise RuntimeError('Select the original --request-id')
        request = tasks.read_request(root, args.request_id)
        args.task_id = args.task_id or request['task_id']
        args.input = args.input or request['input_ref']['path']
        args.input_sha256 = args.input_sha256 or request['input_ref']['sha256']
        if not args.config:
            raise RuntimeError('Select the existing qualified --config; no runtime is configured automatically')
    args.org_role = args.org_role or ('scientific_reviewer' if request.get('independent') else request['recipient_role'])
    original_home = Path(request['input_ref']['path']).parent
    if run and run.get('latest_receipt'):
        original_home = Path(run['latest_receipt']['path']).parent
    args.evidence_home = args.evidence_home or str(original_home)
    if not args.run_id:
        config = load_config(root, args.config, args.org_role)
        args.run_id = 'native:' + digest(json.dumps([request['id'], config['config_ref'], args.action,
                        getattr(args, 'previous_run_id', None)], sort_keys=True).encode())[:32]
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(entry.ROOT))
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('config', 'start', 'resume', 'readresult', 'stop'):
        command = sub.add_parser(action)
        command.add_argument('--config')
        command.add_argument('--org-role', choices=ROLES)
        if action == 'config':
            continue
        command.add_argument('--run-id')
        command.add_argument('--evidence-home')
        if action in ('start', 'resume'):
            for key in ('task-id', 'request-id', 'input', 'input-sha256'):
                command.add_argument('--' + key)
            command.add_argument('--plan-revision', type=int)
        if action == 'resume':
            command.add_argument('--previous-run-id', required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=True)
    try:
        args = original_arguments(root, args)
        if args.action == 'config':
            config = load_config(root, args.config, args.org_role)
            result = {**capability(config), 'config_ref': config['config_ref'], 'config_version': config['config_version']}
        else:
            runtime = Runtime(root)
            common = {'config_path': args.config, 'role': args.org_role, 'run_id': args.run_id, 'evidence_home': args.evidence_home}
            if args.action in ('start', 'resume'):
                result = runtime.execute(**common, task_id=args.task_id, request_id=args.request_id,
                    plan_revision=args.plan_revision, input_path=args.input, input_sha256=args.input_sha256,
                    operation=args.action, previous_run_id=getattr(args, 'previous_run_id', None))
            else:
                result = getattr(runtime, args.action)(**common)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (entry.ProductionError, HostError, OSError, ValueError) as exc:
        print(json.dumps({'error': safe_diagnostic(exc), 'action': args.action,
                          'recovery': 'Read original request/run and native receipt before any retry; no replacement dispatch.'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
