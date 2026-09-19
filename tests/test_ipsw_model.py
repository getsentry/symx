from symx.ipsw.model import IpswArtifactHashes


def test_hashes_accept_field_name() -> None:
    hashes = IpswArtifactHashes(sha1="abc", sha2="def")

    assert hashes.sha1 == "abc"
    assert hashes.sha2 == "def"


def test_hashes_accept_validation_alias() -> None:
    hashes = IpswArtifactHashes.model_validate({"sha1": "abc", "sha2-256": "def"})

    assert hashes.sha1 == "abc"
    assert hashes.sha2 == "def"


def test_hashes_round_trip_with_field_names() -> None:
    hashes = IpswArtifactHashes.model_validate({"sha1": "abc", "sha2-256": "def"})

    assert hashes.model_dump() == {"sha1": "abc", "sha2": "def"}
    assert IpswArtifactHashes.model_validate(hashes.model_dump()) == hashes
    assert IpswArtifactHashes.model_validate_json(hashes.model_dump_json()) == hashes
