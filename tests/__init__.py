"""Offline, isolated test suite; importing it installs the test guards."""

from tests import isolation, offline

offline.install()
isolation.install()
