"""Secrets supplied off the command line."""
import os
import sys
from collections.abc import Callable
from getpass import getpass
from pathlib import Path

from ._settings import renamed


def _warn_argv(what: str, instead: str) -> None:
    # stdout carries the connector's event stream
    print(f'warning: {what} is on the command line, where other local users can read it; use {instead}',
          file=sys.stderr, flush=True)


def read_token_file(path: str) -> str:
    try:
        token = Path(path).read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError) as e:
        raise SystemExit(f'error: cannot read token file {path}: '
                         f'{getattr(e, "strerror", None) or type(e).__name__}') from None
    if not token:
        raise SystemExit(f'error: token file {path} is empty')
    return token


def cli_token(token: str | None, token_file: str | None) -> str | None:
    """None leaves DISCORD_MAILBOX_DISCORD_TOKEN and the token directory to the command."""
    if token:
        _warn_argv('--token', '--token-file or DISCORD_MAILBOX_DISCORD_TOKEN')
        return token
    return read_token_file(token_file) if token_file else None


def connector_token(token: str | None, identity: str, flavor: str | None,
                    find_in_directory: Callable[[str, str | None], tuple[str | None, str]]
                    ) -> tuple[str | None, str]:
    """The caller passes its own directory lookup, so a facade TOKEN_DIR patch reaches it."""
    token = token or renamed(os.environ.get, 'DISCORD_TOKEN')
    if not token:
        return find_in_directory(identity, flavor)
    return token, flavor or 'claude'    # a caller that names no flavor gets Claude's


def cli_password(password: str | None, from_stdin: bool) -> str:
    if password is not None and from_stdin:
        raise SystemExit('error: pass the password argument or --password-stdin, not both')
    if password is not None:
        _warn_argv('the password argument', '--password-stdin or the prompt')
    elif from_stdin:
        password = sys.stdin.read().removesuffix('\n')
    elif sys.stdin.isatty():
        password = getpass('password: ')
    else:
        raise SystemExit('error: no password: pass the password argument or --password-stdin '
                         '(stdin is not a terminal, so there is no prompt)')
    if not password:
        raise SystemExit('error: the password is empty')
    return password
