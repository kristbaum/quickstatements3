# Placeholders for new items in one batch (issue #202)

Upstream issue: <https://github.com/wikimediabrasil/quickstatements3/issues/202>
Branch: `feat/202-placeholders`

## 1. The problem

Today a batch can refer to a newly created entity only through `LAST`, which
always means "the entity created by the most recent `CREATE`". Once a second
`CREATE` runs, the first new item can no longer be addressed. Linking several
new items to each other therefore takes two batches:

1. create the items,
2. look up the new QIDs (CSV report or SPARQL),
3. write and run a second batch with the links.

The issue proposes named placeholders that are bound when the item is created
and replaced by the real QID wherever they appear later in the batch:

```
CREATE	{Person_A}
LAST	Len	"Alice"
LAST	P31	Q5
CREATE	{Person_B}
LAST	Len	"Bob"
LAST	P31	Q5
LAST	P369	{Person_A}
{Person_A}	P369	{Person_B}
```

A placeholder may appear in three positions:

| Position | Example | Field in `BatchCommand.json` |
|---|---|---|
| Entity (subject) | `{Person_A}\|P369\|Q1` | `entity.id` / `item` |
| Main value | `Q1\|P369\|{Person_A}` | `value.value` (and `value_switch`) |
| Qualifier or reference value | `Q1\|P1\|Q2\|P642\|{Person_A}` | `qualifiers[].value`, `references[][].value` |

## 2. How the code works today

These are the parts that placeholders interact with.

**Parsing.** `V1CommandParser.parse()` ([src/core/parsers/v1.py](../src/core/parsers/v1.py))
and `CSVCommandParser.parse()` ([src/core/parsers/csv.py](../src/core/parsers/csv.py))
yield one unsaved `BatchCommand` per command, numbered by `index`. Values go
through `BaseParser.parse_value()`
([src/core/parsers/base.py](../src/core/parsers/base.py)). `LAST` is a special
case in `get_entity_type()` (as entity) and `parse_value_entity()` (as value).
`CREATE` currently rejects any extra column (`parse_create`). The CSV parser
emits a `CREATE` plus `LAST` commands when the `qid` cell is empty.

**Running.** `Batch.run()` ([src/core/models.py](../src/core/models.py)) walks
the commands in `index` order, keeping a single `last_id` variable:

```python
current.check_combination(state, upcoming)
current.update_last_id(last_id)   # replaces entity "LAST" and saves
current.run(client)
...
if current.action == BatchCommand.ACTION_CREATE:
    last_id = current.response_id
```

Relevant details:

- `update_last_id()` replaces only the **entity**. A `LAST` used as a *value*
  is never resolved and would be sent to the API as the literal `"LAST"`. This
  is an existing gap; placeholders fix the same thing for named references.
- The resolved ID is **persisted** into `command.json`, so the preview and the
  report show the real QID afterwards.
- `last_id` lives only in memory. After a stop/restart or rerun it starts as
  `None`, so pending `LAST` commands whose `CREATE` already ran fail with
  `LAST_NOT_EVALUATED`.

**Combining** (`Batch.combine_commands`). `check_combination()` decides whether
`current` should be folded into the next command instead of being sent. A
`CREATE` followed by `LAST …` commands becomes one `POST /entities/items`, sent
by the *last* command of the group. The new ID is only known after that final
command; `finish()` and `propagate_to_previous_commands()` then copy it into
every command in the group, including the original `CREATE`.

**Errors.** If a `CREATE` fails, the following `LAST` commands fail with
`Error.LAST_NOT_EVALUATED` (raised from `Client.wikibase_entity_endpoint` and
`get_entity_or_empty_entity`). With `block_on_errors`, the batch stops.

**Display and label lookups.** `related_identifiers_set` collects every ID in a
command for label lookups, skipping `LAST`. `entity_url()` skips `LAST`. The
template tags `render_entity_datavalue` / `entity_display` render entity
values as links.

