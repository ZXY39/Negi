from __future__ import annotations

from g3ku.config.schema import Config


def test_config_accepts_china_bridge_channels_only():
    cfg = Config.model_validate(
        {
            "models": {
                "catalog": [
                    {
                        "key": "m",
                        "providerModel": "openai:gpt-4.1",
                        "apiKey": "demo-key",
                        "enabled": True,
                        "maxTokens": 1,
                        "temperature": 0.1,
                        "retryOn": [],
                        "description": "",
                    }
                ],
                "roles": {"ceo": ["m"], "execution": ["m"], "inspection": ["m"]},
            },
            "chinaBridge": {
                "enabled": True,
                "sendProgress": True,
                "sendToolHints": False,
                "channels": {
                    "qqbot": {"enabled": True, "appId": "123"},
                    "dingtalk": {"enabled": True, "clientId": "ding-demo"},
                    "wecom": {"enabled": True},
                    "wecomApp": {"enabled": True},
                    "feishuChina": {"enabled": True},
                },
            },
        }
    )

    assert cfg.china_bridge.channels.qqbot.enabled is True
    assert cfg.china_bridge.channels.dingtalk.enabled is True
    assert cfg.china_bridge.channels.feishu_china.enabled is True
