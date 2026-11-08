"""Channel identities resolve to configured persistent sessions."""

from solakit.core.config import Config
from solakit.core.schemas import Session
from solakit.core.store import Store


class SessionRouter:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    def channel(
        self, channel_type: str, channel_id: str, *, source: str | None = None
    ) -> Session:
        for channel in self.config.channels:
            if (channel.type, channel.channel_id) == (channel_type, channel_id):
                return self.store.create_session(
                    Session(
                        id=channel.session,
                        title=channel.id,
                        source=source or channel_type,
                        bound_channel=channel.id,
                        default_profile=channel.profile,
                    )
                )
        raise KeyError("channel is not configured")
