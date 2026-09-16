"""Typed BuildManifest topology and source-scoped IPSW extraction plans."""

import hashlib
import plistlib
import re
import stat
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from symx.ipsw.errors import IpswExtractError
from symx.ipsw.model import IpswPlatform
from symx.model import MACOS_DSC_ARCHITECTURES, Arch

if TYPE_CHECKING:
    from symx.ipsw.extract import IpswExtractionRequest


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class IpswManifestComponentInfo(_ManifestModel):
    path: str = Field(alias="Path", min_length=1, strict=True)


class IpswManifestComponent(_ManifestModel):
    info: IpswManifestComponentInfo = Field(alias="Info")


class IpswBuildIdentityManifest(_ManifestModel):
    cryptex_system_os: IpswManifestComponent | None = Field(None, alias="Cryptex1,SystemOS")
    cryptex_rosetta_os: IpswManifestComponent | None = Field(None, alias="Cryptex1,RosettaOS")
    os: IpswManifestComponent | None = Field(None, alias="OS")

    @field_validator("cryptex_system_os", "cryptex_rosetta_os", "os", mode="before")
    @classmethod
    def reject_null_component(cls, value: object) -> object:
        # Missing keys are absent components; present, malformed keys are not.
        if value is None:
            raise ValueError("present image component cannot be null")

        return value


class IpswBuildIdentityInfo(_ManifestModel):
    variant: str = Field("", alias="Variant")
    device_class: str = Field("", alias="DeviceClass")


class IpswBuildIdentity(_ManifestModel):
    product_type: str = Field("", alias="Ap,ProductType")
    manifest: IpswBuildIdentityManifest = Field(alias="Manifest")
    info: IpswBuildIdentityInfo = Field(default_factory=lambda: IpswBuildIdentityInfo.model_validate({}), alias="Info")


class IpswBuildManifest(_ManifestModel):
    product_version: str | None = Field(None, alias="ProductVersion")
    product_build_version: str | None = Field(None, alias="ProductBuildVersion")
    supported_product_types: tuple[str, ...] = Field(default_factory=tuple, alias="SupportedProductTypes")
    build_identities: tuple[IpswBuildIdentity, ...] = Field(alias="BuildIdentities")


class ImageKind(StrEnum):
    SYSTEM = "SystemOS"
    FILESYSTEM = "OS"
    ROSETTA = "RosettaOS"


@dataclass(frozen=True)
class IpswImageTarget:
    kind: ImageKind
    member: str
    products: tuple[str, ...] = ()
    boards: tuple[str, ...] = ()
    selector: str | None = None

    @property
    def key(self) -> str:
        # Never use a basename as identity: different members can share one.
        digest = hashlib.sha256(self.member.encode()).hexdigest()
        return f"{self.kind}-{digest}"

    @property
    def selector_args(self) -> list[str]:
        return ["--device", self.selector] if self.selector is not None else []


@dataclass(frozen=True)
class IpswDscAttemptRequest:
    image: IpswImageTarget
    arch: Arch | None
    work_dir: Path

    @property
    def output_dir(self) -> Path:
        return self.work_dir / "output"


@dataclass(frozen=True)
class IpswExtractionPlan:
    request: "IpswExtractionRequest"
    system_images: tuple[IpswImageTarget, ...]
    rosetta_images: tuple[IpswImageTarget, ...]
    attempts: tuple[IpswDscAttemptRequest, ...]
    unmatched_devices: tuple[str, ...]

    def attempts_for(self, image: IpswImageTarget) -> tuple[IpswDscAttemptRequest, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.image == image)


def _validate_member(archive: zipfile.ZipFile, member: str) -> None:
    path = PurePosixPath(member)
    if (
        not member
        or path.is_absolute()
        or "\\" in member
        or "\x00" in member
        or any(part in ("", ".", "..") for part in member.split("/"))
    ):
        raise IpswExtractError(f"Unsafe IPSW image member: {member!r}")

    matches = [entry for entry in archive.infolist() if entry.filename == member]
    if len(matches) != 1:
        raise IpswExtractError(f"IPSW member must occur exactly once: {member!r} ({len(matches)} entries)")

    entry = matches[0]
    if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
        raise IpswExtractError(f"IPSW member is not a regular file: {member!r}")


def _manifest_from_archive(archive: zipfile.ZipFile) -> IpswBuildManifest:
    _validate_member(archive, "BuildManifest.plist")
    return IpswBuildManifest.model_validate(plistlib.loads(archive.read("BuildManifest.plist")))


def read_build_manifest(ipsw_path: Path) -> IpswBuildManifest:
    try:
        with zipfile.ZipFile(ipsw_path) as archive:
            return _manifest_from_archive(archive)
    except Exception as error:
        raise IpswExtractError(f"Cannot read IPSW BuildManifest in {ipsw_path}: {error}") from error


def _product(manifest: IpswBuildManifest, identity: IpswBuildIdentity) -> str:
    if identity.product_type:
        return identity.product_type

    # Matches ipsw identityProduct's manifest-only fallback. Board selectors do
    # not require guessing a product from filenames, aliases or device trees.
    if len(manifest.supported_product_types) == 1:
        return manifest.supported_product_types[0]

    return ""


