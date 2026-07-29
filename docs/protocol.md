# Herdr socket API — observed behaviour

Established empirically against **herdr 0.7.5, protocol 17**. The generated
schema (`herdr api schema`) covers message shapes but not connection
semantics, and is wrong or incomplete in a few places noted below. Re-verify
against new versions; `tests/test_integration.py` pins the important claims.

## Transport

A Unix socket whose path herdr exports as `HERDR_SOCKET_PATH` in every managed
pane and in plugin action/startup environments:

```
/home/<user>/.config/herdr/herdr.sock
```

Newline-delimited JSON, one object per line.

```
→ {"id":"1","method":"ping","params":{}}
← {"id":"1","result":{"type":"pong","version":"0.7.5","protocol":17}}
```

`params` is required — omitting it is an `invalid_request`. Responses carry the
caller's `id`; subscription events carry `event`/`data` and no `id`, which is
the whole demultiplexing rule.

## One request per connection

**This is the single most important rule, and nothing in the schema hints at
it.** herdr answers exactly one request per connection and then closes it. A
second request on the same socket fails with `ECONNRESET`.

Verified: two consecutive `ping`s fail; so do `ping`→`snapshot` and
`snapshot`→`ping`, in either order. A single request of any kind succeeds.

`events.subscribe` is the sole exception: it converts the connection into a
persistent, event-only stream. No further request may be issued on it — a
`ping` after subscribing resets it like any other second request.

So every connection is used exactly one of two ways:

| Use | Lifetime |
| --- | --- |
| One request → one response | Closed by the server immediately after |
| `events.subscribe` → event stream | Stays open, delivers events, accepts nothing more |

`HerdrSession` implements this: a fresh connection per request, plus one
long-lived connection for events. A Unix-socket connect is microseconds, so
per-request connections cost little.

This also explains why the `herdr` CLI works the way it does — every subcommand
opens its own connection.

## Malformed requests

A rejected request is answered with an error whose `id` is `""` rather than the
request's id, and the connection is then closed. The error is therefore not
correlatable to the request that caused it. `HerdrClient` keeps the last
uncorrelatable error and raises it in place of the generic
"server closed the connection", which is otherwise very hard to debug.

```
→ {"id":"s","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed"}]}}
← {"id":"","error":{"code":"invalid_request","message":"missing field `pane_id` ..."}}
   <connection closed>
```

## Subscriptions

26 subscribable event types. They split into two groups, and the split governs
both how you subscribe and what the event is called on the wire.

**Global** — subscribe with `{"type": "pane.created"}`, events arrive
**underscored** (`pane_created`):

```
workspace.created  workspace.updated  workspace.metadata_updated
workspace.renamed  workspace.moved    workspace.closed  workspace.focused
worktree.created   worktree.opened    worktree.removed
tab.created  tab.closed  tab.focused  tab.renamed  tab.moved
pane.created  pane.closed  pane.updated  pane.focused  pane.moved
pane.exited  pane.agent_detected  layout.updated
```

**Pane-scoped** — require a `pane_id` in the subscription, and arrive
**dotted**, matching the schema's `SubscriptionEventKind`:

```
pane.output_matched          also requires: source, match
pane.agent_status_changed    optional: agent_status
pane.scroll_changed
```

Omitting `pane_id` rejects the *entire* batch, not just that entry — hence
`protocol.subscription()` validating client-side.

> Schema caveat: `SubscriptionEventKind` lists only the three pane-scoped
> names. The 23 global events are absent from that enum but are delivered all
> the same, underscored. Do not treat the enum as the full event list.

### Subscribing replays history, out of order

**Subscribing does not start a live-only stream.** herdr immediately replays a
backlog of historical events — 118 of them on a modestly used session — before
any live event arrives.

Worse, the replay is **not causally ordered**. Observed immediately after
subscribing, with no user action:

```
pane.created  w6:p1
pane.closed   w6:p3      <-- close arrives BEFORE the matching create
pane.updated  w2:pF
pane.created  w6:p3      <-- ...which is then resurrected
pane.closed   w6:p4
pane.created  w6:p4
```

Applying that in arrival order leaves long-dead panes permanently in the model.
The symptom is subtle: state looks plausible, but every id after the first
phantom is shifted, so actions land on the wrong target — or on panes that
no longer exist and fail with `pane_not_found`.

