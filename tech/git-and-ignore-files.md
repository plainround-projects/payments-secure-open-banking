# Git and the ignore files

## What it is

Git is a **version control system**: a tool that records snapshots (commits) of a project's files, who made them and why, so you can see history, compare versions and go back. A **repository** is the project folder plus the hidden `.git/` folder where the history lives. An **ignore file** lists names that Git (or Docker) must not track or copy.

## Why this repository uses it

The build is delivered as a git repository with meaningful commits, a clean working tree and nothing untracked. History shows the order of the work (SQL, domain rules, the bank's use cases, adapters, interfaces, then tests, CI and docs). The ignore files keep local and generated things (the virtual environment, caches, `.env`) out of the repository and out of the Docker image.

## Where it appears

| Path | Role |
| --- | --- |
| `.git/` | The history (created by `git init`). |
| `.gitignore` | Names Git ignores. |
| `.dockerignore` | Names `docker build` does not copy into the build context. |
| `LICENSE`, `CHANGELOG.md` | Tracked files that travel with the history. |

## How it works underneath

Each commit stores a snapshot of tracked files, a message, an author, a time and the id of its parent commit, forming a chain. The id is a hash of the content, so history cannot change without the ids changing. A file is **tracked** once added with `git add` and committed. `git status` compares three things: the last commit, the staging area and your folder.

## Walkthrough of this repository's use

### `.gitignore`

```
.venv/
__pycache__/
*.pyc
*.egg-info/
build/
dist/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.import_linter_cache/
.coverage
coverage.xml
.env
.DS_Store
logs/
var/
.omc/
.hypothesis/
```

One pattern per line. A trailing `/` matches a folder. `*` matches any characters. `__pycache__/` and `*.pyc` are Python's compiled caches. `.venv/` is the virtual environment (machine-specific). The `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.import_linter_cache`, `.hypothesis` and `.coverage` entries are the caches and data files of the tools in this project. `.env` is the private configuration with the database password and the generated pepper. `.DS_Store` is macOS folder metadata. `logs/` is for run output. `var/` holds everything generated at run time: private keys, PIN files, the merchant's credentials and the one-time-code outbox. **Secrets live there, which is why it is ignored.** `.omc/` is local tooling state.

### `.dockerignore`

```
.git
.venv
logs
tests
docs
**/__pycache__
.mypy_cache
.ruff_cache
.pytest_cache
.hypothesis
.coverage
.env
```

Same idea for `docker build`: these never enter the build context, which makes the build faster and keeps `.env` and the history out of an image. `**/__pycache__` matches at any depth.

### The commit history

Commits are small and named for what they add; the first scaffolds the tooling, then come the SQL, the domain, the use cases, the adapters, the interfaces, the tests, CI and the documentation, each as its own commit. Every commit message ends with a `Co-Authored-By:` trailer naming the assistant that helped write it.

## Try it yourself

1. See the history:

   ```bash
   git log --oneline | head -15
   ```

   Expected: about a dozen lines, newest first, each an id and a message.

2. Check nothing is left over:

   ```bash
   git status --short
   ```

   Expected: empty output when everything is committed.

3. Prove the ignore file works:

   ```bash
   git check-ignore -v .venv .env .hypothesis
   ```

   Expected: one line per path that exists, in the form `.gitignore:1:.venv/	.venv` (rule file, line number, the rule, the path). Paths that do not exist yet may print nothing.

4. See what a commit changed: `git show --stat HEAD`.

## Mistakes people make

- **Committing secrets**, then deleting them in a later commit. They stay in history. Keep `.env` ignored from the start.
- **Committing generated folders** (`.venv`, caches).
- **Huge "fix" commits** that mix topics. Small named commits explain themselves.
- **Forgetting `.dockerignore`**, so the build sends `.git` and `.env` along.
- **Ignoring a file that is already tracked.** The ignore only affects untracked files; use `git rm --cached` first.

## Check yourself

1. What does `.gitignore` do? *Tells Git not to track matching untracked files.*
2. Why is `.env` ignored? *It holds the local password; secrets must not enter history.*
3. What does `.dockerignore` protect? *The build context: no `.env`, `.git` or virtual environment gets copied into an image.*
4. What is a commit? *A snapshot with a message, author, time and parent.*
5. How do you check the tree is clean? *`git status --short` prints nothing.*

## Further reading

- Pro Git book: https://git-scm.com/book/en/v2
- gitignore patterns: https://git-scm.com/docs/gitignore
- `.dockerignore`: https://docs.docker.com/build/concepts/context/#dockerignore-files
