import symx

if __name__ == "__main__":
    # symx.app(["-v", "ota", "extract", "-s", "gs://sentry-devel@apple_symbols"])
    # symx.app(["-v", "ota", "migrate_storage", "-s", "gs://sentry-devel@apple_symbols"])
    # symx.app(["-v", "ota", "mirror", "-s", "gs://sentry-devel@apple_symbols"])
    # symx.app(["-v", "ipsw", "meta-sync", "-s", "gs://sentry-devel@apple_symbols"])
    # symx.app(["-v", "ipsw", "mirror", "-s", "gs://sentry-devel@apple_symbols"])
    symx.app(["-v", "ipsw", "extract", "-s", "gs://sentry-devel@apple_symbols"])
    # symx.app(["-v", "ipsw", "migrate", "-s", "gs://sentry-devel@apple_symbols"])
