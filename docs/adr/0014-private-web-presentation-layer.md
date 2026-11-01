# ADR 0014: Private web presentation layer

- Status: **Accepted** (2026-08-19). [The ADR index](README.md) admits
  "Accepted decisions that shape the **running** system", and this record now
  describes one, so it belongs in that table and is listed there. The basis, in
  three parts:
  1. **The surface exists in the tree, with its gates.**
     `src/wuwaterm_api/web/__init__.py`, `web/app.py`, `web/render.py` and
     `web/session.py` hold the sub-application; `src/wuwaterm_api/app.py`
     mounts it only when the switch is on, and
     `src/wuwaterm_api/settings.py` defaults that switch off
     (`DEFAULT_WEB_ENABLED = False`). Its behaviour is covered by
     `tests/test_api_web.py`.
  2. **It has been deployed and in owner use since 2026-08-16.** The read-only
     production readback of 2026-08-19 found the edge route for the mount live
     and configured, the API answering on loopback only, and the API's own
     request log carrying owner web-layer requests dated 2026-08-16 — served on
     the authenticated path, and refused `401` without credentials. The edge
     block itself, with its rollback, is written down in
     [docs/web-presentation-layer.md](../web-presentation-layer.md), the guide
     that extends `docs/deployment.md` for this surface.
  3. **The owner directed the record to describe the running system.** The
     campaign goal issued 2026-08-19 requires the documentation set and this
     index to describe what is deployed rather than what was proposed.
- Date: 2026-08-14
- Amends the web-UI half of [ADR 0001](0001-telegram-as-presentation-layer.md)
  (the generic-API half was already amended by
  [ADR 0009](0009-http-api-adapter.md)), and the stack reasoning in
  [ADR 0011](0011-pc-client-stack.md)
- Depends on, and deliberately does not disturb,
  [ADR 0010](0010-device-principal-authentication.md); shares the edge selected
  in [ADR 0012](0012-client-transport-selection.md)

## Context

There are two presentation adapters today — the chat bot and the HTTP API —
and one consumer of the API's contract, the desktop client
([ADR 0009](0009-http-api-adapter.md),
[ADR 0011](0011-pc-client-stack.md)). That consumer is Windows-only: the
client's own record states Windows is the only packaged target, and the
artifact is a one-folder PyInstaller build handed by the owner to the owner.
The owner is not always at that machine. A phone is not a place any of the
three surfaces reaches: the desktop artifact cannot run there, and the chat
bot answers in a group's wording and a group's rhythm rather than as a tool
the owner opens on purpose.

