"""The Web UI as a managed channel: an on/off switch for chat sign-in (no credentials needed).

People sign in with email and password (see app/chat_auth.py); this entry lets an Admin turn that off.
Until it has been configured once, the Web UI counts as on, so nothing stops working on upgrade.
"""
from app.channels.base import Adapter, Field


class WebAdapter(Adapter):
    name = "web"
    title = "Web UI"
    SPEC = (
        Field("kb_ids", "Knowledge bases", kind="kbs",
              help="Which knowledge bases the Web UI answers from. Empty = the default knowledge base."),
    )

    def test(self) -> tuple[bool, str]:
        return True, "The Web UI needs no credentials. It is available whenever this channel is on."
