import mavis


def test_package_imports_with_version() -> None:
    assert mavis.__version__ == "0.1.0"
