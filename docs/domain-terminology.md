# Apple artifact and extraction terminology

Apple's firmware formats expose several similarly named layers, and the tools often use shorthand that is not self-explanatory. This document is Symx's central glossary for that domain. It defines how terms are used in this repository; it does not claim that Apple treats these names as a public API.

## Artifact and processing units

### IPSW

An Apple restore image, normally a ZIP archive containing firmware manifests, encrypted or plain disk images, and other device components. An IPSW is usually suitable for reconstructing a full installed system without an older release.

Symx distinguishes:

- **`IpswArtifact`**: the logical release identified by platform, version, and build;
- **`IpswSource`**: one downloadable IPSW file within that artifact, with its own devices, URL, mirror path, and processing state.

IPSW processing state is per source, not per logical artifact. A Universal macOS IPSW can also contain more than one SystemOS image for different device groups.

### OTA

An over-the-air software update. Symx persists one **`OtaArtifact`** per downloadable OTA and tracks processing state directly on it.

An OTA can be:

- **full**: intended to contain the data needed to install the target without reconstructing files from a prerequisite system;
- **delta**: targets a transition from a prerequisite version/build and can carry patches or error-correction data instead of complete target files;
- **recovery**: updates or installs a recovery environment rather than the normal system.

These categories describe the update as a whole. A delta OTA can still carry a complete dyld shared cache family when the changed components do not require that cache to be reconstructed. Conversely, a post-install BOM path proves that the file exists after installation, not that the OTA contains its complete bytes directly.

### SystemOS image

A disk image containing the operating system filesystem used by IPSW extraction. This is distinct from a dyld shared cache. Symx mounts a SystemOS image to symsort ordinary binaries and separately extracts DSC content.

One IPSW can contain multiple SystemOS images selected by device. Choosing one arbitrary image can omit binaries belonging to another device group.

## Dyld shared caches

### Dyld shared cache (DSC)

A collection of system libraries prebuilt into a cache for efficient loading by Apple's dynamic linker, `dyld`. A modern DSC is usually a logical cache distributed across multiple physical files.

### DSC architecture

The selector encoded in a cache basename, such as:

- `arm64e`
- `arm64e_x1`
- `x86_64`
- `x86_64h`

This is the cache family's architecture/variant name. It must not be inferred solely from the device name, and variants such as `arm64e` and `arm64e_x1` are distinct extraction targets.

### Primary DSC

The unsuffixed file named `dyld_shared_cache_<architecture>`, for example:

```text
dyld_shared_cache_arm64e
```

The primary contains the cache header. That header identifies the other files required to open the logical cache and records expected UUIDs for those companions. Finding a primary proves neither that its family is complete nor that it can be split.

`aot_shared_cache.N` files are Rosetta AOT caches and do not follow this primary-DSC naming rule.

### Subcache and sidecar

Companion files belonging to a primary DSC. Names can include numeric or role-specific extensions, for example:

```text
dyld_shared_cache_arm64e.01
dyld_shared_cache_arm64e.31
dyld_shared_cache_arm64e.symbols
dyld_shared_cache_arm64e.25.dylddata
dyld_shared_cache_arm64e.50.dyldlinkedit
dyld_shared_cache_arm64e.atlas
```

The primary header determines which companions are required; filename patterns or a fixed numeric range are not authoritative. A missing file or UUID mismatch makes that family unusable even when the primary itself exists.

### Cache family

One primary DSC plus the subcaches and sidecars that its header requires. The family is the usable unit. Symx must not treat a lone primary or an arbitrary collection of subcaches as a complete DSC.

An artifact can contain more than one family for the same architecture in different filesystem domains, such as normal System, DriverKit, or `x86Support` paths.

### Supported primary

A Symx policy term, narrower than “primary DSC.” Symx currently processes primaries in the normal system locations:

```text
System/Library/dyld/
System/Library/Caches/com.apple.dyld/
```

Symx deliberately excludes DriverKit and `System/x86Support` primaries from its symbol-extraction candidates. Files from those domains can still appear in an `ipsw` materialization report.

## Extraction stages

### Materialization

Turning an archive member, cryptex, disk image, or payload stream into files on disk. For OTA DSC extraction, `ipsw ota extract --dyld --json` owns this stage and returns a schema-1 report listing materialized files and structured errors.

A successful subprocess is not by itself proof of a usable cache family. Apple Archive can successfully extract all directly available files while omitting a path that requires delta reconstruction.

### Complete materialization report

For Symx's `ipsw` contract, `complete=true` means the materialization operation has no unresolved structured errors and each reported/requested DSC architecture has at least one cache family that can be opened with all companions required by its primary header.

It does not mean that every alternate family for the same architecture is usable. For example, a valid normal System family can satisfy `arm64e` even if an unsupported DriverKit family is incomplete.

A report containing `dsc-validation` means no reported family for at least one architecture could be opened completely. Symx may classify that as an expected skip only when trusted metadata independently identifies a delta or recovery artifact; full and unknown artifacts remain failures.

### Split

Extracting the individual Mach-O images from a complete DSC family. In production, `ipsw dyld split` delegates this bulk operation to Apple's Xcode `dsc_extractor.bundle`. Split happens after materialization and is not a substitute for reconstructing missing subcaches.

### Symsort

Running Sentry's `symsorter` over mounted system files or split DSC binaries. Symsort identifies debug-bearing binaries, normalizes the symbol bundle layout, and prepares files for upload. It does not materialize firmware payloads or repair DSC families.

## State and evidence terms

### Present, absent, and unavailable

- **present/materialized**: the requested file or supported primary was emitted and validated for that attempt;
- **absent/not present**: the extractor completed the relevant search and found no matching candidate;
- **unavailable/incomplete**: the search or materialization encountered a real failure, or emitted a cache family that could not be opened completely;
- **not attempted**: no extraction attempt covered that target.

These distinctions matter because aggregate `symbols_extracted` state records the overall processing result, not durable per-architecture or per-family coverage.

### Persisted state versus observability

GCS metadata is authoritative for Symx processing state. GitHub Actions logs and Sentry events explain execution and failures but do not prove which state was persisted. Always attribute production outcomes from a fresh metadata snapshot and the exact `last_run` value.
