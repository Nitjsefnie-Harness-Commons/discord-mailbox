"""Program name."""
import os
import sys


def program_name() -> str:
    name = os.path.basename(sys.argv[0]) if sys.argv else ''
    return name if name and name[0] != '-' else 'discord_mb.py'
