class LlmConfigError(Exception):
    """Base exception for the llm_config package."""


class TemplateNotFoundError(LlmConfigError):
    pass


class ValidationFailedError(LlmConfigError):
    pass


class ProbeRequiredError(LlmConfigError):
    pass


class ProbeFailedError(LlmConfigError):
    pass


class ConfigNotFoundError(LlmConfigError):
    pass


class MissingMasterKeyError(LlmConfigError):
    pass

