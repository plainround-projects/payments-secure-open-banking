# YAML

## What it is

YAML is a plain-text format for structured data, written with **indentation** instead of braces. It has three building blocks: **mappings** (`key: value`), **lists** (lines starting with `- `) and **scalars** (strings, numbers, true/false). Many infrastructure tools use it for configuration. In this repository it appears in the Docker Compose file and the GitHub Actions workflow.

## Why this repository uses it

Both tools that need configuration (Docker Compose and GitHub Actions) define YAML as their input format. There is no choice, but there is a lot of room to make mistakes, so this note explains the rules.

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Services, ports, environment, health checks (see `docker-compose.md`). |
| `.github/workflows/ci.yml` | Events, jobs, steps (see `github-actions.md`). |

## How it works underneath

A YAML parser reads the text line by line, uses the **indentation level** (spaces only, never tabs) to decide nesting, and builds dictionaries and lists. Quotes are optional for most strings, which causes surprises: the parser guesses a type from the text.

## Walkthrough of this repository's use

### Mappings and nesting

```yaml
services:
  postgres:
    image: postgres:18
    ports:
      - "${POSTGRES_PORT:-54613}:5432"
```

`services` maps to a mapping whose key `postgres` maps to a mapping with `image` and `ports`. Two spaces per level (also set in `.editorconfig` for `*.yml`). `ports` is a **list**: each line starts with `- `.

### Quoting

- `"${POSTGRES_PORT:-54613}:5432"` is quoted. Port mappings are always written as strings: YAML 1.1 reads some `a:b` pairs of small numbers (such as `22:22`) as a base-60 number, so Compose's documentation recommends quoting every mapping. This one contains `${...}`, which would not parse as a number anyway, but the habit is safe.
- `BANK_PORT: "8463"` is quoted on purpose: environment values must be strings, and an unquoted `8463` would be a number.
- `python-version: "3.12"` in the workflow is quoted so a version such as `3.10` would not become the number `3.1`.
- `["CMD-SHELL", "pg_isready ..."]` is a **flow list**: a list on one line in square brackets.

### Folded text

```yaml
options: >-
  --health-cmd "pg_isready -h 127.0.0.1 -U openbanking -d openbanking"
  --health-interval 3s
```

`>` starts a **folded block**: the indented lines are joined with spaces into a single string. `-` after it removes the final newline. The result is one line of `docker run` options.

### Booleans and `on`

A YAML 1.1 parser reads the bare word `on` as `true`. GitHub accepts that for the `on:` key. In `docker-compose.yml` there are no such traps because values like `"app"` are quoted where needed.

### Comments and dollar signs

`#` starts a comment. `${VAR}` is not YAML; Docker Compose substitutes it **after** reading the file, and `$$` writes a literal `$`.

### Indentation rules

Spaces only. Items at the same level must line up. A wrong indent changes the meaning silently or breaks the file.

## Try it yourself

1. Read the Compose file the way a program does, with Ruby (included with macOS):

   ```bash
   ruby -ryaml -e 'y = YAML.load_file("docker-compose.yml"); p y["services"].keys; p y["services"]["postgres"]["ports"]'
   ```

   Expected: `["postgres", "bank"]` and `["${POSTGRES_PORT:-54613}:5432"]`.

2. See the quoting effect:

   ```bash
   ruby -ryaml -e 'p YAML.load("a: 8463\nb: \"8463\"\nc: 54613:5432\nd: on\ne: 22:22\nf: 3.10")'
   ```

   Expected: `{"a"=>8463, "b"=>"8463", "c"=>"54613:5432", "d"=>true, "e"=>80520, "f"=>3.1}`. Read it as: unquoted `8463` is a number, quoted is a string; `on` is `true`; `22:22` became the number 80520 (this Ruby parser reads it as a time, 22 hours 22 minutes, counted in seconds); `3.10` lost its zero and became `3.1`. These are the traps that quoting avoids. (`54613:5432` stays a string only because its second part is larger than 59.)

3. Let Compose validate and expand: `docker compose config` (prints the resolved file).

## Mistakes people make

- **Tabs for indentation.** Not allowed.
- **Unquoted values that look like something else:** `no`, `on`, `3.10`, `1:20`, `0123`.
- **Wrong indent for a list item.** It silently attaches to the wrong key.
- **Expecting YAML to expand `${VAR}`.** The tool (Compose) does.
- **Trailing spaces after `>-`** or misaligned folded text.

## Check yourself

1. How does YAML show nesting? *By indentation with spaces.*
2. Why is `"8463"` quoted in `environment`? *So it is a string, as environment values must be.*
3. What does `>-` do? *Folds the following lines into one string without a trailing newline.*
4. Why quote `"3.12"`? *Otherwise a version like `3.10` would become the number 3.1.*
5. Who replaces `${POSTGRES_PORT:-54613}`? *Docker Compose, not YAML.*

## Further reading

- YAML 1.2 specification: https://yaml.org/spec/1.2.2/
- YAML for Docker Compose (the Compose Specification): https://docs.docker.com/reference/compose-file/
- GitHub Actions workflow syntax: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
