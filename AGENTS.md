# Mocker guide for agents

Use this guide to configure and run Mocker or work on this repository. Python
3.11+ is supported. The distribution is `mocker-record-replay`; the CLI and
import are `mocker`. Run commands below from the repository root.

## Setup and first run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,network]'
mocker --help
mocker forward --help
```

On Windows activate with `.venv\Scripts\Activate.ps1`. The `dev` extra includes
the Textual TUI; installations for users can use `.[tui,network]`. Core Python
record/replay requires no runtime dependencies. `python -m mocker` is also valid.

```bash
# Use a fresh cassette path; recording refuses existing destinations/journals.
mocker forward --runner python --cassette cassettes/first-run.json --trace 'demo:get_name_*' -- python examples/demo.py
mocker inspect cassettes/first-run.json
mocker reverse --runner python --cassette cassettes/first-run.json -- python examples/demo.py
```

`record` aliases `forward`; `replay` aliases `reverse`. Put Mocker flags before
`--` and the target command and its arguments after it. The Python runner uses
Mocker's interpreter: a leading `python` token does not select another Python.
It accepts script paths and `-m module`, not interpreter flags such as `-c`.
Script targets use the filename stem (e.g. `demo:get_name_user`); imported targets
use `module:qualified_name`. Prefer qualified targets to avoid name collisions.
Entrypoint source is instrumented automatically; imported source requires
`--scope myapp` or `python.scope`. Scopes can be repeated.

## JSON configuration

Create a config such as `mocker.json`:

```json
{
  "version": 1,
  "cassette": "cassettes/app.json",
  "python": {"scope": ["myapp"]},
  "trace": ["myapp.users:get_name_*"],
  "replay": {"on_miss": "error", "consume": "once"},
  "rules": [
    {
      "id": "fixed-name",
      "priority": 10,
      "modes": ["reverse"],
      "select": {"kind": "python", "target": "myapp.users:get_name"},
      "action": {"type": "mock", "outcome": {"return": "Ryan"}}
    }
  ]
}
```

Replace `myapp` with the actual package and targets, then run:

```bash
mocker validate --config mocker.json
mocker doctor --config mocker.json
mocker forward --runner python --config mocker.json -- python -m myapp
mocker reverse --runner python --config mocker.json -- python -m myapp
```

| Field | Meaning |
| --- | --- |
| `version` | Configuration version, currently `1` |
| `cassette` | Forward destination or reverse source JSON path |
| `python.scope` | List of import roots eligible for instrumentation |
| `trace` | Python selector strings or structured selectors |
| `targets` | Structured Python/HTTP selectors (OR across entries) |
| `mock` | Shorthand list such as `[{"get_name_*": "Ryan"}]`; applies in both modes |
| `rules` | Ordered selector/action rules, optionally restricted by mode |
| `replay.on_miss` | `error` (default), `live`, or `record` |
| `replay.consume` | `once` (default), `reuse` (first occurrence), or `sample` |
| `replay.sample_match` | `input` (default) or `target`, used with sampling |
| `replay.seed` | Integer or null; repeatable sampling for the same pool/call order |
| `network.backend` | `none` or `proxy`; executable runner always uses proxy |
| `tui` | Saved UI run settings; ignored by the CLI |

Relative cassette and program paths resolve against the working directory, not
the config file's directory. `--cassette` overrides the configured path. CLI
traces append to configured traces; CLI scopes replace configured scopes.
Without `--config`, reverse restores configuration from the source cassette.
Supplying `--config` uses that config instead; it is not merged with the recorded
config. CLI sampling flags override the corresponding replay fields.

### Selectors and rules

String shorthand accepts shell globs. Canonical selector fields are exact strings
unless written as `{"glob": "pattern"}`. Fields within a selector combine with
AND. Python fields: `kind: python`, `target`, `module`, `name` (final function
name). HTTP fields: `kind: http`, `method`, `host`, `path`.

`where` predicates use JSON pointers, with `eq`, `in`, or `exists`, for example:

```json
{"kind": "python", "name": "get_name", "where": {"/input/arguments/user_id": {"eq": 42}}}
```

Rules enroll their selectors for interception in their applicable modes. Higher
numeric `priority` wins; ties follow configuration order. The first matching
rule provides the entire action. `modes` accepts `forward` and `reverse` (not
alias names); omitted modes means both. Selected boundaries without a matching
rule record in forward mode and replay in reverse mode.

Actions are `record`, `replay`, `mock`, `live`, and `block`. Replay actions can
override `on_miss`, `consume`, and `sample_match`. Mock outcomes must specify
exactly one of:

```json
{"return": "Ryan"}
```

```json
{"raise": {"type": "builtins:ValueError", "args": ["bad input"]}}
```

```json
{"response": {"status": 200, "headers": [["Content-Type", "application/json"]], "body": {"json": {"name": "Ryan"}}}}
```

The last form is for HTTP; bodies also accept `text` or `base64`. Use canonical
rules with `modes: ["reverse"]` when substitutions should only apply in replay.
`--trace-file` accepts selector lines (blank lines and `#` comments allowed) or a
JSON array of selector strings/objects.

