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
              -7.8807465,
              42.3211809
            ],
            [
              -7.8747688,
              42.3211809
            ],
            [
              -7.8759851,
              42.3227607
            ],
            [
              -7.8828375,
              42.3222746
            ],
            [
              -7.8807465,
              42.3211809
            ]
          ]
        ]
      },
    }

    terrain_path, geotif, metadata_path = generate_terrain_from_geojson(
        geojson=area,
        output_path=Path("output/terrain.glb"),
        zoom=15,
        sample_step=1,
        vertical_exaggeration=2,
        crs="EPSG:25829",
        max_tiles=650,
        workers=8,
        timeout=30.0,
    )

    print(f"Terrain created: {terrain_path}")
    print(f"Metadata created: {metadata_path}")


if __name__ == "__main__":
    main()
