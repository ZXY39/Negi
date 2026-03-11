from pydantic import BaseModel


class JsonEnvelope(BaseModel):
    ok: bool = True
