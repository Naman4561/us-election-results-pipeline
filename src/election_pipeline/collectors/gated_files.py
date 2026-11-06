"""Configured files, fetched only when the primary file has changed.

For a state whose files are all regenerated together (California's results API,
Maryland's results pages), the primary file is fetched first. If it is byte-identical
to the previous bundle's and that bundle is under ``REFRESH_SECONDS`` old, the previous
bundle is reused and no other file is requested. Otherwise every configured file is
fetched, exactly as the standard bundle collector does. The hourly refresh still
catches a change that left the primary file untouched.
"""

from election_pipeline.collectors.common import Bundle


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url):
            return stopped
        if reused := bundle.reuse(previous, {primary: bundle.read(primary)}, force=force_download):
            return reused
        for name, url in source.files:
            if stopped := bundle.get(name, url):
                return stopped
        return bundle.finish()