## Sampling and partial replay

```bash
mocker reverse --runner python --cassette cassettes/first-run.json --sample --seed 42 -- python examples/demo.py
mocker reverse --runner python --cassette cassettes/first-run.json --sample-match target --seed 42 -- python examples/demo.py
```

Sampling is uniform over recorded occurrences, with replacement. `input` requires
matching inputs; `target` ignores input differences but stays within the exact
function or HTTP endpoint. For HTTP, the endpoint includes method, scheme,
host/port and path, but excludes query, body and headers. Config equivalent:

```json
{"replay": {"consume": "sample", "sample_match": "target", "seed": 42}}
```

These are replay settings to include in a full config. Sampling never executes
the original body on a hit. It does not recreate side effects. For a complete
runnable example see `examples/sampling.json` and `examples/sampling.py`.

For partial replay set `replay.on_miss` to `record` (or use a replay rule with
that setting) and supply a distinct, fresh destination:

```bash
mocker reverse --runner python --config mocker.json --write-cassette cassettes/new.json -- python -m myapp
```

The source stays read-only; only newly recorded interactions go to the destination.
Do not delete or overwrite existing recordings just to make a command succeed.

## HTTP and TUI

```bash
mocker forward --runner exec --cassette cassettes/http.json -- curl http://example.com
mocker reverse --runner exec --cassette cassettes/http.json -- curl http://example.com
mocker tui --config examples/mocker.json
```

The default `exec` runner starts mitmproxy and sets child proxy environment
variables. HTTPS clients must trust the mitmproxy CA (normally in `~/.mitmproxy`);
certificate validation is not disabled. Programs that ignore proxy variables,
pinned TLS, non-HTTP protocols and QUIC are unsupported. Python instrumentation
and proxy interception cannot currently be combined in one run.

The TUI supports editing, saving, validating and running configs. Ctrl+S saves,
F5 runs, F6 stops. Its command field is split into arguments without shell
expansion. Cassette/program paths resolve against its selected working directory.

## Diagnostics, limits and development

```bash
mocker inspect cassettes/first-run.json
mocker explain --config mocker.json --event event.json --mode reverse
mocker recover cassettes/first-run.journal.jsonl --output cassettes/first-run.json
python -m pytest -q
python -m build --no-isolation
```

For `explain`, an event file can contain:

```json
{"kind": "python", "target": "myapp.users:get_name", "module": "myapp.users", "name": "get_name", "input": {"arguments": {"user_id": 42}}}
```

Recovery requires the original cassette output filename to be absent; it marks
the recovered cassette incomplete. Inspection does not unpickle. Validation
checks implemented config constraints; it does not verify that targets exist.

Recordings can contain credentials, headers and inputs: automatic redaction is
not implemented. Commit examples with synthetic data, not private recordings.
Pickled outcomes require explicit `--trust-pickle`; load only trusted recordings
because unpickling can execute code. Input matching needs lossless JSON values;
objects such as `self`/`cls` lack a stable matcher. Target-wide sampling can use
replayable outcomes despite non-JSON inputs, but ignores receiver state.

Replay skips function bodies and does not reproduce filesystem/database writes,
argument mutation or other side effects. `--offline strict` deliberately refuses
to launch: OS network isolation is not implemented. HTTP input matching includes
all headers in received order, so volatile headers can cause misses. Generators,
streaming, custom exceptions and child-process Python instrumentation are not
supported. See README.md and docs/design.md for the full limitations.

When changing code, preserve these guarantees and run the relevant tests. The
network tests require the `network` extra; TUI tests require Textual. CI tests
Python 3.11, 3.12 and 3.13. Keep CLI help, this guide and README examples aligned
with actual supported behavior.
