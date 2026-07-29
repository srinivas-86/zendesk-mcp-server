# Windows Server Setup Guide (EC2 / bare metal, no Docker)

Complete, battle-tested guide for deploying the Zendesk MCP Server on a Windows
machine with your own TLS certificate, Caddy as the HTTPS proxy, and NSSM
running everything as auto-starting Windows services. Every step and every FAQ
entry below comes from a real deployment on a Windows EC2 instance.

Target layout used throughout (adjust drive/paths to taste):

```
D:\zendesk-mcp\            app (git clone + venv + data)
D:\caddy_windows_amd64.exe Caddy binary
D:\Caddyfile.txt           Caddy config
D:\certs\                  TLS certificate files
```

---

## 1. Prerequisites

- Windows Server 2019+ (or Windows 10/11), Administrator access.
- AWS security group / firewall: inbound TCP **80** and **443** open. Do NOT
  open 8000 (app) or 9000 (admin console) — those stay loopback-only.
- A DNS A record for your domain (e.g. `mcp.example.com`) pointing at the
  machine's public IP.
- A TLS certificate for that domain (leaf `.cer` + private `.key` + the CA's
  intermediate certificate — see §5).
- Windows Firewall on the box:

  ```powershell
  New-NetFirewallRule -DisplayName "HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow
  ```

Install Python 3.11+ and Git if missing:

```powershell
winget install Python.Python.3.12 Git.Git --accept-package-agreements --accept-source-agreements
# reopen PowerShell afterwards so PATH refreshes
```

## 2. Git clone

```powershell
git clone https://github.com/srinivas-86/zendesk-mcp-server.git D:\zendesk-mcp
cd D:\zendesk-mcp
```

## 3. Python venv + pip install

```powershell
cd D:\zendesk-mcp
python -m venv .venv
.\.venv\Scripts\pip install .
mkdir D:\zendesk-mcp\data
```

`pip install .` installs the server plus all dependencies (FastMCP, Zenpy,
truststore, …) and creates two executables inside the venv:

- `D:\zendesk-mcp\.venv\Scripts\zendesk.exe` — the server
- `D:\zendesk-mcp\.venv\Scripts\zendesk-keys.exe` — API key management CLI

> **Note:** `pip install .` copies the code into the venv's `site-packages`.
> After any `git pull`, run `pip install .` again — editing files under
> `D:\zendesk-mcp\src` alone does NOT change the running server.

## 4. Configure `.env`

```powershell
copy .env.example .env
notepad .env
```

Minimum working configuration:

```ini
# Zendesk connection
ZENDESK_SUBDOMAIN=yourcompany          # yourcompany.zendesk.com
ZENDESK_API_KEY=your-zendesk-api-token
ZENDESK_EMAIL=agent@yourcompany.com

# Optional Layer-4 dual identity (reads via restricted user, writes via full agent)
#ZENDESK_READ_EMAIL=mcp-reader@yourcompany.com
#ZENDESK_WRITE_EMAIL=mcp-writer@yourcompany.com

# Transport — HTTP for remote use
MCP_TRANSPORT=http
MCP_HOST=127.0.0.1        # IMPORTANT: only Caddy should reach the app directly
MCP_PORT=8000
MCP_KEYS_DB=D:\zendesk-mcp\data\keys.db

# Web admin console (loopback port 9000; enabled only when password is set)
MCP_ADMIN_PASSWORD=a-long-random-password
```

Explanation of the important choices:

- `MCP_HOST=127.0.0.1` — the app itself has no TLS; binding to loopback means
  the only way in from outside is through Caddy on 443.
- `MCP_KEYS_DB` — SQLite file holding API keys (hashed), tenants, admin
  config, and the audit log. Back this file up.
- The admin console (`MCP_ADMIN_PASSWORD`) listens on `127.0.0.1:9000`. Reach
  it by RDP-ing into the box and opening `http://localhost:9000`, or via an
  SSH tunnel. Never publish it through Caddy.

## 5. Certificate configuration (incl. chain merge)

You typically receive three files from your CA (DigiCert/GeoTrust example):

| File | Role |
|---|---|
| `vcollab.cer` | Leaf — your domain's certificate |
| `GeoTrust_TLS_RSA_CA_G1.cer` | **Intermediate CA** — required in the served chain |
| `DigiCert_Global_Root_G2.cer` | Root — NOT served; clients already have it |
| `vcollab.key` | Private key |

