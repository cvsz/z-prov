# Contributing to ZeaZ Provider

Thank you for considering contributing to ZeaZ Provider! This document provides guidelines and instructions for contributing.

## Code of Conduct

Please be respectful and constructive in all interactions. We welcome contributors of all backgrounds and experience levels.

## How to Contribute

### Reporting Bugs

Before creating bug reports, please check existing issues as you might find out that you don't need to create one. When you are creating a bug report, please include as many details as possible:

- Use a clear and descriptive title
- Describe the exact steps to reproduce the problem
- Provide specific examples to demonstrate the steps
- Describe the behavior you observed after following the steps
- Explain which behavior you expected to see instead and why
- Include any relevant logs, screenshots, or code samples

### Suggesting Enhancements

Enhancement suggestions are tracked as GitHub issues. When creating an enhancement suggestion, please include:

- Use a clear and descriptive title
- Provide a detailed description of the suggested enhancement
- Explain why this enhancement would be useful
- List some examples of how this enhancement would be used

### Pull Requests

- Fill in the required template
- Follow the coding style used in the project
- Include appropriate tests
- Update documentation as needed
- Ensure all tests pass before submitting

## Development Setup

```bash
# Clone the repository
git clone https://github.com/zeaz/provider.git
cd provider

# Initialize environment
make env-init

# Install dependencies
make install

# Run tests
make test

# Run linting
make lint

# Validate everything
make validate
```

## Coding Standards

- Follow PEP 8 style guidelines
- Use type hints where appropriate
- Write meaningful commit messages
- Keep pull requests focused on a single change

## Testing

- Write unit tests for new features
- Ensure existing tests pass
- Test with different configurations when applicable

## Documentation

- Update README.md if changing functionality
- Add docstrings to new functions and classes
- Update configuration examples if adding new options

## Release Process

Releases follow semantic versioning. Release candidates are marked with `rc` suffix.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
