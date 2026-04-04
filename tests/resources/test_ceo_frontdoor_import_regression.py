import importlib


def test_ceo_frontdoor_runner_module_imports() -> None:
    module = importlib.import_module("g3ku.runtime.frontdoor.ceo_runner")
    assert getattr(module, "CeoFrontDoorRunner", None) is not None
