"""Lake-specific solar and lunar event calculations using PyEphem."""
from functools import lru_cache
from datetime import datetime, timezone, timedelta
import ephem
from quality import utc


def observer(dt, lon, lat):
    site=ephem.Observer()
    site.lon=str(float(lon)); site.lat=str(float(lat));site.elevation=0
    site.date=utc(dt).replace(tzinfo=None)
    # Standard atmospheric refraction, flat astronomical horizon; terrain is not modeled.
    site.pressure=1010; site.temp=15
    return site


def nearest_events(dt,lon,lat,body,event):
    site=observer(dt,lon,lat)
    found=[]
    for direction in ['previous','next']:
        try:
            value=getattr(site,direction+'_'+event)(body)
            found.append(value.datetime().replace(tzinfo=timezone.utc))
        except (ephem.AlwaysUpError,ephem.NeverUpError):
            pass
    return found


def lunar(dt):
    dt=utc(dt); moon=ephem.Moon(dt.replace(tzinfo=None))
    previous=ephem.previous_new_moon(dt.replace(tzinfo=None)).datetime().replace(tzinfo=timezone.utc)
    following=ephem.next_new_moon(dt.replace(tzinfo=None)).datetime().replace(tzinfo=timezone.utc)
    ratio=(dt-previous).total_seconds()/(following-previous).total_seconds()
    names=[('New Moon','🌑'),('Waxing Crescent','🌒'),('First Quarter','🌓'),('Waxing Gibbous','🌔'),
           ('Full Moon','🌕'),('Waning Gibbous','🌖'),('Last Quarter','🌗'),('Waning Crescent','🌘')]
    name,icon=names[int((ratio*8+.5)%8)]
    return {'phase_name':name,'phase_icon':icon,'illumination_pct':round(moon.phase,1),
            'moon_age_days':round((dt-previous).total_seconds()/86400,1),'phase_ratio':ratio}


def solar_factor(dt,lon,lat):
    dt=utc(dt)
    edges=nearest_events(dt,lon,lat,ephem.Sun(),'rising')+nearest_events(dt,lon,lat,ephem.Sun(),'setting')
    if edges and min(abs((dt-v).total_seconds()) for v in edges)<=90*60:
        return 15.0
    transits=nearest_events(dt,lon,lat,ephem.Sun(),'transit')
    if transits and min(abs((dt-v).total_seconds()) for v in transits)<=135*60:
        return -10.0
    sun=ephem.Sun(observer(dt,lon,lat))
    return 5.0 if float(sun.alt)<-6*ephem.degree else 0.0


def solar_context(dt, lon, lat):
    """Actual solar altitude for current tactical guidance, independent of score."""
    sun = ephem.Sun(observer(dt, lon, lat))
    altitude = float(sun.alt)
    if altitude < -6 * ephem.degree:
        return 'night'
    return 'twilight' if altitude < 0 else 'daylight'


def solunar_factor(dt,lon,lat):
    dt=utc(dt); phase=lunar(dt)['phase_ratio']
    dist=min(phase,abs(phase-.5),1-phase)
    bonus=6.0 if dist<.06 else (3.0 if dist<.12 else 0.0)
    major=nearest_events(dt,lon,lat,ephem.Moon(),'transit')+nearest_events(dt,lon,lat,ephem.Moon(),'antitransit')
    minor=nearest_events(dt,lon,lat,ephem.Moon(),'rising')+nearest_events(dt,lon,lat,ephem.Moon(),'setting')
    major_hours=min((abs((dt-e).total_seconds())/3600 for e in major),default=999)
    minor_hours=min((abs((dt-e).total_seconds())/3600 for e in minor),default=999)
    if major_hours<=1:
        return 16*(1-major_hours)+bonus,'MAJOR'
    if minor_hours<=.75:
        return 10*(1-minor_hours/.75)+bonus,'MINOR'
    return bonus,'NONE'
