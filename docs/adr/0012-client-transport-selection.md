# ADR 0012: How the desktop client reaches the service

- Status: Accepted
- Date: 2026-08-11

## Context

The HTTP adapter ([ADR 0009](0009-http-api-adapter.md)) binds loopback on the
service host and refuses to bind anything else
(`validate_loopback_bind`, `src/wuwaterm_api/settings.py`). The desktop client
([ADR 0011](0011-pc-client-stack.md)) runs on the owner's own machine, which
is not that host. Something has to carry requests between them, and that
something is a decision with a blast radius: it is the first time this
deployment has had any inbound application surface at all.

An earlier draft of this project answered the question with the operator's
shell-access channel to the host. **That answer is revoked.** Host shell
access is an administration channel: it is how the operator deploys and
issues credentials, and it is never how the application reaches the service.
Requiring it would mean the desktop application could not start without an
administrative session, and that a translation request and a root shell would
travel the same credential.

Two candidates remained, and the choice was made from inspected facts about
the target host rather than from preference.

## Decision

**The client reaches the service over public HTTPS, through the reverse proxy
that already serves the operator's existing sites on the same host, on a path
route that forwards to the API's loopback port.** Device authentication
([ADR 0010](0010-device-principal-authentication.md)) is mandatory on every
`/v1` call regardless of how the request arrived.

The alternative — a private overlay network between the two machines — was
rejected on facts, not on preference: no overlay software of any kind is
installed on the host, and no overlay interface exists on it. Selecting it
would have meant installing host-level networking software whose operational
impact and rollback are not established, which is an owner decision and not an
executor's. It remains the documented future migration path (below).

### What was NOT changed, and why that matters

The selected route adds no new externally reachable port, no DNS record, no
certificate, no certificate-authority account and no firewall rule:

- The proxy already terminates TLS on the standard port for the existing
  sites, so the route is an addition inside a configuration that is already
  serving traffic.
- Its certificates are installed statically from files. Adding a route
  therefore triggers no automated certificate issuance and no account
  activity of any kind.
- The name used already resolves to that host, because it already serves
  traffic there. Nothing in DNS changes.
- The API keeps its loopback bind. The proxy dials `127.0.0.1`; the service
  itself is not reachable from outside the host at any point.

Everything beyond that one route — a new open port, a new name, a firewall
change, a purchased certificate — is an owner decision, and this record does
not authorize any of it.

### Port

The API's default port moved to **8788**. The previously documented default
was already bound on the target host by an unrelated service of the owner's,
and was the upstream of that host's existing proxy routes; deploying on it
would have taken over a running service rather than adding one. The port is a
setting (`WUWATERM_API_PORT`), so nothing in the contract or the client
depends on the number — but the default should be a value that works where
this software is actually deployed.

### Trust boundary

```
owner's PC                     public internet          service host
+-------------------+                                  +--------------------+
| desktop client    |  https, certificate verified     | reverse proxy      |
|  device token in  | -------------------------------> |  TLS terminates    |
|  the OS vault     |                                  |  path route only   |
+-------------------+                                  |        |           |
                                                       |        v 127.0.0.1 |
                                                       | wuwaterm-api       |
                                                       |  device auth on    |
                                                       |  every /v1 call    |
                                                       |        |           |
                                                       |        v           |
                                                       | application layer  |
                                                       | terms.db (ro), LLM |
                                                       +--------------------+
```

The boundary that matters is the one at the API process, not the one at the
proxy. Everything outside `wuwaterm-api` is untrusted, including anything that
has already passed TLS: the proxy proves who the SERVER is to the client, and
proves nothing at all about the client to the server.

### Why network access does not replace application authentication

This is the whole reason the decision is safe to write down.

Reaching an endpoint is not an authorization. A network arrangement — a
public route, a private overlay, a local network — answers "can these packets
arrive?", and the answer is a property of the network, not of the caller. It
is shared by everyone else on that network, it survives the caller being
dismissed, and it cannot be withdrawn from one participant without
reconfiguring the network for all of them.

A device credential answers a different question: "which principal is this,
and is it still allowed?" It is per device, verifiable at the moment of use,
and revocable in one command with no network change at all. Those are exactly
the properties an authorization has to have, and no network layer provides
them.

So the two are composed, never substituted. Every `/v1` route requires a
device credential; the transport decision above changes which packets can
arrive and nothing else. Had the overlay been selected instead, the
authentication requirement would be identical — the overlay would have hidden
the surface, not authorized anyone on it.

