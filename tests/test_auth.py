import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    os.environ["AIRPOWER_OWNER_USERNAME"] = "owner"
    os.environ["AIRPOWER_OWNER_PASSWORD"] = "StrongPass123!"
    os.environ["AIRPOWER_SESSION_SECRET"] = "test-secret-value-32-bytes-long"
    os.environ["AIRPOWER_SESSION_TTL_SECONDS"] = "60"
    os.environ.pop("AIRPOWER_USERS", None)
    import airpower.app as app_module
    import airpower.persistence as persistence_module
    importlib.reload(persistence_module)
    importlib.reload(app_module)
    client = TestClient(app_module.app)
    yield client
    os.environ.pop("AIRPOWER_OWNER_USERNAME", None)
    os.environ.pop("AIRPOWER_OWNER_PASSWORD", None)
    os.environ.pop("AIRPOWER_SESSION_SECRET", None)
    os.environ.pop("AIRPOWER_SESSION_TTL_SECONDS", None)


def test_public_dashboard_reads_remain_available_without_login(client):
    for path in ['/api/fleet', '/api/v1/spares/position', '/api/v1/risk/14-day']:
        resp = client.get(path)
        assert resp.status_code == 200, f'{path} should be public for dashboard reads: {resp.text}'


def test_unauthenticated_access_is_rejected(client):
    resp = client.post('/api/parts', json={'stock': {}, 'delay': {}, 'disable': []})
    assert resp.status_code == 401
    body = resp.json()
    assert body['detail'] in {'Authentication required', 'Missing/invalid API key'}


def test_login_sets_cookie_and_allows_access(client):
    resp = client.post('/api/login', json={'username': 'owner', 'password': 'StrongPass123!'})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['username'] == 'owner'
    assert 'csrf_token' in body
    assert 'aerotwin_session' in client.cookies
    assert 'x-csrf-token' in resp.headers.get('set-cookie', '').lower()

    csrf = body['csrf_token']
    fleet = client.get('/api/fleet', headers={'X-CSRF-Token': csrf})
    assert fleet.status_code == 200, fleet.text


def test_logout_invalidates_session(client):
    client.post('/api/login', json={'username': 'owner', 'password': 'StrongPass123!'})
    csrf = client.get('/api/session').json()['csrf_token']
    resp = client.post('/api/logout', headers={'X-CSRF-Token': csrf})
    assert resp.status_code == 200, resp.text
    protected = client.post('/api/parts', json={'stock': {}, 'delay': {}, 'disable': []})
    assert protected.status_code == 401


def test_session_expires(client, monkeypatch):
    monkeypatch.setenv('AIRPOWER_SESSION_TTL_SECONDS', '1')
    import airpower.persistence as persistence_module
    importlib.reload(persistence_module)
    import airpower.app as app_module
    importlib.reload(app_module)
    client = TestClient(app_module.app)

    login = client.post('/api/login', json={'username': 'owner', 'password': 'StrongPass123!'})
    assert login.status_code == 200, login.text
    csrf = login.json()['csrf_token']
    time.sleep(1.5)
    resp = client.post('/api/parts', json={'stock': {}, 'delay': {}, 'disable': []}, headers={'X-CSRF-Token': csrf})
    assert resp.status_code == 401, resp.text


def test_state_changing_actions_require_csrf(client):
    client.post('/api/login', json={'username': 'owner', 'password': 'StrongPass123!'})
    resp = client.post('/api/parts', json={'stock': {}, 'delay': {}, 'disable': []})
    assert resp.status_code == 403, resp.text
