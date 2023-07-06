import json
import logging

import sentry_sdk

from symx._gcs import GoogleStorage

logger = logging.getLogger(__name__)

bundle_id_map = {
    "watchos": {
        # TODO: this artifact has been interrupted. Let's continue with the rest and adapt the script to this one.
        #    "10.0_21R5275t_arm64_32": "c40f9ffd9b9c2e0107ecf69a6980d2ff0d2095aa_beta",
        "8.7_19U66_arm64_32": "2bb717ee417743b101a2cec2edafbf0136736508",
        "8.7.1_19U67_armv7k": "1da8fc0973850021bc8bee8bf4973a999355e1e4",
        "9.5_20T562_arm64_32": "1c8f3750a4dd418150a7720c19d1aa9c62754580",
        "9.5.1_20T570_arm64_32": "b399d1e697b9acbbb70dcb11274ff3b25e89c273",
        "9.6_20U5527c_arm64_32": "c404a9468ff65646af56b5d1388d71e9ffb3d031_beta",
        "9.6_20U5538d_arm64_32": "fd76ab4dd2c5c04c7d0223a5840bd655a3c3a4a9_beta",
    },
}


def migrate(storage: GoogleStorage) -> None:
    bucket = storage.bucket
    for platform, bundles in bundle_id_map.items():
        for old_bundle_id, ota_key in bundles.items():
            common_path_prefix = f"symbols/{platform}/bundles/"
            new_bundle_id = f"ota_{ota_key}"
            sentry_sdk.set_tag("ota.bundle.id.old", old_bundle_id)
            sentry_sdk.set_tag("ota.bundle.id.new", new_bundle_id)

            bundle_idx_src_blob_path = f"{common_path_prefix}{old_bundle_id}"
            bundle_idx_dst_blob_path = f"{common_path_prefix}{new_bundle_id}"
            bundle_idx_blob = bucket.blob(bundle_idx_src_blob_path)
            if bundle_idx_blob.exists():
                logger.info(
                    f"moving {bundle_idx_src_blob_path} to {bundle_idx_dst_blob_path}"
                )
                bundle_idx = json.loads(bundle_idx_blob.download_as_string())

                if bundle_idx["name"] != old_bundle_id:
                    logger.error(
                        f"Expected bundle-index name to be equal to {old_bundle_id}"
                    )

                for debug_id in bundle_idx["debug_ids"]:
                    sentry_sdk.set_tag("ota.bundle.debug.id", debug_id)

                    common_back_ref_prefix = (
                        f"symbols/{platform}/{debug_id[0:2]}/{debug_id[2:]}/refs/"
                    )
                    old_bundle_id_back_ref = f"{common_back_ref_prefix}{old_bundle_id}"
                    new_bundle_id_back_ref = f"{common_back_ref_prefix}{new_bundle_id}"
                    back_ref_blob = bucket.blob(old_bundle_id_back_ref)
                    if back_ref_blob.exists():
                        logger.info(
                            f"moving back-ref {old_bundle_id_back_ref} to"
                            f" {new_bundle_id_back_ref}"
                        )
                        # 1: move all back-refs
                        try:
                            bucket.rename_blob(
                                back_ref_blob,
                                new_bundle_id_back_ref,
                                if_generation_match=0,
                            )
                        except Exception as e:
                            sentry_sdk.capture_exception(e)
                    else:
                        logger.error(
                            f"Found debug-id ({debug_id}) in bundle-index"
                            f" {bundle_idx_src_blob_path}, that doesn't back-ref via"
                            f" {old_bundle_id_back_ref}"
                        )

                # 2: overwrite the name in the bundle_idx and store in that blob
                bundle_idx["name"] = new_bundle_id
                try:
                    bundle_idx_blob.upload_from_string(
                        json.dumps(bundle_idx),
                        if_generation_match=bundle_idx_blob.generation,
                    )
                except Exception as e:
                    sentry_sdk.capture_exception(e)

                # 3: move the bundle_idx blob
                try:
                    bucket.rename_blob(
                        bundle_idx_blob, bundle_idx_dst_blob_path, if_generation_match=0
                    )
                except Exception as e:
                    sentry_sdk.capture_exception(e)

            else:
                logger.error(f"Can't stat {bundle_idx_src_blob_path}")
