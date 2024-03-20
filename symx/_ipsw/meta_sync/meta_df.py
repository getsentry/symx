# mypy: ignore-errors
import json

import pandas as pd


def main() -> None:
    with open("/Users/mischan/Downloads/ipsw_meta12.json") as f:
        json_data = json.load(f)
        json_array = []
        for key, value in json_data["artifacts"].items():
            json_array.append(value)

        pd.set_option("display.max_columns", None)
        input_df = pd.json_normalize(json_array)
        input_df["released"] = pd.to_datetime(input_df["released"])

        normalized_source_rows = []
        for index, row in input_df.iterrows():
            if row.empty:
                continue

            sources_json = row["sources"]
            for source in sources_json:
                if source["hashes"] is None:
                    source["hashes"] = {"sha1": None, "sha2": None}
            normalized = pd.json_normalize(sources_json)
            if normalized.empty:
                continue

            # make sure that we keep the parent key, so we can join below
            normalized["key"] = row["key"]
            normalized_source_rows.append(
                normalized.drop(columns=["hashes.sha1", "hashes.sha2", "mirror_path"])
            )

        normalized_sources_df = pd.concat(normalized_source_rows)

        df = pd.merge(
            input_df.drop(columns=["sources"]), normalized_sources_df, on="key"
        )

        pd.set_option("display.max_columns", None)
        pd.set_option("display.max_rows", None)
        print(
            df[(df["processing_state"] == "symbols_extracted")]
            .groupby(["platform", "version", "build"])
            .size()
            .reset_index(name="counts")
        )
        print(
            df[(df["platform"] == "watchOS")]
            .groupby(["version", "build", "processing_state"])
            .size()
            .reset_index(name="counts")
        )
        print(
            df[(df["platform"] == "tvOS")]
            .groupby(["version", "build", "processing_state"])
            .size()
            .reset_index(name="counts")
        )
        print(
            df.groupby(["platform", "processing_state"])
            .size()
            .reset_index(name="counts")
        )
        print(
            df.groupby(["platform", "processing_state"]).agg(
                counts=(
                    "released",
                    "count",
                ),  # Count the number of entries in each group
                min_value=(
                    "released",
                    "min",
                ),  # Min value of 'data_column' for each group
                max_value=(
                    "released",
                    "max",
                ),  # Max value of 'data_column' for each group
            )
        )


if __name__ == "__main__":
    main()
