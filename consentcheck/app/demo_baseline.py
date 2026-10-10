"""Deliberately vulnerable comparison ONLY. No imports, persistence, or executor access."""


def evaluate(displayed, attempted, approval_claim):
    """Naively trust a display's approval claim without binding execution fields."""
    if displayed.get('recipient') != 'alice@example.test' or attempted.get('recipient') not in {
        'alice@example.test', 'bob@example.test'
    }:
        raise ValueError('Synthetic scenario recipients only')
    return {'accepted': approval_claim is True, 'simulated_only': True,
            'displayed_recipient': displayed['recipient'], 'would_send_to': attempted['recipient'],
            'would_send_body': attempted['body'],
            'explanation': 'Isolated vulnerable model trusts an approval flag/display without binding the actual operation. No send performed.'}