### Threat model

| Threat | Mitigation | Residual |
|---|---|---|
| Passive interception of a request, including the credential header | TLS on every non-loopback hop; the client refuses plain HTTP to anything but its own machine and has no way to disable certificate verification | Trust in the certificate chain the client's platform trusts |
| An unauthenticated caller finding the route | Every `/v1` route requires a device credential; failures are one uniform envelope | `/healthz`, `/readyz` and `/openapi.json` answer without a credential, so route existence is discoverable |
| Credential theft from the client machine | The token lives in the OS credential store, never in a config file or plain-text on disk | A compromise of the owner's user session reaches the vault too; the answer is revocation |
| Credential theft from the server | Only a salted scrypt verifier is stored; no secret ever passes through server output or logs | An offline attack on a weak operator-chosen secret, bounded by scrypt cost |
| Device enumeration through the endpoint | Unknown device, wrong secret, malformed token and revoked device are indistinguishable, and an unknown id still pays a compensating derivation | Token SHAPE is distinguishable from a well-formed one |
| Verification cost used as the load | Non-queuing admission sheds with `429`; a dedicated, bounded credential pool is the real ceiling and cannot be released by a caller | The CPU is bounded but access to the bound is not reserved: unknown device ids deliberately pay a full derivation, so an unauthenticated caller can occupy the slots and the owner's own token is shed with `429`. Mitigation would be ingress-side and does not exist today ([ADR 0010](0010-device-principal-authentication.md)). No total in-flight ceiling either |
| A revoked device continuing to be served | Revocation is checked at admission and re-checked before and after the model call | A revocation committing after the final re-check is served once |
| A compromised route sending the client elsewhere | The client refuses any request target whose origin differs from the configured one, before the credential header is attached, and does not follow redirects | A hostile server at the configured origin is a compromise of the host itself |
| The API being exposed directly instead of through the proxy | The bind is validated to a numeric loopback literal on the only path that binds a socket; a non-loopback value exits 2 without ever binding | An operator with host access can always run something else |

Out of scope, deliberately: multi-tenant abuse (there is one owner), denial of
service against the proxy or the host, and the host's own administrative
posture, which is reported to the owner separately.

### Credential lifecycle

1. **Issue.** The operator generates a secret on the machine where it will be
   stored, and registers it on the host over the administration channel:
   `wuwaterm-api device issue` reads it from standard input. The server never
   generates or prints credential material.
2. **Store.** The client keeps the token in the OS credential store. It is
   never written to `config.json` and never logged.
3. **Use.** Presented as a bearer credential on every request, over TLS.
   Server-side logs carry a redacted device identifier and a
   server-generated request id, never the token.
4. **Rotate.** Issue a second device, move the client to it, revoke the
   first. There is no in-place rotation, and none is wanted: two devices
   existing briefly is easier to reason about than one credential changing.
5. **Revoke.** `wuwaterm-api device revoke` stamps `revoked_at`. The next
   request is refused; an in-flight one is refused at the next re-check. The
   row is kept, so a withdrawal stays auditable.
6. **Break glass.** Removing the credential store file revokes every device
   at once, and the request path cannot recreate it.
7. **Uninstall.** "Forget token" removes the vault entry; deleting the
   application does not, so the documented removal step is part of the
   lifecycle, not an afterthought.

Revocation is deliberately independent of the transport: it works whether or
not the network arrangement changes, and a network change is never a
substitute for it.

### Endpoint discovery and configuration

There is no discovery protocol and no bootstrap endpoint. The operator tells
the owner one base address; the owner types it into the client's Settings
dialog once, and it is stored in `config.json` (a non-secret file) next to the
timeouts. The client validates it on entry, on load from disk, and again in
the transport, with one predicate: a usable base address is `https` to any
host, or plain `http` only to this machine's own loopback; it may not carry
user information, a query or a fragment. An address that fails never reaches
the network: since 2026-08-12 there is no default to fall back to, and a
client with no usable address is explicitly unconfigured — it says so in the
main window and refuses every request with the code `not_configured` rather
than substituting a development address for the missing setting (see
[ADR 0011](0011-pc-client-stack.md)).

Because the base address is pure client configuration and nothing in the
contract encodes the network path, moving the service to a different endpoint
later changes zero contract bytes.

### Certificate and peer verification

