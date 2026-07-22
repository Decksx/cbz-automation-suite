# Possible Same-Series Grouping

This update adds a conservative manual-review grouping mode to `organize-series`.

It detects sibling directories that are likely different entries from the same series, such as:

```text
Batman And Superman - Fighting the Joker
Batman & Superman - Battle Against Catwoman
Bat man + Super man - Team Up Against Evil
```

and moves them into:

```text
_Check/
└── Batman And Superman/
    ├── Batman And Superman - Fighting the Joker/
    ├── Batman & Superman - Battle Against Catwoman/
    └── Bat man + Super man - Team Up Against Evil/
```

It also catches typo-tolerant prefix matches such as:

```text
A Class Divided: Enter the Teacher
A Class Divivded: The Lunch Bell
A Class Divided: To the Principals Office
```

and groups them into:

```text
_Check/
└── A Class Divided/
```

## Usage

Dry run first:

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run --possible-series-check
```

Live run:

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --possible-series-check
```

Tune sensitivity:

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run --possible-series-check --series-common-words 3 --series-min-group-size 3
```

## Options

| Option | Default | Meaning |
|--------|---------|---------|
| `--possible-series-check` | off | Enables this grouping mode |
| `--series-common-words` | `2` | Minimum fuzzy common title-prefix words |
| `--series-min-group-size` | `2` | Minimum directories needed to create a review group |

## Safety behavior

- The tool moves candidates into `_Check/<suggested series>/`.
- Original directory names are preserved.
- Existing `_Check` groups are not overwritten; numbered suffixes are added.
- `--dry-run` logs every proposed move without touching files.
- This is intentionally a review workflow, not an automatic merge.
