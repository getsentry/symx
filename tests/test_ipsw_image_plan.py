"""Pure image-plan contracts, using reduced real manifest topology."""

from dataclasses import replace
from pathlib import Path

import pytest

from symx.ipsw.extract import IpswExtractError
from symx.model import Arch
from tests.test_ipsw_systemos_images import COMMON, FILESYSTEM, ROSETTA, SPECIAL, SYSTEM, identity, make_request


def test_plan_deduplicates_images_not_products_or_variants(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(tmp_path)
    plan = build_extraction_plan(request)
    assert plan.request is request
    assert [image.member for image in plan.system_images] == [COMMON, SPECIAL]
    assert [image.selector for image in plan.system_images] == ["Mac13,1", "Mac18,5"]
    assert plan.system_images[0].products == ("Mac13,1", "Mac14,8")
    assert plan.system_images[0].boards == ("j180dap", "j375cap")
    assert [image.member for image in plan.rosetta_images] == [ROSETTA]
    assert [(a.image.member, a.arch) for a in plan.attempts] == [
        (COMMON, Arch.ARM64E),
        (COMMON, Arch.ARM64E_X1),
        (SPECIAL, Arch.ARM64E),
        (SPECIAL, Arch.ARM64E_X1),
        (ROSETTA, Arch.X86_64),
        (ROSETTA, Arch.X86_64H),
    ]
    assert len({a.work_dir for a in plan.attempts}) == 6
    assert plan.unmatched_devices == ("Mac14,8-Rack",)


def test_source_devices_are_preferences_not_a_coverage_filter(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = replace(make_request(tmp_path), devices=("mac14,8", "source-only-alias"))
    plan = build_extraction_plan(request)
    assert [(i.member, i.selector) for i in plan.system_images] == [(COMMON, "Mac14,8"), (SPECIAL, "Mac18,5")]
    assert plan.request.devices == request.devices


def test_board_only_selection(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(tmp_path, [identity(product=""), identity(product="", board="j873gap", system=SPECIAL)])
    plan = build_extraction_plan(request)
    assert [i.selector for i in plan.system_images] == ["j375cap", "j873gap"]


def test_cross_product_board_collision_is_not_a_unique_selector(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(tmp_path, [identity(), identity(product="Mac18,5", board="mAC13,1", system=SPECIAL)])
    plan = build_extraction_plan(request)
    assert plan.system_images[0].selector == "j375cap"
    assert plan.system_images[1].selector == "Mac18,5"


def test_ambiguous_product_uses_unique_board(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(tmp_path, [identity(), identity(product="mAC13,1", board="j873gap", system=SPECIAL)])
    assert [i.selector for i in build_extraction_plan(request).system_images] == ["j375cap", "j873gap"]


@pytest.mark.parametrize("product,board", [("", ""), ("Mac13,1", "j375cap")])
def test_multiple_unselectable_images_fail_early(tmp_path: Path, product: str, board: str) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(
        tmp_path,
        [
            identity(product, board, COMMON),
            identity(product, board, SPECIAL),
        ],
    )
    with pytest.raises(IpswExtractError, match="selector"):
        build_extraction_plan(request)


def test_single_image_without_associations_remains_unfiltered(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    request = make_request(tmp_path, [identity(product="", board="")])
    assert build_extraction_plan(request).system_images[0].selector is None


def test_legacy_fallback_ignores_recovery_and_attempts_all_macos_architectures(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    ordinary = identity()
    ordinary["Manifest"] = {"OS": {"Info": {"Path": FILESYSTEM}}}
    recovery = identity(variant="Recovery Customer Erase Install (IPSW)")
    recovery["Manifest"] = {"OS": {"Info": {"Path": "recovery.dmg"}}}
    request = make_request(tmp_path, [recovery, ordinary], version="15.0")
    plan = build_extraction_plan(request)
    assert [i.member for i in plan.system_images] == [FILESYSTEM]
    assert [a.arch for a in plan.attempts] == [Arch.ARM64E, Arch.ARM64E_X1, Arch.X86_64, Arch.X86_64H]
    assert not plan.rosetta_images


def test_multiple_rosetta_images_are_planned_independently(tmp_path: Path) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    second = identity(product="Mac18,5", board="j873gap")
    second["Manifest"] = {
        SYSTEM: {"Info": {"Path": COMMON}},
        "Cryptex1,RosettaOS": {"Info": {"Path": "other-rosetta.dmg"}},
    }
    request = make_request(tmp_path, [identity(), second], members=(COMMON, ROSETTA, "other-rosetta.dmg"))
    plan = build_extraction_plan(request)
    assert len(plan.system_images) == 1
    assert [(i.member, i.selector) for i in plan.rosetta_images] == [
        (ROSETTA, "Mac13,1"),
        ("other-rosetta.dmg", "Mac18,5"),
    ]


@pytest.mark.parametrize("encrypted", [False, True])
def test_macos27_requires_directly_mountable_rosetta(tmp_path: Path, encrypted: bool) -> None:
    from symx.ipsw.image_plan import build_extraction_plan

    row = identity()
    row["Manifest"] = {SYSTEM: {"Info": {"Path": COMMON}}}
    if encrypted:
        row["Manifest"] = {
            SYSTEM: {"Info": {"Path": COMMON}},
            "Cryptex1,RosettaOS": {"Info": {"Path": "rosetta.dmg.aea"}},
        }
    request = make_request(tmp_path, [row], members=(COMMON, "rosetta.dmg.aea"))
    with pytest.raises(IpswExtractError, match="RosettaOS"):
        build_extraction_plan(request)
