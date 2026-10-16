"""Offline automation contracts: no service or payment SDK is imported."""

import ast
import base64
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d = load("check_discovery")
r = load("check_release")


def terms(amount):
    return {"x402Version": 2, "accepts": [{
        "scheme": "exact", "network": "eip155:8453", "asset": d.USDC,
        "amount": amount, "payTo": "0x" + "12" * 20, "maxTimeoutSeconds": 60,
    }]}


def tool(name, supplier=False):
    schema = {"type": "object", "properties": {}}
    if supplier and name == "check_iban":
        schema.update(properties={"request": {"$ref": "#/$defs/IbanCheckRequest"}},
                      required=["request"], **{"$defs": {
                          "IbanCheckRequest": {"required": ["iban"]}}})
    if not supplier and name == d.SERVICES["car"]["tool"]:
        names = ["make", "model", "year", "mileage_km", "fuel_type"]
        schema.update(properties={key: {"type": "string"} for key in names}, required=names)
    return {"name": name, "inputSchema": schema}


def fixture_openapi(service):
    if service == "car":
        return {"openapi": "3.1.0", "paths": {d.SERVICES[service]["path"]: {"get": {
            "responses": {"402": {}}, "operationId": d.SERVICES[service]["tool"],
            "parameters": [{"name": name, "in": "query", "schema": {"type": kind}}
                           for name, kind in {
                               "make": "string", "model": "string", "year": "integer",
                               "mileage_km": "integer", "fuel_type": "string",
                               "co2_g_km": "integer"}.items()]}}}}
    paths = {path: {"post": {"responses": {"402": {}}}} for path in (
        "/v1/checks/iban", "/v1/checks/vat", "/v1/checks/company",
        "/v1/checks/three-way-match", "/v1/payment-decisions", "/v1/agent-keys",
        "/v1/verifications")}
    paths["/v1/checks/iban"]["post"].update(
        security=[{"x402": []}],
        requestBody={"content": {"application/json": {"schema": {
            "type": "object", "required": ["iban"],
            "properties": {"iban": {"type": "string"}}}}}})
    return {"openapi": "3.1.0", "paths": paths, "components": {
        "securitySchemes": {"x402": {"type": "apiKey", "in": "header",
                                    "name": "PAYMENT-SIGNATURE"}},
        "schemas": {name: {"type": "object", "required": ["field"]} for name in (
            "Supplier", "Invoice", "PurchaseOrder", "GoodsReceipt", "SignatureEnvelope")}}}


class Fake:
    def __init__(self, service, pypi_status=404):
        self.service = service
        self.config = d.SERVICES[service]
        self.calls = []
        self.pypi_status = pypi_status
        names = ({self.config["tool"], "get_service_info", "price_check", "import_calculator"}
                 if service == "car" else {
                     "check_iban", "check_vat", "check_company", "check_three_way_match",
                     "decide_supplier_payment", "register_signing_key", "verify_supplier_payment"})
        self.tools = [tool(name, service == "supplier") for name in sorted(names)]
        self.openapi = fixture_openapi(service)

    def __call__(self, method, url, payload=None):
        self.calls.append((method, url, payload))
        path = urlsplit(url).path
        body, status, headers = {}, 200, {}
        if url.startswith("https://pypi.org/"):
            status = self.pypi_status
            body = {"info": {"name": self.config["repo"], "license_expression": "MIT",
                             "project_urls": {"Repository": "https://github.com/LKennedyShare/"
                                              + self.config["repo"]}}}
        elif path in ("/", "/buyers.txt"):
            return 200, {}, ("https://github.com/LKennedyShare/" + self.config["repo"]).encode()
        elif path in ("/health", "/healthz"):
            body = {"status": "ok"}
        elif path == "/openapi.json":
            body = self.openapi
        elif path == "/.well-known/mcp/server-card.json":
            body = {"serverInfo": {"name": "Service"}, "tools": copy.deepcopy(self.tools),
                    "url": self.config["mcp"], "transport": "streamable-http"}
        elif path == "/.well-known/agent-card.json":
            body = {"url": self.config["origin"] + "/a2a", "preferredTransport": "JSONRPC",
                    "skills": [{"id": "check-iban"}]}
        elif path in ("/mcp", "/mcp/"):
            result = ({"tools": self.tools} if payload["method"] == "tools/list" else
                      {"isError": True, "content": [{"type": "text", "text": json.dumps({
                          "error": "payment_required",
                          "paymentRequired": terms(self.config["amount"])})}]})
            body = {"jsonrpc": "2.0", "id": 1, "result": result}
        elif path == "/a2a":
            body = {"jsonrpc": "2.0", "id": 1, "result": {
                "kind": "task", "status": {"state": "input-required"},
                "metadata": {"x402.payment.required": terms(self.config["amount"])}}}
        elif path == self.config["path"]:
            status = 402
            headers = {"payment-required": base64.b64encode(
                json.dumps(terms(self.config["amount"])).encode()).decode()}
        else:
            raise AssertionError("Unexpected request route")
        return status, headers, json.dumps(body).encode()