The handling, in `DeckController`:

1. **Subscribe first**, so no live change is missed.
2. **Drain the backlog** and discard it (`drain_replay` — read until no event
   for 400 ms, capped at 5 s).
3. **Then snapshot**, and treat the snapshot as authoritative: anything the
   model holds that the snapshot omits is dropped.
4. **Reconcile periodically** (`prime()` again every 60 s) so the model cannot
   drift if an event is ever missed.
5. Treat `pane_not_found` from an action as proof the pane is gone, and drop it.

Snapshotting *before* draining does not work — the stale replayed events simply
overwrite the fresh snapshot.

### Snapshot lists panes twice

`session.snapshot` returns each agent pane under **both** `snapshot.agents[]`
and `snapshot.panes[]`. A naive walk therefore visits agent panes twice (13
records for 7 panes). `_iter_panes` keys by `pane_id` and keeps the richer
record.

### Agent status requires per-pane subscriptions

`pane.agent_status_changed` is pane-scoped, so tracking every pane means
subscribing per pane and re-subscribing as panes come and go. There is no way
around it.

The tempting shortcut does not work. The **global** `pane.updated` event
carries a full pane record whose payload includes an `agent_status` field, so
it looks like one global subscription would be enough. It is not: `pane.updated`
does not *fire* on a status transition. Verified by driving transitions with
`pane.report_agent` and watching both subscriptions — only
`pane.agent_status_changed` arrived. Relying on the global event left status
refreshing only on the 60-second reconcile.

So the daemon holds one `pane.agent_status_changed` subscription per live pane,
rebuilt whenever the pane set changes. Since herdr serves one request per
connection, that rebuild needs a fresh connection — see
`HerdrSession.resubscribe`.

`pane.agent_detected` is global but carries only ids, with no pane object; the
`pane.updated` that follows supplies the detail.

### Where a pane's name lives

A pane record at protocol 17 carries these keys, and **absent optional fields
are omitted rather than sent as null**:

```
pane_id  terminal_id  workspace_id  tab_id  focused  cwd  foreground_cwd
agent  terminal_title  terminal_title_stripped  agent_status  agent_session
scroll  revision
[label]
```

`label` is the user-set name, written by `pane.rename` and absent until it is.
There is no `title` and no `display_agent` — both are accepted by our parser as
forward compatibility, but neither has ever been observed.

That omission is what made badges silently blank: reading only `title`/`label`
is correct but yields nothing on a session where no pane has been renamed,
which is every session by default. The fallback is **`terminal_title`**, which
is always populated, with `terminal_title_stripped` being the same string minus
the agent's own leading status glyph:

```
terminal_title           "✳ orchestrator"
terminal_title_stripped  "orchestrator"
```

Prefer the stripped form — the glyph duplicates the agent mark the deck already
draws, and is wide enough to cost two of the eight badge characters. Note the
glyph is the agent's *spinner*, so it changes frame to frame (`⠂`, `✳`, …);
matching on the raw title would churn.

### `pane.rename` emits no event

herdr has `tab.renamed` and `workspace.renamed` events, but no `pane.renamed` —
and renaming a pane does not fire `pane.updated` either. Verified by holding a
`pane.updated` + `pane.focused` subscription across a `pane.rename` call: 31
events arrived, all `pane.focused`, none carrying the new label.

So a pane rename is invisible until the next periodic reconcile, which bounds
the delay at `--reconcile-interval` (60 s). This is the one piece of state the
event stream cannot keep current.

## Snapshot shape

`session.snapshot` returns panes nested under workspaces and tabs. The exact
nesting has moved across protocol versions, so `daemon._iter_panes` walks the
tree looking for objects with both `pane_id` and `terminal_id` rather than
indexing a fixed path. Bare `pane_id`s (as in `pane.closed` events) are not
pane records and are deliberately skipped.

Measured size on a 4-workspace session: ~9 KB, comfortably under asyncio's
64 KiB default line limit. Raise `limit=` on `open_unix_connection` if very
large sessions ever exceed it.

## Pane ids are opaque

Ids look like `w6:p1` but the suffix is not always numeric — `w2:pF` occurs in
practice. Never parse or construct them; read them from responses. Closed ids
are not reused, and a pane moved between workspaces gets a new id.
