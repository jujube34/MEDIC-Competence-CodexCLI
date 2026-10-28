"""Minimal runtime profile switches.

The lite profile keeps the assessment graph and evidence validation, but turns
off optional machinery that is not required to run a complete 17-item pass:
mailbox conversations and the third business-rework round.
"""
import os


def enabled():
    return os.environ.get('MEDIC_LITE', '').strip().lower() in ('1', 'true', 'on', 'yes')
