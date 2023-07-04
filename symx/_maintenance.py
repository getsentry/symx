import json
import logging


from symx._gcs import GoogleStorage

logger = logging.getLogger(__name__)

bundle_id_map = {
    "audioos": {
        "16.5_20L563_arm64": "ba7615742126a4a3c727cfeec7da0e4c7d5c4b0e",
        "16.5_20L563_arm64e": "fe999a477d39a37121e91860af854c713027151f",
        "16.6_20M5527e_arm64": "d9464b91a411fe794833b739e168a3b4d4b7cac2_beta",
        "16.6_20M5527e_arm64e": "008f3e3e55f5eca765ad7128e17688568d6a69f9_beta",
        "16.6_20M5538d_arm64e": "78844ef60730f4d147c996963151549f9982bc9f_beta",
        "16.6_20M5538d_arm64": "b6e3d6609ef0775b63f9b2dfdb4d7582fefda301_beta",
        "17.0_21J5273q_arm64e": "81bd7a2835470d8091fa9cd53144f222ffea0de1_beta",
        "17.0_21J5273q_arm64": "d2b127a578451c6e907167b502b391f7917b7be5_beta",
    },
    "ios": {
        "15.7.6_19H349_arm64": "9537a523ed19beb67addf6e9de5c8b6091aab5f0",
        "16.5_20F66_arm64": "c05206dde37d2cbc8e39050247e2c91fa10c6d94",
        "16.6_20G5026e_arm64e": "635ac77cc20f5068f1eb272871b7476fb2a7489b_beta",
        "16.6_20G5026e_arm64": "716f90b9a7b26dc5012d09013434cac72b64ad14_beta",
        "16.6_20G5037d_arm64e": "c0bdf2fcb96d4a448f8b3bc345c9fb2d6a84e6dd_beta",
        "16.6_20G5037d_arm64": "5959aab033b25a906e7b9b86c31b29c3c112da24_beta",
        "17.0_21A5248v_arm64e": "f5e0811127bf9e3156b6da52427b89ffe117c2bc_beta",
        "17.0_21A5248v_arm64": "75d5f6f4975be74b858bc48cb723ff188a4b3bb7_beta",
    },
    "macos": {
        "11.7.7_20G1345_arm64e": "e018c2b94e8d1475ce7ea1b1abe4213a35d92f27",
        "12.6.6_21G646_arm64e": "e64124caeb479e5c41fe0c47faaf2d668e2dea32",
        "13.4_22F2073_arm64e": "5307f416e192663b5dcbefd9a15a83641daa1d2e",
        "13.4_22F66_arm64e": "5d3973f84c1366770d5fb5dc76edeec023072cef",
        "13.5_22G5027e_arm64e": "63348f1da7367d85409324e21a4dc7de70d8aa1f_beta",
        "13.5_22G5038d_arm64e": "996a2cb30dafbaa7538774d759461135d1671654_beta",
        "14.0_23A5257q_arm64e": "d2d63d5f9a352d23b18f16952753191cd63111b0_beta",
    },
    "tvos": {
        "16.5_20L563_arm64": "3d3491b70addc0e542763a4b1d17c41d48cb482c",
        "16.5_20L563_arm64e": "78e189e763dc596930efafd2f2711bed0efa781b",
        "16.5_20L6563_arm64": "1337686a31add4f0846190167d8f7ee38d0e6733",
        "16.6_20M5527e_arm64": "d3a02fbbba7ed551703c286c8e5c0a70115b8a79_beta",
        "16.6_20M5527e_arm64e": "99c6c7b94c6347cc74209edba87cf642da9c3adb_beta",
        "16.6_20M5538d_arm64e": "f5e0cc2bb17464e296eccc71a8e2d0d957f3f200_beta",
        "16.6_20M5538d_arm64": "991e260ecd5840b41c0564f153ac4d0d685db5aa_beta",
        "17.0_21J5273q_arm64": "de356c0887e99f2dec2643b794a319516fce1041_beta",
        "17.0_21J5273q_arm64e": "492fa904241ec4dd7cc0ff7a067181ad91d0898d_beta",
    },
    "watchos": {
        "10.0_21R5275t_arm64_32": "c40f9ffd9b9c2e0107ecf69a6980d2ff0d2095aa_beta",
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
            bundle_idx_src_blob_path = f"{common_path_prefix}{old_bundle_id}"
            bundle_idx_dst_blob_path = f"{common_path_prefix}{new_bundle_id}"
            bundle_idx_blob = bucket.blob(bundle_idx_src_blob_path)
            if bundle_idx_blob.exists():
                # TODO: 3: move that blob
                logger.info(
                    f"dry-run: moving {bundle_idx_src_blob_path} to"
                    f" {bundle_idx_dst_blob_path}"
                )
                bundle_idx = json.loads(bundle_idx_blob.download_as_string())

                # TODO: 2: overwrite the name in the bundle_idx and store in that blob
                if bundle_idx["name"] != old_bundle_id:
                    logger.error(
                        f"Expected bundle-index name to be equal to {old_bundle_id}"
                    )

                for debug_id in bundle_idx["debug_ids"]:
                    common_back_ref_prefix = (
                        f"symbols/{platform}/{debug_id[0:2]}/{debug_id[2:]}/refs/"
                    )
                    old_bundle_id_back_ref = f"{common_back_ref_prefix}{old_bundle_id}"
                    new_bundle_id_back_ref = f"{common_back_ref_prefix}{new_bundle_id}"
                    back_ref_blob = bucket.blob(old_bundle_id_back_ref)
                    if back_ref_blob.exists():
                        # TODO 1: move the back-refs
                        logger.info(
                            f"dry-run: moving {old_bundle_id_back_ref} to"
                            f" {new_bundle_id_back_ref}"
                        )
                    else:
                        logger.error(
                            f"Found debug-id ({debug_id}) in bundle-index"
                            f" {bundle_idx_src_blob_path}, that doesn't back-ref via"
                            f" {old_bundle_id_back_ref}"
                        )
            else:
                logger.error(f"Can't stat {bundle_idx_src_blob_path}")
