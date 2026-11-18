"""One-shot channel bootstrap: exit 0 only once the attachments channel exists."""
import os
import sys

from . import _guild, _prog


def run(token: str | None, bridge_name: str, attachments_name: str, meta_name: str) -> None:
    import discord

    token = token or os.environ.get('DISCORD_TOKEN')
    if not token:
        print('No token. Pass --token-file or set DISCORD_TOKEN.', file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.guilds = True
    client = discord.Client(intents=intents)
    # True only once the channel was found or created; any other exit leaves it False.
    settled = False

    @client.event
    async def on_ready():
        nonlocal settled
        try:
            if not client.guilds:
                print(f'bot is not in any guild — invite it first ({_prog.program_name()} does not handle OAuth flow)', file=sys.stderr)
                return
            guild = _guild.setup_guild(client.guilds, bridge_name)
            # Locate or create Meta category
            meta = discord.utils.find(lambda c: c.name.lower() == meta_name.lower(), guild.categories)
            if meta is None:
                print(f'Meta category {meta_name!r} not found; placing channel at guild root')
            existing = discord.utils.find(lambda c: c.name == attachments_name, guild.text_channels)
            if existing:
                report = f'#{attachments_name} already exists (id={existing.id})'
            else:
                ch = await guild.create_text_channel(
                    attachments_name,
                    category=meta,
                    topic='Binary side-channel for oversized mailbox payloads. See discord_mb.py.',
                    reason='discord_mb.py setup',
                )
                report = f'created #{attachments_name} (id={ch.id})'
            settled = True    # before the report: a failing print leaves the channel in place
            print(report)
        except discord.Forbidden as e:
            print(f'missing perms (need Manage Channels): {e}', file=sys.stderr)
        except Exception as e:
            print(f'setup error: {e}', file=sys.stderr)
        finally:
            await client.close()

    client.run(token, log_handler=None)
    if not settled:
        print('setup finished without finding or creating the attachments channel', file=sys.stderr)
        sys.exit(1)
