# Security Policy

## Secrets

Never commit OKX API keys, OKX secret keys, passphrases, PushPlus tokens,
database passwords, or proxy credentials. Use environment variables or a
secret manager on the server.

OKX keys should be limited to read and trade permissions, should have
withdrawal disabled, and should use an IP allowlist where possible.

## Trading safety

The default mode is OKX demo trading. Live trading must remain an explicit,
separately guarded configuration. The risk engine must fail closed when market
data, account state, order acknowledgements, or the outbound proxy are stale
or unavailable.

## Reporting

Please do not publish secret values, private account data, or unredacted
production logs in issues. Report security issues privately to the repository
maintainers.

