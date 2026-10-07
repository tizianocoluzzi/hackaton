import matplotlib.pyplot as pl
import numpy as np

import cartopy.crs as ccrs
import cartopy.io.img_tiles as cimgt

request = cimgt.OSM()



# Bounds: (lon_min, lon_max, lat_min, lat_max):
extent = [1, 13, 45, 53]

ax = pl.axes(projection=request.crs)
ax.set_extent(extent)
ax.add_image(request, 5)    # 5 = zoom level

# Just some random points/lines:
pl.scatter(4.92, 51.97, transform=ccrs.PlateCarree())
pl.plot([4.92, 9], [51.97, 47], transform=ccrs.PlateCarree())
pl.show()
