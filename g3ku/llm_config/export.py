from __future__ import annotations

from .models import GenericRuntimeConfig, NormalizedProviderConfig


def to_generic_runtime_config(
    config: NormalizedProviderConfig, *, include_secrets: bool = True
) -> GenericRuntimeConfig:
    parameters = dict(config.parameters)
    timeout_s = parameters.pop("timeout_s", 8)
    auth_header = parameters.pop("auth_header", True)
    auth = {
        "type": config.auth.get("type", "api_key"),
        "api_key": config.auth.get("api_key") if include_secrets else "***",
        "auth_header": auth_header,
    }
    return GenericRuntimeConfig(
        provider_id=config.provider_id,
        protocol_adapter=config.protocol_adapter,
        capability=config.capability,
        auth_mode=config.auth_mode,
        connection={"base_url": config.base_url, "timeout_s": timeout_s},
        auth=auth,
        defaults={"model": config.default_model},
        parameters=parameters,
        headers=dict(config.headers),
        extra_options=dict(config.extra_options),
    )
