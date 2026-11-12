"""Allowed events (generated from docs/spec/audit/schemas/event-v1.json; tests/test_audit_capture.py keeps it in step).
ACTIONS maps action -> (required data keys, optional data keys, required envelope keys beyond the base ones)."""

ENVELOPE = ("v", "id", "t", "action", "actor", "origin", "provenance", "pro", "pro_name", "run", "session", "msg", "data")
ENVELOPE_REQUIRED = ("v", "id", "t", "action", "actor", "origin", "data")
ACTORS = ("user", "professional", "system", "hook", "pump", "operator", "importer")

ACTIONS = {
    'msg_created': (('body_bytes', 'chain_len', 'deadline', 'from_role', 'kind', 'parent', 'project', 'task', 'to'), ('from_pro', 'origin_session', 'task_form'), ('msg',)),
    'msg_claimed': (('attempt', 'via'), (), ('msg',)),
    'msg_injected': (('attempt', 'dup', 'tmux', 'via'), (), ('msg', 'pro', 'run', 'session')),
    'msg_answered': (('answered_by', 'reply_msg'), (), ('msg',)),
    'msg_read': (('reader',), (), ('msg',)),
    'msg_failed': (('attempts', 'reason'), (), ('msg',)),
    'msg_overdue': (('deadline', 'observed_by'), (), ('msg',)),
    'msg_refused': (('reason', 'task', 'to'), ('task_form',), ()),
    'wait_start': (('caller', 'target_msg', 'task', 'timeout_s', 'wait'), ('task_form',), ()),
    'wait_end': (('dur_ms', 'exit', 'outcome', 'returned', 'returned_msg', 'wait'), (), ()),
    'spawn': (('kind', 'live_count', 'origin', 'parent_session', 'revision', 'scope', 'tmux'), (), ('pro', 'run', 'session')),
    'session_end': (('answered', 'failed', 'reason', 'timeout'), (), ('pro', 'run')),
    'hook_session': (('deduped', 'hook', 'transcript_ref'), (), ('session',)),
    'rate': (('allowed', 'limit', 'live', 'live_limit', 'summons_last_hour'), (), ()),
    'registry_change': (('change', 'origin', 'revision'), ('renamed_from',), ()),
    'reap': (('reason',), (), ()),
    'audit_config': (('setting', 'value'), (), ()),
    'audit_import': (('batch', 'duplicates', 'events', 'gaps', 'last_seq', 'spool_files'), ('conflicts', 'purged_skipped'), ()),
    'audit_gap': (('count', 'from_t', 'kind', 'source', 'to_t'), (), ()),
    'audit_evaluation': (('criteria', 'effective_at', 'eval', 'evaluator', 'evaluator_kind', 'evaluator_version', 'rubric', 'rubric_version', 'status', 'supersedes', 'target', 'target_kind'), ('evidence',), ('msg',)),
    'audit_export': (('export', 'format', 'manifest_sha256', 'mode', 'rows', 'schema_version'), (), ()),
    'audit_purge': (('before', 'class', 'reason', 'rows'), (), ()),
    'audit_correction': (('fields', 'kind', 'target_event'), (), ()),
    'audit_migrate': (('from_version', 'outcome', 'to_version'), (), ()),
}
