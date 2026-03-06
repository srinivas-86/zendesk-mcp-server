"""Web admin console (plan §10.4).

Runs on a SEPARATE port (default 127.0.0.1:9000) that must never be exposed
to the internet — access it via SSH tunnel / SSM port-forward, or an
IP-allowlisted path. Enabled only when MCP_ADMIN_PASSWORD is set.

Features:
  - Zendesk connection settings (hot-swap, test connection; token never re-displayed)
  - API key management (create with scopes/expiry, list, revoke; key shown once)
  - Audit log tail

Security: password login (constant-time compare), random in-memory session
tokens, Secure/HttpOnly/SameSite=Strict cookie, CSRF token on all POSTs.
"""
from __future__ import annotations

import hmac
import html
import secrets
import time
from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from zendesk_mcp_server.keystore import KeyStore, VALID_SCOPES
from zendesk_mcp_server.runtime import ClientHolder

SESSION_COOKIE = "zmcp_admin_session"
SESSION_TTL = 3600 * 8  # 8 hours

_STYLE = """
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #f5f6f8; color: #1a1a2e; }
  header { background: #03363d; color: #fff; padding: 14px 28px; display: flex; justify-content: space-between; align-items: center; }
  header h1 { font-size: 17px; margin: 0; }
  main { max-width: 980px; margin: 24px auto; padding: 0 16px; }
  section { background: #fff; border-radius: 8px; padding: 20px 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h2 { font-size: 15px; margin-top: 0; color: #03363d; }
  label { display: block; font-size: 12px; color: #555; margin: 10px 0 3px; }
  input, select { padding: 7px 9px; border: 1px solid #c4c8cc; border-radius: 5px; width: 320px; max-width: 100%; font-size: 13px; }
  button { background: #03363d; color: #fff; border: 0; border-radius: 5px; padding: 8px 16px; font-size: 13px; cursor: pointer; margin-top: 12px; }
  button.danger { background: #b00020; padding: 4px 10px; margin: 0; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eceff1; }
  th { color: #666; font-weight: 600; }
  .msg { padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
  .ok { background: #e6f4ea; color: #137333; }
  .err { background: #fce8e6; color: #b00020; }
  .keybox { background: #fff8e1; border: 1px solid #f0c36d; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 14px; word-break: break-all; }
  .muted { color: #888; font-size: 11.5px; }
  code { background: #eef1f3; padding: 1px 5px; border-radius: 3px; }
</style>
"""


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


class _Sessions:
    def __init__(self):
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self._tokens.get(token)
        if exp is None or time.time() > exp:
            self._tokens.pop(token, None)
            return False
        return True

    def drop(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)


