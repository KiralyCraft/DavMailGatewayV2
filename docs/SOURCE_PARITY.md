# Source inspection and compatibility boundaries

This comparison uses a patched DavMail sending export from the original development work. The source archive and its credentials are not distributed. Paths below identify files in that export; they are not claims about the current upstream DavMail release.

## Source-derived behavior

| Supplied source | Observed behavior | Python implementation |
|---|---|---|
| `src/java/davmail/smtp/SmtpConnection.java`, fields near lines 48 and 66–67, SMTP DATA path around 183–192 | Embedded account authenticates upstream; clients need not authenticate. `SUBJECT_NAT_PREFIX` is misnamed: `_NAT_` is tested against the first **From** identity. Remove all marker occurrences, append ` (Sender: ...)` to Subject, set From to embedded username. | `smtp.py`, `message.py`, encrypted single-account credentials |
| `src/java/davmail/exchange/ExchangeSession.java`, `convertResentHeader` / `sendMessage`, around 754–807 | Resent-From/To/Cc/Bcc/Message-ID replace ordinary equivalents. Header recipients retained; SMTP-only recipients added as Bcc. A last-message-ID field suppresses the immediately repeated ID. | Effective Resent headers, default union recipient policy; deliberate sender-ordering and dedup changes below |
| `src/java/davmail/exchange/ews/EwsExchangeSession.java`, MIME send around 458–490 | Base64 MIME in EWS CreateItem; SendAndSaveCopy or SendOnly; multipart/report item class | `backend.py` SOAP request and response classifier |
| `src/java/davmail/exchange/auth/O365Token.java` | Resource-based v1 OAuth refresh, persist a rotated refresh token | `migration.py`, `oauth.py` imported `legacy_v1` flow |
| `src/java/davmail/exchange/auth/O365Authenticator.java` and `O365InteractiveAuthenticator.java` | Default client ID, common tenant/native redirect, interactive EWS OAuth scope | Corresponding EWS defaults; new browser-admin flow is a reimplementation |
| `src/java/davmail/util/StringEncryptor.java` | Patched “encryption” is base64 encoding/decoding; encryption password is irrelevant | Explicit local token importer; real Fernet encryption for new stored records |

Non-secret settings relevant to the replacement included SMTP port 1025, O365Modern, one configured organizational sender, a Sent Items copy, the Exchange Online EWS endpoint, blank other protocol ports, and remote listening in the original. The original embedded password is unnecessary in this Python service and is not imported. Credential contents are deliberately excluded from this report and all output artifacts.

## Intentional changes, not silent parity claims

1. **Queued SMTP acknowledgement.** DavMail calls upstream synchronously before 250. Python commits the local queue before 250 and submits asynchronously. This enables burst absorption, but late failures need monitoring. It does not make provider quotas disappear.
2. **Effective sender validation.** Resent-From is applied before NAT and validation, so it cannot bypass the account restriction. Unmarked senders must equal the configured account; malformed or conflicting sender headers are rejected. Original envelope sender remains non-authoritative, as in the source.
3. **No last-Message-ID suppression.** The Java session records its last ID before upstream success and can silently discard a retry or legitimate reuse. Python queues separate submissions independently and keeps their Message-ID for correlation. This is not exactly-once delivery.
4. **Closed network defaults and protected administration.** Original unrestricted remote binding is not copied. Passwordless SMTP uses explicit trusted CIDRs plus the operator's firewall. The WebUI has its own password/session/CSRF controls.
5. **Credential protection.** No dependency on the patched fake encryption password; imported tokens are immediately encrypted. Fresh OAuth validates signed identity claims and requires the configured login identity.
6. **Retry/ambiguity accounting.** Definite throttling rejection retries with persisted cooldown. Ambiguous POST outcomes and crash-interrupted attempts are held rather than automatically duplicated. The daily ledger conservatively counts uncertain attempts; local cancellations do not undo possible upstream submission.
7. **Stale authentication headers.** Return-Path, DKIM-Signature, Authentication-Results and ARC assertions are stripped before the sending provider processes the rewritten message. Header formatting is reserialized. Body bytes remain untouched.
8. **Graph backend.** Added as a migration path in response to Microsoft's announced Exchange Online EWS retirement. It was not a behavior in the uploaded Java send path. MIME Graph submission always saves Sent Items in this implementation.
9. **Narrow protocol subset and bounds.** Single valid From, ASCII envelope addresses, CRLF framing, bounded message/line/recipient/buffer/queue size, and no other mail/calendar protocols. The pathological case of several From addresses is rejected rather than emulating first-address-only NAT.

## External verification, distinct from source inspection

Checked September 10, 2026. These sources informed the new implementation and operational warnings, not the interpretation of the uploaded patches.

- Microsoft Exchange Online limits: <https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits>
- Microsoft Graph service-specific throttling: <https://learn.microsoft.com/en-us/graph/throttling-limits>
- Microsoft Graph MIME sendMail and 202 semantics: <https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0>
- Microsoft Exchange Team EWS retirement update: <https://techcommunity.microsoft.com/blog/exchange/exchange-online-ews-your-time-is-almost-up/4492361>
- EWS send/CreateItem: <https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/how-to-send-email-messages-by-using-ews-in-exchange>
- Microsoft authorization code protocol: <https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow>
- Microsoft ID token claims: <https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference>
- SMTP protocol: <https://www.rfc-editor.org/info/rfc5321/>
- SQLite WAL: <https://www.sqlite.org/wal.html>

No attempt was made to sign into the actual tenant, refresh its token, send a message, inspect its mailbox, alter consent, or revoke its credentials.
