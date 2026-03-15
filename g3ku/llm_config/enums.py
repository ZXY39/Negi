from __future__ import annotations

from enum import Enum


class ProtocolAdapter(str, Enum):
    OPENAI_COMPLETIONS = "openai-completions"
    OPENAI_RESPONSES = "openai-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"
    OLLAMA = "ollama"
    DASHSCOPE_EMBEDDING = "dashscope-embedding"
    DASHSCOPE_RERANK = "dashscope-rerank"
    CUSTOM_DIRECT = "custom-direct"
    OAUTH_PROXY = "oauth-proxy"


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
    EMBEDDING = "embedding"
    RERANK = "rerank"


class AuthMode(str, Enum):
    API_KEY = "api_key"
    TOKEN = "token"
    OAUTH_CACHE = "oauth_cache"
    NONE = "none"