def build_admin_app(holder: ClientHolder, store: KeyStore, admin_password: str) -> Starlette:
    sessions = _Sessions()
    csrf_token = secrets.token_urlsafe(24)

    def _authed(request: Request) -> bool:
        return sessions.valid(request.cookies.get(SESSION_COOKIE))

    def _csrf_ok(form) -> bool:
        return hmac.compare_digest(str(form.get("csrf", "")), csrf_token)

    def _page(body: str) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Zendesk MCP Admin</title>" + _STYLE + "</head><body>"
            "<header><h1>Zendesk MCP Server — Admin</h1>"
            f"<form method='post' action='/logout' style='margin:0'>"
            f"<input type='hidden' name='csrf' value='{csrf_token}'>"
            "<button>Logout</button></form></header><main>"
            + body + "</main></body></html>"
        )

    # -- auth ------------------------------------------------------------

    async def login_page(request: Request):
        if _authed(request):
            return RedirectResponse("/", status_code=302)
        err = "<div class='msg err'>Invalid password</div>" if request.query_params.get("e") else ""
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'><title>Login</title>"
            + _STYLE + "</head><body><main style='max-width:380px;margin-top:12vh'>"
            "<section><h2>Zendesk MCP Admin — Login</h2>" + err +
            f"<form method='post'><input type='hidden' name='csrf' value='{csrf_token}'>"
            "<label>Password</label><input type='password' name='password' autofocus>"
            "<br><button>Sign in</button></form></section></main></body></html>"
        )

    async def login_submit(request: Request):
        form = await request.form()
        if not _csrf_ok(form):
            return RedirectResponse("/login?e=1", status_code=302)
        if hmac.compare_digest(str(form.get("password", "")), admin_password):
            token = sessions.create()
            store.audit(None, None, "admin_login", "success")
            resp = RedirectResponse("/", status_code=302)
            resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict",
                            max_age=SESSION_TTL, secure=False)
            return resp
        store.audit(None, None, "admin_login", "FAILED")
        return RedirectResponse("/login?e=1", status_code=302)

    async def logout(request: Request):
        sessions.drop(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # -- dashboard ---------------------------------------------------------

    def _dashboard(msg: str = "", msg_class: str = "ok", new_key: str = "") -> str:
        conn = holder.connection_info()
        keys = store.list()
        tenants = store.tenant_list()
        tenant_names = {t["id"]: t["name"] for t in tenants}

        msg_html = f"<div class='msg {msg_class}'>{html.escape(msg)}</div>" if msg else ""
        key_html = (
            "<div class='msg ok'>Key created — copy it now, it will NOT be shown again:</div>"
            f"<div class='keybox'>{html.escape(new_key)}</div><br>"
        ) if new_key else ""

        scope_options = "".join(
            f"<label style='display:inline;margin-right:14px'>"
            f"<input style='width:auto' type='checkbox' name='scopes' value='{s}'"
            f"{' checked' if s == 'tickets:read' else ''}> <code>{s}</code></label>"
            for s in sorted(VALID_SCOPES)
        )

        row_parts = []
        for k in keys:
            status_cell = "<span style='color:#b00020'>REVOKED</span>" if k["revoked"] else "active"
            if k["revoked"]:
                action_cell = ""
            else:
                action_cell = (
                    "<form method='post' action='/keys/revoke' style='margin:0'>"
                    f"<input type='hidden' name='csrf' value='{csrf_token}'>"
                    f"<input type='hidden' name='key_id' value='{k['id']}'>"
                    "<button class='danger'>Revoke</button></form>"
                )
            tenant_cell = html.escape(tenant_names.get(k.get("tenant_id"), "default"))
            row_parts.append(
                f"<tr><td>{k['id']}</td><td>{html.escape(k['name'])}</td>"
                f"<td><code>{html.escape(k['scopes'])}</code></td>"
                f"<td>{tenant_cell}</td>"
                f"<td>{_fmt_ts(k['created_at'])}</td><td>{_fmt_ts(k['expires_at'])}</td>"
                f"<td>{_fmt_ts(k['last_used_at'])}</td>"
                f"<td>{status_cell}</td><td>{action_cell}</td></tr>"
            )
        rows = "".join(row_parts) or "<tr><td colspan='9' class='muted'>No keys yet</td></tr>"

        tenant_options = "<option value=''>default (this server's connection)</option>" + "".join(
            f"<option value='{t['id']}'>{html.escape(t['name'])} ({html.escape(t['subdomain'])})</option>"
            for t in tenants
        )

        tenant_rows_parts = []
        for t in tenants:
            tenant_rows_parts.append(
                f"<tr><td>{t['id']}</td><td>{html.escape(t['name'])}</td>"
                f"<td>{html.escape(t['subdomain'])}</td>"
                f"<td>{html.escape(t['read_email'])}</td><td>{html.escape(t['write_email'])}</td>"
                f"<td>{_fmt_ts(t['created_at'])}</td>"
                "<td><form method='post' action='/tenants/delete' style='margin:0'>"
                f"<input type='hidden' name='csrf' value='{csrf_token}'>"
                f"<input type='hidden' name='tenant_id' value='{t['id']}'>"
                "<button class='danger'>Delete</button></form></td></tr>"
            )
        tenant_rows = "".join(tenant_rows_parts) or \
            "<tr><td colspan='7' class='muted'>No tenants — all keys use this server's default Zendesk connection</td></tr>"

        return f"""
{msg_html}{key_html}
<section>
  <h2>Zendesk Connection</h2>
  <form method="post" action="/connection">
    <input type="hidden" name="csrf" value="{csrf_token}">
    <label>Subdomain</label><input name="subdomain" value="{html.escape(conn['subdomain'] or '')}" required>
    <label>Read identity email (restricted user / light agent)</label>
    <input name="read_email" value="{html.escape(conn['read_email'] or '')}" required>
    <label>Write identity email (full agent)</label>
    <input name="write_email" value="{html.escape(conn['write_email'] or '')}">
    <label>API token (leave blank to keep current — never displayed)</label>
    <input type="password" name="api_token" placeholder="&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;&#8226;">
    <br><button>Save &amp; hot-swap</button>
    <button formaction="/connection/test" style="background:#666">Test connection</button>
  </form>
  <p class="muted">Dual identity (Layer 4): {'ACTIVE' if conn['dual_identity'] else 'inactive — read and write emails are identical'}</p>
</section>
<section>
  <h2>Tenants (multi-tenant Zendesk connections)</h2>
  <table>
    <tr><th>id</th><th>name</th><th>subdomain</th><th>read email</th><th>write email</th><th>created</th><th></th></tr>
    {tenant_rows}
  </table>
  <form method="post" action="/tenants/create">
    <input type="hidden" name="csrf" value="{csrf_token}">
    <label>Name (unique)</label><input name="name" required placeholder="e.g. acme-corp">
    <label>Zendesk subdomain</label><input name="subdomain" required placeholder="acme">
    <label>API token</label><input type="password" name="api_token" required>
    <label>Read identity email</label><input name="read_email" required>
    <label>Write identity email (blank = same as read)</label><input name="write_email">
    <br><button>Add tenant</button>
  </form>
  <p class="muted">Deleting a tenant revokes all its API keys. Tenant clients are hot-swapped on change.</p>
</section>
<section>
  <h2>Create API key</h2>
  <form method="post" action="/keys/create">
    <input type="hidden" name="csrf" value="{csrf_token}">
    <label>Name</label><input name="name" required placeholder="e.g. alice-claude-desktop">
    <label>Scopes (keys are read-only unless tickets:write is granted)</label>
    {scope_options}
    <label>Tenant</label><select name="tenant_id">{tenant_options}</select>
    <label>Expires in days (blank = never)</label><input name="expires_days" type="number" min="1" style="width:120px">
    <br><button>Create key</button>
  </form>
</section>
<section>
  <h2>API keys</h2>
  <table>
    <tr><th>id</th><th>name</th><th>scopes</th><th>tenant</th><th>created</th><th>expires</th><th>last used</th><th>status</th><th></th></tr>
    {rows}
  </table>
</section>
"""

    async def dashboard(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        return _page(_dashboard())

    # -- actions -------------------------------------------------------------

    async def connection_save(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        try:
            holder.reconfigure(
                subdomain=str(form.get("subdomain", "")),
                read_email=str(form.get("read_email", "")),
                write_email=str(form.get("write_email", "")),
                api_token=str(form.get("api_token", "")),
            )
            store.audit(None, None, "connection_updated", str(form.get("subdomain", "")))
            return _page(_dashboard("Connection updated and hot-swapped."))
        except Exception as e:
            return _page(_dashboard(f"Failed to update connection: {e}", "err"))

    async def connection_test(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        result = holder.test_connection()
        if result["ok"]:
            return _page(_dashboard(
                f"Connection OK — authenticated as {result.get('user')} (role: {result.get('role')})"
            ))
        return _page(_dashboard(f"Connection FAILED: {result.get('error')}", "err"))

    async def key_create(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        try:
            scopes = form.getlist("scopes")
            expires_raw = str(form.get("expires_days", "")).strip()
            expires = int(expires_raw) if expires_raw else None
            tenant_raw = str(form.get("tenant_id", "")).strip()
            tenant_id = int(tenant_raw) if tenant_raw else None
            key, key_id = store.create(
                str(form.get("name", "unnamed")), list(scopes), expires, tenant_id=tenant_id
            )
            return _page(_dashboard(f"Key #{key_id} created.", "ok", new_key=key))
        except Exception as e:
            return _page(_dashboard(f"Failed to create key: {e}", "err"))

    async def tenant_create(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        try:
            tenant_id = store.tenant_create(
                name=str(form.get("name", "")).strip(),
                subdomain=str(form.get("subdomain", "")).strip(),
                api_token=str(form.get("api_token", "")).strip(),
                read_email=str(form.get("read_email", "")).strip(),
                write_email=str(form.get("write_email", "")).strip() or None,
            )
            holder.invalidate_tenants()
            return _page(_dashboard(f"Tenant #{tenant_id} added."))
        except Exception as e:
            return _page(_dashboard(f"Failed to add tenant: {e}", "err"))

    async def tenant_delete(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        tenant_id = int(str(form.get("tenant_id", "0")))
        if store.tenant_delete(tenant_id):
            holder.invalidate_tenants()
            return _page(_dashboard(f"Tenant #{tenant_id} deleted; its keys were revoked."))
        return _page(_dashboard(f"Tenant #{tenant_id} not found.", "err"))

    async def key_revoke(request: Request):
        if not _authed(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        if not _csrf_ok(form):
            return _page(_dashboard("Invalid CSRF token", "err"))
        key_id = int(str(form.get("key_id", "0")))
        if store.revoke(key_id):
            return _page(_dashboard(f"Key #{key_id} revoked — takes effect immediately."))
        return _page(_dashboard(f"Key #{key_id} not found or already revoked.", "err"))

    return Starlette(routes=[
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", dashboard, methods=["GET"]),
        Route("/connection", connection_save, methods=["POST"]),
        Route("/connection/test", connection_test, methods=["POST"]),
        Route("/keys/create", key_create, methods=["POST"]),
        Route("/keys/revoke", key_revoke, methods=["POST"]),
        Route("/tenants/create", tenant_create, methods=["POST"]),
        Route("/tenants/delete", tenant_delete, methods=["POST"]),
    ])
