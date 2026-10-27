"""Fixture boundaries for tests of historical records and execution in isolation.

New-policy tests must call services.propose_operation directly. Legacy fixtures
represent rows already pending before the policy change, not the public API.
"""
from dataclasses import replace
from unittest.mock import patch

from apps.planner import services
from apps.planner.operations import get


def legacy_proposal(user, **kwargs):
    spec = get(kwargs['intent'])
    if spec is None or spec.requires_confirmation:
        return services.propose_operation(user, **kwargs)
    legacy = replace(spec, auto_execute=False, requires_confirmation=True)
    with patch.object(services, 'get_spec', side_effect=lambda intent: legacy if intent == spec.intent else get(intent)):
        return services.propose_operation(user, **kwargs)


def prepare_automatic_operation(user, **kwargs):
    # Pause before dispatch so a test can set up a racing change or assert CAS.
    # The real new policy has already set ARMED, zero confirmations and TTL.
    with patch.object(services, '_auto_execute_proposal', side_effect=lambda actor, row, ticket, payload: payload):
        return services.propose_operation(user, **kwargs)
