from symx._common import ArtifactProcessingState
from symx._ota import generate_duplicate_key_from, OtaArtifact

duplicate_value = OtaArtifact(
    build="21C66",
    description=['iOS1721Long'],
    version='17.2.1',
    platform='ios',
    id='387534500408f0c0867b48bef124a1e581b12ed0',
    url='https://updates.cdn-apple.com/2023FallFCS/patches/052-17498/3AD9B31B-52C3-4422-871D-F4E17B42C6E5'
        '/com_apple_MobileAsset_SoftwareUpdate/387534500408f0c0867b48bef124a1e581b12ed0.zip',
    download_path='mirror/ota/ios/17.2.1/21C66/387534500408f0c0867b48bef124a1e581b12ed0.zip',
    devices=['iPhone11, 2_D321AP', 'iPhone11, 6_D331pAP'],
    hash='67af066e7cb5e9548ec57d6eff295c20df1758b6', hash_algorithm='SHA-1',
    last_run=7269434682,
    processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED
)


def test_generate_duplicate_key_from_without_existing_duplicate() -> None:
    their_key = "387534500408f0c0867b48bef124a1e581b12ed0"
    meta_store: dict[str, OtaArtifact] = {
        their_key: duplicate_value
    }

    duplicate_key = generate_duplicate_key_from(meta_store, their_key)
    assert duplicate_key == f"{their_key}_duplicate_1"


def test_generate_duplicate_key_from_with_existing_duplicate() -> None:
    their_key = "387534500408f0c0867b48bef124a1e581b12ed0"
    meta_store: dict[str, OtaArtifact] = {
        their_key: duplicate_value,
        f"{their_key}_duplicate_1": duplicate_value
    }

    duplicate_key = generate_duplicate_key_from(meta_store, their_key)
    assert duplicate_key == f"{their_key}_duplicate_2"

    meta_store[duplicate_key] = duplicate_value

    duplicate_key = generate_duplicate_key_from(meta_store, their_key)
    assert duplicate_key == f"{their_key}_duplicate_3"