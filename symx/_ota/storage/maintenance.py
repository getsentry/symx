import logging

from symx._ota.storage.gcs import OtaGcsStorage

logger = logging.getLogger(__name__)

otas_to_reset = [
    "76ca77392a62eb19239ae33b5266b9bb0aabc831",
    "d57611b8bf0b6f81ec62ff17a5fa5e4e511c2d3e_beta",
    "827288e19fa2a72d627e4cd96737a4508a9f00bb_beta",
    "d420a93df15c741b96d88a42840eee21501c97db_beta",
    "4889c7dcc169f27de39244779df87e6d30c81270_beta",
    "e63a8ac7a511065b0725cb9ec49169829ef9d8fa_beta",
    "462921a62fd1347f49a6e5d4485ec81ffe38244d_beta",
    "0fc25a287a1b93657889cef36da5c7d65291974a_beta",
    "6683e8ec7572a62a5c01206909148bbea1c24754_beta",
    "dd27270bea850ef8a5f0c45356a38e5b09c7ba8c_beta",
    "570f685f442946839d658ed8949436a46ed6d275_beta",
    "66f18981ad2eebbcb8b2146fcd35587196694323_beta",
    "678c27d58a6a1a1734c4e959109f423cbd3eabfe_beta",
    "e8a110d935d35b8553f1e31be6438a2613d7c402_beta",
    "801f75599001dcdcae7553f9febb6e7e8c0ff836_beta",
    "aaee1d1fcb97388455622901d20360a44fbc4bfc_beta",
    "4cd2eab9b3135cae6bf0d6d7e4d5df3c231f0e97_beta",
    "9de5bd6ecf4616f32787ad02d933fb2a3800f83b_beta",
    "79f2cb9f28b2f74b3b689c0765477a5c3bcd81a4_beta",
    "64e37db43b50d5bd87412be39c64fd9524dd53f3_beta",
    "924999380a30a575cd12ce4da40c6b1d8db13140_beta",
    "44165d42eff7b876653481a6aeab7d50b188995c_beta",
    "ad7ce93bd1a766f87970930eac543d236829c8c7_beta",
    "44e465ef8f6500e51ae814ca8b2ec77e4e7f22b0_beta",
    "4dd4a1cb58cf68fe327703e85f473a3f5cf5f7c5_beta",
    "2d41271742e50b533f189fbc11fe459eb558cbf9_beta",
    "a96c7538340d30cb3232446a750e022e556708e3_beta",
    "ea54619b2cd0e2a45f9f87fc8cec1ece6b66537c_beta",
    "425b733851c2bccb507de48183efdba82a01816f_beta",
    "c32bf27124ccb370f4624eb8d7f3ca90c1b0266f_beta",
    "1b886c09b4f035f1ddefc2e860850e85383270fc",
    "8ea82b599c5b8655bacf026f0444d082e9290534",
    "49aa4785a3d4c3784943e86b2840252d15734719",
    "87d1ad8823763ca7ad7c53adf440c785c8b722b2",
    "a756bef2c19b684ef3039213ed145c7fe76c4bbc",
    "d57611b8bf0b6f81ec62ff17a5fa5e4e511c2d3e",
    "827288e19fa2a72d627e4cd96737a4508a9f00bb",
    "d420a93df15c741b96d88a42840eee21501c97db",
    "4889c7dcc169f27de39244779df87e6d30c81270",
    "e63a8ac7a511065b0725cb9ec49169829ef9d8fa",
    "462921a62fd1347f49a6e5d4485ec81ffe38244d",
    "0fc25a287a1b93657889cef36da5c7d65291974a",
    "6683e8ec7572a62a5c01206909148bbea1c24754",
    "dd27270bea850ef8a5f0c45356a38e5b09c7ba8c",
    "570f685f442946839d658ed8949436a46ed6d275",
    "66f18981ad2eebbcb8b2146fcd35587196694323",
    "678c27d58a6a1a1734c4e959109f423cbd3eabfe",
    "e8a110d935d35b8553f1e31be6438a2613d7c402",
    "801f75599001dcdcae7553f9febb6e7e8c0ff836",
    "aaee1d1fcb97388455622901d20360a44fbc4bfc",
    "4cd2eab9b3135cae6bf0d6d7e4d5df3c231f0e97",
    "9de5bd6ecf4616f32787ad02d933fb2a3800f83b",
    "79f2cb9f28b2f74b3b689c0765477a5c3bcd81a4",
    "64e37db43b50d5bd87412be39c64fd9524dd53f3",
    "924999380a30a575cd12ce4da40c6b1d8db13140",
    "44165d42eff7b876653481a6aeab7d50b188995c",
    "ad7ce93bd1a766f87970930eac543d236829c8c7",
    "44e465ef8f6500e51ae814ca8b2ec77e4e7f22b0",
    "4dd4a1cb58cf68fe327703e85f473a3f5cf5f7c5",
    "2d41271742e50b533f189fbc11fe459eb558cbf9",
    "a96c7538340d30cb3232446a750e022e556708e3",
    "ea54619b2cd0e2a45f9f87fc8cec1ece6b66537c",
    "425b733851c2bccb507de48183efdba82a01816f",
    "c32bf27124ccb370f4624eb8d7f3ca90c1b0266f",
]


def migrate(storage: OtaGcsStorage) -> None:
    ota_meta = storage.load_meta()
    if ota_meta is None:
        logger.error("Could not retrieve meta-data from storage.")
        return

    for key, ota in ota_meta.items():
        if key in otas_to_reset:
            print(f"{key}: {ota}")
            assert ota.is_mirrored()
