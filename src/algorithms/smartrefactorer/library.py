"""Save library versions and the evidence used to publish them.

A commit writes its intent, version document, current manifest, and receipt.
The intent identifies the parent and resulting manifest so opening the library
can complete a receipt left behind by an interrupted write.
"""

import copy
import hashlib
import json
import os
import time
from pathlib import Path


def digest(value):
    """Return the SHA-256 of a JSON value with sorted object keys."""

    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()
    ).hexdigest()


def atomic(path, value):
    """Write a JSON value to a flushed temporary file, then replace the destination."""
    # path: str | Path -> Path.

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.new')
    # Encode the complete JSON value once; large checkpoints otherwise produce many small
    # writes.

    encoded = json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')
    with temporary.open('wb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    deadline = time.monotonic() + 5
    while True:
        try:
            # The flushed temporary file becomes the current document at this replacement.
            os.replace(temporary, path)
            break
        except PermissionError as error:
            # A Windows reader can hold the destination open while this replacement is
            # attempted.
            # The retry uses the same flushed temporary file.

            if (
                os.name != 'nt'
                or getattr(error, 'winerror', None) not in (5, 32, 33)
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)


class Library:
    """Committed skill components and their publication history on disk.

    folder is the library directory; path names its manifest.json. manifest is a
    dictionary with version, components, and commits. components maps skill IDs to
    their source, hash, interface, contract, and dependencies. Each commit records
    the proposal and passing checks that produced the new library version.

    Opening the library loads the manifest and reconciles its last intent. commit()
    checks the candidate and parent version, writes the new version, and updates
    manifest in memory. Repeating the same publication inputs returns its commit ID.

    Example, using the components and checks produced by development::

        library = Library(directory)
        parent = digest(library.manifest)
        commit_id = library.commit(parent, components, evidence, proposal)

    The library now exposes the committed components and a receipt for commit_id.
    """

    def __init__(self, folder):
        """Load the saved manifest, or start an empty library, then reconcile its intent."""

        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'manifest.json'
        self.manifest = (
            json.loads(self.path.read_text(encoding='utf-8'))
            if self.path.exists()
            else {'version': 0, 'components': {}, 'commits': []}
        )
        self.reconcile()

    def reconcile(self):
        """Write a missing receipt when the current manifest matches the recorded intent."""

        intent = self.folder / 'intent.json'
        if not intent.exists():
            return
        value = json.loads(intent.read_text(encoding='utf-8'))
        if digest(self.manifest) == value['candidate_hash']:
            atomic(self.folder / 'receipts' / (value['id'] + '.json'), value)
        elif digest(self.manifest) != value['parent_hash']:
            raise ValueError('Unresolved library transaction; retain intent and artifacts')

    def commit(self, parent, components, evidence, proposal, *, validated_candidate=None):
        """Publish components with passing checks and return the transaction ID.

        parent is the current library-manifest digest. validated_candidate supplies
        the checked component map when components contains selected exports. The
        intent records the new manifest digest as candidate_hash and the checked
        component-map digest as validated_candidate_hash.
        """
        if not evidence or any(e.get('status') != 'PASS' for e in evidence):
            raise ValueError('Publication requires all required checks to pass')
        validated = components if validated_candidate is None else validated_candidate
        # This digest identifies the component map examined by the development checks.
        validated_hash = digest(validated)
        if any(e.get('candidate_hash') != validated_hash for e in evidence):
            raise ValueError(
                'Publication evidence belongs to a different or unspecified candidate'
            )
        for name, component in components.items():
            if component != self.manifest['components'].get(
                name
            ) and component != validated.get(name):
                raise ValueError('Published component differs from the validated candidate')
        # The same parent, components, checks, and proposal produce the same transaction ID.
        transaction = digest([parent, components, evidence, proposal])
        if transaction in self.manifest['commits']:
            return transaction
        if parent != digest(self.manifest):
            raise ValueError('Stale library parent')
        # candidate: dict, the next manifest with its version, component mapping, and commit
        # IDs.
        candidate = {
            'version': self.manifest['version'] + 1,
            'components': copy.deepcopy(components),
            'commits': self.manifest['commits'] + [transaction],
        }
        for name, component in components.items():
            if component['id'] != name or component['hash'] != digest(component['source']):
                raise ValueError('Component identity/hash mismatch')
        # Record both the next manifest and the component map checked during development.
        intent = {
            'id': transaction,
            'parent_hash': parent,
            'candidate_hash': digest(candidate),
            'validated_candidate_hash': validated_hash,
            'evidence': evidence,
            'proposal': proposal,
        }
        # Record the intent first so reconcile() can match the manifest to its receipt.
        atomic(self.folder / 'intent.json', intent)
        atomic(self.folder / 'versions' / (digest(candidate) + '.json'), candidate)
        atomic(self.path, candidate)
        # The in-memory manifest now refers to the newly written version.
        self.manifest = candidate
        atomic(self.folder / 'receipts' / (transaction + '.json'), intent)
        return transaction
