"""Checks the package imports, so `make test` proves the setup works."""

import chaain


def test_package_imports():
    assert chaain.__doc__
