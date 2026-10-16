"""Unpaid, bounded public discovery checks; deliberately standard-library only."""

import argparse
import base64
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LABEL = "scout-distribution-monitor"
MAX_BODY = 2_000_000
TIMEOUT = 15
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SERVICES = {
    "car": {
        "repo": "irish-car-import-assistant",
        "origin": "https://irish-auto-arbitrage-production.up.railway.app",
        "mcp": "https://mcp-production-f77f.up.railway.app/mcp",
        "health": "/health",
        "path": "/api/v1/a2a/uk-import-check",
        "tool": "is_it_cheaper_to_import_car_from_uk_to_ireland",
        "amount": "20000",
    },
    "supplier": {
        "repo": "supplier-payment-assistant",
        "origin": "https://supplier-payment-verifier-production.up.railway.app",
        "mcp": "https://supplier-payment-verifier-production.up.railway.app/mcp/",
        "health": "/healthz",
        "path": "/v1/checks/iban",
        "tool": "check_iban",
        "amount": "5000",
    },
}
ORIGINS = {
    "https://irish-auto-arbitrage-production.up.railway.app",
    "https://mcp-production-f77f.up.railway.app",
    "https://supplier-payment-verifier-production.up.railway.app",
    "https://pypi.org",
}


class CheckError(Exception):
    """A sanitized contract failure (never includes remote content)."""


def need(condition):
    if not condition:
        raise CheckError("contract_mismatch")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckError("redirect_refused")


def transport(method, url, payload=None):
    parsed = urllib.parse.urlsplit(url)
    need(f"{parsed.scheme}://{parsed.netloc}" in ORIGINS)
    need(not parsed.username and not parsed.password and not parsed.fragment)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": LABEL, "X-Client-Name": LABEL,
                 "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_BODY + 1)
        need(len(body) <= MAX_BODY)
        need(not 300 <= response.code < 400)
        return response.code, {k.lower(): v for k, v in response.headers.items()}, body


def decode(body):
    return json.loads(body)


def rpc_body(body, headers):
    if headers.get("content-type", "").split(";")[0] == "text/event-stream":
        events = [
            json.loads("\n".join(line[5:].lstrip() for line in block.splitlines()
                                 if line.startswith("data:")))
            for block in body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
            if any(line.startswith("data:") for line in block.splitlines())
        ]
        need(len(events) == 1)
        return events[0]
    return decode(body)