## 3. The two versions of the feature

The deciding question is whether a placeholder may be used **before** the
`CREATE` that defines it (a forward reference).

### 3a. Without reordering (define before use)

Rule: a placeholder must be defined by `CREATE {name}` on an earlier line than
any line that uses it. The parser rejects violations.

The example in the issue already satisfies this rule. The mutual link
`A → B` and `B → A` is expressed by placing the second link after both
`CREATE`s, which is how users write such batches anyway.

What makes it simple:

- Execution order stays exactly `index` order. Every placeholder is bound
  before any command that needs it runs, so resolution is a lookup in a dict
  that grows as the batch runs, the same mechanism as `last_id`.
- Validation is a single pass in the parser: keep a set of defined names.
- Indices, preview, report, `block_on_errors`, stop/restart and rerun keep
  their current meaning.
- Combining needs only one guard (see below).

The remaining difficulties:

1. **Self-reference inside a combined creation.** With combining on, this group
   is a single POST:

   ```
   CREATE	{A}
   LAST	P31	Q5
   LAST	P1659	{A}        # "see also: itself" (contrived, but valid)
   ```

   When the third command is folded into the group, `{A}` has no QID yet, since
   it is assigned only by the POST. The same applies to any command that
   refers to the entity currently being created. Fix: in `check_combination()`,
   do not combine `next` if it references a placeholder that is not yet bound.
   Parse-time validation guarantees that such a placeholder can only be the one
   still being created, so the group is sent first, `{A}` gets its QID, and the
   command becomes a separate PATCH.
2. **Binding inside combined groups.** The QID arrives on the *last* command of
   the group, not on the `CREATE`. Binding has to look at
   `[current, *current.previous_commands]` after each run and take the
   `CREATE` command that carries the placeholder name.
3. **Restart and rerun.** The map must not live only in memory. It can be
   rebuilt at the start of `Batch.run()` from the database: every `DONE` create
   command with a `placeholder` key already has its QID in `json.entity.id`,
   thanks to `finish()` / `propagate_to_previous_commands()`. No new model
   field or migration is needed.
4. **Failure propagation.** If `CREATE {A}` fails, every later command that
   references `{A}` (in any position) must fail with a clear error, analogous
   to `LAST_NOT_EVALUATED`.
5. **Preview before running.** Before the batch runs, placeholders have to be
   shown as plain text (`{Person_A}`), not as links or label lookups.

### 3b. With reordering (forward references allowed)

Rule: a placeholder may be used anywhere in the batch, as long as it is defined
somewhere in it. Examples that need this:

```
CREATE	{A}
LAST	P369	{B}        # B does not exist yet
CREATE	{B}
LAST	P369	{A}
```

or a CSV where row 1 links to the item created in row 5. For CSV this case is
much more common than for V1, since one row is one item and rows naturally
point at each other.

Possible strategies:

1. **Defer at run time.** Run in `index` order. When a command references an
   unbound placeholder, put it on a deferred queue and retry it after the
   corresponding `CREATE` has run (or at the end of the batch).
2. **Reorder at parse time.** Topologically sort commands so that every use
   follows its definition, and store the new order in `index`.
3. **Split automatically.** Turn every command with a forward reference into a
   separate "patch" command appended after all `CREATE`s. This always
   terminates, cycles included, because items are created first and linked
   afterwards.

Problems shared by all three:

- **Cycles.** `A → B` and `B → A` cannot both be written during creation. One
  item has to be created without the link and patched later. A topological
  sort (strategy 2) alone cannot handle this; it has to fall back to splitting.
