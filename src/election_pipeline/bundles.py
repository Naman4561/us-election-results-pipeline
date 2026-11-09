"""Pin a multi-file source in one deterministic, compressed, replayable archive."""

from importlib import import_module

from election_pipeline.collectors.common import Bundle
from election_pipeline.states import STATES


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    adapter = STATES.get(source.state)
    if adapter and adapter.collection_backend:
        backend = import_module("election_pipeline.collectors." + adapter.collection_backend)
        return backend.fetch_bundle(
            source,
            settings,
            destination,
            previous=previous,
            store=store,
            force_download=force_download,
        )
    with Bundle(source, settings, destination) as bundle:
        for name, url in (*source.files, (source.primary_filename, source.url)):
            if stopped := bundle.get(name, url):
                return stopped
        return bundle.finish()
