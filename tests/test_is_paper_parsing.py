"""IS_PAPER env parsing (P4 audit).

Regression: config/credentials.py parsed IS_PAPER with a strict
`os.getenv("IS_PAPER", "true").lower() == "true"`, so the extremely common
`IS_PAPER=1` (or yes/on/TRUE) silently evaluated to False -- the engine then
ran against the LIVE Alpaca account. Every other boolean in the config layer
(config/params.py `_env_bool`) accepts 1/true/yes/on; IS_PAPER must parse the
same way, and an empty value must fall back to the default (paper), never to
live.
"""
import importlib
import os

import pytest

import config.credentials as cred


@pytest.fixture
def fresh_cred(monkeypatch):
    monkeypatch.delenv("IS_PAPER", raising=False)
    yield monkeypatch
    os.environ.pop("IS_PAPER", None)
    importlib.reload(cred)


def _is_paper_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("IS_PAPER", raising=False)
    else:
        monkeypatch.setenv("IS_PAPER", value)
    return importlib.reload(cred).IS_PAPER


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " true "])
def test_truthy_values_mean_paper(fresh_cred, v):
    assert _is_paper_with(fresh_cred, v) is True


@pytest.mark.parametrize("v", ["0", "false", "FALSE", "no", "off", "nonsense"])
def test_falsy_values_mean_live(fresh_cred, v):
    assert _is_paper_with(fresh_cred, v) is False


def test_unset_defaults_to_paper(fresh_cred):
    assert _is_paper_with(fresh_cred, None) is True


def test_empty_falls_back_to_default_paper(fresh_cred):
    # IS_PAPER= (empty assignment) must not silently flip the account to live.
    assert _is_paper_with(fresh_cred, "") is True
