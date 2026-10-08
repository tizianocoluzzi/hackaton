import matplotlib.pyplot as pl
import numpy as np

import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt

from opensky_api import OpenSkyApi
api = OpenSkyApi()
states = api.get_states()
#for s in states.states:
#    print("(%r, %r, %r, %r)" % (s.longitude, s.latitude, s.baro_altitude, s.velocity))

request = cimgt.OSM()

track = api.get_track_by_aircraft("3c4b26")
p = track.path
latitudes = [i.latitude for i in p]
longitudes = [i.longitude for i in p]

# Bounds: (lon_min, lon_max, lat_min, lat_max):
extent = [np.min(longitudes), np.max(longitudes), np.min(latitudes), np.max(latitudes)]

ax = pl.axes(projection=request.crs)

ax.set_extent(
    extent,
    crs=ccrs.PlateCarree()
)

ax.add_image(request, 5)


# Aircraft positions
ax.scatter(
    longitudes,
    latitudes,
    transform=ccrs.PlateCarree(),
    s=20,
    zorder=10
)


# Aircraft track
ax.plot(
    longitudes,
    latitudes,
    transform=ccrs.PlateCarree(),
    linewidth=2,
    zorder=9
)


pl.show()
#print(l)
#    f"{i.latitude}, {i.logitude}"
