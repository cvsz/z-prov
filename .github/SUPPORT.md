# Support

## Getting Help

There are several ways to get help with ZeaZ Provider:

- **Documentation**: Check the [README.md](../README.md) and [docs/](../docs/) directory for detailed documentation.
- **Issues**: Search existing [GitHub issues](https://github.com/zeaz/provider/issues) or create a new one.
- **Discussions**: Participate in [GitHub Discussions](https://github.com/zeaz/provider/discussions) for questions and community support.

## Documentation

### Quick Start

```bash
# Initialize environment
make env-init

# Install dependencies
make install

# Validate configuration
make validate

# Run the gateway
make run
```

### Docker Deployment

```bash
# Build container
make build

# Start service
make up

# Check health
make health
```

### Configuration

- **Environment**: `.env` file for secrets and runtime configuration
- **Providers**: `config/providers.yaml` for provider and model routing
- **Examples**: See `config/providers.example.yaml` and `.env.example`

## Reporting Issues

### Bug Reports

Before filing a bug report:
1. Search existing issues to avoid duplicates
2. Check if the issue persists on the latest version
3. Gather relevant logs and configuration (remove secrets first)

When filing a bug report, include:
- Version of ZeaZ Provider
- Operating system and environment (native, Docker, etc.)
- Steps to reproduce the issue
- Expected vs actual behavior
- Relevant logs and configuration snippets

Use the [Bug Report template](./ISSUE_TEMPLATE/bug_report.yml) when creating an issue.

### Feature Requests

We welcome feature requests! When submitting:
1. Describe the problem you're trying to solve
2. Explain your proposed solution
3. Provide use cases and examples

Use the [Feature Request template](./ISSUE_TEMPLATE/feature_request.yml) when creating an issue.

## Support Levels

### Community Support (Free)

- GitHub Issues for bugs
- GitHub Discussions for questions
- Best effort response time

### Priority Support

For organizations requiring priority support, SLA-backed assistance, or custom development, please contact us at [support@zeaz.io](mailto:support@zeaz.io).

## Troubleshooting

### Common Issues

#### Port Already in Use

```bash
# Check what's using port 8080
lsof -i :8080

# Or configure a different port
export ZEAZ_PORT=8081
```

#### Configuration Validation

```bash
# Validate YAML syntax
python -c "import yaml; yaml.safe_load(open('config/providers.yaml'))"

# Run full validation
make validate
```

#### Container Issues

```bash
# Check container status
docker compose ps

# View logs
make logs

# Validate security settings
make validate-container
```

#### Dependency Issues

```bash
# Reinstall dependencies
rm -rf .venv
make install

# Validate locks
make validate-locks
```

## Version Support

| Version | Supported | End of Life |
| ------- | --------- | ----------- |
| 0.4.x   | Yes       | TBD         |
| 0.3.x   | No        | 2025-01-01  |
| < 0.3   | No        | Ended       |

Always upgrade to the latest stable version for security updates and new features.

## Additional Resources

- [ROADMAP.md](../ROADMAP.md) - Project roadmap and planned features
- [CHANGELOG.md](../CHANGELOG.md) - Version history and changes
- [CONTRIBUTING.md](./CONTRIBUTING.md) - How to contribute to the project
- [SECURITY.md](./SECURITY.md) - Security policy and reporting
- [AGENTS.md](../AGENTS.md) - Agent-specific documentation

## Contact

- **General Inquiries**: [info@zeaz.io](mailto:info@zeaz.io)
- **Security Issues**: [security@zeaz.io](mailto:security@zeaz.io)
- **Support Requests**: [support@zeaz.io](mailto:support@zeaz.io)
