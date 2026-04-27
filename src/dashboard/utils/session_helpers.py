from core.sessions_store import _iso_elapsed_seconds

def _session_messages_for_client(session: dict) -> list:
    out = []
    for m in session.get('messages', []):
        if m.get('from') not in ('user', 'agent'):
            continue
        item = {
            'from': m.get('from'),
            'text': m.get('text', ''),
        }
        if m.get('plan') is not None:
            item['plan'] = m.get('plan')
        out.append(item)
    return out

def _session_payload_for_client(session: dict) -> dict:
    status = dict(session.get('planningStatus') or {})
    started_at = status.get('requestStartedAt')
    elapsed = _iso_elapsed_seconds(started_at)
    if elapsed is not None:
        status['requestElapsedSeconds'] = elapsed
    return {
        'sessionId': session['id'],
        'status': status.get('stage') or session.get('status') or 'active',
        'messages': _session_messages_for_client(session),
        'plan': session.get('draftPlan') or {'epics': []},
        'planSource': session.get('draftPlanSource'),
        'planningStatus': status,
    }