def _component(identity: IpswBuildIdentity, kind: ImageKind) -> IpswManifestComponent | None:
    match kind:
        case ImageKind.SYSTEM:
            return identity.manifest.cryptex_system_os
        case ImageKind.ROSETTA:
            return identity.manifest.cryptex_rosetta_os
        case ImageKind.FILESYSTEM:
            if "Recovery" in identity.info.variant:
                return None

            return identity.manifest.os


def _targets(manifest: IpswBuildManifest, kind: ImageKind, devices: tuple[str, ...]) -> tuple[IpswImageTarget, ...]:
    groups: dict[str, list[IpswBuildIdentity]] = {}
    selector_members: dict[str, set[str]] = {}
    for identity in manifest.build_identities:
        component = _component(identity, kind)
        if component is None:
            continue

        member = component.info.path
        groups.setdefault(member, []).append(identity)
        for selector in (_product(manifest, identity), identity.info.device_class):
            if selector:
                selector_members.setdefault(selector.lower(), set()).add(member)

    targets: list[IpswImageTarget] = []
    preferred = {device.lower() for device in devices}

    for member, identities in sorted(groups.items()):
        products = tuple(sorted({_product(manifest, identity) for identity in identities} - {""}))
        boards = tuple(sorted({identity.info.device_class for identity in identities} - {""}))
        selector = None

        if len(groups) > 1:
            candidates = sorted(products, key=lambda p: (p.lower() not in preferred, p)) + list(boards)
            selector = next(
                (candidate for candidate in candidates if selector_members[candidate.lower()] == {member}), None
            )
            if selector is None:
                raise IpswExtractError(f"No unique product/board selector for {kind} image {member}")

        targets.append(IpswImageTarget(kind, member, products, boards, selector))

    return tuple(targets)


def macos_dsc_architectures(version: str | None) -> tuple[Arch, ...]:
    if version is None or re.match(r"^\d+", version) is None:
        label = "<missing>" if version is None else repr(version)
        raise IpswExtractError(
            f"Cannot determine required macOS DSC architectures: missing or unparseable macOS version {label}"
        )

    return MACOS_DSC_ARCHITECTURES


def macos_requires_rosetta(version: str | None) -> bool:
    match = re.match(r"^(\d+)", version or "")
    return match is not None and int(match[1]) >= 27


def build_extraction_plan(request: "IpswExtractionRequest") -> IpswExtractionPlan:
    architectures: tuple[Arch | None, ...] = (
        macos_dsc_architectures(request.version) if request.platform == IpswPlatform.MACOS else (None,)
    )
    requires_rosetta = request.platform == IpswPlatform.MACOS and macos_requires_rosetta(request.version)
    try:
        with zipfile.ZipFile(request.ipsw_path) as archive:
            manifest = _manifest_from_archive(archive)

            system = _targets(manifest, ImageKind.SYSTEM, request.devices)
            if not system:
                system = _targets(manifest, ImageKind.FILESYSTEM, request.devices)
            if not system:
                raise IpswExtractError("BuildManifest has no SystemOS or non-recovery OS image")

            rosetta = _targets(manifest, ImageKind.ROSETTA, request.devices) if requires_rosetta else ()

            if requires_rosetta and not rosetta:
                raise IpswExtractError(
                    f"macOS {request.version} x86_64 DSC requires Cryptex1,RosettaOS in BuildManifest"
                )

            for image in (*system, *rosetta):
                _validate_member(archive, image.member)
                # ipsw's ZIP helpers also match case-insensitively/by basename.
                # Exact-entry validation alone does not prove what it extracts.
                tool_matches = [
                    entry.filename
                    for entry in archive.infolist()
                    if entry.filename.lower() == image.member.lower()
                    or PurePosixPath(entry.filename).name.lower() == image.member.lower()
                ]
                if tool_matches != [image.member]:
                    raise IpswExtractError(
                        f"IPSW image member is ambiguous for ipsw selection: {image.member}: {tool_matches}"
                    )
                if image.kind == ImageKind.ROSETTA and image.member.endswith(".aea"):
                    raise IpswExtractError(
                        f"RosettaOS DMG is AEA encrypted and cannot be mounted directly: {image.member}"
                    )

    except IpswExtractError:
        raise
    except Exception as error:
        raise IpswExtractError(f"Cannot plan IPSW images for {request.ipsw_path}: {error}") from error

    attempts: list[IpswDscAttemptRequest] = []
    for image in (*system, *rosetta):
        for arch in architectures:
            rosetta_arch = arch in (Arch.X86_64, Arch.X86_64H)
            if requires_rosetta and rosetta_arch != (image.kind == ImageKind.ROSETTA):
                continue

            work_dir = request.processing_dir / "dsc" / image.key / (str(arch) if arch else "default")
            attempts.append(IpswDscAttemptRequest(image, arch, work_dir))

    products = {product.lower() for image in system for product in image.products}
    unmatched = tuple(sorted(device for device in request.devices if device.lower() not in products))
    return IpswExtractionPlan(request, system, rosetta, tuple(attempts), unmatched)