So a third presentation layer is added: an owner-private, mobile-first web UI
covering exactly two things — dictionary lookup and sentence translation.
Nothing else. It is not an administration console: the "web admin" row in
[the architecture map's extension table](../architecture.md) describes bulk
allowlist and audit work with multi-owner roles, and none of that is in scope
here. It has one human user, the owner, on a phone browser, and it renders
answers the existing pipeline already produces.

Two earlier records said no to something shaped like this, and both deserve a
straight answer rather than a quiet reversal:

- [ADR 0001](0001-telegram-as-presentation-layer.md) said "a separate web UI
  or generic HTTP API would add auth, hosting, and client surface without
  improving dictionary fidelity". ADR 0009 amended the API half of that
  sentence by showing where the shared behaviour lives so two adapters cannot
  drift. This record amends the web-UI half on the same terms, and inherits
  the same obligation: the new surface may not hold translation logic, and it
  may not reach past the application layer.
- [ADR 0011](0011-pc-client-stack.md) rejected a browser UI for the desktop
  client because it "would have reintroduced a hosted surface the project
  deliberately does not have". That sentence was about which toolkit the PC
  application is written in, and it stands for that question. It is
  nonetheless the honest objection to this record: a hosted surface is exactly
  what is being added. What changed is not the objection but the price. The
  surface below is off by default, lives inside a process that already exists
  rather than adding one, and is refused at the edge before application code
  runs. Those three facts are the whole of the answer, and the Consequences
  section states what they do not cover.

The constraint that shapes every decision below: **add no new amplification
surface**. Not "add a small one" — none. The host and the model account must
end up with the same worst case they had before this layer existed.

## Decision

### Mounted inside the API process, at one path prefix

The web UI is mounted as a **sub-application inside the existing API process**
at the mount path `/wuwaterm-web`. It is not a new process and not a new
container. It is served from the SAME host name as the API, through a second
path prefix in the same edge site block that already carries
`/wuwaterm-api/*` (`docs/deployment.md`). Same origin, therefore: no
cross-origin request policy is relaxed anywhere, because there is no second
origin for anything to be relaxed against.

### The private protection layer: refused at the edge, then bound to a session

Three parts, outermost first:

1. **`basic_auth` at the edge.** The proxy that already terminates TLS for
   this name challenges every request under `/wuwaterm-web` before it is
   forwarded. An unauthenticated request never reaches application code.
2. **A shared secret header the edge injects.** The proxy sets — replaces,
   never appends to — a header carrying a secret known to the edge and to the
   process. The application refuses any request under the mount that does not
   carry it, so a request that reached the mount by some other path on the
   host is refused as well.
3. **An app-issued session cookie.** Once past the first two, the application
   issues an opaque `HttpOnly` cookie and the browser presents it thereafter,
   so the phone is not re-challenged on every navigation and no credential
   material of the service's own is ever typed into the browser.

Why `basic_auth` and not the alternatives:

- **Mutual TLS was rejected as hostile from a phone.** It is the strongest of
  the three, and it is the one a mobile browser makes worst: a client
  certificate has to be generated, transported to the phone, installed into a
  platform trust store, and re-installed on expiry or device replacement, and
  the selection prompt is a system-level experience the application cannot
  improve. A control the owner will route around is not a control.
- **An IP allow list was rejected on the facts of mobile networks.** Phone
  addresses rotate — between cellular and wireless networks, between carrier
  gateways, sometimes mid-session. An allow list either has to be edited from
  the phone whose access it is gating (which is a bootstrap problem) or has to
  be widened until it stops distinguishing anyone.
- **`basic_auth` is usable from a phone and is enforced before application
  logic.** TLS is already terminated at that edge for this name
  ([ADR 0012](0012-client-transport-selection.md)), so the credential is not
  travelling in the clear; the browser's own credential manager holds it; and
  the check happens in the proxy, which means a defect in the web layer cannot
  be reached by a caller who has not passed it.

The shared secret header is not authentication and is not written down as
such. It is a bearer secret: it answers "did this request come through the
edge?" and nothing about who sent it. It exists because the mount is otherwise
reachable by anything that can already talk to the process, and it must be
compared in constant time and never logged. `basic_auth` is what proves a
human presented a credential; the header is what proves the path.

### Why this is NOT a separate process

> **A REASON WAS RETIRED HERE. It is recorded rather than reworded.**
>
> This section originally rested on a single argument: a third process would
> be a third budget, the aggregate ceiling would rise, and a rising ceiling is
> a "new amplification surface" that the work was required not to add.
>
> The requirement it appealed to was mis-specified. "Add no new amplification
> surface" was carried over from the threat model of a service open to
> strangers. Every principal in this deployment is the owner — his phone, his
> desktop, his browser — so the owner opening a second window is not an
> attacker gaining leverage. A product with public registration would need
> user accounts, authentication and tiered quotas, and would not solve that
> problem with a shared bucket either.
>
> Two measurements sharpen what was actually true. The per-device rate limiter
> buckets by device id, so **within a single process** two devices already
> receive two full admission allowances — the "one shared ceiling" phrasing was
> never accurate about admission. The model-call budget, by contrast, is one
> global account per process: two devices spending against it were measured
> sharing a budget of two, with the model invoked exactly twice.
>
> So the retired claim is the SECURITY framing, not the arithmetic. What
> survives of it is narrower and is about money, and it is demoted below the
> two reasons that were always the stronger ones. Anyone re-opening this
> decision should argue against those, not against the ceiling.

**Read this section before proposing a third container.** The pattern is
inviting: ADR 0009 gave the API its own service from the same image, and its
own section is literally titled "Separate process, separate budgets, separate
state". A reader who meets "a third adapter" will reach for a third
`command:` entry in Compose by reflex. That reflex is still wrong, for the
reasons below, in order of weight.

**First: the credential surface stays where it is.** A separate web process
could not reach the credential store's in-memory principal, so it would have
to hold a device token of its own and forward it — a backend-for-frontend, and
a second place in the deployment where a live credential sits at rest. In
process, the browser session resolves to an already-authenticated principal
inside the objects that hold it, and the token never leaves the one process
that already had it. ADR 0010's device-principal semantics are untouched
because nothing new presents a credential.

**Second: the failure and credential domains do not spread.** A third process
is a third thing to configure, restart, monitor, patch and get wrong, and a
third place for a secret to be misplaced — the Compose file already showed how
easily that happens, since both existing services read one env file and the
web credentials had to be blanked explicitly for the bot. Fewer processes
holding secrets is a smaller surface in the sense that actually applies here.

**Third, and now secondary: spending.** A second serving process would carry
its own model-call budget, so the money ceiling — not the attack surface —
would double. That is a real consideration and the accounting below is
retained for it. It is no longer the reason this decision was made.

The accounting, in full, because paraphrasing it is how it gets lost:

- ADR 0009 records that the API adapter "has its own model concurrency limit
  and its own per-minute call budget. Those budgets are per process: the worst
  case for the host is the SUM of the two surfaces, never one shared ceiling."
- [The architecture map's cost topology](../architecture.md) states the same
  thing with numbers and with the consequence spelled out. Each serving
  process has its OWN limiter objects, in its own memory; nothing coordinates
  between them and nothing in either process can observe the other's spend.
  With the defaults — four concurrent model calls in the bot, two in the API —
  up to six can be in flight at once, and there is no configuration that makes
  six into four. It says explicitly that "a second copy of either process
  would have its own counters and would double the ceiling".
- No shared cross-process budget exists, deliberately. Building one means a
  shared durable store, a protocol for reserving and releasing a slot, and a
  decided failure mode for when that store is unavailable. The map lists the
  triggers that would justify that machinery, and one of them is "any topology
  with more than one instance of either surface".

Therefore a third serving process is a **third independent budget**. Its
limiter objects would be new objects in new memory, coordinating with nothing,
and the host's worst case would become six plus whatever the third process was
given. Nothing would bring it back down, because there is nothing that could:
the counters are per process by construction. Read this as a statement about
the BILL rather than about exposure — see the retirement note at the head of
this section — and note that it would be incurred not by the feature but by
the packaging decision, which is what makes it easy to do by accident.

Mounted in-process, the web layer spends from the API's existing budget
because it runs inside the objects that hold it. The aggregate ceiling stated
in the cost topology is unchanged by this record. That is the property being
bought, and it is the whole reason for the cost accepted in the next section.

There is a second reason, smaller but not decorative. A separate web process
would have to reach the service the way every other caller does — over the
authenticated HTTP contract, holding a device credential and forwarding it.
That is a backend-for-frontend: a second copy of a device token, on a second
surface, with its own storage, its own rotation and its own compromise story.
In-process, the browser session maps to a principal that is already
authenticated inside the same process, so
[ADR 0010](0010-device-principal-authentication.md)'s device-principal
semantics are untouched: no new principal type, no second credential store, no
new rotation path, and the device id remains the principal id.

The rule to carry forward: **splitting this surface into its own process is a
budget decision, not a deployment step.** It requires the shared
cross-process budget the architecture map describes as not built, and the
triggers it names for building one.

### Credential posture: the token stays on the server

The device token that the web layer presents to the pipeline's admission path
lives **only server-side**, read from the process environment at start. It is
never sent to the browser, never rendered into a page, and never typed into
the browser. The browser holds one thing: an opaque session cookie.

Two properties this buys, and they are different from each other:

- **Nothing on the phone is a credential of the service.** The token does not
  transit a mobile keyboard, a clipboard, an autofill store, a screenshot, or
  a browser's saved-password store. A phone that is lost carries a session,
  not a credential, and the session is server-side state the owner can drop.
- **The web layer stays a caller, not an exemption.** Being in-process, it
  could in principle call the application layer directly and skip device
  admission entirely. It does not. Presenting a device credential keeps this
  surface subject to the same checks every other caller gets — scope,
  per-device rate limiting, and the revocation re-checks ADR 0010 performs at
  admission, before the model call and before returning — which also means
  `wuwaterm-api device revoke` on that device turns the web surface off
  without touching the edge, the desktop client, or the chat allowlist.

Reading the token from the process environment adds no new class of secret at
rest: both serving containers already receive their credentials that way
(`deploy/docker-compose.yml` `env_file`, and trust boundary 4 in
[the architecture map](../architecture.md)). It adds one more consumer of a
posture that already exists.

### The cookie, and the prefix that was not used

The session cookie is `wuwaterm_session`, with
`Path=/wuwaterm-web; HttpOnly; Secure; SameSite=Strict`, and its value is
opaque — a session identifier, carrying no principal material a reader could
use anywhere else.

`HttpOnly` means script in the page cannot READ it, so a rendering defect in a
term or a translation cannot exfiltrate the session. It does not mean script
cannot ACT as the session: a request the page issues still carries the cookie
automatically. That distinction is the honest bound of what the flag provides.
`Secure` keeps it off any plain-HTTP request. `SameSite=Strict` keeps it off
cross-site navigations.

**A `__Host-` prefixed cookie was considered and NOT used.** The prefix's
guarantee is bought with a fixed shape: `Secure`, no `Domain` attribute, and
`Path=/`. It is the `Path=/` that disqualifies it here. This host name is not
this application's alone — the edge site block being extended already serves
the operator's other sites, and carries `/wuwaterm-api/*` besides
([ADR 0012](0012-client-transport-selection.md), `docs/deployment.md`). A
cookie at `Path=/` would be attached by the browser to every request to every
one of those applications, forever, as routine traffic. Path scoping to
`/wuwaterm-web` was preferred, and the cost is stated plainly: the session
cookie gives up the `__Host-` anti-forgery property, which is the browser-side
assurance that a cookie of that name was set by this exact host for the whole
host with no `Domain`. Without the prefix, a cookie of the same name set from
another path or a domain-adjacent writer can shadow the real one, and the
request carries back no path or domain for the server to tell them apart.

The trade was made in that direction because the risk given up is a
cookie-forcing attacker who already has same-site write access, while the risk
avoided is routine, every-request exposure of the session to co-hosted
applications. The second is certain and continuous; the first is conditional.

Neither choice closes same-site request forgery: a request originating from
another path on this same host is same-site, so `SameSite=Strict` does not
withhold the cookie from it, and `__Host-` would not have either. The control
that would close it is a per-session anti-forgery token on state-changing
requests. It is named here as an open item, not claimed as present.

CROSS-site forgery is a separate matter, and the cookie attributes alone did
not close it either. While any request could create a session, an absent cookie
was not a refusal but the trigger to mint one from the server-held token — so a
form on a hostile origin still succeeded: the browser correctly withheld the
`SameSite=Strict` cookie, and it did not matter, because the edge injects its
marker on every proxied request regardless of what caused the request and the
browser attaches the basic-auth credentials it has cached for the site. The
attacker could not read the response, but could spend the model-call budget at
will, and each such request also re-ran a deliberate ~16 MiB scrypt derivation
through the same admission slots the desktop client uses.

The decision, therefore: **only `GET` and `HEAD` may create a session.** A
state-changing request that arrives without a live one is sent to the matching
page rather than served. That is what makes `SameSite=Strict` load-bearing
instead of decorative — the cookie is now genuinely required for a `POST`, and
it is a cookie no browser will attach to a cross-site request. The cost is one
redirect for a form submitted after its session expired, which is the same
recovery the owner would perform by hand.

### One layout, deliberately, for every viewport

The surface is a private web interface for the owner, reachable from **any
browser on any device**. An earlier framing of this work said "mobile-first,
for the owner's phone"; the motivation behind it was real — looking a term up
while lying down — but it narrowed a general tool into a single form factor,
and the narrowing was never a requirement. A phone is one entry point, not the
only one.

The layout carries no width-based media query. That is a decision, not an
omission, and it was checked in both viewports before being recorded rather
than assumed:

| | mobile (emulated 375×812) | desktop (real Chrome, 1707×898) |
| --- | --- | --- |
| content column | 375px, full width | 640px, centred (gaps 534 / 533) |
| body type | 16px / 28px line height | 16px / 28px line height |
| Han characters per line | 23 | 40 |
| horizontal overflow | none | none |
| elements past the viewport | 0 | 0 |

A single `max-width: 40rem` on the content column is doing all of the work. On
a phone the column simply fills the screen; on a wide display it stops growing
and centres, so a line never runs past about 40 Han characters — comfortably
inside the range where Chinese stays readable, and the reason a full-bleed
layout would have needed fixing. Since the wide case already lands where a
responsive rewrite would have aimed, there is nothing to add: no breakpoints,
no second stylesheet, no component that reflows.

What this costs: the desktop view uses a fraction of a wide screen and will
look sparse next to an application designed for one. That is accepted. The
alternative buys a denser desktop layout at the price of a second set of
layout rules to keep correct in two places, on a surface with two views and
one user.

## Consequences

- **Negative, and the one to state first: a defect in the web presentation
  layer can take down the API process the desktop client depends on.** They
  are one process and one event loop. An exception at mount time, a middleware
  that never returns, a blocking call on the loop, or memory the web path
  leaks degrades or stops `/v1` for the desktop client as surely as if the
  fault had been in `/v1` itself. Before this record the casual surface and
  the depended-on surface were not in the same failure domain; now they are.
  This is the price of one budget instead of two, and it is paid knowingly.
  Three mitigations, all of which are part of the decision and none of which
  is isolation:
  1. **Mounted as a sub-application, not merged into the API's own router.**
     Its routes, middleware and exception handling are its own object, so a
     handler registered there cannot alter `/v1`'s middleware chain or its
     error envelope by being registered, and the two route tables cannot
     collide. Stated honestly: this bounds configuration and routing drift,
     NOT failure. A sub-application shares the process, the loop and the
     memory, and nothing about the mount makes a crash survivable.
  2. **A startup switch, default OFF.** An environment setting read at process
     start decides whether the sub-application is constructed at all. Unless
     it is explicitly enabled, the surface does not exist — no routes, no
     middleware, no code path — so the default deployment's failure domain is
     exactly what it was before this record, and disabling it after an
     incident is one setting and a restart rather than a code change.
  3. **The protection layer sits in front, at the edge, not inside the app.**
     An unauthenticated request is refused by the proxy, so the cheapest way
     to exercise a web-layer defect is not available to an anonymous caller.
     Honest bound: this covers outside probing. It does nothing about a defect
     the owner's own legitimate use triggers, which is the likelier way this
     surface takes the process down.
- **Negative, and stated in the two halves that actually differ, because an
  earlier version of this record ran them together and was wrong about one.**

  *Spending is shared, and that is the point.* The model-call budget is ONE
  global account for the whole process, not a per-device bucket — measured
  with the two devices the deployment guide creates: the desktop principal
  exhausted a budget of two, and the web principal, a different device, was
  refused immediately afterwards with the model invoked exactly twice in
  total. So the browser cannot open a second tab on the bill. That is the
  property worth having and it holds.

  *Admission is NOT shared, and that is expected here.* The rate limiter
  buckets by device id, so the desktop credential and the separate web
  credential the guide issues each receive a full per-minute allowance —
  measured: the desktop client was refused at its limit while the web surface
  continued to serve. An earlier draft of this record claimed browser requests
  spend the desktop client's bucket. That claim was false for the deployment as
  documented, and the test that appeared to support it used ONE device for both
  surfaces, which makes "same device shares a bucket" true by definition and
  proves nothing about two.

  This is accepted rather than fixed. Every principal here is the owner's own
  device; a second allowance for his browser is him being able to use two of
  his own things at once, not leverage an attacker gained. A deployment open to
  strangers would need accounts, authentication and tiered quotas, and would
  not solve that with a shared bucket either.

  How heavy the cost is depends on one thing, and it was MEASURED rather than
  reasoned about — counted at `hashlib.scrypt`, the real derivation, not by
  reading the call graph:

  | request shape | scrypt | admission slots | rate-limit tokens |
  | --- | --- | --- | --- |
  | first browser request (creates the session) | 1 | 1 | 1 |
  | 20 further page views on that session | **0** | **0** | 20 |
  | 10 form submissions on that session | **0** | **0** | 10 |
  | 10 JSON API calls (the desktop client's shape) | 10 | 10 | 10 |

  So an established browser session re-runs **no** credential derivation and
  takes **no** admission slot. Verification happens once, when the session is
  created.

  **Correction, because a later change made the sentence that stood here
  false.** This paragraph previously said the cost was "bounded to the
  rate-limit bucket alone" and that browser traffic "does not contend for the
  bounded verifier". Closing a separate finding — usage was being recorded only
  when a session was created, so an established session never stamped a last-use
  time — moved `record_use` onto every admitted request, and that call goes
  through the same bounded credential pool the desktop client's verification
  uses. So browser traffic DOES touch that pool: once per admitted request, as
  a single indexed UPDATE rather than a ~16 MiB derivation. The asymmetry that
  matters survives — a page view costs a write, a desktop call costs a
  derivation, and those differ by orders of magnitude — but "does not contend"
  was too strong and is withdrawn. Deferring the write to session creation and
  TTL boundaries was considered and not taken: it would keep the pool quieter at
  the cost of a second liveness mechanism and a staler last-use record, and
  adding a caching path to close a contention gap this small is a poor trade
  on a surface where each added mechanism has been producing defects.

  Worth stating because it is the opposite of the intuition: per request, the
  browser surface is the CHEAPER of the two clients. The desktop client
  presents its credential on every call and pays a derivation every time; the
  browser pays once per session. The cost of this layer is contention for the
  bucket, and nothing beyond it.

  Pinned by `test_an_established_session_re_runs_no_credential_derivation`, so
  a future change that reintroduced per-request verification would fail rather
  than quietly make this paragraph false.
- Positive: the aggregate model ceiling is unchanged. The worst case for the
  host and the model account is still the SUM the cost topology already
  states, because no new limiter objects are created.
- Positive: ADR 0010 is untouched. No new principal type, no second credential
  store, no device token in a browser, and one existing command already
  revokes this surface.
- Positive: same host name and a second path prefix mean no cross-origin
  relaxation anywhere, and no bootstrap endpoint or discovery protocol is
  added.
- Positive: the edge change is additive and reversible in the same way the API
  route was — one block in a site that is already serving, removed by deleting
  it and reloading.
- Negative: there is now a third credential in the deployment. The
  architecture map's identity model says there are two separate controls,
  Telegram and the HTTP API; edge `basic_auth` is a third thing to safeguard
  and rotate, and that table will need a note saying so, plus the honest
  statement that it gates reachability of one mount and confers no principal.
- Negative: the edge configuration now carries two path routes and a
  credential list for this name. A misconfiguration that drops `basic_auth`
  leaves only the shared secret header between the internet and the mount, and
  that header is a bearer secret, not a proof of who the caller is.
- Constraint: the mount path is a deployment fact. The existing route strips
  its prefix before forwarding (`handle_path` in `docs/deployment.md`), so the
  application must not hard-code assumptions about the prefix surviving.
- Constraint: the web layer inherits ADR 0009's boundary rule in full. It may
  not hold translation logic, and it may not reach past the application layer
  — whatever import allowlist covers `src/wuwaterm_api` must cover this
  package too, or the guard that makes "cannot bypass the shared pipeline"
  structural stops covering the newest way to bypass it.
- Constraint: per-session anti-forgery on state-changing requests is not
  decided by this record. Until it is, same-site request forgery against the
  mount is a residual, and the translation route is the one that spends model
  budget.

## Evidence

When this record was written (2026-08-14) the surface was not yet committed to
the tree, so this section was written in two parts: what was already true, and
what had to become true before the status could move. Mixing them would have
let a decision read as a description of running code. Both parts are kept as
they were written; the second one has since been delivered, and where it landed
is recorded under it.

Already true, and read to write this record:

- [ADR 0009](0009-http-api-adapter.md) — "Separate process, separate budgets,
  separate state": per-process budgets whose worst case is their SUM; the
  four-module import allowlist enforced by
  `scripts/check_architecture_boundaries.py`
- [The architecture map](../architecture.md) — cost topology (the numbers, the
  "second copy would double the ceiling" statement, and the triggers that
  would justify a shared budget); trust boundaries; the identity model's two
  controls; the extension table's separate "web admin" row
- [ADR 0010](0010-device-principal-authentication.md) — device principals,
  scopes, revocation re-checks, `device revoke`
- [ADR 0012](0012-client-transport-selection.md) — the edge that already
  terminates TLS for this name, and why reaching an endpoint is not an
  authorization
- `docs/deployment.md` — the existing `/wuwaterm-api/*` path route, the
  prefix-stripping form, the backup/validate/reload sequence and the rollback

Required before the status above may move to Accepted:

- the sub-application package and its mount, plus the startup switch defaulting
  to off, with a test that proves the surface is absent when it is off
- the shared-secret-header check, with a test that a request without it is
  refused and one that the value is never logged
- session issuance and the cookie attributes, with a test pinning
  `Path=/wuwaterm-web`, `HttpOnly`, `Secure` and `SameSite=Strict` on the
  response the application actually sends
- a test that no page and no response body ever carries the device token
- the import-boundary guard extended to the new package, and a test that fails
  when it is not
- the edge site block carrying the second path route with `basic_auth` and the
  injected header, documented in `docs/deployment.md` with its rollback
- the identity-model note in `docs/architecture.md`, and this file's entry in
  [the ADR index](README.md)

Delivered. The package and its mount are `src/wuwaterm_api/web/` and the mount
in `src/wuwaterm_api/app.py`, with the switch defaulting off in
`src/wuwaterm_api/settings.py`; the behavioural items above are covered by
`tests/test_api_web.py`; the import-boundary guard reaches the new package
through `scripts/check_architecture_boundaries.py`; the identity-model note and
the third presentation layer are in [the architecture map](../architecture.md);
and this record is listed in [the ADR index](README.md). One item landed
differently from the way it was written: the edge site block, its validation
command and its rollback are in
[docs/web-presentation-layer.md](../web-presentation-layer.md) — the guide that
opens by declaring it extends `docs/deployment.md` — rather than inside
`docs/deployment.md` itself.