**Critical step — merge leaf + intermediate into a full chain.** If you serve
only the leaf, browsers may still work (they fetch missing intermediates), but
Node.js and Python clients will fail with `UNABLE_TO_VERIFY_LEAF_SIGNATURE`.
Order matters: leaf first, intermediate second, root omitted.

```powershell
cmd /c copy /b D:\certs\vcollab.cer + D:\certs\GeoTrust_TLS_RSA_CA_G1.cer D:\certs\vcollab-fullchain.cer
```

Verify later with `curl.exe -v https://mcp.example.com/health` — it must
succeed **without** `-k`.

## 6. Caddy install + config

Download the Windows binary:

```powershell
Invoke-WebRequest "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile D:\caddy_windows_amd64.exe
```

Create `D:\Caddyfile.txt`:

```
:443 {
    tls D:\certs\vcollab-fullchain.cer D:\certs\vcollab.key
    reverse_proxy localhost:8000 {
        flush_interval -1
        transport http {
            read_timeout 300s
            write_timeout 300s
        }
    }
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        -Server
    }
}
```

Why each piece matters:

- `tls <fullchain> <key>` — your own certificate (skip this line entirely and
  put your domain name instead of `:443` if you want Caddy's automatic
  Let's Encrypt certificates).
- `flush_interval -1` + 300 s timeouts — MCP Streamable HTTP holds long-lived
  SSE streams; default proxy buffering/timeouts cut them off mid-response.

Config lifecycle commands (run from where the exe lives):

```powershell
.\caddy_windows_amd64.exe run    --config .\Caddyfile.txt   # foreground run
.\caddy_windows_amd64.exe reload --config .\Caddyfile.txt   # push config into the RUNNING instance
.\caddy_windows_amd64.exe fmt --overwrite .\Caddyfile.txt   # fix formatting warnings
```

> **Gotcha:** `reload` changes only the running instance's in-memory config.
> Whatever config file Caddy loads at **startup** wins after a reboot — make
> sure the service (§8) points at the right file.

## 7. First manual test

Terminal 1 — start the app:

```powershell
D:\zendesk-mcp\.venv\Scripts\zendesk.exe
# expect: "Starting Streamable HTTP transport on 127.0.0.1:8000/mcp"
```

Terminal 2 — start Caddy, then verify both hops:

```powershell
D:\caddy_windows_amd64.exe run --config D:\Caddyfile.txt

curl.exe http://127.0.0.1:8000/health          # app directly    -> {"status":"ok",...}
curl.exe https://mcp.example.com/health        # through Caddy   -> {"status":"ok",...}
```

Create your first API key:

```powershell
cd D:\zendesk-mcp
$env:MCP_KEYS_DB="D:\zendesk-mcp\data\keys.db"
.\.venv\Scripts\zendesk-keys.exe create --name "first-key" --scopes tickets:read,kb:read
# For a key that can also write tickets:
# ... --scopes tickets:read,tickets:write,kb:read --expires-days 30
```

The `zmk_...` key is shown **once**. Keys are stored only as SHA-256 hashes.

## 8. NSSM — run both as Windows services

Running from console windows dies on logoff/reboot. NSSM ("Non-Sucking
Service Manager") wraps any exe as a real Windows service.

```powershell
winget install NSSM.NSSM     # reopen PowerShell afterwards
```

Stop the manual console instances from §7 first (Ctrl+C), then:

```powershell
# App service
nssm install zendesk-mcp "D:\zendesk-mcp\.venv\Scripts\zendesk.exe"
nssm set zendesk-mcp AppDirectory D:\zendesk-mcp
nssm set zendesk-mcp AppStdout D:\zendesk-mcp\data\app.log
nssm set zendesk-mcp AppStderr D:\zendesk-mcp\data\app.log
nssm start zendesk-mcp

# Caddy service
nssm install caddy "D:\caddy_windows_amd64.exe" "run --config D:\Caddyfile.txt"
nssm set caddy AppDirectory D:\
nssm start caddy
```

Key settings explained:

- `AppDirectory` — the service's working directory. **The app finds `.env`
  here**; get this wrong and the server starts with missing config.
- `AppStdout/AppStderr` — console output goes to a log file instead of a
  window: `Get-Content D:\zendesk-mcp\data\app.log -Tail 50 -Wait`.
- NSSM defaults to startup type *Automatic* and restarts the process if it
  crashes.

### NSSM GUI and daily commands

NSSM has **no Start-Menu app** — its GUI opens only from the command line:

```powershell
nssm edit zendesk-mcp     # settings dialog for an existing service
nssm edit caddy
nssm install <newname>    # same dialog, for creating a service interactively
```

The dialog tabs let you change the exe, arguments, AppDirectory, log files,
exit/restart behavior, and the account the service runs as.

Daily driving:

```powershell
nssm status zendesk-mcp            # SERVICE_RUNNING / SERVICE_STOPPED
Get-Service zendesk-mcp, caddy     # both at once
nssm restart zendesk-mcp           # after .env or code changes
nssm stop caddy / nssm start caddy
nssm remove zendesk-mcp            # uninstall (confirmation dialog)
services.msc                       # Windows' own services GUI — both appear there
```

Final acceptance test: **reboot the machine**. If
`curl.exe https://mcp.example.com/health` answers with nobody logged in, the
stack is production-ready.

## 9. Connect Claude Desktop (Windows client machine)

Claude Desktop's config speaks stdio, so the `mcp-remote` bridge forwards to
your HTTPS endpoint with the Bearer key. Requires Node.js **18+** on the
client machine.

Install the bridge globally (avoids npx flakiness):

```powershell
npm install -g mcp-remote
npm root -g     # note the printed path
```

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "zendesk": {
      "command": "C:\\PROGRA~1\\nodejs\\node.exe",
      "args": [
        "C:\\PROGRA~1\\nodejs\\node_modules\\mcp-remote\\dist\\proxy.js",
        "https://mcp.example.com/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer zmk_YOUR_KEY"
      }
    }
  }
}
```

Every quirk in that config is deliberate — see the FAQ:

- `C:\PROGRA~1\...` — 8.3 short name; Claude Desktop doesn't quote paths, so a
  space in `C:\Program Files` breaks the command (FAQ #4).
- Direct `node.exe` + `proxy.js` — bypasses npx/npm and the PATH entirely, so
  stale Node versions in PATH can't hijack the process (FAQ #3).
- `Authorization:${AUTH_HEADER}` + env var — spaces inside args get split on
  Windows, which silently deletes the word `Bearer` (FAQ #5).
- URL with forward slashes — backslashes break it (FAQ #5).

Then fully quit Claude Desktop (system tray → Quit — closing the window is not
enough) and relaunch. Logs live at `%APPDATA%\Claude\logs\mcp-server-*.log`.

## 10. Updating the server

```powershell
cd D:\zendesk-mcp
git pull
.\.venv\Scripts\pip install .
nssm restart zendesk-mcp
```

---

## FAQ — every problem we actually hit, and the fix

**1. `git push` rejected: "refusing to allow a Personal Access Token to create or update workflow .github/workflows/ci.yml without workflow scope"**
GitHub requires a special token scope to push workflow files. Either add the
`workflow` scope to your PAT (classic: tick the checkbox; fine-grained:
Workflows → Read and write), push over SSH instead, or remove the workflow
file from the commit. We removed it and re-added CI later.

**2. Caddy won't start: `listen tcp 127.0.0.1:2019: bind: Only one usage of each socket address`**
Another Caddy instance is already running (2019 is Caddy's admin port). Either
push the new config into the running instance —
`caddy reload --config .\Caddyfile.txt` — or find and stop the old one:
`Get-Process caddy* | Select-Object Path`, then `Stop-Process -Name caddy* -Force`.
Remember `reload` doesn't survive reboots; fix the startup config too (§8).

**3. Claude Desktop: `npm does not support Node.js v15.x` + `SyntaxError ... node:fs/promises does not provide an export named 'constants'`**
Claude Desktop inherited a stale PATH whose first entries pointed at ancient
Node versions (old nvm folders), so `npx mcp-remote` ran under Node 15 —
mcp-remote needs Node 18+. Your terminal showing Node 24 is irrelevant; the
*app's* PATH is what counts. Fix: bypass PATH completely — call the correct
`node.exe` by absolute path on mcp-remote's `proxy.js` (§9). (Also worth
deleting the stale `...\nvm\v15.x`/`v16.x` entries from User/System Path.)

**4. Claude Desktop: `'C:\Program' is not recognized as an internal or external command`**
Claude Desktop launches commands via `cmd /c` **without quoting**, so any
space in the command path splits it. Use the 8.3 short name:
`C:\PROGRA~1\nodejs\node.exe`. Same trick for any spaced path.

**5. Server returns 401 / log shows header became `Authorization: zmk_...` (the word `Bearer` vanished) and URL became `https:\host\mcp`**
Two separate Windows arg-mangling bugs: (a) spaces inside a single arg get
split, so `"Authorization: Bearer zmk_x"` loses `Bearer` — pass the header as
`Authorization:${AUTH_HEADER}` with the value (including `Bearer `) in the
`env` block; (b) forward slashes in the URL were typed/escaped as backslashes —
the URL in JSON must be plain `https://host/mcp`.

