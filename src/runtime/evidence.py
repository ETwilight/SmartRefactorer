"""Ask the environment provider to archive its own evidence."""


def archive_environment(folder, environment, reports):
    archive = getattr(environment, 'archive_evidence', None)
    if archive is None:
        return []
    return archive(folder, reports)
