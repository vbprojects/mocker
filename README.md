# mocker

A Python package and CLI for recording and replaying selected Python function
outcomes and HTTP(S) requests from proxy-aware executables. One selector/rule
engine drives capture, replay, fixed substitutions, live exceptions, and blocking.

**Status:** initial implementation, Python 3.12+. The distribution name is
`mocker-record-replay`; the import and command are `mocker`. This project has
not been published to PyPI.

## Install

```bash
python -m pip install -e '.[dev,network]'
```

The core has no runtime dependencies. The optional `network` extra supplies
mitmproxy. Use a virtual environment so the `mocker` and `mitmdump` commands are
on PATH.

## Terminal UI

```bash
python -m pip install -e '.[tui,network]'
mocker tui
# Open an existing config:
mocker tui --config examples/mocker.json
```

The UI has three tabs:

- **Configuration:** edit the cassette path, Python scopes, trace names/globs,
  fixed mock values, canonical rules, and replay miss/consumption policies. Sampling controls select the input/target
  pool and an optional repeatable seed.
- **JSON:** edit the full configuration, including HTTP targets, structured trace
  selectors, and advanced options. Unknown top-level options and structured
  selectors are preserved when using the form. Invalid JSON cannot be saved or run.
- **Run & output:** enter a program command, working directory, forward/reverse
  mode, and Python or executable/HTTP runner. Optional partial-replay destination
  and explicit pickle trust are available. Output streams while the UI stays
  responsive; send line-based input or close stdin from the bottom controls.

Use **New**, **Open**, **Save**, and **Validate** at the top. **Ctrl+S** saves,
**Ctrl+O** opens the path entered at the top, **F5** runs, **F6** or **Ctrl+C**
stops, and **Ctrl+Q** quits. Changing New/Open or quitting with unsaved edits
prompts before discarding them. Stop the running program before quitting.

A saved config includes run settings under `tui`; the existing CLI ignores that
section. Relative config file paths resolve from the directory where the TUI
started. Relative cassette and program paths resolve from the selected program
working directory. Runs use a temporary snapshot of the current edits, so they
also work before saving and do not silently overwrite the config file.

Commands are split into arguments without a shell: quoted spaces work; pipes,
redirects, environment assignments, and shell expansions are not interpreted.
Use an explicit shell command if your program needs those features. This is a
line-oriented runner, not a PTY: programs requiring terminal control or password
prompts should be run through the ordinary CLI. Stop interrupts the process group
on POSIX, allows three seconds for cassette finalization, and then kills remaining
descendants. Windows uses `taskkill /T /F`. Forced termination can leave a journal
requiring recovery. Core offline and interception limitations still apply.

## Python recording and replay

```bash
mocker forward --runner python --cassette cassettes/demo.json \
  --trace 'demo:get_name_*' -- python examples/demo.py
mocker reverse --runner python --cassette cassettes/demo.json \
  -- python examples/demo.py
```

Run `examples/demo.py` from this checkout: its canonical entrypoint identity is
`demo`, derived from the script basename. Imports use their ordinary module
names. For a package entrypoint:

```bash
mocker forward --runner python --scope myapp --trace 'myapp.users:get_name' \
  --cassette cassettes/app.json -- python -m myapp
```

`--scope` selects import roots to instrument, and can be repeated. Entrypoint
source is always eligible for instrumentation. Use `--trace-file functions.txt`
for one selector per line (blank lines and `#` comments allowed), or a JSON array
of selector strings/objects. Reverse mode restores selection and scope from the
cassette when no configuration file is supplied.

The Python runner uses the current interpreter, executes the target source in
`__main__`, and preserves script arguments, cwd, standard streams, and exit
codes. A leading `python` command is accepted as syntax, not as interpreter
selection. Interpreter flags such as `-O`, `-c`, and `-I` are unsupported.

## Shared configuration

```json
{
  "version": 1,
  "cassette": "cassettes/demo.json",
  "python": {"scope": ["myapp"]},
  "trace": ["get_name", "myapp.users:get_name_*"],
  "mock": [{"get_name_*": "Ryan"}],
  "replay": {"on_miss": "error", "consume": "once"},
  "rules": [
    {
      "id": "reject-delete",
      "priority": 100,
      "modes": ["reverse"],
      "select": {"kind": "http", "method": "DELETE"},
      "action": {"type": "block"}
    }
  ]
}
```

