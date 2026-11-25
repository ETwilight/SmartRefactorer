"""Archive environment records through the environment provider."""


def archive_environment(folder, environment, reports):
    """Delegate environment evidence collection to the configured provider."""

    archive = getattr(environment, 'archive_evidence', None)
    if archive is None:
        return []
    return archive(folder, reports)
