# Herdr plugin system — reverse-engineered notes

Herdr 0.7.5 / protocol 17. The plugin system is not documented in the CLI help
beyond command names, so the following was established empirically by linking a
probe plugin and observing behaviour. Re-verify against newer versions.

## Manifest

A plugin is a directory containing **`herdr-plugin.toml`**.

Required top-level fields (each discovered by omission, one error at a time):

```toml
id                = "herdr-streamdeck"   # plugin_id; must be a valid slug
name              = "Stream Deck"
version           = "0.1.0"
min_herdr_version = "0.7.5"
```

Optional top-level:

```toml
description = "..."
platforms   = ["linux", "macos"]   # omit to leave support undeclared;
                                   # an empty array is rejected outright
```

Sections, all arrays of tables, all supporting a per-entry `platforms` gate:

| Section | Fields | Purpose |
| --- | --- | --- |
| `[[startup]]` | `command`, `platforms` | Process launched at server start |
| `[[actions]]` | `id`, `title`, `command`, `contexts`, `description`, `platforms` | Invocable commands |
| `[[events]]` | `on`, `command`, `platforms` | Fork a command on a herdr event |
| `[[panes]]` | `id`, `title`, `command`, `placement`, `width`, `height` | Plugin-owned panes |
| `[[build]]` | `command`, `platforms` | Build step run at install |
| `[[link_handlers]]` | `pattern`, `action` | Handle clicked URLs |

`command` is an argv array, never a shell string. Empty argv is rejected
(`invalid_plugin_command`).

Action `contexts` is a subset of: `global`, `workspace`, `tab`, `pane`,
`selection`. `placement` for panes is `overlay` (default) or `popup`; `width`
and `height` are only accepted when placement is `popup`.

## Execution model

Actions are **fork/exec per invocation** — one short-lived process each time.
A trivial `bash -c 'echo'` action measured ~13ms wall clock end to end.
Exit code, stdout and stderr are captured and retrievable via
`herdr plugin log list --plugin <id>`.

The working directory is `plugin_root`. Context is passed via **environment
variables**, not argv templating:

| Variable | Example |
| --- | --- |
| `HERDR_PLUGIN_ID` | `herdr-streamdeck` |
| `HERDR_PLUGIN_ACTION_ID` | `reload-profile` |
| `HERDR_PLUGIN_ROOT` | the plugin directory |
| `HERDR_PLUGIN_CONFIG_DIR` | `~/.config/herdr/plugins/config/<id>` |
| `HERDR_PLUGIN_STATE_DIR` | `~/.local/state/herdr/plugins/<id>` |
| `HERDR_PLUGIN_CONTEXT_JSON` | full invocation context, JSON |
| `HERDR_SOCKET_PATH` | the API socket, so actions can call back in |
| `HERDR_BIN_PATH` | path to the `herdr` binary |

`HERDR_PLUGIN_CONTEXT_JSON` carries:

```json
{"workspace_id":"w6","workspace_label":"...","workspace_cwd":"...",
 "tab_id":"w6:t1","tab_label":"...","focused_pane_id":"w6:p1",
 "focused_pane_cwd":"...","focused_pane_agent":"claude",
 "focused_pane_status":"working","invocation_source":"cli",
 "correlation_id":"cli:plugin"}
```

`invocation_source` distinguishes CLI from UI invocation.

## Startup lifecycle — confirmed

**`[[startup]]` runs when a herdr *server* starts, and at no other time.**

Verified by linking a probe plugin whose startup command logged its pid and
then slept, and observing each candidate trigger:

| Trigger | Runs startup? |
| --- | --- |
| `herdr plugin link` | no |
| `herdr plugin enable` (linked `--disabled` first) | no |
| `herdr server reload-config` | no |
| **A new herdr server starting** | **yes**, immediately |

The registry (`~/.config/herdr/plugins.json`) is global, so any server picks up
every linked plugin. Note it is the *server* that matters, not the client: a
`herdr --session X` invocation that spawned a server and then died initialising
its TUI still ran the startup hooks.

Consequence for development: linking is not enough to (re)start a plugin's
daemon. Run it by hand instead — the startup hook only matters after install.

### Startup processes are orphaned, not reaped

**herdr does not stop startup processes when the server stops.** After
`server.stop` on two probe servers, both `sleep 300` children were still
running, reparented to `/init` (ppid 1). Nothing killed them.

So there is no supervision in the systemd sense — and, more importantly, no
teardown. A plugin daemon that holds an exclusive resource (like a Stream Deck,
which hidapi opens exclusively) would survive a herdr restart and lock out its
own replacement.

`herdr-streamdeck` handles this by exiting when its event-stream connection
closes: stopping the server drops the socket, the daemon's `events()` iterator
ends, `run()` returns, and the process tears down its device handle and exits.
That makes it self-reaping despite herdr not reaping it. Any long-lived plugin
should do the same rather than reconnecting forever.

Whether herdr *restarts* a startup process that crashes on its own was not
tested; assume not, and make the daemon crash-tolerant.

### Running an isolated server for testing

Nested herdr is refused by default (`exit_if_nested_disabled`), governed by
`experimental.allow_nested` in `config.toml`. The check reads the environment,
so it can be bypassed for a throwaway server without editing any config:

```bash
env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID \
    herdr --session <name>
```

Needs a TTY, so run it inside a pane. Named sessions get their own tree under
`~/.config/herdr/sessions/<name>/`, including `herdr.sock` — address that
socket to stop it, and never the default one:

```bash
# stop only the test server
python -c "..."  # send {"method":"server.stop"} to sessions/<name>/herdr.sock
```

Afterwards, kill any startup children by hand (see above) and remove
`~/.config/herdr/sessions/<name>/`.

## Distribution

```bash
herdr plugin install <owner>/<repo>[/<subdir>] [--ref <ref>] [-y]
herdr plugin link   <path> [--enabled|--disabled]     # local development
herdr plugin unlink <plugin-id>
herdr plugin enable|disable <plugin-id>
herdr plugin list [--plugin <id>] [--json]
herdr plugin config-dir <plugin-id>
```

Install pulls from GitHub with optional ref pinning, and runs `[[build]]`.
Herdr aborts the install if the build step mutates `herdr-plugin.toml`
(`plugin build changed herdr-plugin.toml after install preview; aborting
install`), so a build hook must not rewrite its own manifest.

Manifest problems that are non-fatal (unknown event names, missing files) are
collected into a `warnings` array on the plugin record and surfaced by
`plugin.list` rather than failing the link.

## Events

26 subscribable event types at protocol 17:

```
workspace.created  workspace.updated  workspace.metadata_updated
workspace.renamed  workspace.moved    workspace.closed  workspace.focused
worktree.created   worktree.opened    worktree.removed
tab.created  tab.closed  tab.focused  tab.renamed  tab.moved
pane.created  pane.closed  pane.updated  pane.focused  pane.moved
pane.exited  pane.agent_detected  pane.output_matched
pane.agent_status_changed  pane.scroll_changed  layout.updated
```

These are available two ways, and the choice matters:

- `[[events]]` manifest hooks — herdr forks a process per event. Fine for rare
  events; unusable for anything high-frequency.
- `events.subscribe` over the socket — one persistent connection, events pushed
  as NDJSON. This is what a long-running process should use.
