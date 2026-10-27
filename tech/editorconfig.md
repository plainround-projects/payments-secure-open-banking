# EditorConfig

## What it is

EditorConfig is a tiny, editor-independent settings file named `.editorconfig`. It tells any supporting editor (VS Code, JetBrains IDEs, Vim, Sublime and others, some through a plugin) how to handle files: indent with spaces or tabs, how wide, which line endings, whether to add a final newline. It does not run code; editors read it when you open or save a file.

## Why this repository uses it

Small invisible differences (tabs vs spaces, trailing spaces, Windows vs Unix line endings) create noisy diffs and, in two cases here, real errors: a `Makefile` **requires tabs**, and YAML **forbids tabs**. One file makes every contributor's editor do the right thing.

## Where it appears

| Path | Role |
| --- | --- |
| `.editorconfig` | The settings (18 lines). |

## How it works underneath

The editor looks for `.editorconfig` in the file's folder and each parent folder up to one marked `root = true`. Sections in `[brackets]` are glob patterns on file names; a file takes the settings of every section that matches, later sections overriding earlier ones.

## Walkthrough of this repository's use

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4
```

- `root = true`: stop searching parent folders.
- `[*]`: applies to every file.
- `charset = utf-8`: the text encoding.
- `end_of_line = lf`: Unix line endings, so files are the same on macOS, Linux and CI.
- `insert_final_newline`: files end with one newline (a POSIX convention that keeps `cat` and diffs tidy).
- `trim_trailing_whitespace`: strip spaces at line ends.
- `indent_style = space`, `indent_size = 4`: four spaces per level, which is also what Python's formatter (Ruff) produces.

```ini
[*.{yml,yaml,json,toml}]
indent_size = 2

[Makefile]
indent_style = tab

[*.md]
trim_trailing_whitespace = false
```

- YAML, JSON and TOML use two-space indentation by convention.
- A `Makefile` recipe line must begin with a tab, so the editor must not turn tabs into spaces there.
- In Markdown, two trailing spaces mean a line break, so trimming is turned off for `.md` files.

## Try it yourself

1. Read the file: `cat .editorconfig`. Expected: the 18 lines above.

2. Find files that break the "final newline" rule:

   ```bash
   for f in $(git ls-files); do [ -n "$(tail -c1 "$f")" ] && echo "no final newline: $f"; done
   ```

   Expected: no output.

3. Check the Makefile uses tabs:

   ```bash
   grep -n "^$(printf '\t')" Makefile | head -3
   ```

   Expected: lines beginning with a tab character.

4. In an editor with EditorConfig support, open `Makefile` and press Tab: it inserts a real tab.

## Mistakes people make

- **Assuming it is enforced.** An editor without support ignores it; CI does not check it. Ruff enforces formatting for Python.
- **Spaces in a Makefile**: `missing separator`.
- **Tabs in YAML**: parse error.
- **Trimming trailing whitespace in Markdown** and losing intentional line breaks.

## Check yourself

1. Why is `Makefile` set to tabs? *Make requires a tab at the start of recipe lines.*
2. What does `root = true` do? *Stops the editor from reading `.editorconfig` files in parent folders.*
3. Why is trimming off for `.md`? *Two trailing spaces can mean a line break in Markdown.*
4. Does EditorConfig change files by itself? *No, editors apply it when you edit.*
5. Which setting makes line endings the same everywhere? *`end_of_line = lf`.*

## Further reading

- EditorConfig specification and supported properties: https://editorconfig.org/
