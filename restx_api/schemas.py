from marshmallow import EXCLUDE, Schema, ValidationError, fields, post_load, pre_load, validate

from utils.constants import CRYPTO_EXCHANGES, VALID_EXCHANGES


def _coerce_quantity_to_int(data):
    """Convert quantity from float to int for non-crypto exchanges.

    Raises ValidationError if a fractional quantity (e.g. 1.9) is sent
    to a non-crypto exchange, since brokers like Zerodha only accept integers.
    """
    if data.get("exchange") not in CRYPTO_EXCHANGES and "quantity" in data:
        qty = data["quantity"]
        if qty != int(qty):
            raise ValidationError(
                {"quantity": [f"Fractional quantity ({qty}) is not allowed for non-crypto exchanges."]}
            )
        data["quantity"] = int(qty)
    return data


class OrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Float(
        required=True, validate=validate.Range(min=0, min_inclusive=False, error="Quantity must be a positive number.")
    )
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(missing="MIS", validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )
    underlying_ltp = fields.Float(
        missing=None, allow_none=True
    )  # Optional: passed from options order for execution reference

    @post_load
    def coerce_quantity(self, data, **kwargs):
        return _coerce_quantity_to_int(data)


class SmartOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Float(
        required=True,
        validate=validate.Range(min=0, error="Quantity must be a non-negative number."),
    )
    position_size = fields.Float(required=True)
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(missing="MIS", validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )

    @post_load
    def coerce_quantity(self, data, **kwargs):
        return _coerce_quantity_to_int(data)


class ModifyOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    orderid = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    product = fields.Str(required=True, validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    pricetype = fields.Str(
        required=True, validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    price = fields.Float(
        required=True, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    quantity = fields.Float(
        required=True, validate=validate.Range(min=0, min_inclusive=False, error="Quantity must be a positive number.")
    )
    disclosed_quantity = fields.Int(
        required=True,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )
    trigger_price = fields.Float(
        required=True,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )

    @post_load
    def coerce_quantity(self, data, **kwargs):
        return _coerce_quantity_to_int(data)


class CancelOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    orderid = fields.Str(required=True)


class ClosePositionSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)


class CancelAllOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)


class BasketOrderItemSchema(Schema):
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Float(
        required=True, validate=validate.Range(min=0, min_inclusive=False, error="Quantity must be a positive number.")
    )
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(missing="MIS", validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )

    @post_load
    def coerce_quantity(self, data, **kwargs):
        return _coerce_quantity_to_int(data)


class BasketOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    orders = fields.List(
        fields.Nested(BasketOrderItemSchema), required=True
    )  # List of order details


class SplitOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Float(
        required=True,
        validate=validate.Range(min=0, min_inclusive=False, error="Total quantity must be a positive number."),
    )  # Total quantity to split
    splitsize = fields.Int(
        required=True,
        validate=validate.Range(min=1, error="Split size must be a positive integer."),
    )  # Size of each split
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(missing="MIS", validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )

    @post_load
    def coerce_quantity(self, data, **kwargs):
        return _coerce_quantity_to_int(data)


class OptionsOrderSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    underlying = fields.Str(
        required=True
    )  # Underlying symbol (NIFTY, BANKNIFTY, RELIANCE, or NIFTY28NOV24FUT)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))  # Exchange (NSE_INDEX, NSE, BSE_INDEX, BSE, NFO, BFO)
    expiry_date = fields.Str(
        required=False
    )  # Optional if underlying includes expiry (DDMMMYY format)
    strike_int = fields.Int(
        required=False, validate=validate.Range(min=1), allow_none=True
    )  # OPTIONAL: Strike interval. If not provided, actual strikes from database will be used (RECOMMENDED for accuracy)
    offset = fields.Str(required=True)  # ATM, ITM1-ITM50, OTM1-OTM50
    option_type = fields.Str(
        required=True, validate=validate.OneOf(["CE", "PE", "ce", "pe"])
    )  # Call or Put
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Int(
        required=True, validate=validate.Range(min=1, error="Quantity must be a positive integer.")
    )
    splitsize = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Split size must be a non-negative integer."),
        allow_none=True,
    )  # Optional: If > 0, splits order into multiple orders of this size
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(
        missing="MIS", validate=validate.OneOf(["MIS", "NRML"])
    )  # Options only support MIS and NRML
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )


