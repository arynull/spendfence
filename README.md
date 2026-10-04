# spendfence

spendfence meters token spend from JSONL session logs into a local, hash-chained
ledger and enforces dollar caps on that spend. It answers one question — *how
much has this session or project cost, and should it keep running?* — and it
answers it entirely offline, from logs already on your disk. Caps can be enforced
per session, per project, per day, per week, or globally; `spendfence check` is a
non-zero-exit gate you can put in front of a job or in CI.

It never makes a network request, never phones home, and never runs the code it
is measuring.

## Install

From PyPI, once published:

```console
$ pip install spendfence
```

From a source checkout:

```console
$ pip install .
```

As an isolated CLI, if you prefer `pipx`:

```console
$ pipx install spendfence
```

Python 3.10 or newer. No runtime dependencies.

## Quickstart

```console
$ spendfence ingest ~/.session-log.jsonl --session nightly-refactor --project web
ingested 248 records ($4.182913) from /home/you/.session-log.jsonl

$ spendfence budget set --scope session --key nightly-refactor --cap 25.00
budget set: session:nightly-refactor cap $25.00 (warn at 80%)

$ spendfence status --session nightly-refactor
spend as of 2026-10-02 14:55 UTC
--------------------------------------------------------------
session:nightly-refactor    $     4.18 / $25.00     [##--------]   17%

burn rate (session nightly-refactor): $1.04/hr
projected breach:
  session:nightly-refactor          ~20.0h at current burn

$ spendfence check --session nightly-refactor --project web
```

`check` prints nothing and exits 0 when you are under your caps, so it can sit
unattended in a script:

```bash
spendfence check --session "$SESSION" --project "$PROJECT" || exit 1
```

## Commands

### `spendfence ingest <logfile>`

Parses a JSONL session log and appends one metered ledger record per usage
entry. Cost is computed at ingest time from the current pricing table and stored
alongside the token counts, so every dollar figure is traceable to the log line
that produced it.

```console
$ spendfence ingest run-42.jsonl --session run-42 --project api
ingested 1 records ($0.218750) from run-42.jsonl
```

| Option | Meaning |
|---|---|
| `--session NAME` | Label this session in the ledger. |
| `--project NAME` | Label this project in the ledger. |
| `--shape {auto,claude-code,codex,openai,generic}` | Log format. Default `auto`. |
| `--field-map k=path,...` | Dot-path mapping for `--shape generic`. |

`auto` sniffs the shape from the keys on each line and meters what it finds. If
no line in the first 50 carries token usage, ingestion stops and tells you which
field it was looking for rather than writing zeroes:

```console
$ spendfence ingest harness.log
error: could not read the log format of harness.log: no line in the first 50
lines carried token usage. Expected message.usage.input_tokens (claude-code
shape), usage.input_tokens (codex shape), usage.prompt_tokens (openai shape).
Pass --format generic with --map key=dotted.path for any other log shape.
Top-level keys seen: cmd, duration.
```

OpenAI Chat Completions / Responses logs are read as one line per completion:

```json
{"model":"gpt-4o","usage":{"prompt_tokens":3000,"completion_tokens":400,"prompt_tokens_details":{"cached_tokens":1200}}}
```

`usage.prompt_tokens` becomes input tokens, `usage.completion_tokens` output
tokens, and `usage.prompt_tokens_details.cached_tokens` cache reads — priced
from the cache-read column, like every other shape. `auto` picks this shape up
on its own; `--shape openai` forces it.

Unparseable lines are counted and reported, not fatal:

```console
$ spendfence ingest run-42.jsonl
ingested 2 records ($0.011875) from run-42.jsonl
skipped 1 line(s) with no readable token usage
  line 7: not a JSON object
```

For any other log shape, map the fields yourself:

```console
$ spendfence ingest vendor.log --shape generic \
    --field-map input=usage.in,output=usage.out,model=engine,cache_read=usage.cached
ingested 12 records ($0.064500) from vendor.log
```

### `spendfence budget set|list|remove`

```console
$ spendfence budget set --scope project --key web --cap 25.00 --warn-pct 80
budget set: project:web cap $25.00 (warn at 80%)

$ spendfence budget list
SCOPE     KEY                   CAP       WARN
project   web                   $25.00       80%
global                          $100.00       80%

$ spendfence budget remove --scope project --key web
budget removed: project:web
```

`--scope` is `session`, `project`, `day`, `week`, or `global`. `--key` is required
except for `global` — a `day` budget's key is the UTC date, `YYYY-MM-DD`, and a
`week` budget's key is the UTC ISO week, `YYYY-Www` (e.g. `2026-W41`). A
global budget ignores its key entirely, so there is only ever one. Caps must be
at least `$0.01`; `--warn-pct` is an integer from 1 to 99.