The client's HTTP client is constructed with certificate verification
explicitly on and with environment proxy variables not trusted; there is no
setting, flag, environment variable or code path that turns verification off.
A test inspects the SSL context `httpx` actually creates rather than trusting
the constructor argument. Server identity is therefore
proven by the certificate chain on every request; client identity is proven by
the device credential. Neither proves the other.

### Timeouts and reconnect behaviour

The client applies two configurable deadlines: a short one for lookups and
status, a longer one for translation, both clamped to a sane range. They are
per-phase deadlines (connect, read, write, pool acquisition), so a request
that keeps making progress is not cut off. There is no automatic retry: a
translation request that reached the service may already have spent model
budget, and a silent retry would spend it twice. "Reconnect" is therefore the
user retrying, or the next request opening a new connection; the client holds
no session, so there is no session to restore. Server-side, a request that
exceeds the server time budget is answered `504` and rendered as a timeout,
and a connect-level failure is rendered as an offline state — both stable
codes, not prose.

### Deployment and rollback

Deployment of the route is one addition to the proxy's existing configuration
for a site it already serves: a path-prefix route that strips the prefix and
forwards to `127.0.0.1` on the API port. The configuration file is backed up
before the edit, the route is applied with the proxy's own reload (no restart
of the existing sites), and the readback is a request from the owner's machine
that must be answered by the API — a request made ON the host proves nothing
about the path being tested.

Rollback is deleting that one block and reloading again, or restoring the
backup file. Nothing else on the host is touched by it: the API keeps running,
bound to loopback, exactly as it did before the route existed, and both
containers are unaffected. Because the API never binds a public interface,
removing the route is sufficient to remove the exposure — there is no second
place where it could still be reachable.

The service deployment itself is unchanged and remains the transactional
updater's job ([ADR 0008](0008-candidate-verification-and-transactional-deployment.md)).

### Future migration path

A private overlay network between the owner's machine and the host remains
the documented alternative. It would remove the public surface entirely: the
client would be configured with the overlay address, the proxy route would be
deleted, and the API would keep its loopback bind behind a proxy on the
overlay interface.

What would trigger it:

- evidence of unwanted traffic on the route, or any successful unauthorized
  request;
- a second machine or a second person needing access, which changes the
  identity model as well as the network one;
- the owner deciding the surface should not be public regardless of evidence.

What it would cost: installing and maintaining overlay software on the host
and on every client machine, an owner decision because its operational impact
and rollback are not established today. What it would NOT cost: any change to
the API contract, the client's code, or the authentication model. The client
would be pointed at a different base address and nothing else would move.

### SSH is operations only

Host shell access is the operator's administration channel: deployment,
credential issuance and revocation, log reading, break-glass. It is not a
product transport and never a fallback for one. The client contains no key
material, starts no processes, and opens no shell; the repository suite
enforces that with a scan over the shipped client surface and the deployment
files, so a future edit that reintroduced the revoked design fails a pull
request rather than a review.

## Consequences

- Positive: the client works from a normal login session, with no
  administrative session, no helper process and nothing to start first.
- Positive: the change to the host is one reversible block in a configuration
  file. The rollback is deleting it, and the service keeps running throughout.
- Positive: nothing new listens on the host, and the port the API binds is
  unreachable from outside it.
- Negative: the service now answers requests that originate on the public
  internet. Device authentication is the control that makes that acceptable,
  and it is the only one — which is why the credential's properties, not the
  network's, carry the argument above.
- Negative: three routes answer without a credential, so the existence of the
  service is discoverable by anyone who finds the path.
- Constraint: the API must keep its loopback bind. A deployment that binds
  anything else has removed the property this record depends on.
- Constraint: adding another route, port, name or firewall rule is an owner
  decision, not a deployment step.

## Evidence

- `src/wuwaterm_api/settings.py` — `validate_loopback_bind`, `DEFAULT_PORT`
- `src/wuwaterm_api/cli.py` — the bind guard on the only socket-binding path
- `deploy/docker-compose.yml` — loopback bind fixed in the file, not in `.env`
- `client/src/wuwaterm_client/config.py` — `usable_base_url`,
  `endpoint_is_confidential`
- `client/src/wuwaterm_client/api.py` — verification on, proxies not trusted,
  request-target guard before the credential header
- `client/tests/test_transport_security.py` — the SSL context actually used
- `docs/deployment.md` — the route, the readback and the rollback
- [ADR 0009](0009-http-api-adapter.md),
  [ADR 0010](0010-device-principal-authentication.md),
  [ADR 0011](0011-pc-client-stack.md)
