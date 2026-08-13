# DirectSkip "contactinfo" Column Reference

Observed on a 266-column, 196-row return (`248923-contactinfo-*.csv`, 2026-08).
Column *names* are stable; the *count* varies with how many relative and person
blocks the vendor fills.

## Block layout

The file is four blocks, and blocks 3-4 repeat three times.

### 1. Echo (what you sent) — cols 0-12
```
Input Last Name, Input First Name,
Input Mailing Address, Input Mailing City, Input Mailing State, Input Mailing Zip,
Input Property Address, Input Property City, Input Property State, Input Property Zip,
Input Custom Field 1, Input Custom Field 2, Input Custom Field 3
```
**`Input Custom Field 1/2/3` came back 100% EMPTY.** Do not plan a join around
them. Rejoin key is normalized `Input Property Address` + 5-digit ZIP.

`Input * Zip` may be ZIP+4 (`78758-7421`) — truncate to 5.

### 2. Match result — cols 13-17
```
ResultCode, Matched First Name, Matched Last Name, Age, Deceased
```

| ResultCode | Meaning | Observed |
|---|---|---|
| `CI` | Contact info found, name matched | 190 / 196 |
| `AB1` | **Address-based** match — different person returned | 3 |
| `AB2` | **Address-based** match — different person returned | 2 |
| *(blank)* | No match at all | 1 |

`Deceased` is `Y` / `N` / blank. `Y` observed on 9 / 196.

### 3. Person block (repeats: no prefix, `Person2 `, `Person3 `)
```
{p}Phone1..{p}Phone7  +  {p}Phone{n} Type
{p}Email1, {p}Email2
{p}Confirmed Mailing Address / City / State / Zip
```
The unprefixed block is the primary person. `Person2 First Name` /
`Person2 Last Name` / `Person2 Age` / `Person2 Deceased` precede the Person2
block.

Fill observed: Person2 on 25 / 196, Person3 on 3 / 196.

### 4. Relative block (5 per person, all three persons)
```
{p}Relative{j} Name, {p}Relative{j} Age,
{p}Relative{j} Phone1..Phone5  +  {p}Relative{j} Phone{n} Type
```
`j` = 1..5. So the full relative universe per row is up to
**3 persons x 5 relatives x 5 phones = 75 numbers**, on top of 21 person phones.

A relative slot with a blank `Name` must be skipped entirely — an unattributable
number must never reach a dial slot.

## Line types

| Vendor value | DataSift | Notes mark |
|---|---|---|
| `Mobile` | `MOBILE` | `M` |
| `Residential` | `LANDLINE` | `L` |
| `OtherPhone` | `OTHER` | `O` |
| *(blank)* | *(blank)* | `O` |

## Volume observed (196 records)

| Bucket | Count |
|---|---|
| Primary-person phones | 859 |
| Person2 / Person3 phones | 101 |
| Relative phones | 2,485 |
| **Unique after cross-dedup** | **3,035** |
| Per record | min 0, avg 15.5, **max 58** |
| Records over DataSift's 30 slots | 4 |

The gap between 3,445 raw and 3,035 unique is real: a co-owner is usually also
listed as one of the owner's relatives, and households share landlines.

## Data-quality patterns seen

- **Name drift on `AB1`/`AB2`** — `Barbara Smith → DONALD SMITH`,
  `C J C Drake → DAVID DRAKE`, `Gonzalez Martinez → JOSE MARTINEZ GONZALEZ`.
  Same household, different person. Flag; do not silently accept.
- **Junk input surfaces here** — `Training Tcad → MICHAEL CATON` was a TCAD
  training record that never belonged in a marketing list.
- **Confirmed mailing often differs** (71 / 196) and is frequently a real
  relocation to another city. Record it; do not apply it.
- **All names arrive ALL-CAPS.**
- **Look-alike numbers exist within a single record** — e.g. `505-642-0041` and
  `575-642-0041` are both genuine and one digit apart. Matters when picking a
  canary record for an append/replace test: a look-alike makes the "did the old
  number survive?" check unreadable.
