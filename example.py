from pathlib import Path
from src.build_terrain import generate_terrain_from_geojson


def main() -> None:

    area = {
        "type": "Feature",
        "properties": {
            "name": "Example terrain area",
        },
        "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [
              -3.8429727661878132,
              40.41612839959822
            ],
            [
              -3.8233315,
              40.41612839959822
            ],
            [
              -3.8233315,
              40.4046795
            ],
            [
              -3.8429727661878132,
              40.4046795
            ],
            [
              -3.8429727661878132,
              40.41612839959822
            ]
          ]
        ]
      },
    }

    terrain_path, metadata_path = generate_terrain_from_geojson(
        geojson=area,
        output_path=Path("output/terrain.glb"),
        zoom=15,
        sample_step=1,
        vertical_exaggeration=2,
        crs="EPSG:25829",
        max_tiles=64,
        workers=8,
        timeout=30.0,
    )

    print(f"Terrain created: {terrain_path}")
    print(f"Metadata created: {metadata_path}")


if __name__ == "__main__":
    main()
