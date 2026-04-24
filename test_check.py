import requests

provider_model = "grok:grok-4.20"
provider_prefix, model_name = provider_model.split(":", 1)

frontier_resp = requests.get("http://localhost:8090/api/frontier-models")
data = frontier_resp.json()
for p in data.get("providers", []):
    if model_name in p.get("models", []):
        print(f"Found model in provider {p['id']}, credentials: {p.get('credentials')}")
