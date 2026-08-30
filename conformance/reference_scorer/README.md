# Foundation reference scorer

This package holds the isolated deterministic reference scorer for
Foundation conformance. The scorer is one pure function from input
bytes to output bytes. Foundation conformance calls this package
directly, before Evaluation V2 exists.

The scorer is not an evaluation authority. It performs no database,
network, artifact, runtime, or benchmark interface access. It writes
no file and no canonical evaluation record.

## Scorers

The package provides exactly two scoring modes:

1. `exact_match` gives score `1.000000` when the actual string equals
   the expected string, and `0.000000` in every other case. The
   comparison is exact. It does not trim, fold case, or normalize
   Unicode forms.
2. `bounded_numeric` gives `max(0, min(1, 1 - |actual - expected| /
   tolerance))`. The tolerance must be greater than zero. All
   arithmetic uses decimal numbers, never binary floating point.

Each score quantizes to exactly six fraction digits with banker's
rounding (`ROUND_HALF_EVEN`).

## Pinned contract

| Element | Pinned value |
| --- | --- |
| Executable format | One Python 3.13 module: `reference_scorer.py` |
| Dependencies | The Python standard library only |
| Input schema | One UTF-8 JSON document; see the input contract below |
| Output schema | `result.schema.json` (`bmas.reference_scorer_result`) |
| Contract version | `1.0.0`, stored only in explicit metadata values |
| Decimal context | 34 significant digits, `ROUND_HALF_EVEN` |
| Locale | The output never depends on the process locale |
| Random source | None. The scorer uses no randomness |
| Clock source | None. The scorer never reads a clock |
| Resource limits | The `MAX_*` module constants |
| Failure codes | `0` success, `2` invalid input, `3` invalid output, `4` resource limit |

Resource limits: 1,048,576 input bytes; 8,388,608 output bytes;
10,000 cases; 65,536 characters per text; 120 characters per case
identifier; 34 digits and a decimal exponent magnitude of at most 50
per number.

## Input contract

```json
{
  "schema_id": "bmas.reference_scorer_input",
  "metadata": { "contract_version": "1.0.0" },
  "scorer": "exact_match",
  "cases": [
    { "case_id": "greeting", "expected": "hello board", "actual": "hello board" }
  ]
}
```

A `bounded_numeric` case carries `case_id`, `expected`, `actual`, and
`tolerance` as JSON numbers. The scorer rejects unknown fields,
duplicate keys, duplicate case identifiers, empty case lists,
non-finite numbers, and every value outside the resource limits.

## Determinism

The scorer normalizes each valid input document before scoring:
numbers take one canonical decimal text form, keys sort, and the
encoding is compact ASCII JSON. The result stores the SHA-256 digest
of that normalized form as `input_sha256`. Two inputs with one meaning
therefore produce byte-identical output on every supported host. The
output bytes are canonical ASCII JSON with one trailing line feed.

## Invocation

In memory:

```python
from reference_scorer import score_bytes
output_bytes = score_bytes(input_bytes)
```

As a command that reads one input and writes the result to stdout:

```sh
python3 conformance/reference_scorer/reference_scorer.py fixtures/exact-match-mixed.input.json
python3 conformance/reference_scorer/reference_scorer.py < input.json
```

The authoritative test manifest registers this package as the
required group `conformance.reference-scorer`.

## Fixtures

`fixtures/` holds frozen input bytes (`<name>.input.json`), frozen
expected output bytes (`<name>.expected.json`), and `digests.json`
with the SHA-256 digest of every input, every normalized input, and
every expected output. An implementation in any language can verify
itself against these bytes and digests without running Python.

## Relation to the test-manifest contract

`result.schema.json` validates scorer output bytes only. It is not
the runner-result schema from package P.1. Neither schema can
validate a record from the other, and the test suites of both
packages assert that exclusion.
