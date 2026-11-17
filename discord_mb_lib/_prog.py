"""The command name the operator invoked, for help and recovery hints."""
import os
import sys

FACADE_NAME = 'discord_mb.py'


def program_name() -> str:
    """`discord-mb` through the console script, `discord_mb.py` through the facade.

    The name is the basename of `sys.argv[0]`, as argparse derives it; an empty
    argv or an interpreter flag (`python -c` leaves `-c`) names the facade.
    """
    name = os.path.basename(sys.argv[0]) if sys.argv else ''
    return name if name and not name.startswith('-') else FACADE_NAME
