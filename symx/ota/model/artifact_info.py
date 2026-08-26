"""Typed subset of the root Info.plist embedded in ZIP OTA artifacts."""

from pydantic import BaseModel, ConfigDict, Field


class OtaMobileAssetProperties(BaseModel):
    """Mobile asset fields used to identify prerequisite/delta OTAs."""

    model_config = ConfigDict(extra="ignore")

    prerequisite_build: str = Field(default="", alias="PrerequisiteBuild")


class OtaArtifactInfo(BaseModel):
    """Trusted classification fields from an OTA's root Info.plist."""

    model_config = ConfigDict(extra="ignore")

    mobile_asset_properties: OtaMobileAssetProperties = Field(alias="MobileAssetProperties")

    @property
    def prerequisite_build(self) -> str | None:
        return self.mobile_asset_properties.prerequisite_build or None
