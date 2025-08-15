import tempfile
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from symx._common import check_sha1
from symx._ipsw.common import IpswArtifact, IpswSource, IpswPlatform, IpswReleaseStatus, IpswArtifactHashes
from symx._ipsw.extract import IpswExtractor


def require_sha1(hashes: IpswArtifactHashes | None) -> str:
    assert hashes is not None, "Test data: hashes missing"
    assert hashes.sha1 is not None, "Test data: sha1 missing"
    return hashes.sha1


def test_ipsw_extract_run_watchos():
    source = IpswSource(
        devices=["Watch6,11"],
        link=HttpUrl(
            "https://updates.cdn-apple.com/2023FallFCS/fullrestores/042-23163/8A3CBCE7-1FC4-4B7E-9A00-C68187E4F514/Watch6,11_9.6.3_20U502_Restore.ipsw"
        ),
    )
    ipsw_path = Path(f"./{source.file_name}").resolve()
    if not ipsw_path.exists():
        # this is used in local testing
        # TODO: download artifact when it doesn't exit for CI
        pytest.skip("IPSW artifact does not exist", allow_module_level=True)

    artifact = IpswArtifact(
        platform=IpswPlatform.WATCHOS,
        version="9.6.3",
        build="20U502",
        release_status=IpswReleaseStatus.RELEASE,
        sources=[source],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        processing_dir = Path(tmpdir)
        extractor = IpswExtractor(artifact, source, processing_dir, ipsw_path)
        extractor.run()


def test_ipsw_extract_run_ios():
    source = IpswSource(
        devices=["iPhone14,7"],
        link=HttpUrl(
            "https://updates.cdn-apple.com/2024FallFCS/fullrestores/072-42532/9439A909-D980-44CD-ADFC-DBE49BC3A84D/iPhone14,7_18.2_22C152_Restore.ipsw"
        ),
    )
    ipsw_path = Path(f"./{source.file_name}").resolve()
    if not ipsw_path.exists():
        # this is used in local testing
        # TODO: download artifact when it doesn't exit for CI
        pytest.skip("IPSW artifact does not exist", allow_module_level=True)

    artifact = IpswArtifact(
        platform=IpswPlatform.WATCHOS,
        version="18.2",
        build="22C152",
        release_status=IpswReleaseStatus.RELEASE,
        sources=[source],
    )

    processing_dir = Path("/Users/mischan/devel/tmp/test_out")
    ipsw_path = Path("/Users/mischan/devel/tmp/iPhone14,7_18.2_22C152_Restore.ipsw")
    extractor = IpswExtractor(artifact, source, processing_dir, ipsw_path)
    extractor.run()


def test_ipsw_extract_run_macos():
    source = IpswSource(
        devices=[
            "iMac21,1",
            "iMac21,2",
            "Mac13,1",
            "Mac13,2",
            "Mac14,2",
            "Mac14,3",
            "Mac14,5",
            "Mac14,6",
            "Mac14,7",
            "Mac14,8",
            "Mac14,8-Rack",
            "Mac14,9",
            "Mac14,10",
            "Mac14,12",
            "Mac14,13",
            "Mac14,14",
            "Mac14,15",
            "Mac15,3",
            "Mac15,4",
            "Mac15,5",
            "Mac15,6",
            "Mac15,7",
            "Mac15,8",
            "Mac15,9",
            "Mac15,10",
            "Mac15,11",
            "Mac15,12",
            "Mac15,13",
            "MacBookAir10,1",
            "MacBookPro17,1",
            "MacBookPro18,1",
            "MacBookPro18,2",
            "MacBookPro18,3",
            "MacBookPro18,4",
            "Macmini9,1",
            "VirtualMac2,1",
        ],
        link=HttpUrl(
            "https://updates.cdn-apple.com/2024SummerSeed/fullrestores/062-22022/AB066FFB-B7FE-4132-83AC-E58A323805C1/UniversalMac_15.0_24A5279h_Restore.ipsw"
        ),
        hashes=IpswArtifactHashes(sha1="737fe83903996f53be20231788d8cfc7f9d3ac8a", sha2=None),
        size=16230968934,
    )
    ipsw_path = Path(f"./{source.file_name}").resolve()
    if not ipsw_path.exists():
        # this is used in local testing
        # TODO: download artifact when it doesn't exit for CI
        pytest.skip("IPSW artifact does not exist", allow_module_level=True)

    artifact = IpswArtifact(
        platform=IpswPlatform.MACOS,
        version="15.0_beta_2",
        build="24A5279h",
        release_status=IpswReleaseStatus.BETA,
        released=datetime.fromisoformat("2024-06-24"),
        sources=[source],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        processing_dir = Path(tmpdir)
        assert ipsw_path.stat().st_size == source.size
        assert check_sha1(require_sha1(source.hashes), ipsw_path)
        extractor = IpswExtractor(artifact, source, processing_dir, ipsw_path)
        extractor.run()