`trace` strings and `mock` keys accept shell globs. Canonical selectors use
explicit `{"glob": "..."}` patterns; plain strings in canonical fields are exact.
Python selectors support `target` (`module:qualified_name`), `module`, and `name`
(the final function name). HTTP selectors support `method`, `host`, and `path`.
Fields combine with AND; target lists combine with OR.

Rules have `select`, `action`, optional `id`, numeric `priority`, and `modes`.
Higher priority wins; ties follow configuration order. The first matching rule
provides the complete action. Rules also enroll their selectors for capture.
Mocks apply in both modes unless constrained with `modes`.

Actions: `record`, `replay`, `mock`, `live`, and `block`. Replay accepts
`on_miss: error|live|record`; errors are the default. Fixed Python mocks use
`{"return": value}` or `{"raise": {"type": "builtins:ValueError", "args": ["bad"]}}`.
HTTP mocks use `{"response": {"status": 200, "headers": [["Content-Type",
"application/json"]], "body": {"json": {"name": "Ryan"}}}}`.
Bodies also support `text` and `base64`.

Input predicates use JSON pointers and `eq`, `in`, or `exists`:

```json
{
  "kind": "python",
  "name": "get_name",
  "where": {"/input/arguments/user_id": {"eq": 42}}
}
```

## HTTP(S) and executables

```bash
mocker forward --cassette cassettes/http.json -- curl https://example.com
mocker reverse --cassette cassettes/http.json -- curl https://example.com
```

The executable runner starts a loopback mitmproxy instance and configures proxy
variables for the child. HTTPS requires configuring that client to trust the
mitmproxy CA, normally generated in `~/.mitmproxy`. It never disables certificate
validation for the target. Programs that ignore proxy variables, pinned TLS,
non-HTTP protocols, and QUIC are outside this backend's guarantee.

HTTP lookup keys include method, scheme, host, port, path including ordered
query, raw request body, and **all request headers** in their received order.
Changing headers can cause a miss; volatile-header exclusions are future work.
Duplicate response headers and binary bodies are preserved. A replay miss or
blocked request returns a synthetic HTTP 502 diagnostic. Transport failures are
recorded as explicitly non-replayable. Replay uses lazy connections with
upstream certificate sniffing disabled. Unselected proxy requests are blocked
in reverse mode; explicit live rules can allow them.

## Partial replay

```bash
mocker reverse --runner python --config partial.json \
  --write-cassette cassettes/new.json -- python -m myapp
```

Set `replay.on_miss` or a replay rule's `on_miss` to `record`. Source cassettes
remain read-only; new interactions go to the separate destination. Existing
recording destinations are refused. There is no implicit append/overwrite.

## Sample recorded responses in reverse mode

Instead of consuming recordings in order, choose a random recorded outcome on
each call, **with replacement**:

```bash
mocker reverse --runner python --cassette cassettes/demo.json \
  --sample --seed 42 -- python -m myapp
# The same options work for HTTP requests from executables:
mocker reverse --cassette cassettes/http.json --sample --seed 42 -- curl https://example.com
```

Or save the policy in your config (also available in the TUI):

```json
{
  "replay": {
    "consume": "sample",
    "sample_match": "input",
    "seed": 42
  }
}
```

`input` samples only occurrences with the same callable/request identity and
matching inputs. `target` samples the entire pool for that exact Python function
or HTTP endpoint, ignoring recorded argument/request-input differences. For HTTP,
a target consists of method, scheme, host/port, and path; query parameters, headers,
and bodies are ignored by target-wide sampling. Different endpoints and functions
never share a pool. Use `--sample-match target` to opt into this broader pool (the
flag also enables sampling).

A specific boundary can override the global policy:

```json
{
  "select": {"kind": "python", "name": {"glob": "get_name_*"}},
  "modes": ["reverse"],
  "action": {"type": "replay", "consume": "sample", "sample_match": "target"}
}
```

Sampling chooses uniformly among recorded **occurrences**, so duplicates retain
their empirical frequency. It never exhausts a sampled occurrence. Seeded runs
produce the same sequence for the same pool and call order; concurrent scheduling
can change the order. Without a seed, runs use fresh randomness. The existing
`on_miss` policy still applies when no replayable candidate exists. Non-replayable
outputs are excluded; pickle outputs still require `--trust-pickle`.

