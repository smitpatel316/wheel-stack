from dotenv import load_dotenv
import os

load_dotenv()  # Load from .env file in root; real environment variables win over .env entries

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def _env_flag(name, default):
    """Parse a boolean env flag.

    Accepts 1/true/yes/on (any case, surrounding whitespace tolerated) as
    true -- the same convention as config/params.py `_env_bool`. An unset or
    empty value falls back to *default*: an empty assignment must never
    silently flip a paper/live switch.
    """
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# IS_PAPER must parse like every other boolean in the config layer.
# 2026-09-09 (P4 audit): the old strict `== "true"` made IS_PAPER=1/yes/on
# silently select LIVE trading -- a real-money footgun.
IS_PAPER = _env_flag("IS_PAPER", True)
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "***REMOVED***3g***REMOVED***40")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "***REMOVED***")
# For core modules that use get_api_key()
def get_finnhub_key():
    return FINNHUB_API_KEY
def get_alpha_key():
    return ALPHA_VANTAGE_API_KEY