```console
$ spendfence budget set --scope week --key 2026-W41 --cap 50.00
budget set: week:2026-W41 cap $50.00 (warn at 80%)
```

### `spendfence check`

The enforcement gate. Order of precedence: **tampered ledger → kill switch →
breach → warning → ok**.

```console
$ spendfence check --project web --session nightly-refactor --verbose
ok project:web $22.14 / $25.00 [########--]
```

Over a warn threshold but under the cap, it warns on stderr and still exits 0:

```console
$ spendfence check --project web
WARNING: budget 'project:web' at 89% of cap (spent $22.14 of $25.00)
$ echo $?
0
```

Over the cap, it reports each breach and exits 2:

```console
$ spendfence check --project web
budget 'project:web' $12.50 cap exceeded: spent $13.02
$ echo $?
2
```

Add `--verbose` for one `ok` line per applicable budget.

### `spendfence status`

Spend against each applicable cap with a text gauge, the current burn rate, and
when each cap is projected to break.

```console
$ spendfence status --session nightly-refactor --project web
spend as of 2026-10-02 14:55 UTC
--------------------------------------------------------------
project:web              $     5.00 / $12.50     [####------]   40%

burn rate (session nightly-refactor): $1.00/hr
projected breach:
  project:web                       ~7.5h at current burn
```

Burn rate comes from the ledger's own timestamps and reads `$0.00/hr` with fewer
than two of them — a single point has no elapsed time, so any rate would be a
guess. A projection is `n/a` whenever the burn rate is 0, and `breached` when
the cap is already gone.

### `spendfence report`

Attribution tables: totals by session, by model, and by day, plus text bars.

```console
$ spendfence report --project web --since 7d
spend report — project web — last 7d
total: $18.412500  across 913 record(s)

top sessions
  SESSION                      SPEND
  nightly-refactor          $12.418500
  pr-review                    $6.994000

top models
  MODEL                        SPEND
  claude-sonnet-4            $12.418500
  gpt-4o-mini                  $6.994000

daily spend
  2026-09-26  [####------]  $4.812500
  2026-09-27  [########--]  $9.100000
```

`--since` takes `24h`, `7d`, or `30d` (default: all time). JSON output carries
every row, not just the top five:

```console
$ spendfence report --format json
{
  "by_day": {
    "2026-09-26": 4.8125
  },
  "by_model": {
    "claude-sonnet-4": 12.4185
  },
  "by_session": {
    "nightly-refactor": 12.4185
  },
  "total_usd": 12.4185
}
```

### `spendfence models`

Shows the pricing table, in dollars per million tokens. Locally-set models are
marked `*`; the cache columns only appear where they differ from the default.

```console
$ spendfence models
MODEL                   IN $/1M   OUT $/1M   CACHE R   CACHE W
claude-opus-4             15.00      75.00
claude-sonnet-4            3.00      15.00      0.30      3.75
gpt-4o*                    2.50      10.00      1.25      2.50

* = locally set   (shipped prices are estimates — check them against your provider)
```

```console
$ spendfence models set claude-sonnet-4 3.50 17.00
pricing set: claude-sonnet-4 in $3.50/1M out $17.00/1M
```

Cache columns are optional and only written when you name them:

```console
$ spendfence models set gpt-4o 2.50 10.00 --cache-read 0.50 --cache-write 3.00
pricing set: gpt-4o in $2.50/1M out $10.00/1M
```

Pricing a model the table does not know yet fills its cache columns from the
input price, and says so — otherwise cached tokens would price at `$0.00` and
under-report your spend:

```console
$ spendfence models set my-local-model 1.00 2.00
pricing set: my-local-model in $1.00/1M out $2.00/1M
  assumed cache read and cache write = input price (pass --cache-read / --cache-write to change)
```

### `spendfence advise`

Suggests the cheapest model that can plausibly handle a task, from a built-in
keyword map. It is advice only and enforces nothing.

```console
$ spendfence advise --task "summarize this PR"
ADVICE ONLY — not enforced. Recommended: gemini-2.0-flash (~$0.16/1M blended) for "summarize this PR"
  task class: fast chat   blended = 3x input + 1x output per 1M tokens
  set your own prices with: spendfence models set gemini-2.0-flash <in_per_1m> <out_per_1m>
```

Summarizing, reviewing, and docs work map to the fast-chat class; refactoring,
migrating, debugging, and architecture map to strong reasoning; anything else
falls back to balanced. Keywords match on word boundaries, so "PR" does not fire
inside "prompt". Blended cost weights three parts input to one part output.

