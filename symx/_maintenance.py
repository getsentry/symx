import json
import logging

import sentry_sdk

from symx._gcs import GoogleStorage

logger = logging.getLogger(__name__)

bundle_id_map = {
    "watchos": {
        "10.0_21R5275t_arm64_32": "c40f9ffd9b9c2e0107ecf69a6980d2ff0d2095aa_beta",
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
                            f"found back-ref {old_bundle_id_back_ref}... moving to"
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
                        # if we cannot find the old back-ref, the moved one should exist
                        back_ref_blob = bucket.blob(new_bundle_id_back_ref)
                        if not back_ref_blob.exists():
                            # if it doesn't then we'll just create the marker
                            logger.info(
                                f"couldn't find either new or old back-ref... creating {new_bundle_id_back_ref}"
                            )
                            back_ref_blob.upload_from_string("")
                        else:
                            logger.info(
                                f"back-ref was already move to {new_bundle_id_back_ref}"
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
