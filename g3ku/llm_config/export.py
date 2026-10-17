from __future__ import annotations

from .models import GenericRuntimeConfig, NormalizedProviderConfig

# 唯一的掩码口径：任何"不外发密钥"的读面都发这个值，写回闸门 (`facade._apply_draft_patch`)
# 认的也是它。两处字面量分家时，把红acted 记录读回来再写回去的调用方会把真实 api key
# 覆盖成掩码字符串本身。
MASKED_SECRET_VALUE = "********"


def to_generic_runtime_config(
    config: NormalizedProviderConfig, *, include_secrets: bool = True
) -> GenericRuntimeConfig:
    parameters = dict(config.parameters)
    auth_header = parameters.pop("auth_header", True)
    auth = {
        "type": config.auth.get("type", "api_key"),
        "api_key": config.auth.get("api_key") if include_secrets else MASKED_SECRET_VALUE,
        "auth_header": auth_header,
    }
    return GenericRuntimeConfig(
        provider_id=config.provider_id,
        protocol_adapter=config.protocol_adapter,
        capability=config.capability,
        auth_mode=config.auth_mode,
        connection={"base_url": config.base_url},
        auth=auth,
        defaults={"model": config.default_model},
        parameters=parameters,
        headers=dict(config.headers),
        extra_options=dict(config.extra_options),
    )
