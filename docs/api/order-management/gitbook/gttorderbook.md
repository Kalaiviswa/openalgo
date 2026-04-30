# Gttorderbook

## Endpoint URL

This API Function lists **active** GTT triggers for the authenticated user. Triggered, cancelled, expired, and rejected GTTs are filtered out — only triggers that can still fire are returned.

```http
Local Host   :  POST http://127.0.0.1:5000/api/v1/gttorderbook
Ngrok Domain :  POST https://<your-ngrok-domain>.ngrok-free.app/api/v1/gttorderbook
Custom Domain:  POST https://<your-custom-domain>/api/v1/gttorderbook
```

```json
{
    "apikey": "<your_app_apikey>"
}
```

## Sample API Request

```json
{
    "apikey": "<your_app_apikey>"
}
```

## Sample API Response

```json
{
    "status": "success",
    "data": [
        {
            "trigger_id": "23132604291205",
            "trigger_type": "single",
            "status": "active",
            "symbol": "IDEA",
            "exchange": "NSE",
            "trigger_prices": [9.55],
            "last_price": 9.50,
            "legs": [
                {
                    "action": "BUY",
                    "quantity": 1,
                    "price": 9.50,
                    "pricetype": "LIMIT",
                    "product": "CNC"
                }
            ],
            "created_at": "2026-04-29 12:18:42",
            "updated_at": "",
            "expires_at": ""
        },
        {
            "trigger_id": "23132604291213",
            "trigger_type": "two-leg",
            "status": "active",
            "symbol": "INFY",
            "exchange": "NSE",
            "trigger_prices": [1480, 1620],
            "last_price": 1550,
            "legs": [
                {
                    "action": "SELL",
                    "quantity": 5,
                    "price": 1478,
                    "pricetype": "LIMIT",
                    "product": "CNC"
                },
                {
                    "action": "SELL",
                    "quantity": 5,
                    "price": 1622,
                    "pricetype": "LIMIT",
                    "product": "CNC"
                }
            ],
            "created_at": "2026-04-29 12:25:11",
            "updated_at": "",
            "expires_at": ""
        }
    ]
}
```

## Parameter Description

| Parameters | Description | Mandatory/Optional | Default Value |
| ---------- | ----------- | ------------------ | ------------- |
| apikey | App API key | Mandatory | - |

### Response Fields

| Field | Description |
| ----- | ----------- |
| status | `success` or `error` |
| data | Array of active GTT entries (see below) |

### GTT Entry

| Field | Description |
| ----- | ----------- |
| trigger\_id | Unique trigger ID assigned by the broker |
| trigger\_type | `single` (one trigger) or `two-leg` (OCO) |
| status | Always `active` (this endpoint filters out non-active states) |
| symbol | Symbol in OpenAlgo format |
| exchange | Exchange code |
| trigger\_prices | Trigger prices, sorted ascending. SINGLE → `[trigger]`. OCO → `[stoploss_trigger, target_trigger]`. |
| last\_price | LTP captured at place / last-modify time. `0` if the broker doesn't expose it. |
| legs | Per-leg child order details — see below |
| created\_at | Creation timestamp from broker |
| updated\_at | Last-update timestamp (empty if never modified) |
| expires\_at | Expiry timestamp (empty if the broker doesn't expose an explicit expiry) |

### Leg Object

| Field | Description |
| ----- | ----------- |
| action | `BUY` or `SELL` |
| quantity | Order quantity |
| price | Child order limit price (`0` for MARKET-style legs) |
| pricetype | `LIMIT` or `MARKET` |
| product | `CNC` or `NRML` |
