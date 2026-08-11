# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.4.x   | :white_check_mark: |
| < 0.4   | :x:                |

## Reporting a Vulnerability

We take the security of ZeaZ Provider seriously. If you believe you have found a security vulnerability, please report it to us as described below.

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via email to [security@zeaz.io](mailto:security@zeaz.io) with the following information:

- Description of the vulnerability
- Steps to reproduce the issue
- Potential impact of the vulnerability
- Any suggested fixes (if applicable)

You should receive a response within 48 hours acknowledging your report. After the initial reply, we will keep you informed of the progress towards a fix and announcement.

## Security Best Practices

When deploying ZeaZ Provider, please follow these security best practices:

### Configuration Security

- Never commit API keys or secrets to version control
- Use environment variables or secure secret management for sensitive configuration
- Regularly rotate API keys and credentials
- Review the `providers.yaml` configuration for exposed secrets

### Container Security

The production container is hardened with:
- Non-root user (UID/GID 10001)
- Read-only root filesystem
- Dropped Linux capabilities
- No-new-privileges flag
- Size-limited tmpfs for `/tmp`

Verify runtime security with:
```bash
make validate-container
```

### Dependency Security

- All dependencies are hash-verified with SHA-256
- Regular security audits are performed
- SBOM (Software Bill of Materials) is generated for each release
- Review dependency updates before applying them

Update and review dependencies:
```bash
make lock
make validate-locks
```

### Network Security

- Default binding is loopback-only (127.0.0.1)
- Configure firewall rules to restrict access
- Use TLS termination proxy for production deployments
- Enable authentication for remote access

### Monitoring and Logging

- Monitor logs for unusual activity
- Set up alerts for failed authentication attempts
- Regularly review access patterns
- Implement rate limiting for production use

## Known Limitations

- The gateway does not provide built-in authentication; deploy behind an authenticating proxy for remote access
- Rate limiting should be configured at the infrastructure level for production use

## Security Updates

Security patches are released as soon as possible. Critical vulnerabilities may result in out-of-band releases.

Subscribe to releases to stay informed about security updates.

## Recognition

We appreciate responsible disclosure and will acknowledge researchers who report valid security issues (with permission).

## License

This security policy is part of the ZeaZ Provider project, licensed under MIT.
