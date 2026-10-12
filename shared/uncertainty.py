"""Metric local tangent-plane geometry; heuristic extents, not calibrated coverage."""
import math

R = 6371000.0


def xy(latitude, longitude, origin):
    return (R * math.radians(longitude - origin[1]) * math.cos(math.radians(origin[0])),
            R * math.radians(latitude - origin[0]))


def latlon(east, north, origin):
    return [origin[0] + math.degrees(north / R),
            origin[1] + math.degrees(east / (R * math.cos(math.radians(origin[0]))))]


def area(position, along, lateral, heading=0):
    origin = [position['latitude'], position['longitude']]
    angle = math.radians(heading)
    points = []
    for i in range(65):
        t = i * 2 * math.pi / 64
        u, v = along * math.cos(t), lateral * math.sin(t)
        points.append(latlon(u * math.sin(angle) + v * math.cos(angle),
                             u * math.cos(angle) - v * math.sin(angle), origin))
    return dict(kind='circle' if abs(along - lateral) < .01 else 'ellipse', center=position,
                semi_along_m=along, semi_lateral_m=lateral, heading_deg=heading,
                coordinates=points, label='Area d’incertezza stimata')


def intersects(shape, coordinates, margin=0):
    """Exact line/ellipse intersection in local metres, including segment interiors."""
    origin = [shape['center']['latitude'], shape['center']['longitude']]
    theta = math.radians(shape['heading_deg'])
    a, b = shape['semi_along_m'] + margin, shape['semi_lateral_m'] + margin
    points = []
    for lat, lon in coordinates:
        x, y = xy(lat, lon, origin)
        points.append(((x * math.sin(theta) + y * math.cos(theta)) / a,
                       (x * math.cos(theta) - y * math.sin(theta)) / b))
    for p, q in zip(points, points[1:]):
        dx, dy = q[0] - p[0], q[1] - p[1]
        norm = dx * dx + dy * dy
        t = max(0, min(1, -(p[0] * dx + p[1] * dy) / norm)) if norm else 0
        if (p[0] + t * dx) ** 2 + (p[1] + t * dy) ** 2 <= 1:
            return True
    return False
