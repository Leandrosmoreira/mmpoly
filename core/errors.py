"""Standardized error codes for structured logging.

Categories:
  E1xxx — Connection / WebSocket
  E2xxx — Orders (place, cancel, reject)
  E3xxx — Data (book, inventory, snapshots)
  E4xxx — Risk (kill switch, limits, phantom fills)
  E5xxx — Scanner / Discovery
"""

from enum import Enum


class ErrorCode(str, Enum):
    # --- Connection (1xxx) ---
    WS_DISCONNECTED = "E1001"
    WS_CONNECTION_ERROR = "E1002"
    WS_MESSAGE_ERROR = "E1003"
    WS_SUBSCRIBE_ERROR = "E1004"
    API_CONNECTION_FAILED = "E1005"
    API_DERIVE_KEY_FAILED = "E1006"
    BINANCE_WS_DISCONNECTED = "E1010"
    BINANCE_WS_ERROR = "E1011"

    # --- Orders (2xxx) ---
    ORDER_REJECTED = "E2001"
    ORDER_PLACE_FAILED = "E2002"
    CANCEL_FAILED = "E2003"
    CANCEL_ERROR = "E2004"
    CANCEL_ALL_ERROR = "E2005"
    CANCEL_RATE_LIMITED = "E2006"
    GET_BOOK_ERROR = "E2007"

    # --- Data (3xxx) ---
    BOOK_STALE = "E3001"
    BOOK_INVALID = "E3002"
    INVENTORY_SNAPSHOT_FAILED = "E3003"
    INVENTORY_LOAD_FAILED = "E3004"
    LOGGING_ERROR = "E3005"

    # --- Risk (4xxx) ---
    KILL_SWITCH_PNL = "E4001"
    KILL_SWITCH_REJECTS = "E4002"
    KILL_SWITCH_CONSEC_LOSSES = "E4003"
    HARD_LIMIT_BREACHED = "E4004"
    PHANTOM_FILL_BLOCKED = "E4005"
    PHANTOM_INVENTORY_ZEROED = "E4006"

    # --- Scanner (5xxx) ---
    GAMMA_API_TIMEOUT = "E5001"
    GAMMA_API_ERROR = "E5002"
    DISCOVER_MARKET_ERROR = "E5003"
    SCANNER_LOOP_ERROR = "E5004"
    INVALID_MARKET_TOKENS = "E5005"

    # --- Reconciliation (5xxx) ---
    ORPHAN_ORDER_DETECTED = "E5010"
    RECONCILE_ERROR = "E5011"
    RECONCILE_CANCEL_ALL_SAFETY = "E5012"

    # --- Adverse movement (4xxx cont.) ---
    ADVERSE_MOVEMENT = "E4010"
    KILL_MID_EXECUTION = "E4011"   # BUG-019: kill switch triggered during intent execution
    SELL_ALLOWANCE_ERROR = "E4012" # BUG-017: SELL failed due to token approval, not phantom
    ZERO_SIDE_BLOCKED = "E4013"    # BUG-017: zero_side blocked (shares from live fills)

    TOKEN_APPROVAL_FAILED = "E4014"   # BUG-020: token approval failed
    CROSSES_BOOK_CLAMPED = "E2010"   # BUG-021: order price clamped to avoid crossing book
    CROSSES_BOOK_SKIPPED = "E2011"   # BUG-021: order skipped — no valid price after clamp
    RESIDUAL_SELL_FOK = "E2012"      # BUG-023: residual shares < min sold via FOK
    ORDER_SIZE_TOO_SMALL = "E2013"   # BUG-023: BUY size < min, skipped
    EXIT_DUMP_FOK = "E2014"          # BUG-028: exit dump — FOK sell at expiry
    APPROVAL_CACHE_STALE = "E2015"   # BUG-024: approval cache invalidated on SELL failure
    BUYS_BLOCKED_SIDE_LOSS = "E2016" # BUG-026: buys blocked — side already lost money
    BUYS_BLOCKED_PRICE_FLOOR = "E2017"  # BUG-025: buys blocked — token price below floor

    SELL_FAIL_LIMIT = "E2018"         # BUG-032: repeated approval-sell failures → phantom
    APPROVAL_COOLDOWN_ACTIVE = "E2019"  # BUG-032: approval retry skipped (cooldown)
    ADVERSE_SELL_FOK = "E2020"       # BUG-033: adverse emergency sell via FOK
    ADVERSE_FOK_EXHAUSTED = "E2021"  # BUG-033: FOK attempts exhausted, switching POST_ONLY
    ADVERSE_SELL_FLOORED = "E2022"   # BUG-034: sell price floored at max_loss_per_share
    ADVERSE_COOLDOWN = "E2023"       # BUG-035: adverse cooldown active, blocking re-entry
    BUY_BLOCKED_NOT_PERSISTED = "E3006"  # BUG-036: buy_blocked state lost on restart

    # --- Bot lifecycle (6xxx) ---
    TICK_ERROR = "E6001"
    SCANNER_ERROR = "E6002"
