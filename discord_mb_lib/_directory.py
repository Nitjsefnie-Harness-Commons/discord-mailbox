"""The agent directory: the identity name and user id a directory-channel message names."""
import re


class _Directory:
    # The map also splits on ' · ', the separator of the `**name** · <@id>` header.
    MAP_SEPARATORS = (':', '=', ' · ')
    LIST_SEPARATORS = (':', '=')

    @staticmethod
    def parse_entry(content: str | None, separators: tuple[str, ...]) -> tuple[str, int] | None:
        """(name, mentioned user id), or None. The id is the first `<@id>` mention; the
        name is the first **bold** token, else the text before the first separator on
        the first non-empty line that holds one."""
        content = content or ''
        uid_match = re.search(r'<@!?(\d+)>', content)
        if not uid_match:
            return None
        name = None
        bold = re.search(r'\*\*([^*\n]+)\*\*', content)
        if bold:
            name = bold.group(1).strip()
        else:
            for line in content.splitlines():
                line = line.strip().lstrip('-').strip()
                if not line:
                    continue
                for sep in separators:
                    if sep in line:
                        name = line.split(sep, 1)[0].strip()
                        break
                if name:
                    break
        return (name, int(uid_match.group(1))) if name else None