- **Order-dependent semantics.** V1 commands are not commutative. Moving a
  command past others can change the result:
  - `-Q1|P31|Q5` followed by `Q1|P31|Q5` versus the reverse,
  - `SWITCH_VALUE` / `SWITCH_PROPERTY` and `REMOVE_QUAL`/`REMOVE_REF` that
    depend on a statement added by an earlier line,
  - two `Len` lines on the same entity (last one wins),
  - `LAST` itself: a moved `LAST|…` line would bind to a different `CREATE`.
    Every `LAST` would have to be rewritten to an implicit placeholder (for
    example `{#3}` for the `CREATE` at index 3) *before* any reordering.
- **Combining.** Moved commands break combine groups, so an item gets several
  edits instead of one. That undercuts one of QS3's selling points. A smarter
  approach would also move the *other* edits to the same entity together,
  which again risks the ordering problems above.
- **User expectations and UI.** The preview, the run log, the report and the
  error messages are all organised by `index`. If execution order differs from
  line order, the UI has to show both, or users will not understand which line
  failed and why an item has two edits.
- **`block_on_errors`.** "Stop at the first failing command" becomes ambiguous
  if the first failure in execution order is not the first in line order.
- **Restart and rerun.** A deferred queue (strategy 1) is in-memory state that
  has to survive a stop/restart; it would have to be persisted or rebuilt.
- **Failure of a definition.** If `CREATE {B}` fails, the deferred commands that
  reference `{B}` must fail too. They may be far away from the failing line,
  which makes the report harder to read.

Summary of the trade-off:

| | Without reordering | With reordering |
|---|---|---|
| Covers the issue's example | yes | yes |
| Covers forward references and CSV rows that point "down" | no, the user must reorder lines | yes |
| Execution order equals line order | yes | no |
| Cycles | written by the user as a later line | need automatic splitting |
| Effect on combining | one extra guard | groups get split, more edits |
| Effect on `LAST` semantics | none | `LAST` must become implicit placeholders first |
| Restart/rerun | map rebuilt from DB | map plus deferred queue must be persisted |
| Risk of changed results | none | order-dependent commands can change meaning |
| Size of change | parser, `Batch.run()`, a few helpers, UI | all of the left, plus scheduler, UI for execution order |

**Recommendation:** implement 3a first. It covers the request in the issue,
keeps all existing guarantees, and is a strict subset of 3b: the parser error
"placeholder used before definition" can later be relaxed into "defer this
command" without changing the syntax or the stored data. A cheap middle ground
for CSV is a clear error message that names the row defining the placeholder,
so the user can move that row up.

## 4. Implementation plan for 3a (no reordering)

### 4.1 Syntax

- Placeholder token: `{name}`, where `name` matches `[A-Za-z0-9_-]{1,100}`
  (regex `^\{[A-Za-z0-9_\-]{1,100}\}$`). Names are case-sensitive.
- Definition, V1: `CREATE<TAB>{name}` and `CREATE_PROPERTY<TAB>datatype<TAB>{name}`.
  A `CREATE` without a placeholder keeps working as before.
- Definition, CSV: a `qid` cell containing `{name}` means "create a new item
  bound to this name". An empty `qid` keeps its current meaning.
- Use: anywhere an entity ID is accepted as subject, main value, switch value,
  qualifier value or reference value.
- Not allowed: in `MERGE`, `-STATEMENT` (statement IDs), and as a property
  (`P…`) position.
- `LAST` keeps its current meaning. `CREATE {A}` also sets `LAST`, so the
  issue's example works as written.

### 4.2 Parser (`src/core/parsers/`)

1. `base.py`
   - `PLACEHOLDER_RE` and `is_placeholder(value)`.
   - `get_entity_type()`: return `"item"` for a placeholder. Property
     placeholders can come later; they are rare and need the datatype.
   - `parse_value_entity()`: return
     `{"type": "wikibase-entityid", "value": "{name}"}` for a placeholder.
     Keep the original case (do not `.upper()` it).
