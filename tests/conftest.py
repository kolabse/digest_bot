import os

# Exercise python-telegram-bot's future RetryAfter timedelta behavior without warnings.
os.environ.setdefault("PTB_TIMEDELTA", "1")
