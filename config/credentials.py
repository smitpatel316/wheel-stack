from dotenv import load_dotenv
import os

load_dotenv(override=True)  # Load from .env file in root

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
IS_PAPER = os.getenv("IS_PAPER", "true").lower() == "true"
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "***REMOVED***3g***REMOVED***40")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "***REMOVED***")
# For core modules that use get_api_key()
def get_finnhub_key():
    return FINNHUB_API_KEY
def get_alpha_key():
    return ALPHA_VANTAGE_API_KEY