2. `v1.py`
   - `parse_create()` accepts an optional second column. If present it must be
     a placeholder: `{"action": "create", "type": "item", "placeholder": "A"}`.
     Same for `parse_create_property()` with a third column.
   - In `parse()`, keep `defined: dict[str, int]` (name → index). For each
     command:
     - definition of an already defined name → error "Placeholder {A} is
       already defined on line N",
     - use of an undefined name → error "Placeholder {A} is used before it is
       defined" (or "never defined").
     - Collect uses with a helper `BatchCommand.placeholders_used()` (below),
       so V1 and CSV share it.
   - A failed `CREATE {A}` line (parse error) does not define `{A}`, so its
     uses get the "not defined" error too.
3. `csv.py`
   - Interpret `{name}` in the `qid` column as a definition: emit
     `{"action": "create", "type": "item", "placeholder": name}` and continue
     with `LAST` exactly like the empty-`qid` path.
   - Values already go through `parse_value()`, so `{B}` in a cell becomes an
     entity value automatically.
   - Same `defined` bookkeeping and errors as V1, referencing rows.

### 4.3 Model (`src/core/models.py`)

New helpers on `BatchCommand`:

- `placeholder` (property): `self.json.get("placeholder")` on create commands.
- `placeholders_used()`: names found in entity ID, `value`, `value_switch`,
  qualifier values and reference values.
- `resolve_placeholders(mapping: dict[str, str])`: replace every placeholder in
  those fields with its QID, then `save()`. Raise
  `PlaceholderCouldNotBeEvaluated(name)` if a name is missing from the map.
  Reset the cached `_value_dict`.
- `has_unresolved_placeholders(mapping)`: used by combining.

New error:

- `Error.PLACEHOLDER_NOT_EVALUATED` and exception
  `PlaceholderCouldNotBeEvaluated` in `src/core/exceptions.py`, handled in
  `BatchCommand.run()` like `LastCouldNotBeEvaluated`. Adding the choice
  produces a migration (`0044_…`, choices only, no schema change).

`Batch.run()` changes:

```python
placeholders = self.bound_placeholders()   # rebuilt from DONE create commands
...
current.check_combination(state, upcoming, placeholders)
current.update_last_id(last_id)
current.resolve_placeholders(placeholders)  # may mark the command as error
current.run(client)
...
for cmd in [current, *getattr(current, "previous_commands", [])]:
    if cmd.placeholder and cmd.status == BatchCommand.STATUS_DONE and cmd.entity_id:
        placeholders[cmd.placeholder] = cmd.entity_id
```

- `bound_placeholders()` queries create commands with status `DONE`, reads
  `json["placeholder"]` and `json["entity"]["id"]`. This also makes restart and
  rerun work for placeholders.
- `resolve_placeholders` is called after `check_combination` but before `run`.
  On failure it calls `error_with_value(...)`, so the existing error path and
  `block_on_errors` handling apply unchanged.
- `check_combination(..., placeholders)`: add
  `and not next.has_unresolved_placeholders(placeholders)` to the
  combinability condition (difficulty 1 above).

Optional, in the same change or as a follow-up: also resolve `LAST` used as a
value, which closes the existing gap mentioned in section 2.

### 4.4 Display

- `related_identifiers_set`: skip placeholders (as it does for `LAST`), so no
  label lookup is made for `{A}`.
- `entity_url()`: return `""` for placeholders.
- `render_entity_datavalue` / `entity_display` in
  `src/core/templatetags/quickstatements.py`: render a placeholder as escaped
  plain text, for example `<span class="placeholder">{A}</span>`.
- After a run, values hold real QIDs and render as links, the same as `LAST`
  today.

### 4.5 API

`api/serializers.py` uses the same parsers, so batches created through the REST
API get placeholders for free. Update the API docs/examples if they list the
syntax.

### 4.6 Tests

Parser (`test_v1_parser.py`, `test_csv_parser.py`, `test_base_parser.py`):

