from __future__ import annotations

from enum import Enum


class ProtocolAdapter(str, Enum):
    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"


class FieldInputType(str, Enum):
    TEXT = "text"
    SECRET = "secret"
    URL = "url"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    JSON = "json"
    KV_LIST = "kv-list"


class ProbeStatus(str, Enum):
    SUCCESS = "success"
    AUTH_ERROR = "auth_error"
    CONNECTION_ERROR = "connection_error"
    INVALID_RESPONSE = "invalid_response"
    TIMEOUT = "timeout"


class Capability(str, Enum):
    CHAT = "chat"


class AuthMode(str, Enum):
    API_KEY = "api_key"
    TOKEN = "token"
    NONE = "none"
