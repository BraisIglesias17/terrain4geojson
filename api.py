from fastapi import FastAPI
from pydantic import BaseModel
from typing import List
from datetime import datetime
from src.terrain_service import TerrainService


class Point(BaseModel):
    latitude: float
    longitude: float

class EnrichedPoint(BaseModel):
    latitude: float
    longitude: float
    altitude: float

geotiff_path="./output/terrain.tif"

with TerrainService(geotiff_path) as terrain:
    print("#####  Model terrain loaded!")
            
    app = FastAPI()

    @app.post("/terrain",response_model=List[EnrichedPoint])
    def endpoint(points: List[Point]):

      toret=[]

      for point in points:

        try:

          t0=datetime.now()
          altitude=(terrain.get_elevation(latitude=point.latitude,longitude=point.longitude))
          print("- Inference time: ",datetime.now()-t0,"ms")
          toret.append({
             "latitude":point.latitude,
             "longitude":point.longitude,
             "altitude":altitude
          })
        except:
          print("Error on computing altitude for ",point) 

      return toret