**6. Client: `UNABLE_TO_VERIFY_LEAF_SIGNATURE` / "unable to verify the first certificate"**
The server was serving only the leaf certificate without the intermediate CA.
Browsers tolerate this; Node/Python don't. Fix properly by serving the full
chain (§5 merge + point Caddy at `-fullchain.cer`). Client-side stopgap: add
`--use-system-ca` as the first `node.exe` argument (uses the Windows cert
store).

**7. Server-side Zendesk calls fail: `SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate (_ssl.c)`**
The Python server couldn't verify `*.zendesk.com` — fresh Windows machines
haven't cached the intermediates and Python doesn't fetch them the way
browsers do (also happens behind corporate TLS-inspection proxies). Fix: make
Python use the OS trust store via **truststore**. Current code does this
automatically at startup; for an already-deployed build, drop-in fix without
redeploying:

```powershell
D:\zendesk-mcp\.venv\Scripts\pip install truststore
Set-Content -Path "D:\zendesk-mcp\.venv\Lib\site-packages\sitecustomize.py" -Value "import truststore`ntruststore.inject_into_ssl()" -Encoding ascii
nssm restart zendesk-mcp
```

Test: `.venv\Scripts\python.exe -c "import urllib.request; print(urllib.request.urlopen('https://www.zendesk.com').status)"` —
an **HTTP 403** here is SUCCESS (TLS handshake worked; the 403 is just
zendesk.com's WAF disliking bare scripts). Only `CERTIFICATE_VERIFY_FAILED`
means it's still broken.

**8. `SSL_CERT_FILE` in `.env` didn't help**
Two reasons: the path pointed at the wrong drive (`C:\...` while the install
lived on `D:\`), and the truststore approach (#7) supersedes it anyway. If you
use `SSL_CERT_FILE`, verify the exact path first:
`.venv\Scripts\python -c "import certifi; print(certifi.where())"`.

**9. Zendesk API 400: `{"error":"InvalidPaginationParameter","description":"sort is not valid"}`**
Zendesk **cursor pagination** only accepts `sort` fields `id`, `status`,
`updated_at` — not `created_at`/`priority`. Fixed in code by mapping
`created_at → id` (identical ordering). If you see this, your deployed build
predates the fix: `git pull` + `pip install .` + restart.

**10. After every server restart the log shows a burst of `POST /mcp → 404` and `GET /mcp → 400`**
Normal and self-healing. Streamable HTTP sessions live in server memory;
connected clients still hold old `Mcp-Session-Id`s, the spec says answer 404,
and that's the signal that makes clients re-initialize (you'll see
`Created new transport with session ID: ...` then all 200s).

**11. Log shows `GET /.well-known/oauth-protected-resource → 404` (three times)**
Also normal in `MCP_AUTH_MODE=keys`. mcp-remote probes the OAuth discovery
endpoints before falling back to the Bearer header. In `oauth`/`both` mode
these same URLs return 200 with the RFC 9728 metadata.

**12. Ctrl+C prints `ASGI callable returned without completing response` and a big `KeyboardInterrupt` traceback**
Cosmetic. The ASGI errors are the open SSE streams being cut by shutdown; the
traceback is Python surfacing your Ctrl+C. Current code catches it and exits
with one clean log line. Better yet: run as a service (§8) and use
`nssm restart` instead of Ctrl+C.

**13. First MCP call after a restart times out (`MCP error -32001: Request timed out`)**
Cold-start + session re-establishment. Just retry — the second call goes
through.

**14. I edited files in `D:\zendesk-mcp\src` but nothing changed**
The venv runs the copy in `site-packages` (installed via `pip install .`),
not your working tree. Re-run `pip install .` and restart the service. (Or
install editable — `pip install -e .` — during development.)

**15. Where do I see which permissions an API key has?**
Admin console (`http://localhost:9000` on the server) lists every key with its
scopes, tenant, expiry, last-used time, and revocation status — or
`zendesk-keys list` on the box. Scope meanings: `tickets:read` → the 9 read
tools; `kb:read` → article search + KB resource; `tickets:write` → create/
update/comment/upload (these tools are invisible to keys without it);
`*` → everything.
