# Deployment and cutover

## Native Linux / systemd

Install the package into `/opt/noreply-gateway/.venv`. Create an unprivileged `noreply-gateway` service account, an operator-owned `/etc/noreply-gateway` configuration directory, and a state directory owned only by that service account:

```bash
sudo useradd --system --home-dir /var/lib/noreply-gateway \
    --shell /usr/sbin/nologin noreply-gateway
sudo install -d -m 0750 -o root -g noreply-gateway /etc/noreply-gateway
sudo install -d -m 0700 -o noreply-gateway -g noreply-gateway /var/lib/noreply-gateway
```

Run initialization as the service account, writing its initial configuration inside its writable state directory. Prompted WebUI passwords are not echoed:

```bash
sudo -u noreply-gateway /opt/noreply-gateway/.venv/bin/noreply-gateway init \
    --config /var/lib/noreply-gateway/gateway.toml \
    --data-dir /var/lib/noreply-gateway
sudo install -m 0640 -o root -g noreply-gateway \
    /var/lib/noreply-gateway/gateway.toml /etc/noreply-gateway/gateway.toml
```

Edit `/etc/noreply-gateway/gateway.toml` for the Microsoft application registration, client CIDRs and browser origin. A new installation can leave `account.sender` empty and select its From address in the web UI after Microsoft login. Set `account.sender` only when preserving a separate legacy route. Replace example account values with the values for your tenant; Graph is recommended for new deployments. To import local DavMail settings, add `--from-davmail PATH` during initialization and ensure that the service user can read that file; do not loosen its permissions globally. Add `--import-token` only when intentionally migrating the exposed base64 credential instead of performing a fresh login.

```bash
sudo install -m 0644 deploy/noreply-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now noreply-gateway
sudo journalctl -u noreply-gateway -f
```

The service logs its binding/backend and sanitized operational failure types. It does not log message bodies, OAuth codes, passwords or access/refresh tokens. There is no verbose HTTP-debug mode. Inspect the administrative queue for per-message errors. A systemd restart does not make an ambiguous send safe to replay; restart recovery deliberately holds it.

For a confidential OAuth Web app, set an appropriately protected `/etc/noreply-gateway/secrets.env` containing the environment variable named in `additional.client_secret_env` (or `account.client_secret_env` for a legacy route). This is the application's secret, not the user's Microsoft password or SMTP client password. Keep the variable out of process arguments, shell history and source control.

### Remote administration

The easiest private route is an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 administrator@gateway-server
```

Then use `http://127.0.0.1:8080` locally, matching the default `web.base_url` exactly. For an HTTPS service, adapt `deploy/nginx.conf` or [the separate Apache e10 example](../deploy/e10-internal-mailgateway.ext) and set the exact public HTTPS URL in `web.base_url`. For Apache, explicitly include the e10 file inside the intended HTTPS virtual host **before** its catch-all `ProxyPass /` mapping; keep `ProxyAddHeaders On` so the last forwarded address is the real client. A path-prefix deployment such as `https://example.org/internal/mailgateway` must forward that same prefix to the gateway and map the untrailed prefix so the application can redirect it to a trailing slash. The proxy must preserve the browser Host header. With non-default ports, include the port in the configured URL. Bind the HTTP listener only to a private interface reachable by the proxy and restrict that port to the proxy host. If `web.trusted_proxy_ips` is set, the trusted proxy must append the real client IP last in `X-Forwarded-For`; untrusted peers cannot supply the login rate-limit key.

For automatic login callback, register and configure the exact origin plus `/oauth/callback`. The connected Graph account may use the separate `[additional]` client ID, redirect URI, and client-secret environment variable, leaving a legacy sender's OAuth configuration unchanged. A native-client redirect instead uses manual URL paste-back. Public/native and confidential/Web Entra registration types have different requirements; choose the matching setup in the README. Test callback behavior under the tenant's actual MFA/Conditional Access rules.

## Docker on Linux

The compose example uses **host networking**, so configured listener bindings and actual SMTP peer IPs have their normal Linux meaning. It deliberately does not publish ports through a Docker NAT bridge. It is not a Windows/macOS Docker Desktop recipe.

From the package root:

```bash
mkdir runtime
sudo chown 10001:10001 runtime
sudo chmod 0700 runtime
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml run --rm gateway init \
    --config /runtime/gateway.toml --data-dir /runtime/state
```

Edit `runtime/gateway.toml` as root, retain private permissions, then:

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml logs -f
```

Do not change loopback binds until firewall and CIDR rules are ready. Mount an explicitly selected read-only file for properties import instead of copying it into the image. A confidential OAuth application secret can be supplied through a reviewed compose environment/secret arrangement; it is not bundled in the example. The root filesystem is read-only; all persistent queue/key state is in `runtime/`. Back up the entire state consistently while stopped.

The image build installs the declared dependencies from the configured Python package index. Pin an internally reviewed image digest and dependency lock for a controlled production rollout. The supplied examples were inspected but were not deployed against a real Docker daemon/systemd host here.

## Initial acceptance and cutover

1. Keep SMTP bound to loopback and application traffic on the old gateway. Validate configuration and WebUI login. Sign into the correct Microsoft account through the new UI. Do not run a load test against the actual mailbox.
2. Send one uniquely identified normal message to a controlled recipient. If a legacy sender is configured, also test one NAT message. Verify the visible From/Subject, MIME/attachment integrity, and Sent Items copy. Check message trace or actual receipt; `submitted` alone is not that proof.
3. Test To/Cc/Bcc and SMTP-only recipient handling with controlled addresses. The default preserves DavMail's recipient union; use `envelope_strict` when header-only recipients should instead be rejected.
4. Stop the old gateway, set the reviewed binds/CIDRs/firewall, then redirect application traffic. Keep the conservative upstream rate. Watch queue age, retained bytes, retries, quota use, and attention states.
5. For rollback, stop new intake, pause sending and preserve the queue. Reconcile any in-flight/uncertain entries before routing or replaying mail elsewhere. Do not start both gateways against the same submitted workload or erase the queue to make an alert disappear.

The package does not support 100 sustained Microsoft-mailbox sends/second. Decide whether the actual requirement is a short SMTP burst or a high-volume external delivery SLA before cutover. The latter needs a different approved sending service and backend.

## Operations

An operator pause is persisted and does not stop intake. Backoff is persisted across restart. Credentials are locally encrypted and rotated, while in-memory WebUI sessions are deliberately not persisted. Account disconnect removes local credentials and pauses delivery, but does not revoke Microsoft grants and cannot undo requests already in flight.

The web UI's Microsoft sending account uses `additional_account.enc` and its selected From address uses `additional_sender.enc` in the same private state directory. Back up or restore them with the vault key and queue, plus the original credential if a legacy sender exists. New installations can leave `account.sender` empty; the login supplies the default address. A configured legacy sender continues to serve its original From address. A missing or expired connected credential causes its messages to retry; inspect that queue before replacing the Microsoft identity.

Stop the service before `reset-admin`. Stop it before moving/restoring the private state directory. Preserve ownership and permissions. The copied configuration's `data_dir` is absolute, so moving only the TOML file does not move its queue.

Do not run multiple Python web workers: SMTP, rate state and queue lifecycle belong to one service process. Do not independently copy a live SQLite file without its WAL. An old backup can contain mail already accepted upstream after the backup was made; reconcile before resuming.

No automated bounce monitoring is included. Arrange mailbox/message-trace monitoring separately, and use the authenticated stats API or dashboard for local queue alerts. API mutations require a logged-in session, exact `Origin`, JSON body and `X-CSRF-Token`; do not disable those protections to make an external monitoring script writable.
