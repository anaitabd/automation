# Contributing to Nexus Cloud

## Local Development Setup

### Prerequisites

| Tool | Min Version |
|------|-------------|
| Python | 3.12+ |
| Docker | 24+ |
| Node.js | 20+ |
| AWS CLI | 2.x |
| AWS CDK | 2.100+ |

### 1. Clone and configure

```bash
git clone <repo-url> && cd automation
cp env.exemple .env
# Edit .env with your API keys
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. (Optional) Install pre-commit hooks

```bash
pip install pre-commit
pre-commit install
```

### 4. Start the local Docker stack

```bash
docker compose up --build
```

This starts LocalStack (S3, Secrets Manager, Step Functions), PostgreSQL, and all 9 Lambda containers. See [dockeruse.md](dockeruse.md) for full details.

---

## Code Style Guidelines

- Python 3.12, line length 120 characters.
- Linting is enforced with [ruff](https://docs.astral.sh/ruff/). Selected rule sets: `E`, `F`, `W`, `I`, `N`, `UP`, `B`, `C4`.
- No comments, no docstrings, no separator lines in Lambda handler files — only functional code.
- Run the linter before committing:

```bash
ruff check . --fix
```

---

## Pull Request Process

1. Fork the repository and create a feature branch from `main`.
2. Make your changes with minimal scope — one logical change per PR.
3. Ensure all tests pass locally:
   ```bash
   python -m pytest -v
   ```
4. Ensure linting passes:
   ```bash
   ruff check . --output-format=github
   ```
5. Open a PR against `main`. Fill in the PR template completely.
6. The CI pipeline will automatically run linting, tests, and `cdk diff` on your PR.
7. A maintainer will review and merge once CI is green.

---

## Testing Requirements

- All new Lambda logic must be covered by unit tests in a `test_*.py` file at the repository root.
- Tests must not require real AWS credentials — stub `boto3` and other external clients as shown in `test_repair.py` and `test_drawtext.py`.
- Run the full test suite with:

```bash
python -m pytest -v
```

- The CI pipeline runs `python -m pytest -v` on every push and PR.
