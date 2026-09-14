"""Typed subset of the root Info.plist embedded in ZIP OTA artifacts."""

from pydantic import BaseModel, ConfigDict, Field

RECOVERY_OTA_BUNDLE_IDENTIFIER = "com.apple.MobileAsset.RecoveryOSUpdate"
RECOVERY_OTA_RELEASE_TYPE = "Darwin Recovery"


class OtaMobileAssetProperties(BaseModel):
    """Mobile asset fields used to identify prerequisite/delta OTAs."""

    model_config = ConfigDict(extra="ignore")

    prerequisite_build: str = Field(default="", alias="PrerequisiteBuild")
    release_type: str = Field(default="", alias="ReleaseType")


class OtaArtifactInfo(BaseModel):
    """Trusted classification fields from an OTA's root Info.plist."""

    model_config = ConfigDict(extra="ignore")

    bundle_identifier: str = Field(default="", alias="CFBundleIdentifier")
    mobile_asset_properties: OtaMobileAssetProperties = Field(alias="MobileAssetProperties")

    @property
    def prerequisite_build(self) -> str | None:
        return self.mobile_asset_properties.prerequisite_build or None

    @property
    def is_recovery(self) -> bool:
        return (
            self.bundle_identifier == RECOVERY_OTA_BUNDLE_IDENTIFIER
            or self.mobile_asset_properties.release_type == RECOVERY_OTA_RELEASE_TYPE
        )
