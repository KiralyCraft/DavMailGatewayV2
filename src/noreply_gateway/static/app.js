"use strict";
const appBase = new URL(".", document.currentScript.src);
const appUrl = (path) => new URL(path.replace(/^\/+/, ""), appBase);
let csrf = "";
let before = null;
let lastRows = [];
let refreshing = false;
let oauthKind = "original";
let senderDialogDismissed = false;
const element = (id) => document.getElementById(id);
const number = (value) => Number(value || 0).toLocaleString();
const bytes = (value) => value >= 1073741824 ? (value / 1073741824).toFixed(2) + " GiB" : (value / 1048576).toFixed(1) + " MiB";
function notice(text, success = false) { const node = element("notice"); node.textContent = text; node.className = success ? "success" : ""; node.hidden = false; }
function formStatus(id, value) { const node = element(id); node.textContent = value; node.hidden = !value; }
async function api(path, payload) {
    const options = {credentials: "same-origin", headers: {}};
    if (payload !== undefined) { options.method = "POST"; options.headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}; options.body = JSON.stringify(payload); }
    const response = await fetch(appUrl(path), options);
    let data;
    try { data = await response.json(); } catch { throw new Error("Gateway returned HTTP " + response.status + " without a readable response"); }
    if (response.ok === false) { if (response.status === 401) showLogin(); throw new Error(data.error || "Request failed"); }
    return data;
}
function showLogin() { csrf = ""; if (element("sender-dialog").open) element("sender-dialog").close(); text("default-sender", ""); element("custom-sender").value = ""; element("login-panel").hidden = false; element("dashboard").hidden = true; element("logout").hidden = true; text("status-pill", "Sign in required"); element("status-pill").className = "pill"; }
function showDashboard() { element("login-panel").hidden = true; element("dashboard").hidden = false; element("logout").hidden = false; }
async function guarded(action) { try { await action(); } catch (error) { notice(error.message); } }
function text(id, value) { element(id).textContent = value; }
function stateBadge(state) { const node = document.createElement("span"); node.className = "pill" + (["uncertain", "failed", "retry"].includes(state) ? " warn" : ""); node.textContent = state; return node; }
function renderStats(data) {
    const c = data.counters, s = data.states;
    text("accepted", number(c.accepted)); text("submitted", number(c.submitted));
    text("queued", number((s.queued || 0) + (s.retry || 0) + (s.sending || 0)));
    text("attention", number((s.failed || 0) + (s.uncertain || 0)));
    text("oldest", data.oldest_pending_seconds ? "Oldest pending: " + Math.floor(data.oldest_pending_seconds / 60) + " min" : "No pending messages");
    element("legacy-route").hidden = !data.sender;
    text("sender", data.sender); text("backend", data.backend.toUpperCase()); text("login-username", data.login_username);
    text("credential-mode", data.account.credential_origin); text("sent-items", data.save_in_sent ? "Save a copy" : "Do not save");
    text("nat-marker", data.nat_marker + " in From");
    text("account-state", data.account.credential_present ? (data.account.needs_login ? "Original account needs Microsoft sign-in again." : data.account.last_error || (data.account.last_refresh ? "Original account connected." : "Original credential present; not yet verified with Microsoft.")) : "Original account disconnected. Its messages cannot be delivered until reconnected.");
    element("account-disconnect").disabled = !data.account.credential_present;
    const additional = data.additional;
    if (additional) {
        text("additional-state", additional.account.needs_login && additional.account.credential_present ? "Microsoft sign-in required again." : additional.selected_sender ? "Microsoft account connected. Mail from the selected address will use this account." : additional.default_sender ? "Microsoft account connected; choose a From address." : "No Microsoft account connected.");
        text("additional-identity", additional.account.credential_present ? additional.account.username : "Not connected");
        text("additional-default", additional.default_sender || "—");
        text("additional-from", additional.selected_sender || "—");
        element("additional-delivery").hidden = !data.paused;
        element("sender-change").hidden = !additional.default_sender;
        if (additional.choice_needed && !senderDialogDismissed && !element("sender-dialog").open) openSenderDialog(additional);
    }
    element("ews-warning").hidden = data.backend !== "ews";
    const state = data.healthy === false ? "Queue fault" : data.paused ? "Paused" : data.cooldown_remaining > 0 ? "Backing off" : "Running";
    text("status-pill", state); text("run-state", state);
    element("status-pill").className = "pill" + (state === "Running" ? "" : " warn");
    text("delivery-state", number(data.upstream_inflight) + " upstream requests in flight" + (data.cooldown_remaining > 0 ? " · Retry-After: " + Math.ceil(data.cooldown_remaining) + "s" : ""));
    if (document.activeElement !== element("rate")) element("rate").value = data.rate;
    text("quota", number(data.recipient_budget_used_24h) + " / " + (data.recipient_limit_24h ? number(data.recipient_limit_24h) : "local guard disabled"));
    text("spool", bytes(c.retained_bytes || 0) + " / " + bytes(data.max_queue_bytes));
    text("connections", number(data.smtp_connections)); text("listener", data.smtp_host + ":" + data.smtp_port + " · no AUTH");
    text("networks", data.allowed_networks.join(", "));
    text("updated", "Updated " + new Date().toLocaleTimeString());
    renderActivity(data.buckets);
}
function renderActivity(buckets) {
    const byMinute = new Map();
    for (const row of buckets) { if (byMinute.has(row.minute) === false) byMinute.set(row.minute, {}); byMinute.get(row.minute)[row.name] = row.value; }
    const now = Math.floor(Date.now() / 60000) * 60;
    const values = Array.from({length: 60}, (_, i) => { const minute = now - (59 - i) * 60; return {minute, ...(byMinute.get(minute) || {})}; });
    const maximum = Math.max(1, ...values.flatMap((row) => [row.accepted || 0, row.submitted || 0]));
    const fragment = document.createDocumentFragment();
    for (const row of values) {
        const group = document.createElement("div"); group.className = "minute";
        group.title = new Date(row.minute * 1000).toLocaleTimeString() + ": " + number(row.accepted) + " accepted / " + number(row.submitted) + " submitted";
        // SVG avoids inline CSS so the restrictive style-src policy stays intact.
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 10 100"); svg.setAttribute("width", "100%"); svg.setAttribute("height", "100%"); svg.setAttribute("preserveAspectRatio", "none");
        for (const [index, name] of ["accepted", "submitted"].entries()) { const height = Math.max(1, (row[name] || 0) / maximum * 100); const rect = document.createElementNS(svg.namespaceURI, "rect"); rect.setAttribute("x", String(index * 5)); rect.setAttribute("y", String(100 - height)); rect.setAttribute("width", "4"); rect.setAttribute("height", String(height)); rect.setAttribute("fill", index === 0 ? "#a7bbb8" : "#12675f"); svg.append(rect); }
        group.append(svg); fragment.append(group);
    }
    element("activity").replaceChildren(fragment);
}
function renderMessages(rows) {
    lastRows = rows;
    const fragment = document.createDocumentFragment();
    if (rows.length === 0) { const row = document.createElement("tr"), cell = document.createElement("td"); cell.colSpan = 6; cell.className = "empty"; cell.textContent = "No messages in this view."; row.append(cell); fragment.append(row); }
    for (const item of rows) {
        const row = document.createElement("tr");
        const cells = Array.from({length: 6}, () => document.createElement("td"));
        cells[0].textContent = new Date(item.created * 1000).toLocaleString(); cells[1].append(stateBadge(item.status)); cells[2].className = "message";
        const subject = document.createElement("span"); subject.className = "subject"; subject.textContent = item.subject || "(no subject)"; cells[2].append(subject);
        for (const detail of [item.original_from + (item.nat ? " · NAT" : ""), item.id, item.error]) { if (!detail) continue; const node = document.createElement("span"); node.className = "detail"; node.textContent = detail; cells[2].append(node); }
        cells[3].textContent = number(item.recipient_count); cells[4].textContent = number(item.attempts);
        const actions = document.createElement("div"); actions.className = "row-actions";
        if (["submitted", "cancelled", "sending"].includes(item.status) === false) {
            const download = document.createElement("a"); download.href = appUrl("/api/messages/" + encodeURIComponent(item.id) + "/eml"); download.textContent = "Export"; actions.append(download);
            for (const action of ["retry", "cancel"]) {
                const button = document.createElement("button"); button.className = "small quiet"; button.textContent = action === "retry" ? "Retry" : "Cancel";
                button.addEventListener("click", () => guarded(async () => {
                    const uncertain = item.status === "uncertain";
                    const question = action === "retry" ? (uncertain ? "The previous attempt may already have sent this message. Retrying can create a duplicate. Retry anyway?" : "Start a fresh retry budget for this message?") : "Cancel this queued message and delete its local body? This does not recall any message already accepted upstream.";
                    if (window.confirm(question) === false) return;
                    await api("/api/messages/" + encodeURIComponent(item.id) + "/" + action, {acknowledge_duplicate: uncertain}); await refresh();
                })); actions.append(button);
            }
        }
        cells[5].append(actions); row.append(...cells); fragment.append(row);
    }
    element("message-rows").replaceChildren(fragment);
    element("older").disabled = rows.length < 100;
}
async function refresh() {
    if (csrf === "" || refreshing) return;
    refreshing = true;
    try { const params = new URLSearchParams({status: element("filter").value}); if (before !== null) { params.set("before", String(before.created)); params.set("before_id", before.id); } const [stats, rows] = await Promise.all([api("/api/stats"), api("/api/messages?" + params)]); renderStats(stats); renderMessages(rows); } finally { refreshing = false; }
}
element("login-form").addEventListener("submit", (event) => { event.preventDefault(); guarded(async () => { const result = await api("/api/login", {password: element("password").value}); element("password").value = ""; csrf = result.csrf; senderDialogDismissed = false; element("notice").hidden = true; showDashboard(); await refresh(); }); });
element("logout").addEventListener("click", () => guarded(async () => { await api("/api/logout", {}); showLogin(); }));
element("pause").addEventListener("click", () => guarded(async () => { await api("/api/control", {paused: true}); await refresh(); }));
element("resume").addEventListener("click", () => guarded(async () => { await api("/api/control", {paused: false}); await refresh(); }));
element("rate-form").addEventListener("submit", (event) => { event.preventDefault(); guarded(async () => { const rate = Number(element("rate").value); if (rate > 0.5 && window.confirm("This exceeds the default Microsoft mailbox sending rate. It does not increase provider quotas. Apply anyway?") === false) return; await api("/api/control", {rate}); await refresh(); }); });
function openSenderDialog(data) {
    formStatus("sender-status", "");
    text("default-sender", data.default_sender);
    const mode = data.mode === "custom" ? "custom" : "default";
    element("sender-form").elements["sender-mode"].value = mode;
    element("custom-sender").value = mode === "custom" ? data.selected_sender : "";
    element("custom-sender").required = mode === "custom";
    element("sender-dialog").showModal();
}
async function beginMicrosoftLogin(kind, path) {
    oauthKind = kind;
    const data = await api(path, {});
    if (data.redirect_uri === appUrl("oauth/callback").href) {
        window.location.assign(data.authorization_url);
        return;
    }
    formStatus("oauth-status", "");
    element("authorization-link").href = data.authorization_url;
    element("oauth-panel").hidden = false;
}
element("account-login").addEventListener("click", () => guarded(() => beginMicrosoftLogin("original", "/api/account/login")));
element("additional-login").addEventListener("click", () => guarded(() => beginMicrosoftLogin("additional", "/api/additional/login")));
element("oauth-form").addEventListener("submit", (event) => { event.preventDefault(); const button = element("oauth-submit"); if (button.disabled) return; button.disabled = true; formStatus("oauth-status", "Completing Microsoft login…"); (async () => { try { const value = element("redirect-url").value; const result = await api("/api/account/complete", {redirect_url: value}); element("oauth-panel").hidden = true; element("redirect-url").value = ""; senderDialogDismissed = false; notice(oauthKind === "additional" ? "Microsoft account connected. Choose the From address." : "Account connected. Resume delivery when you are ready.", true); await refresh(); if (oauthKind === "additional" && result.additional && !element("sender-dialog").open) openSenderDialog(result.additional); } catch (error) { formStatus("oauth-status", error.message + ". Start a new Microsoft login before retrying if the code was used."); } finally { button.disabled = false; } })(); });
element("sender-change").addEventListener("click", () => guarded(async () => { const data = await api("/api/stats"); openSenderDialog(data.additional); }));
element("sender-form").addEventListener("change", () => { element("custom-sender").required = element("sender-form").elements["sender-mode"].value === "custom"; });
element("sender-dialog").addEventListener("close", () => { senderDialogDismissed = true; });
element("sender-later").addEventListener("click", () => element("sender-dialog").close());
element("sender-form").addEventListener("submit", (event) => { event.preventDefault(); const button = element("sender-save"); if (button.disabled) return; button.disabled = true; formStatus("sender-status", "Saving sender choice…"); (async () => { try { const mode = element("sender-form").elements["sender-mode"].value; await api("/api/additional/sender", {mode, custom_sender: element("custom-sender").value}); element("sender-dialog").close(); notice("From address selected. Send a test before using it for automated mail.", true); await refresh(); } catch (error) { formStatus("sender-status", error.message); } finally { button.disabled = false; } })(); });
element("account-disconnect").addEventListener("click", () => guarded(async () => { if (window.confirm("Disconnect the original account and pause ALL delivery, including the additional sender? Requests already in flight may finish.") === false) return; await api("/api/account/disconnect", {}); await refresh(); }));
element("filter").addEventListener("change", () => guarded(async () => { before = null; await refresh(); }));
element("older").addEventListener("click", () => guarded(async () => { if (lastRows.length) before = lastRows[lastRows.length - 1]; await refresh(); }));
element("newest").addEventListener("click", () => guarded(async () => { before = null; await refresh(); }));
guarded(async () => { try { const session = await api("/api/session"); csrf = session.csrf; showDashboard(); await refresh(); } catch (error) { showLogin(); } });
setInterval(() => { if (document.hidden === false && csrf) guarded(refresh); }, 3000);
