import zento


def test_package_imports_with_version() -> None:
    assert zento.__version__ == "0.1.0"
