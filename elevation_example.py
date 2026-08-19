from src.terrain_service import TerrainService

geotiff_path="./output/terrain.tif"

with TerrainService(geotiff_path) as terrain:
    elevation = terrain.get_elevation(
        latitude= 40.408785, 
        longitude= -3.834247
    )
            
    route_elevations = terrain.get_elevations([

        (
            40.4066943,
            -3.8398262
        ),
          (
            40.4064168,
            -3.8335093
            
          ),
          (
            40.4096543,
            -3.8275567
            
          ),
          (
            40.4137241,
            -3.8274353
            
          ),
          (
            40.4155739,
            -3.8315656
            
          ),
          (
            40.4129841,
            -3.8346026
            
          )
    ])

    print(elevation)
    print(route_elevations)