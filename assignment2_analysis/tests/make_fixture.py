"""Build an entirely artificial dataset for integration tests (no user records)."""
from pathlib import Path
import gzip
import xml.etree.ElementTree as ET
import pandas as pd
import shapefile
from pyproj import CRS, Transformer


def make_fixture(base):
    base=Path(base)
    for sub in ['Boundary','microcensus','Neuchatel_10pct/simulation_output','Neuchatel_10pct/network_calibration_files']:
        (base/sub).mkdir(parents=True,exist_ok=True)
    scenario=base/'Neuchatel_10pct'; sim=scenario/'simulation_output'
    convert=Transformer.from_crs(21781,2056,always_xy=True)
    hx,hy=convert.transform(600000,200000)
    def xy(x,y): return convert.transform(x,y)
    x0,y0=hx-1000,hy-1000; x1,y1=hx+19000,hy+9000
    with shapefile.Writer(str(base/'Boundary/neuchatel')) as w:
        w.field('id','N'); w.poly([[(x0,y0),(x0,y1),(x1,y1),(x1,y0),(x0,y0)]]); w.record(1)
    (base/'Boundary/neuchatel.prj').write_text(CRS.from_epsg(2056).to_wkt())
    population=ET.Element('population'); households=[]; people=[]; ways=[]; stages=[]; outtrips=[]
    def attr(parent,values):
        a=ET.SubElement(parent,'attributes')
        for key,value in values.items(): ET.SubElement(a,'attribute',{'name':key,'class':'java.lang.String'}).text=str(value)
    def act(plan,kind,x,y):
        a=ET.SubElement(plan,'act',{'type':kind,'x':str(x),'y':str(y)})
        if kind=='home': attr(a,{'municipalityId':'1','municipalityType':'urbanCore'})
    def outputrow(pid,no,mode,a,b,kindA,kindB):
        outtrips.append(dict(person=pid,trip_number=no,trip_id=f'{pid}_{no}',main_mode=mode,
            start_x=a[0],start_y=a[1],end_x=b[0],end_y=b[1],start_activity_type=kindA,end_activity_type=kindB,
            dep_time='08:00:00' if no==1 else '17:00:00',euclidean_distance=((a[0]-b[0])**2+(a[1]-b[1])**2)**.5))
    for i in range(120):
        pid=f'p{i}'; h=(hx+(i%2)*10000,hy); d=(h[0]+2000,h[1]+1000)
        age=70 if i%2 else 30; cars=i%2; available=1 if i%3==0 else 2 if i%3==1 else 3
        p=ET.SubElement(population,'person',{'id':pid})
        values={'age':age,'sex':'m' if i%2 else 'f','home_coordinate_x':h[0],'home_coordinate_y':h[1],
                'home_x':h[0],'home_y':h[1],'numberOfCars':cars,'carAvail':'always' if available in [1,2] else 'never',
                'subscriptions':'none','isOutside':str(i in [0,1]).lower(),'isCrossBorder':str(i==6).lower(),'isFreight':'false'}
        if i==4: values['hasWalkLoopTrip']='true'
        attr(p,values); plan=ET.SubElement(p,'plan',{'selected':'yes'})
        h03=(600000+(i%2)*10000,200000); d03=(h03[0]+2000,h03[1]+1000)
        households.append(dict(HHNR=pid,W_X_CH1903=h03[0],W_Y_CH1903=h03[1],f30100=cars,W_BFS=1))
        person=dict(HHNR=pid,alter=age,WP=1+i%3,tag=2,gesl=1 if i%2 else 2,f42100e=available,
          f41610a=2,f41610b=2,f41610c=2,f50200=2 if i in [2,5] else 1,T_WEGE=0 if i in [2,5] else 2,
          A_X_CH1903=d03[0],A_Y_CH1903=d03[1],AU_X_CH1903=d03[0],AU_Y_CH1903=d03[1])
        people.append(person)
        if i==3: continue
        act(plan,'home',*h)
        if i in [2]: continue
        if i==5:
            ET.SubElement(plan,'leg',{'mode':'remote_walk'}); act(plan,'work',*h)
            ET.SubElement(plan,'leg',{'mode':'remote_walk'}); act(plan,'home',*h)
            outputrow(pid,1,'remote_walk',h,h,'home','work'); outputrow(pid,2,'remote_walk',h,h,'work','home')
            continue
        if i==4:
            ET.SubElement(plan,'leg',{'mode':'walk_loop'}); act(plan,'home',*h)
            outputrow(pid,1,'walk_loop',h,h,'home','home')
            ways.append(dict(HHNR=pid,WEGNR=1,wzweck1=11,wzweck2=3,pseudo=1,wmittel=15,
               S_X_CH1903=h03[0],S_Y_CH1903=h03[1],Z_X_CH1903=h03[0],Z_Y_CH1903=h03[1],f51100=480))
            stages.append(dict(HHNR=pid,WEGNR=1,f51300=1,f52950=11)); person['T_WEGE']=1
            continue
        outside=i==1
        if outside: d=(hx+25000,hy);d03=(625000,200000)
        kind='outside' if outside else 'work'; mode='walk'
        ET.SubElement(plan,'leg',{'mode':mode}); act(plan,kind,*d)
        ET.SubElement(plan,'leg',{'mode':mode}); act(plan,'home',*h)
        outputrow(pid,1,mode,h,d,'home',kind);outputrow(pid,2,mode,d,h,kind,'home')
        for no,a,b in [(1,h03,d03),(2,d03,h03)]:
            ways.append(dict(HHNR=pid,WEGNR=no,wzweck1=13 if outside and no==1 else 2 if no==1 else 11,
              wzweck2=1 if no==1 else 2,pseudo=1,wmittel=15,S_X_CH1903=a[0],S_Y_CH1903=a[1],
              Z_X_CH1903=b[0],Z_Y_CH1903=b[1],f51100=480 if no==1 else 1020))
            stages.append(dict(HHNR=pid,WEGNR=no,f51300=1,f52950=11 if no==1 else 2))
    # Non-resident and freight agents must not enter resident statistics.
    for pid,x,freight in [('external',hx+25000,False),('freight',hx,True)]:
        p=ET.SubElement(population,'person',{'id':pid});attr(p,{'age':40,'home_coordinate_x':x,'home_coordinate_y':hy,
          'isFreight':str(freight).lower(),'numberOfCars':1,'sex':'m'});pl=ET.SubElement(p,'plan',{'selected':'yes'});act(pl,'home',x,hy)
    def xml(path,element):
        with gzip.open(path,'wb') if str(path).endswith('.gz') else open(path,'wb') as f: f.write(ET.tostring(element))
    xml(scenario/'neuchatel_population.xml.gz',population)
    for filename,rows in [('haushalte.csv',households),('zielpersonen.csv',people),('wege.csv',ways),('etappen.csv',stages)]:
        pd.DataFrame(rows).to_csv(base/'microcensus'/filename,index=False,encoding='latin1')
    pd.DataFrame(outtrips).to_csv(sim/'output_trips.csv.gz',index=False,sep=';',compression='gzip')
    network=ET.Element('network');nodes=ET.SubElement(network,'nodes');links=ET.SubElement(network,'links')
    for n,x,y in [('a',hx,hy),('b',hx+1000,hy),('c',hx+25000,hy),('d',hx+26000,hy)]:
        ET.SubElement(nodes,'node',{'id':n,'x':str(x),'y':str(y)})
    ET.SubElement(links,'link',{'id':'L1','from':'a','to':'b','permlanes':'2','modes':'car,bus,truck'})
    ET.SubElement(links,'link',{'id':'L2','from':'c','to':'d','permlanes':'1','modes':'car'})
    xml(sim/'output_network.xml.gz',network)
    target=scenario/'network_calibration_files/target_counts.csv'
    pd.DataFrame({'link_id':['L1','L2'],'count':[50,100]}).to_csv(target,index=False)
    config=ET.Element('config')
    for name,params in [('eqasim',{'sampleSize':'0.1'}),('eqasim:flow',{'startTime':'0','endTime':'86400','activate':'true'}),
      ('eqasim:networkCalibration',{'countsFile':str(target)})]:
        m=ET.SubElement(config,'module',{'name':name})
        for key,value in params.items():ET.SubElement(m,'param',{'name':key,'value':value})
    xml(sim/'output_config.xml',config)
    schedule=ET.Element('transitSchedule');stops=ET.SubElement(schedule,'transitStops')
    for ident,x,y in [('S1',hx,hy),('S2',hx+1000,hy),('S3',hx+25000,hy)]:
        ET.SubElement(stops,'stopFacility',{'id':ident,'x':str(x),'y':str(y)})
    line=ET.SubElement(schedule,'transitLine',{'id':'line'});route=ET.SubElement(line,'transitRoute',{'id':'route'})
    ET.SubElement(route,'transportMode').text='bus';profile=ET.SubElement(route,'routeProfile')
    for s in ['S1','S2']: ET.SubElement(profile,'stop',{'refId':s})
    dep=ET.SubElement(route,'departures');ET.SubElement(dep,'departure',{'id':'d1','departureTime':'100','vehicleRefId':'bus'})
    xml(sim/'output_transitSchedule.xml.gz',schedule)
    events=ET.Element('events')
    def ev(t,kind,**kwargs):ET.SubElement(events,'event',{'time':str(t),'type':kind,**kwargs})
    ev(0,'vehicle enters traffic',vehicle='car',networkMode='car')
    for t in range(1,11):ev(t,'entered link',vehicle='car',link='L1')
    ev(100,'TransitDriverStarts',vehicleId='bus',driverId='driver',transitLineId='line',transitRouteId='route',departureId='d1')
    ev(150,'VehicleArrivesAtFacility',vehicle='bus',facility='S1',delay='30')
    ev(151,'PersonEntersVehicle',vehicle='bus',person='driver')
    ev(160,'PersonEntersVehicle',vehicle='bus',person='passenger')
    ev(180,'VehicleDepartsAtFacility',vehicle='bus',facility='S1',delay='10')
    ev(190,'entered link',vehicle='bus',link='L1')
    ev(400,'VehicleArrivesAtFacility',vehicle='bus',facility='S2',delay='60')
    ev(401,'PersonLeavesVehicle',vehicle='bus',person='passenger')
    ev(410,'VehicleDepartsAtFacility',vehicle='bus',facility='S2',delay='0')
    xml(sim/'output_events.xml.gz',events)
    pd.DataFrame({'linkId':['L1','L2'],'dailyCount':[10.1,0]}).to_csv(sim/'output_traffic_flow_daily_counts.csv',index=False,sep=';')
    pd.DataFrame([dict(stop_id='S1',line_id='line',time_bin='00:00:00',boardings=1,alightings=0),
                  dict(stop_id='S2',line_id='line',time_bin='00:00:00',boardings=0,alightings=1),
                  dict(stop_id='S3',line_id='line',time_bin='00:00:00',boardings=100,alightings=100)]).to_csv(
                      sim/'pt_passenger_counts.csv.gz',index=False,compression='gzip')
    return base


if __name__=='__main__':
    import sys
    print(make_fixture(Path(sys.argv[1]).resolve()))
