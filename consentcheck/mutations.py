"""Pure, deterministic mutations of a later execution request."""

from schemas import ActionProposal, AttackType


def recipient_substitution(action: ActionProposal) -> ActionProposal:
    return action.model_copy(update={"recipient": "attacker@example.com"})


def content_substitution(action: ActionProposal) -> ActionProposal:
    # Identity and filename stay fixed; only the underlying content changes.
    return action.model_copy(update={"document_content": "This is malicious substituted content."})


def replay(action: ActionProposal) -> ActionProposal:
    return action.model_copy()


def cross_task_reuse(action: ActionProposal) -> ActionProposal:
    return action.model_copy(update={"task_id": action.task_id + "__different_task"})


MUTATIONS = {
    "recipient_substitution": recipient_substitution,
    "content_substitution": content_substitution,
    "replay": replay,
    "cross_task_reuse": cross_task_reuse,
}


def mutate(action: ActionProposal, attack_type: AttackType) -> ActionProposal:
    return MUTATIONS[attack_type](action)
