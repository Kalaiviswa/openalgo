"""FivepaisaXTS broker base URLs configuration."""

# Base URL for FivepaisaXTS API endpoints
BASE_URL = "https://xtsmum.5paisa.com"

# Derived URLs for specific API endpoints
MARKET_DATA_URL = f"{BASE_URL}/apimarketdata"
# Live ticks: 5paisa streams them on /feedmarketdata, with the same market data
# session as /apimarketdata. /apimarketdata's socket carries no ticks (#2493).
FEED_MARKET_DATA_URL = f"{BASE_URL}/feedmarketdata"
INTERACTIVE_URL = f"{BASE_URL}/interactive"
