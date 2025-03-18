# Contributing to DAIV

Thank you for your interest in contributing to DAIV! This document provides guidelines and instructions for contributing to the project. By participating in this project, you agree to abide by its terms.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Guidelines](#development-guidelines)
  - [Code Style](#code-style)
  - [Testing](#testing)
  - [Type Checking](#type-checking)
- [Making Contributions](#making-contributions)
  - [Branch Naming Convention](#branch-naming-convention)
  - [Commit Messages](#commit-messages)
  - [Pull Request Process](#pull-request-process)
- [Reporting Issues](#reporting-issues)
- [License](#license)

## Code of Conduct

We expect all contributors to be respectful and constructive. Please ensure that your interactions with the community are positive and inclusive.

## Development Guidelines

### Code Style

DAIV uses [ruff](https://github.com/astral-sh/ruff) for linting and formatting:

- **Linting**: `make lint-check`
- **Formatting**: `make lint-format`
- **Linting and formatting**: `make lint`
- **Fix linting and formatting issues**: `make lint-fix`

Our code formatting configuration includes:

- Line length: 120 characters
- Target Python version: 3.12
- isort configuration for import sorting

Before submitting a pull request, ensure your code passes all linting checks:

```bash
make lint
```

### Testing

DAIV uses pytest for testing:

1. **Run all tests**:

   ```bash
   make test
   ```

2. **Writing tests**:

   - Tests should be placed in the `tests/` directory.
   - Test file names should start with `test_` and follow the same directory structure as the source code.
   - Test classes should follow the pattern `Test*` or `*Test`.
   - Use pytest fixtures for test setup/teardown.

3. **Coverage**:
   - The test suite reports coverage using the pytest-cov plugin.
   - Aim for high test coverage with meaningful tests.

### Type Checking

We use mypy for static type checking but we don't enforce it, we encourage you to use it to improve your code quality:

```bash
make lint-typing
```

## Making Contributions

### Branch Naming Convention

Use descriptive branch names that reflect the purpose of your changes:

- `feat/description` for new features
- `fix/description` for bug fixes
- `chore/description` for chores
- `security/description` for security fixes

### Commit Messages

Write clear and concise commit messages that explain what changes were made and why. Follow these guidelines:

- Use the present tense ("Add feature" not "Added feature")
- Use the imperative mood ("Move cursor to..." not "Moves cursor to...")
- Limit the first line to 72 characters or less
- Reference issues and pull requests where appropriate

### Pull Request Process

1. **Fork the repository** and create your branch from `main`
2. **Ensure code quality** by running `make lint`
3. **Ensure all tests pass** by running `make test`
4. **Update documentation** if necessary
5. **Submit a pull request** to the `main` branch
6. **Respond to feedback** from maintainers during the review process
7. **Update your PR** if requested with additional changes

## Reporting Issues

When reporting issues, please include as much information as possible:

1. **Steps to reproduce** the issue
2. **Expected behavior** and what actually happened
3. **Environment details**: Python version, OS, etc.
4. **Screenshots** if applicable
5. **Possible solutions** if you have suggestions

## License

By contributing to DAIV, you agree that your contributions will be licensed under the project's [Apache-2.0 license](LICENSE).

---

Thank you for contributing to DAIV! Your efforts help make this project better for everyone.
