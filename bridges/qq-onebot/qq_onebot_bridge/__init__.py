"""QQ/OneBot reference bridge for the G3KU External Agent API.

An independent application (zero g3ku imports) that connects a NapCat /
LLOneBot / Lagrange OneBot 11 endpoint to g3ku's ``/api/v1`` headless agent
API. Behavior parity table against the legacy China transport QQ semantics
lives in the package README.
"""

__all__ = ["config", "dispatcher", "g3ku_client", "onebot"]
