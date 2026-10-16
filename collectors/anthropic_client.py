"""One place to build the Anthropic client for every collector.

Justin's key is a personal multi-workspace key (sk-ant-usr-...). The API rejects those unless each request
names a workspace (header anthropic-workspace-id). Instead of making him find and paste the ID, we ask the
API for the organization's workspaces once and use one:
    ANTHROPIC_WORKSPACE_ID   if set, used as is
    ANTHROPIC_WORKSPACE_NAME if set, the workspace with that name
    otherwise                the only workspace, or the Default workspace
A key scoped to one workspace needs none of this and is used untouched.
"""
import os, requests
from .common import log

API = "https://api.anthropic.com/v1/organizations/workspaces?include_default=true&limit=100"


def _discover_workspace(key):
    try:
        r = requests.get(API, headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=20)
    except requests.RequestException as e:
        log.warning("workspace lookup failed: %s", e); return None
    if r.status_code != 200:
        log.warning("workspace lookup returned %s: %s", r.status_code, r.text[:200]); return None
    ws = [w for w in r.json().get("data", []) if not w.get("archived_at")]
    want = (os.environ.get("ANTHROPIC_WORKSPACE_NAME") or "").strip().lower()
    pick = next((w for w in ws if want and w.get("name", "").lower() == want), None) \
        or (ws[0] if len(ws) == 1 else None) \
        or next((w for w in ws if w.get("name", "").lower() == "default"), None) \
        or (ws[0] if ws else None)
    if pick:
        log.info("using Anthropic workspace %r (%s) out of %d", pick.get("name"), pick["id"], len(ws))
        return pick["id"]
    return None


def make_client():
    import anthropic
    key = os.environ["ANTHROPIC_API_KEY"]
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if not ws and key.startswith("sk-ant-usr"):
        ws = _discover_workspace(key)
    return anthropic.Anthropic(api_key=key, default_headers={"anthropic-workspace-id": ws} if ws else None)