class OptionsMultiOrderLegSchema(Schema):
    """Schema for a single leg in options multi-order (no symbol - resolved from offset)"""

    offset = fields.Str(required=True)  # ATM, ITM1-ITM50, OTM1-OTM50
    option_type = fields.Str(
        required=True, validate=validate.OneOf(["CE", "PE", "ce", "pe"])
    )  # Call or Put
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Int(
        required=True, validate=validate.Range(min=1, error="Quantity must be a positive integer.")
    )
    splitsize = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Split size must be a non-negative integer."),
        allow_none=True,
    )  # Optional: If > 0, splits leg into multiple orders of this size
    expiry_date = fields.Str(
        required=False
    )  # Optional per-leg expiry (DDMMMYY format) - for diagonal/calendar spreads
    pricetype = fields.Str(
        missing="MARKET", validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    product = fields.Str(
        missing="MIS", validate=validate.OneOf(["MIS", "NRML"])
    )  # Options only support MIS and NRML
    price = fields.Float(
        missing=0.0, validate=validate.Range(min=0, error="Price must be a non-negative number.")
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be a non-negative number."),
    )
    disclosed_quantity = fields.Int(
        missing=0,
        validate=validate.Range(min=0, error="Disclosed quantity must be a non-negative integer."),
    )


class OptionsMultiOrderSchema(Schema):
    """Schema for options multi-order with multiple legs sharing common underlying"""

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    underlying = fields.Str(required=True)  # Underlying symbol (NIFTY, BANKNIFTY, RELIANCE)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))  # Exchange (NSE_INDEX, NSE, BSE_INDEX, BSE)
    expiry_date = fields.Str(
        required=False
    )  # Optional if underlying includes expiry (DDMMMYY format)
    strike_int = fields.Int(
        required=False, validate=validate.Range(min=1), allow_none=True
    )  # Optional strike interval
    legs = fields.List(
        fields.Nested(OptionsMultiOrderLegSchema),
        required=True,
        validate=validate.Length(min=1, max=20, error="Legs must contain 1 to 20 items."),
    )


class SyntheticFutureSchema(Schema):
    """Schema for synthetic future calculation"""

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    underlying = fields.Str(required=True)  # Underlying symbol (NIFTY, BANKNIFTY, RELIANCE)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))  # Exchange (NSE_INDEX, NSE, BSE_INDEX, BSE)
    expiry_date = fields.Str(required=True)  # Expiry date in DDMMMYY format (e.g., 28OCT25)


class MarginPositionSchema(Schema):
    """Schema for a single position in margin calculation"""

    symbol = fields.Str(
        required=True,
        validate=validate.Length(
            min=1, max=50, error="Symbol must be between 1 and 50 characters."
        ),
    )
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    quantity = fields.Str(required=True)  # String to match API contract, validated in service layer
    product = fields.Str(required=True, validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    pricetype = fields.Str(
        required=True, validate=validate.OneOf(["MARKET", "LIMIT", "SL", "SL-M"])
    )
    price = fields.Str(missing="0")  # String to match API contract
    trigger_price = fields.Str(missing="0")  # String to match API contract


class MarginCalculatorSchema(Schema):
    """Schema for margin calculator request"""

    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256, error="API key must be between 1 and 256 characters.")
    )
    positions = fields.List(
        fields.Nested(MarginPositionSchema),
        required=True,
        validate=validate.Length(min=1, max=50, error="Positions must contain 1 to 50 items."),
    )


# -----------------------------------------------------------------------------
# GTT (Good Till Triggered) Schemas
# -----------------------------------------------------------------------------

def _validate_gtt_place_request(data):
    """Validate flat GTT-place fields and normalise.

    SINGLE: a single trigger value is required. Callers may pass it as any of
    ``trigger_price``, ``stoploss``, or ``target`` — whichever is non-zero is
    used as the trigger. Direction (above/below LTP) is inferred by the broker
    from the trigger value vs current price. ``price`` is the order's limit.
    OCO: ``stoploss`` (stoploss trigger) + ``price`` (stoploss leg limit) and
         ``trigger_price`` (target trigger) + ``target`` (target leg limit).

    On SINGLE the unused trigger fields are cleared so downstream consumers
    can rely on ``trigger_price`` being the resolved trigger and stoploss/
    target being absent.
    """
    trigger_type = (data.get("trigger_type") or "").upper()
    if trigger_type not in ("SINGLE", "OCO"):
        raise ValidationError({"trigger_type": ["Must be 'SINGLE' or 'OCO'."]})
    data["trigger_type"] = trigger_type

    if trigger_type == "OCO":
        stoploss = data.get("stoploss")
        target = data.get("target")
        trigger_price = data.get("trigger_price")
        if stoploss in (None, 0, 0.0):
            raise ValidationError({"stoploss": ["Required for OCO (stoploss trigger price)."]})
        if target in (None, 0, 0.0):
            raise ValidationError({"target": ["Required for OCO (target leg limit price)."]})
        if trigger_price in (None, 0, 0.0):
            raise ValidationError({"trigger_price": ["Required for OCO (target trigger price)."]})
        if float(stoploss) >= float(trigger_price):
            raise ValidationError({
                "stoploss": [
                    "Stoploss trigger must be less than the target trigger (trigger_price)."
                ]
            })
    else:  # SINGLE — pick the first non-zero trigger from trigger_price / stoploss / target.
        candidates = [
            data.get("trigger_price"),
            data.get("stoploss"),
            data.get("target"),
        ]
        resolved = next(
            (float(c) for c in candidates if c not in (None, 0, 0.0)),
            None,
        )
        if resolved is None or resolved <= 0:
            raise ValidationError({
                "trigger_price": [
                    "SINGLE GTT requires a positive trigger value via trigger_price, stoploss, or target."
                ]
            })
        data["trigger_price"] = resolved
        data["stoploss"] = None
        data["target"] = None

    exchange = data.get("exchange")
    qty = data.get("quantity")
    if qty is not None and exchange and exchange not in CRYPTO_EXCHANGES:
        if qty != int(qty):
            raise ValidationError({
                "quantity": [f"Fractional quantity ({qty}) is not allowed for non-crypto exchanges."]
            })
        data["quantity"] = int(qty)

    data["action"] = data["action"].upper()
    return data


