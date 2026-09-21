import payment_module


def test_package_exposes_version() -> None:
    assert payment_module.__version__ == "0.1.0.dev0"
