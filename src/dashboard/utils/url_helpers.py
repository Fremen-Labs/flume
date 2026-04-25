def is_remote_url(url: str) -> bool:
    """Return True when `url` looks like an HTTPS or SSH git URL."""
    if not url:
        return False
    lower = url.strip().lower()
    return (
        lower.startswith('https://')
        or lower.startswith('http://')
        or lower.startswith('git@')
        or lower.startswith('ssh://')
        or lower.startswith('git://')
    )
