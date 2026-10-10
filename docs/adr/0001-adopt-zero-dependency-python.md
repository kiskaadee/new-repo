# ADR 001: Adopt Zero-Dependency Python Over Compiled Rust

## Status
Accepted (2026-10-09)

## Context
The initial design for `new-repo` proposed a compiled native Rust binary utilizing `clap`, `reqwest`, `serde`, and `secrecy`. The motivation was strong compile-time type safety and static single-binary distribution.

However, during practical evaluation, several forces made Rust ill-suited for this specific tool:
1. **Compilation Latency & Iteration Friction**: As an interactive CLI used occasionally to provision repositories, waiting for `cargo build` and maintaining dependency trees creates high developer friction when tweaking CLI options or REST payloads.
2. **Boilerplate vs. Scope**: The tool fundamentally orchestrates three HTTP REST calls (Gitea API, GitHub API, push mirror registration) and executes a local `git clone`. Rust's asynchronous/synchronous network ceremony and dependency footprint are disproportionate to this scope.
3. **Workstation Integration**: Modifying a shell or provisioning script should allow immediate execution without an intermediate compile step.

We evaluated three runtime alternatives:
- **Bash with curl and jq**: Lowest barrier, but fragile when constructing nested JSON payloads, parsing multi-step API errors, and managing tokens securely.
- **Python with external libraries (httpx/requests)**: Clean syntax, but introduces virtual environment or `uv` packaging requirements.
- **Python Standard Library only**: Native JSON parsing, structured error handling via `urllib.error`, built-in argument parsing via `argparse`, and process memory safety via `subprocess`.

## Decision
We will implement `new-repo` in **Python 3 using only the standard library** (`urllib.request`, `argparse`, `json`, `subprocess`, `dataclasses`, `typing`).
To maintain the rigor initially sought in Rust:
- The codebase will enforce strict static type hints validated via **Pyright**.
- Code quality, modern Python idiom adherence, and formatting will be continuously enforced via **Ruff**.

## Consequences
### Good
- **Zero Build Step**: Edits are immediately executable without compilation or binary relinking.
- **Zero External Dependencies**: Works out of the box on any system with Python 3.10+ without virtual environments, `pip install`, or wheel caching.
- **Clean JSON Handling**: Native Python dictionaries serialize cleanly to JSON, avoiding escaping and quoting bugs inherent in shell scripts.
- **High Type Safety**: Strict Pyright type checking provides confidence similar to compiled languages without the build overhead.

### Bad
- **No Standalone Static Binary**: Requires an installed Python 3 interpreter on the host machine.
- **Slightly More Verbose HTTP Calls**: `urllib.request` requires more explicit request/header construction than third-party libraries like `httpx` or `requests`.

### Neutral
- Formatting and linting must be maintained using `ruff format` and `ruff check`.