- `CREATE\t{A}` produces `placeholder: "A"`; `CREATE` without it is unchanged.
- Placeholder in entity, value, qualifier and reference positions.
- Errors: use before definition, never defined, duplicate definition, invalid
  name, placeholder in `MERGE` or as property.
- CSV: `{A}` in `qid` column; `{A}` in a value cell pointing to an earlier row;
  forward reference error.

Processing (`test_batch_processing.py`, using the existing mocked API):

- Issue example, with `combine_commands` off and on; assert the final JSON of
  every command contains real QIDs and the API received them.
- Self-reference in a combined creation is split into POST + PATCH.
- `CREATE {A}` fails, so later uses of `{A}` fail with
  `PLACEHOLDER_NOT_EVALUATED`; with `block_on_errors` the batch blocks.
- Stop after `CREATE {A}`, restart, and the remaining `{A}` commands resolve
  from the DB.
- Rerun of a batch where a command using `{A}` failed for an unrelated reason.

Template tags (`test_templatetags.py`): placeholder rendered as text, no link.

### 4.7 Documentation

- README "Command formats" section: short placeholder example for V1 and CSV.
- CHANGELOG entry.
- Note for the on-wiki user guide (meta.wikimedia.org) to be updated by the
  maintainers.
- New user-visible strings (error messages, labels) go through
  `pgettext_lazy` so translatewiki.net picks them up.

### 4.8 Order of work

1. Parser support plus parser tests (no behaviour change for existing input).
2. Model helpers, error type, migration.
3. `Batch.run()` resolution and binding, combining guard, processing tests.
4. Display changes and template tag tests.
5. README, CHANGELOG.
6. Run the test suite (`make test`, i.e. `pytest` in the app container) and
   `ruff`, then open a PR upstream referencing #202.

## 5. Implementation status

Version 3a is implemented on this branch.

| Part | Where |
|---|---|
| Placeholder regex | `src/core/placeholders.py` (own module, because the parsers import the models) |
| Parsing and define-before-use check | `BaseParser.check_placeholders`, `V1CommandParser.parse_create*`, `CSVCommandParser.parse_line` |
| Resolution, binding, combining guard | `BatchCommand.placeholders_used / resolve_placeholders / bind_placeholders / check_combination`, `Batch.bound_placeholders / run` |
| Error | `Error.PLACEHOLDER_NOT_EVALUATED`, `PlaceholderCouldNotBeEvaluated`, migration `0044` |
| Display | `render_entity_datavalue`, `is_placeholder` filter, `batch_commands.html` |
| Tests | `src/core/tests/test_placeholders.py` |

Differences from the plan above:

- **`{A}` as entity joins its own creation.** With combining on,
  `CREATE {A}` followed by `{A}|Len|…` is treated like `CREATE` followed by
  `LAST|Len|…` and sent as one POST. Only a placeholder used as a *value*
  inside its own creation group splits the group.
- **CSV keeps `LAST` internally.** A `{A}` in the `qid` column creates a
  `CREATE` with `placeholder: "{A}"`, and the row's other commands keep using
  `LAST`, like the empty-`qid` path.
- **Fixed an existing `LAST` bug.** When a combined creation was sent by the
  last command of its group (an ADD), `last_id` was not updated, so a later
  `LAST` command that could not be combined failed. Splitting groups for
  self-references made this reachable, so `Batch.run()` now also takes the id
  from a combined creation.
- **Migration `0044` also contains the `remove_reference_block` operation
  choice**, which was missing from the migrations before this change
  (choices only, no schema change).
- **`LAST` used as a value is still not resolved** (the existing gap described
  in section 2). Not part of this change.

## 6. Possible follow-ups

- Forward references (section 3b), most likely via automatic splitting into a
  trailing "link" phase, behind an explicit option such as "allow forward
  references" so existing batches are unaffected.
- Property placeholders (`CREATE_PROPERTY … {P_new}` used as a property ID),
  which also affects value-type verification.
- Export of the placeholder → QID map in the batch report CSV.
