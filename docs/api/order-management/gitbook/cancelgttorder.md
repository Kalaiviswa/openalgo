# Cancelgttorder

## Endpoint URL

This API Function cancels an active GTT trigger by its `trigger_id`. Cancelling an OCO removes both legs atomically.

```http
Local Host   :  POST http://127.0.0.1:5000/api/v1/cancelgttorder
Ngrok Domain :  POST https://<your-ngrok-domain>.ngrok-free.app/api/v1/cancelgttorder
Custom Domain:  POST https://<your-custom-domain>/api/v1/cancelgttorder
```

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "My GTT Strategy",
    "trigger_id": "23132604291205"
}
```

## Sample API Request

```json
{
    "apikey": "<your_app_apikey>",
    "strategy": "My GTT Strategy",
    "trigger_id": "23132604291205"
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
| strategy | Strategy name (used in event logs) | Mandatory | - |
| trigger\_id | The trigger ID of the active GTT to cancel (returned by `placegttorder`) | Mandatory | - |