### `spendfence stop` / `spendfence resume`

The kill switch. `stop` writes a sentinel; while it exists, `check` fails closed
regardless of caps.

```console
$ spendfence stop
kill switch ENGAGED

$ spendfence check --project web
STOPPED: kill switch engaged (spendfence resume to clear)
$ echo $?
2

$ spendfence resume
kill switch cleared
```

### `spendfence verify`

Asks "is the ledger intact?" — it checks the hash chain and then validates
every record's schema. `check` also verifies the chain before trusting any
total, but `verify` is the explicit integrity report.

```console
$ spendfence verify
ledger intact: 913 records, head 9f2c…

$ spendfence verify --format json
{
  "failures": [],
  "head": "9f2c…",
  "intact": true,
  "records": 913
}
```

A broken chain or a bad record exits 3:

```console
$ spendfence verify
LEDGER TAMPER DETECTED at record 3
$ echo $?
3
```

The detail line goes to stderr, e.g. `record 3: hash chain broken at this
record` for a chain break or `record 5: field 'input_tokens' is negative` for
a schema failure. In `--format json` mode stdout stays pure JSON and the
one-line summary moves to stderr.

### `spendfence --version`

```console
$ spendfence --version
spendfence 0.1.3
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. A budget *warning* is still success. |
| 1 | Usage error, or a failure while reading or writing state. |
| 2 | `check` only: a cap was breached, or the kill switch is engaged. |
| 3 | The ledger fails integrity: hash chain broken or record schema invalid (`check` and `verify` can both produce it). |

Warnings and failures go to stderr; everything a script might parse goes to
stdout. Errors are always a single plain-language line — never a traceback.

Exit code 2 is reserved for `check`. A mistyped flag is a usage error and exits
1, so a broken invocation can never be mistaken for a breached budget.

## State

Everything lives in one directory: `$SPENDFENCE_DATA_DIR` when set, otherwise
`~/.spendfence`. The directory is created mode `0700`; the files in it are mode
`0600`.

### `ledger.jsonl`

Append-only, one JSON object per line, hash-chained so any edit breaks every
line after it. `spendfence verify` is the explicit way to ask "is the ledger
intact?"; `spendfence check` also verifies the chain and exits 3 at the first
record that does not verify.

```json
{"cache_read":10000,"cache_write":2000,"cost_usd":"0.075000","hash":"9f2c…","input_tokens":1000,"model":"claude-sonnet-4","output_tokens":500,"prev_hash":"GENESIS","project":"web","session":"nightly-refactor","ts":"2026-10-02T09:00:00Z"}
```

`prev_hash` is `"GENESIS"` on the first record and the previous record's `hash`
after that. `cost_usd` is a fixed 6-decimal string, so a record reads back
byte-identically and exact-cent arithmetic survives the round-trip.

### `budgets.json`

```json
{
  "version": 1,
  "budgets": [
    {"cap_usd": "25.00", "key": "web", "scope": "project", "warn_pct": 80},
    {"cap_usd": "100.00", "key": "", "scope": "global", "warn_pct": 80}
  ]
}
```

A `global` budget stores an empty `key`. Cap edits through `budget set` are
validated before anything is written, so a rejected value leaves the file
untouched.

### `pricing.json`

Only the models you have overridden; anything absent falls back to the shipped
defaults.

```json
{
  "claude-sonnet-4": {
    "input_per_1m": 3.5,
    "output_per_1m": 17.0,
    "cache_read_per_1m": 0.3,
    "cache_write_per_1m": 3.75
  }
}
```

Fields you omit keep their default, so a future release can improve a default
without discarding your edits.

## About the default prices

**The shipped prices are estimates, not a live feed.** spendfence never makes a
network request, so it cannot know today's rates — they are sensible starting
points that make the arithmetic work, and nothing more. Cache columns follow each
vendor's published convention where one exists.

Correct them against your actual invoice:

```console
$ spendfence models set claude-sonnet-4 3.50 17.00
```

A model with no price at all is an error, never a silent `$0.00` — under-reporting
is the one failure a spend fence must not have.

## Offline guarantee

No network calls, no telemetry, no vendor API access, no update checks — ever.
The only thing spendfence reads is the log file you point it at and its own state
directory. It never executes the code it is measuring and never shells out to
anything.

Costs are deterministic: the same log and the same pricing table always produce
identical cents, because every dollar figure is `Decimal` arithmetic rounded to
six decimal places.

## License

MIT. See [LICENSE](LICENSE).