class PlaceGTTOrderSchema(Schema):
    """Schema for placing a GTT in the flat shape.

    Required fields (all GTTs): apikey, strategy, trigger_type ('SINGLE' or
    'OCO'), exchange, symbol, action, product, quantity, pricetype, price,
    trigger_price.

    OCO-only: ``stoploss`` (stoploss trigger) and ``target`` (target leg's
    limit price). For SINGLE these may be omitted or sent as empty strings.

    ``last_price`` is fetched server-side from the quotes API and should not
    be sent by clients.
    """

    class Meta:
        unknown = EXCLUDE

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    trigger_type = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    product = fields.Str(required=True, validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    quantity = fields.Float(
        required=True,
        validate=validate.Range(min=0, min_inclusive=False, error="Quantity must be a positive number."),
    )
    pricetype = fields.Str(missing="LIMIT", validate=validate.OneOf(["LIMIT", "MARKET"]))
    price = fields.Float(
        required=True,
        validate=validate.Range(min=0, error="Price must be a non-negative number."),
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be non-negative."),
    )
    stoploss = fields.Float(missing=None, allow_none=True)
    target = fields.Float(missing=None, allow_none=True)
    expires_at = fields.Str(missing=None, allow_none=True)

    @pre_load
    def coerce_empty_to_none(self, data, **kwargs):
        if isinstance(data, dict):
            for key in ("stoploss", "target"):
                if data.get(key) == "":
                    data[key] = None
        return data

    @post_load
    def post_process(self, data, **kwargs):
        return _validate_gtt_place_request(data)


class ModifyGTTOrderSchema(Schema):
    """Schema for modifying an active GTT in the flat shape.

    Same fields as :class:`PlaceGTTOrderSchema`, plus ``trigger_id``. Modify
    is a full replacement: the broker's PUT semantics replace trigger prices,
    last price, and order params atomically.
    """

    class Meta:
        unknown = EXCLUDE

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    trigger_id = fields.Str(required=True, validate=validate.Length(min=1))
    trigger_type = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    symbol = fields.Str(required=True)
    action = fields.Str(required=True, validate=validate.OneOf(["BUY", "SELL", "buy", "sell"]))
    product = fields.Str(required=True, validate=validate.OneOf(["MIS", "NRML", "CNC"]))
    quantity = fields.Float(
        required=True,
        validate=validate.Range(min=0, min_inclusive=False, error="Quantity must be a positive number."),
    )
    pricetype = fields.Str(missing="LIMIT", validate=validate.OneOf(["LIMIT", "MARKET"]))
    price = fields.Float(
        required=True,
        validate=validate.Range(min=0, error="Price must be a non-negative number."),
    )
    trigger_price = fields.Float(
        missing=0.0,
        validate=validate.Range(min=0, error="Trigger price must be non-negative."),
    )
    stoploss = fields.Float(missing=None, allow_none=True)
    target = fields.Float(missing=None, allow_none=True)

    @pre_load
    def coerce_empty_to_none(self, data, **kwargs):
        if isinstance(data, dict):
            for key in ("stoploss", "target"):
                if data.get(key) == "":
                    data[key] = None
        return data

    @post_load
    def post_process(self, data, **kwargs):
        return _validate_gtt_place_request(data)


class CancelGTTOrderSchema(Schema):
    """Schema for cancelling an active GTT."""

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True)
    trigger_id = fields.Str(required=True, validate=validate.Length(min=1))


class GTTOrderBookSchema(Schema):
    """Schema for listing all GTT triggers for a user."""

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