def objects(value):
    """Walk structured responses and JSON text content without following URLs."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except ValueError:
            return
        yield from objects(parsed)


def payment(value, amount):
    envelopes = [obj for obj in objects(value) if "accepts" in obj]
    need(len(envelopes) >= 1)
    for envelope in envelopes:
        need(envelope.get("x402Version") == 2)
        options = envelope["accepts"]
        need(isinstance(options, list) and len(options) == 1)
        option = options[0]
        need(option["scheme"] == "exact" and option["network"] == "eip155:8453")
        need(option["asset"].lower() == USDC and option["amount"] == amount)
        need(bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", option["payTo"])))
        need(int(option["payTo"][2:], 16) != 0)
        need(type(option["maxTimeoutSeconds"]) is int and option["maxTimeoutSeconds"] > 0)


def tool_map(tools):
    need(isinstance(tools, list) and bool(tools))
    result = {}
    for tool in tools:
        name = tool["name"]
        need(isinstance(name, str) and name not in result)
        schema = tool["inputSchema"]
        need(schema["type"] == "object" and isinstance(schema["properties"], dict))
        need(isinstance(schema.get("required", []), list))
        need(set(schema.get("required", [])) <= set(schema["properties"]))
        result[name] = schema
    return result


def check_openapi(doc, service):
    need(doc["openapi"].startswith("3."))
    paths = doc["paths"]
    operation = paths[service["path"]]["get" if service["repo"].startswith("irish") else "post"]
    need("402" in operation["responses"])
    if service["repo"].startswith("irish"):
        parameters = {p["name"]: p for p in operation["parameters"]}
        for name, kind in {"make": "string", "model": "string", "year": "integer",
                           "mileage_km": "integer", "fuel_type": "string",
                           "co2_g_km": "integer"}.items():
            need(parameters[name]["in"] == "query")
            need(parameters[name]["schema"]["type"] == kind)
        need(operation["operationId"] == service["tool"])
    else:
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        need(schema["type"] == "object" and schema["required"] == ["iban"])
        need(schema["properties"]["iban"]["type"] == "string")
        need({"x402": []} in operation["security"])
        auth = doc["components"]["securitySchemes"]["x402"]
        need(auth["type"] == "apiKey" and auth["in"] == "header"
             and auth["name"] == "PAYMENT-SIGNATURE")
        for path in ("/v1/checks/vat", "/v1/checks/company", "/v1/checks/three-way-match",
                     "/v1/payment-decisions", "/v1/agent-keys", "/v1/verifications"):
            need("402" in paths[path]["post"]["responses"])
        models = doc["components"]["schemas"]
        for name in ("Supplier", "Invoice", "PurchaseOrder", "GoodsReceipt", "SignatureEnvelope"):
            need(models[name]["type"] == "object" and bool(models[name]["required"]))


def monitor(service_name, require_pypi=False, send=transport):
    service = SERVICES[service_name]
    origin, mcp = service["origin"], service["mcp"]
    repo_url = "https://github.com/LKennedyShare/" + service["repo"]
    checks = []

    def run(name, check):
        try:
            status = check() or "ok"
        except (CheckError, ValueError, KeyError, TypeError, AttributeError, RecursionError,
                OSError, urllib.error.URLError, socket.timeout):
            status = "failed"
        checks.append({"check": name, "status": status})

    def request(url, method="GET", payload=None, expected=200):
        status, headers, body = send(method, url, payload)
        need(status == expected and len(body) <= MAX_BODY)
        return headers, body

    def document(url):
        return decode(request(url)[1])

    def rpc(method, params=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        headers, body = request(mcp, "POST", payload)
        result = rpc_body(body, headers)
        need(result["jsonrpc"] == "2.0" and result["id"] == 1 and "error" not in result)
        return result["result"]

    def health():
        doc = document(origin + service["health"])
        need(isinstance(doc, dict))
        if "status" in doc:
            need(doc["status"] in ("ok", "healthy"))
        else:
            need(doc.get("ok") is True)

    def links(path):
        text = request(origin + path)[1].decode("utf-8")
        need(repo_url in text)

    def mcp_schema():
        card_origin = urllib.parse.urlsplit(mcp)
        card = document(f"{card_origin.scheme}://{card_origin.netloc}/.well-known/mcp/server-card.json")
        advertised = tool_map(card["tools"])
        actual = tool_map(rpc("tools/list")["tools"])
        expected = {service["tool"]}
        if service_name == "supplier":
            need(card["url"] == mcp and card["transport"] == "streamable-http")
            expected |= {"check_vat", "check_company", "check_three_way_match",
                         "decide_supplier_payment", "register_signing_key", "verify_supplier_payment"}
        else:
            need(isinstance(card["serverInfo"]["name"], str))
            expected |= {"get_service_info", "price_check", "import_calculator"}
        need(expected <= actual.keys() and expected <= advertised.keys())
        for name in expected:
            need(actual[name] == advertised[name])
        schema = actual[service["tool"]]
        if service_name == "supplier":
            need(schema["required"] == ["request"])
            need(schema["$defs"]["IbanCheckRequest"]["required"] == ["iban"])
        else:
            need({"make", "model", "year", "mileage_km", "fuel_type"} <=
                 set(schema["required"]))

    def rest_payment():
        url = origin + service["path"]
        method, payload = "POST", {}
        if service_name == "car":
            url += "?" + urllib.parse.urlencode({
                "make": "Volkswagen", "model": "Golf", "year": 2023,
                "mileage_km": 50000, "fuel_type": "Petrol", "co2_g_km": 125,
            })
            method, payload = "GET", None
        headers, _ = request(url, method, payload, 402)
        encoded = headers["payment-required"]
        payment(decode(base64.b64decode(encoded, validate=True)), service["amount"])

    def mcp_payment():
        result = rpc("tools/call", {"name": service["tool"], "arguments": {}})
        payment(result, service["amount"])

    def agent_card():
        card = document(origin + "/.well-known/agent-card.json")
        need(card["url"] == origin + "/a2a" and card["preferredTransport"] == "JSONRPC")
        need("check-iban" in {skill["id"] for skill in card["skills"]})

    def a2a_payment():
        _, body = request(origin + "/a2a", "POST", {
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"kind": "message", "messageId": LABEL,
                                  "role": "user", "parts": [
                                      {"kind": "data", "data": {
                                          "operation": "check_iban", "request": {}}}]}}},
        )
        response = decode(body)
        need(response["jsonrpc"] == "2.0" and response["id"] == 1)
        task = response["result"]
        need(task["kind"] == "task" and task["status"]["state"] == "input-required")
        need(any(obj.get("x402.payment.status") == "payment-required"
                 or "x402.payment.required" in obj for obj in objects(task)))
        payment(task, service["amount"])

    def pypi():
        status, _, body = send("GET", "https://pypi.org/pypi/" + service["repo"] + "/json", None)
        need(len(body) <= MAX_BODY)
        if status == 404 and not require_pypi:
            return "pending"
        need(status == 200)
        info = decode(body)["info"]
        need(info["name"] == service["repo"])
        need(info["project_urls"]["Repository"].rstrip("/") == repo_url)
        license_value = info.get("license_expression") or info.get("license")
        need(license_value == "MIT")

    run("health", health)
    run("homepage_client_link", lambda: links("/"))
    run("buyers_client_link", lambda: links("/buyers.txt"))
    run("openapi", lambda: check_openapi(document(origin + "/openapi.json"), service))
    run("mcp_schema", mcp_schema)
    run("rest_unpaid_challenge", rest_payment)
    run("mcp_unpaid_challenge", mcp_payment)
    if service_name == "supplier":
        run("agent_card", agent_card)
        run("a2a_unpaid_challenge", a2a_payment)
    run("pypi", pypi)
    return {"service": service_name, "diagnostic": LABEL, "checks": checks,
            "status": "failed" if any(c["status"] == "failed" for c in checks) else "ok"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--require-pypi", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = monitor(args.service, args.require_pypi)
    serialized = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        summary = "| Check | Status |\n| --- | --- |\n"
        summary += "".join(f"| {c['check']} | {c['status']} |\n" for c in report["checks"])
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Unpaid distribution diagnostics\n\n" + summary)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
