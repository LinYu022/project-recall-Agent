"""Product read views cannot bypass authentication, and UI serving is local-only."""

import pytest
from fastapi.testclient import TestClient

from portal.api import create_product_app
from portal.demo import DemoModel

TOKEN = "synthetic-product-user-credential-24-plus"


class NoAccess:
    pass


def test_public_metadata_is_honest_and_all_private_views_require_owner():
    client = TestClient(create_product_app(NoAccess(), {TOKEN: "one"}, profile="synthetic-demo"))
    info = client.get("/product/info")
    assert info.json()["model_quality_acceptance"] == "not_established"
    assert info.json()["is_simulated_generation"] is True
    assert "frame-ancestors 'none'" in info.headers["content-security-policy"]
    assert info.headers["cache-control"] == "no-store"
    for path in [
        "/product/catalog",
        "/product/tasks",
        "/product/memories",
        "/product/tasks/T-1/original/E001",
        "/product/tasks/T-1/evidence",
        "/product/memories/M-1/source",
    ]:
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 403


def test_cross_origin_mutation_rejected_even_with_token():
    client = TestClient(create_product_app(NoAccess(), {TOKEN: "one"}, profile="synthetic-demo"))
    result = client.post(
        "/tasks",
        json={},
        headers={"Authorization": "Bearer " + TOKEN, "Origin": "https://outside.invalid"},
    )
    assert result.status_code == 403 and result.json()["status"] == "same_origin_required"


def test_missing_frontend_build_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="build_frontend_first"):
        create_product_app(NoAccess(), {TOKEN: "one"}, profile="synthetic-demo", ui_root=tmp_path)


def test_demo_model_cannot_call_provider_and_does_not_fabricate_usage():
    model = DemoModel()
    value = model.generate("ignored", {}, max_tokens=2000)
    assert model.identity["is_simulated"]
    assert value["proposal"]["disposition"] == "insufficient_evidence"
    assert value["proposal"]["claims"] == []
    assert value["usage"] is None and value["estimated_cost"] is None


def test_explicit_demo_fault_is_local_and_has_no_sensitive_error_text():
    from models.chat_api import ModelCallFailed

    with pytest.raises(ModelCallFailed, match="synthetic_transport_failure_no_external_request"):
        DemoModel().generate("ignored", {"question": "[DEMO_FAIL]"}, max_tokens=2000)