Target-wide sampling can also use recordings whose inputs lack a stable JSON
matcher, as long as their recorded output is replayable. It deliberately ignores
receiver state and input differences. It does not execute the original function
or reproduce its side effects. Sampled mutable results are freshly reconstructed.
Replaying a sampled Python parent retires its skipped descendant recordings while
keeping that parent reusable; once-consumed recordings are excluded from sample
pools. Nested or concurrent sampling does not reproduce the original execution.

Try the included example:

```bash
mocker forward --runner python --config examples/sampling.json -- python examples/sampling.py
mocker reverse --runner python --config examples/sampling.json -- python examples/sampling.py
```

## Storage and replay guarantees

- Versioned JSON cassette plus `<stem>.blobs/` for pickled output values.
- Strict lossless JSON values remain inline; tuples, objects, cycles, and shared
  references use pickle. Non-picklable outputs become non-replayable records.
- Pickle replay requires `--trust-pickle`. Only use it for trusted recordings:
  unpickling can execute code. Blob paths are confined and SHA-256 verified.
- Arguments bind to the callable signature and defaults. Replay inputs must have a
  lossless JSON representation for input-matched replay in this release. Forward capture can store
  non-JSON inputs as pickle, but marks them non-replayable for input matching
  without a stable matching codec. Target-wide sampling can use their outcomes. Matching refuses arbitrary
  objects, including `self`/`cls`, rather than ignoring receiver state.
  Fixed mocks and live actions can work without serializable arguments.
- Inputs are snapshotted before execution. Dictionary insertion order matters.
- Repeated calls consume outcomes once per key; `consume: reuse` reuses the first
  recorded occurrence; `consume: sample` samples with replacement. Every mutable JSON/pickle result is reconstructed afresh.
- Nested Python calls have parent IDs. Replaying a parent consumes its recorded
  descendants so they cannot drift into subsequent calls.
- Reservations and journal writes are locked. Identical concurrent calls consume
  in arrival order; this does not identify logical callers or reproduce a
  scheduler. Use deterministic boundaries for such programs.
- Capture writes an fsynced append-only journal; finalized JSON is atomically
  replaced. Journals are retained for diagnostics/recovery.

Cassettes include complete request headers and inputs and may contain secrets.
Automatic redaction and safe identity fingerprinting are not implemented.

## Diagnostics

```bash
mocker validate --config mocker.json
mocker doctor --config mocker.json
mocker inspect cassettes/demo.json
mocker explain --config mocker.json --event event.json
mocker recover cassettes/demo.journal.jsonl --output cassettes/demo.json
```

Recovery tolerates a truncated final journal line and marks the recovered
cassette incomplete. Output must be the original cassette filename; it must not
already exist. Inspection never unpickles.

## Scope and remaining milestones

This release supplies **boundary-outcome replay**, not state replay. Function
argument mutation, filesystem/database changes, object identity across calls,
and other side effects are not reproduced when a body is skipped.

Implemented: synchronous/async source-defined Python callables, selected source
imports, import-time calls, descriptor-aware static/class method substitutions,
explicit HTTP proxy capture/replay, selectors, CLI/file selection, mocks,
built-in exceptions, partial replay, journals, diagnostics, and guarded pickle.

Not implemented: OS network isolation (`--offline strict` fails before launching),
transparent/local process capture, simultaneous Python and proxy interception,
Python child-process propagation, stable receiver/custom input codecs,
generator/stream replay, custom exception codecs, arbitrary alias/extension
patching, exact-name ambiguity diagnostics, cross-layer parent correlation,
configuration JSON Schema, redaction, and scheduler/side-effect replay.

Source AST instrumentation inserts wrappers at selected definitions inside
existing decorators. Decorators that replace the function entirely can bypass
capture. Closures/loader extensions and unusual decoration need explicit future
capability handling. Bare names select all matching definitions within eligible
source modules; qualify names when collisions matter.

## Development

```bash
python -m pytest -q
python -m build --no-isolation
```

Tests cover actual subprocess CLI execution, selected import-time calls, skipped
replay bodies, async/exception outcomes, nested consumption, immutable source
cassettes, pickle trust/corruption, recovery, concurrency, and HTTP replay with
its upstream server shut down. The optional network tests skip without
mitmproxy. See `docs/design.md` for architecture and milestones.
