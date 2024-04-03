import logging

from symx._common import ArtifactProcessingState
from symx._ota.storage.gcs import OtaGcsStorage

logger = logging.getLogger(__name__)

otas_to_reset = [
    "403d095827b109342216c122b75693078fa84e97",
    "933366066b39e26c45088eeefe2f581787642b2d",
    "1f530040800182d40987d57530ebee58fa17e2a7",
    "e0e75b302004636114c20dd978047b6990ad5556",
    "f4f1e695118dbc89d06ccf395fddc383cd594fd4",
    "bfcd8684d5f6d6852ac2a43b032bfa9de52e8e1a",
    "62df0a96d80dc1910f7f6d7aae2874c5d6d0a850",
    "53a2dcb317392f1fdd5d1f4e37dddec2b8720bd1",
    "0e5342667e08f614603036058def8a08e94b9729",
    "9b2a905d98a9c256f435683db67abaffcd28302a",
    "c3a26308567402c4fbd3708b1d2d23844a741004",
    "df9c178d7fb5df48ce5fcfd5ce88adc1fa117fa3",
    "b4df78c0f42da7193a5e470ab8c432f7af364b39",
    "ed0632a537b6eab04447e121fe6c48456af99497",
    "fa3ee130028570aaafb6ef1eb331124a6a839805",
    "fbd5e611989b7df36f3544d76e0d0db79ed54cbe",
    "1229a4cb49ad44f0ed996364942d975e91bdc99b",
    "165c23c5334254e89256e97b46365f0c7dd746d8",
    "8b551f441d2850f6a1797181f2c1025c5ab3362e",
    "2232c0e3815e5f19902d3e2ad904612d308f4e2c",
    "f987d9b824d0dd7ed0c020caff27a34f52891e04",
    "ef838b64317dd28bd248e1ca325f57c7aec7255e",
    "dec1cbfbcd9b25ed3c59970e3168b84378cf91b6",
    "b6e6049e6f5e29795b72eec6403385e46e5f17a2",
    "6a20a8cedf1ef0976c6aae7122bfa0755feca7cf",
    "6e3a1865ea78ce56f11fc6e3142a11f4e0f7290b",
    "15ecc3f110a69be1343aeb16bf8a8e74cb445bbb",
    "4c8af6fa85ddda4bed87559a702e8e57719a2f77",
    "f539196a1a212930c3e8c78daf8f914099251cb3",
    "7d4022dc4937eb101f5aa357dfc8b5e101347184",
    "ab35b774b4754b7d06ca029a42331816f60a9543",
    "1bacd64089b6d620d7db7cbdfdecbc43d2fdec2a",
    "3e8e9d241db734d411e2c306ec9e43ff5a010719",
    "bdb7dc98e372e0482dcf1d4ec52d6132c5cf6648",
    "9d281abf37a8a8c2759a07df56416e4262fdef42",
    "84e7d16db1ddf919652e91217dc842dbf2d80e72",
    "2fdd3bdbbfa3d04fd1bf575080153db95b0ecfd2",
    "11c2bb006379aecc77809cd89a35aa6b0e283ebe",
    "af97dc408952112dc23c91f49b61b4d8c9a1b9f6",
    "2c4e6b64b5b429baa2efc9ffd6a6dfbdfb3a433d",
    "f5b3e689205f9b69101202420c185a7c8bf445a8",
    "7bd9fe8dec15b63f8fe5c9cfd7ea0bcd2b41d299",
    "57c978075341aeae918c8eacebb0423ad21423e1",
    "ec921d6e99d593d1a0319cf6e7edaa94e31952b9",
    "6fab5cd26710474d9c1c42bece5e88597cd733e8",
    "46f4da87d211eb4dc2ef9a842951fdd25b2ac9f0",
    "5f3b11555464ba96d25b0c7a02d8d14bcb0f28c0",
    "f401fbe35a1a93c066caca0bff05ae2962d9e960",
    "63704e5124b334b36d72ca4cc1c1aaff0457934c",
    "dd252911ce988b52d8cec760c095b44cc3de1404",
    "5efc9e35f27eb3c5b81d9f6b8d52df29f145eecf",
    "870113d8679501160ae054d657ec5ae8818be85a",
    "afa41ff6f37b0a25d29b37488d66b72fc8657373",
    "14713174c293910e944a7f5e8e81d256676ecd4d",
]


def migrate(storage: OtaGcsStorage) -> None:
    ota_meta = storage.load_meta()
    if ota_meta is None:
        logger.error("Could not retrieve meta-data from storage.")
        return

    for key, ota in ota_meta.items():
        if key in otas_to_reset:
            print(f"{key}: {ota}")
            assert (
                ota.processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
            )
            ota.processing_state = ArtifactProcessingState.MIRRORED
            ota.update_last_run()
            storage.update_meta_item(key, ota)
