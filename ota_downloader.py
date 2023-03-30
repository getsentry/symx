import json
import subprocess
from pathlib import Path

import common


def download_otas(output_dir: Path, platform: str) -> None:
    ota_download_cmd = [
        "ipsw",
        "download",
        "ota",
        "--output",
        str(output_dir),
        "-y",
        "--platform",
        platform,
        "--resume-all",
    ]
    subprocess.run(ota_download_cmd)

    ota_beta_download_cmd = ota_download_cmd.copy()
    ota_beta_download_cmd.append("--beta")
    subprocess.run(ota_beta_download_cmd)


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data_store: dict[str, common.OtaArtifact],
) -> None:
    if result.returncode != 0:
        print(result.stderr)
    else:
        platform_meta = json.loads(result.stdout)
        for meta_item in platform_meta:
            url = meta_item["url"]
            zip_id = url[url.rfind("/") + 1 : -4]
            if len(zip_id) != 40:
                raise RuntimeError(f"Unexpected url-format in {meta_item}")

            if zip_id in meta_data_store.keys():
                store_item = meta_data_store[zip_id]
                if not (
                    store_item.build == meta_item["build"]
                    and store_item.description == meta_item.get("description")
                    and store_item.version == meta_item["version"]
                    and store_item.platform == platform
                    and store_item.url == url
                    and store_item.devices == meta_item.get("devices")
                    and store_item.hash == meta_item["hash"]
                    and store_item.hash_algorithm == meta_item["hash_algorithm"]
                ):
                    raise RuntimeError(
                        f"Same matching keys with different value:\n\tlocal: {store_item}\n\tapple: {meta_item}"
                    )
                pass
            else:
                meta_data_store[zip_id] = common.OtaArtifact(
                    id=zip_id,
                    build=meta_item["build"],
                    description=meta_item.get("description"),
                    version=meta_item["version"],
                    platform=platform,
                    url=url,
                    devices=meta_item.get("devices"),
                    download_path=None,
                    hash=meta_item["hash"],
                    hash_algorithm=meta_item["hash_algorithm"],
                )


def download_ota_metadata(output_dir: Path) -> None:
    print("Updating meta-data for...")

    meta_data_store = common.load_ota_images_meta(output_dir)

    for platform in common.OTA_PLATFORMS:
        print(platform)
        ota_download_meta_cmd = [
            "ipsw",
            "download",
            "ota",
            "--platform",
            platform,
            "--urls",
            "--json",
        ]

        parse_download_meta_output(
            platform,
            subprocess.run(ota_download_meta_cmd, capture_output=True),
            meta_data_store,
        )

        ota_beta_download_meta_cmd = ota_download_meta_cmd.copy()
        ota_beta_download_meta_cmd.append("--beta")
        parse_download_meta_output(
            platform,
            subprocess.run(ota_beta_download_meta_cmd, capture_output=True),
            meta_data_store,
        )

    common.save_ota_images_meta(meta_data_store, output_dir)


def main() -> None:
    args = common.downloader_parse_args()
    common.downloader_validate_shell_deps()

    # get the meta-data for all platforms first, so we can be sure to continuously update the meta-data store
    # for __all__ platforms everytime we start the downloader.
    download_ota_metadata(args.output_dir)

    # only now start with the mirroring process
    for platform in common.OTA_PLATFORMS:
        print(f"Downloading OTAs for {platform}...")
        download_otas(args.output_dir, platform)


if __name__ == "__main__":
    main()