@pytest.mark.parametrize("service", ["car", "supplier"])
def test_success_pending_and_exact_unpaid_requests(service):
    fake = Fake(service)
    report = d.monitor(service, send=fake)
    assert report["status"] == "ok"
    assert report["checks"][-1] == {"check": "pypi", "status": "pending"}
    assert len(fake.calls) == (9 if service == "car" else 11)
    paid = [call for call in fake.calls if urlsplit(call[1]).path == fake.config["path"]]
    assert len(paid) == 1
    if service == "car":
        assert paid[0][0] == "GET" and paid[0][2] is None
        assert parse_qs(urlsplit(paid[0][1]).query) == {
            "make": ["Volkswagen"], "model": ["Golf"], "year": ["2023"],
            "mileage_km": ["50000"], "fuel_type": ["Petrol"], "co2_g_km": ["125"]}
        assert not any("/a2a" == urlsplit(url).path for _, url, _ in fake.calls)
    else:
        assert paid[0][0] == "POST" and paid[0][2] == {}
        message = next(p for _, url, p in fake.calls if urlsplit(url).path == "/a2a")
        assert message["params"]["message"]["parts"] == [
            {"kind": "data", "data": {"operation": "check_iban", "request": {}}}]
    calls = [p for _, _, p in fake.calls if p and p.get("method") == "tools/call"]
    assert calls == [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": fake.config["tool"], "arguments": {}}}]
    serialized = json.dumps(report)
    assert d.USDC not in serialized and "accepts" not in serialized
    assert all("preview" not in url for _, url, _ in fake.calls)


@pytest.mark.parametrize("service", ["car", "supplier"])
def test_pypi_required_and_published(service):
    assert d.monitor(service, True, Fake(service))["status"] == "failed"
    assert d.monitor(service, True, Fake(service, 200))["status"] == "ok"


@pytest.mark.parametrize("bad", [b"not json", b"{}", b"[]", b"null",
                                b'{"status":"error","ok":true}',
                                b'{"status":"error","private":"DO NOT REPORT"}'])
def test_malformed_health_does_not_mask_pending(bad):
    fake = Fake("car")

    def send(method, url, payload=None):
        if url.endswith("/health"):
            return 200, {}, bad
        return fake(method, url, payload)

    report = d.monitor("car", send=send)
    assert report["status"] == "failed"
    assert report["checks"][0]["status"] == "failed"
    assert report["checks"][-1]["status"] == "pending"
    assert "DO NOT REPORT" not in json.dumps(report)


@pytest.mark.parametrize("change", [
    {"amount": "1"}, {"asset": "ETH"}, {"network": "eip155:1"},
    {"scheme": "upto"}, {"payTo": "invalid"}, {"maxTimeoutSeconds": 0},
])
def test_wrong_payment_terms_rejected(change):
    value = terms("5000")
    value["accepts"][0].update(change)
    with pytest.raises(d.CheckError):
        d.payment(value, "5000")


def test_schema_drift_and_network_errors_fail_closed():
    fake = Fake("supplier")
    fake.openapi["paths"]["/v1/checks/iban"]["post"]["security"] = []
    assert d.monitor("supplier", send=fake)["status"] == "failed"

    def failure(*args):
        raise urllib.error.URLError("private remote text")

    report = d.monitor("car", send=failure)
    assert all(check["status"] == "failed" for check in report["checks"])
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("route", ["/openapi.json", "/mcp", "/.well-known/mcp/server-card.json"])
@pytest.mark.parametrize("status,body", [(302, b"redirect"), (500, b"private"),
                                       (200, b"{}"), (200, b"invalid")])
def test_discovery_contract_errors_are_failures(route, status, body):
    fake = Fake("car")

    def send(method, url, payload=None):
        if urlsplit(url).path == route:
            return status, {}, body
        return fake(method, url, payload)

    report = d.monitor("car", send=send)
    assert report["status"] == "failed"
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("field,value", [("name", "another-project"), ("license_expression", "GPL"),
                                        ("project_urls", {"Repository": "https://wrong.example"})])
def test_pypi_identity_mismatch(field, value):
    fake = Fake("car", 200)

    def send(method, url, payload=None):
        status, headers, body = fake(method, url, payload)
        if url.startswith("https://pypi.org/"):
            doc = json.loads(body)
            doc["info"][field] = value
            body = json.dumps(doc).encode()
        return status, headers, body

    assert d.monitor("car", send=send)["checks"][-1]["status"] == "failed"


def test_sse_and_redirect_refusal():
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    assert d.rpc_body(body, {"content-type": "text/event-stream"})["id"] == 1
    with pytest.raises(d.CheckError):
        d.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")
    with pytest.raises(d.CheckError):
        d.transport("GET", "https://arbitrary.example/")


def test_transport_headers_bound_and_timeout(monkeypatch):
    class Response:
        code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            assert count == d.MAX_BODY + 1
            return b"x" * count

    class Opener:
        def open(self, req, timeout):
            assert timeout == 15
            assert req.get_header("User-agent") == d.LABEL
            assert req.get_header("X-client-name") == d.LABEL
            assert req.get_header("Accept") == "application/json, text/event-stream"
            assert not any("signature" in key.lower() for key in req.headers)
            return Response()

    monkeypatch.setattr(d.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(d.CheckError):
        d.transport("GET", d.SERVICES["car"]["origin"] + "/health")


def test_monitor_imports_stdlib_only():
    tree = ast.parse((ROOT / "scripts" / "check_discovery.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names
    source = (ROOT / "scripts" / "check_discovery.py").read_text()
    assert "PRIVATE_KEY" not in source and "PAYMENT-SIGNATURE" in source  # schema check only


def test_release_identity():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    tag = "v" + project["version"]
    r.validate(project, r.REPOSITORY, tag)
    for wrong in ("main", project["version"], tag + ".wrong", "v9.9.9"):
        with pytest.raises(ValueError):
            r.validate(project, r.REPOSITORY, wrong)
    with pytest.raises(ValueError):
        r.validate(project, "another/repo", tag)
    for key, value in (("name", "wrong"), ("scripts", {}), ("urls", {"Repository": "wrong"})):
        changed = copy.deepcopy(project)
        changed[key] = value
        with pytest.raises(ValueError):
            r.validate(changed, r.REPOSITORY, tag)


def test_release_script_rejects_manual_invocation():
    import os
    env = {**os.environ, "GITHUB_EVENT_NAME": "workflow_dispatch"}
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_release.py"),
                             "--tag", "v0.1.0"], env=env, cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Only release.published" in result.stderr


@pytest.mark.parametrize("change", [{}, {"action": "created"}, {"draft": True},
                                  {"tag_name": "v9.9.9"}, {"ref": "refs/heads/main"}])
def test_release_event_validation(monkeypatch, change):
    project_text = (ROOT / "pyproject.toml").read_text()
    version = tomllib.loads(project_text)["project"]["version"]
    tag = "v" + version
    event = {"action": change.get("action", "published"), "release": {
        "draft": change.get("draft", False), "tag_name": change.get("tag_name", tag)}}
    monkeypatch.setenv("GITHUB_EVENT_NAME", "release")
    monkeypatch.setenv("GITHUB_EVENT_PATH", "mock-event.json")
    monkeypatch.setenv("GITHUB_REPOSITORY", r.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", change.get("ref", "refs/tags/" + tag))
    monkeypatch.setattr(sys, "argv", ["check_release.py", "--tag", tag])
    monkeypatch.setattr(Path, "read_text", lambda path, **kwargs:
                        project_text if path.name == "pyproject.toml" else json.dumps(event))
    if change:
        with pytest.raises(ValueError):
            r.main()
    else:
        r.main()


def test_workflow_permission_separation():
    workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    build, publish = workflow.split("\n  publish:\n")
    assert "id-token" not in build
    assert "id-token: write" in publish and "environment: pypi" in publish
    assert "actions/checkout" not in publish and "pip install" not in publish
    assert "types: [published]" in workflow and "workflow_dispatch" not in workflow
    assert "skip-existing" not in workflow and "secrets." not in workflow
    assert "pypa/gh-action-pypi-publish@release/v1" in publish
    assert "actions/upload-artifact@v4" in build
    assert "actions/download-artifact@v4" in publish
    nightly = (ROOT / ".github" / "workflows" / "discovery.yml").read_text()
    assert "id-token" not in nightly and "secrets." not in nightly and "pip install" not in nightly
    assert "contents: read" in nightly and "workflow_dispatch:" in nightly
