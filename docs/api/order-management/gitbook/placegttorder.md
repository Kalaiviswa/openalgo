# Placegttorder

## Endpoint URL

This API Function places a GTT (Good Till Triggered) order — a price-trigger that sits with the broker until LTP crosses your level, then automatically places the underlying order. Two trigger types are supported: **SINGLE** (one trigger fires one order) and **OCO** (One-Cancels-Other — two triggers fire one of two orders, the other is auto-cancelled).

```http
Local Host   :  POST http://127.0.0.1:5000/api/v1/placegttorder
Ngrok Domain :  POST https://<your-ngrok-domain>.ngrok-free.app/api/v1/placegttorder
Custom Domain:  POST https://<your-custom-domain>/api/v1/placegttorder
```

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "My GTT Strategy",
    "trigger_type": "SINGLE",
    "exchange": "NSE",
    "symbol": "IDEA",
    "action": "BUY",
    "product": "CNC",
    "quantity": 1,
    "pricetype": "LIMIT",
    "price": 9.50,
    "triggerprice_sl": 9.55,
    "triggerprice_tg": 0,
    "stoploss": null,
    "target": null
}
```

## Sample API Request

**SINGLE — buy IDEA on a dip to 9.55 with a LIMIT order at 9.50**

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "My GTT Strategy",
    "trigger_type": "SINGLE",
    "exchange": "NSE",
    "symbol": "IDEA",
    "action": "BUY",
    "product": "CNC",
    "quantity": 1,
    "pricetype": "LIMIT",
    "price": 9.50,
    "triggerprice_sl": 9.55,
    "triggerprice_tg": 0,
    "stoploss": null,
    "target": null
}
```

**SINGLE — buy RELIANCE at MARKET if it breaks above 1450**

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "My GTT Strategy",
    "trigger_type": "SINGLE",
    "exchange": "NSE",
    "symbol": "RELIANCE",
    "action": "BUY",
    "product": "CNC",
    "quantity": 1,
    "pricetype": "MARKET",
    "price": 0,
    "triggerprice_sl": 0,
    "triggerprice_tg": 1450,
    "stoploss": null,
    "target": null
}
```

**OCO — bracket an INFY short with stop at 1480 / target at 1620**

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "Bracket OCO",
    "trigger_type": "OCO",
    "exchange": "NSE",
    "symbol": "INFY",
    "action": "SELL",
    "product": "CNC",
    "quantity": 5,
    "pricetype": "LIMIT",
    "price": 0,
    "triggerprice_sl": 1480,
    "stoploss": 1478,
    "triggerprice_tg": 1620,
    "target": 1622
}
```

## Sample API Response

```json
{
    "status": "success",
    "trigger_id": "23132604291205"
}
```

## Parameter Description

| Parameters | Description | Mandatory/Optional | Default Value |
| ---------- | ----------- | ------------------ | ------------- |
| apikey | App API key | Mandatory | - |
| strategy | Strategy name | Mandatory | - |
| trigger\_type | `SINGLE` (one trigger / one order) or `OCO` (two triggers / one of two orders) | Mandatory | - |
| exchange | Exchange code (NSE, BSE, NFO, BFO, CDS, BCD, MCX) | Mandatory | - |
| symbol | Trading symbol in OpenAlgo format | Mandatory | - |
| action | `BUY` or `SELL`. For OCO, applies to both legs. | Mandatory | - |
| product | `CNC` (equity delivery) or `NRML` (F&O overnight). MIS is not supported — GTTs can sit for days. | Mandatory | - |
| quantity | Order quantity (integer for equity/F&O; fractional float allowed for crypto) | Mandatory | - |
| pricetype | `LIMIT` or `MARKET` | Optional | LIMIT |
| price | SINGLE-only limit price for the child order. Send `0` when `pricetype=MARKET`. Ignored for OCO. | Mandatory | - |
| triggerprice\_sl | Trigger price below LTP. **SINGLE**: use this OR `triggerprice_tg`. **OCO**: required (the stoploss-leg trigger). | Conditional | 0 |
| triggerprice\_tg | Trigger price above LTP. **SINGLE**: use this OR `triggerprice_sl`. **OCO**: required (the target-leg trigger). | Conditional | 0 |
| stoploss | OCO-only limit price for the stoploss leg's child order. Ignored for SINGLE. | Conditional | null |
| target | OCO-only limit price for the target leg's child order. Ignored for SINGLE. | Conditional | null |
