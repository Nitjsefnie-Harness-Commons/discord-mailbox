#!/usr/bin/env python3
"""The agent directory: what an entry names, who holds a name, and how much of
the channel each read sees.

The pure half drives `discord_mb_lib._directory` directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402

sys.path.insert(0, _util.SCRIPTS)
from discord_mb_lib._directory import _Directory as D  # noqa: E402

M, L = D.MAP_SEPARATORS, D.LIST_SEPARATORS


def test_an_entry_names_its_first_mention_and_bold_token_or_separated_word(tmp):
    rows = [  # content, map result, list-agents result
        ("**alpha** · <@11>", ("alpha", 11), ("alpha", 11)),
        ("<@!11> then **alpha** and **beta**", ("alpha", 11), ("alpha", 11)),
        ("**alpha** <@11> <@22>", ("alpha", 11), ("alpha", 11)),
        ("**alpha**", None, None),
        (None, None, None),
        ("** ** <@11>", None, None),
        ("alpha: <@11>", ("alpha", 11), ("alpha", 11)),
        ("\n  - alpha = x\n<@11>", ("alpha", 11), ("alpha", 11)),
        ("no separator here\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        (": empty\nalpha: <@11>", ("alpha", 11), ("alpha", 11)),
        # The one difference: only the map splits on the header's ' · '.
        ("alpha · <@11>", ("alpha", 11), None),
    ]
    for content, mapped, listed in rows:
        assert D.parse_entry(content, M) == mapped, (content, D.parse_entry(content, M))
        assert D.parse_entry(content, L) == listed, (content, D.parse_entry(content, L))


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="directory_")


if __name__ == "__main__":
    raise SystemExit(main())
